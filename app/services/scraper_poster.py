from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

async def _get_scrape_client():
    global _SCRAPE_CLIENT
    if _SCRAPE_CLIENT is None or _SCRAPE_CLIENT.is_closed:
        async with _SCRAPE_CLIENT_LOCK:
            if _SCRAPE_CLIENT is None or _SCRAPE_CLIENT.is_closed:
                _SCRAPE_CLIENT = httpx.AsyncClient(
                    timeout=httpx.Timeout(7.0, connect=3.0),
                    follow_redirects=True,
                    limits=httpx.Limits(max_connections=120, max_keepalive_connections=60),
                )
    return _SCRAPE_CLIENT

async def _close_scrape_client():
    global _SCRAPE_CLIENT
    client = _SCRAPE_CLIENT
    _SCRAPE_CLIENT = None
    if client and not client.is_closed:
        await client.aclose()

async def scrape_channel_paginated(profile_id, channel, max_pages=5, stream="combined"):
    """
    Scrape public Telegram channel pages and return ONLY messages newer than
    the cursor belonging to this profile + source + stream.

    stream is intentionally independent for config/proxy workers so one
    worker cannot consume the source cursor of another worker.

    Returns:
        (configs, proxies, newest_message_id)

    The cursor is NOT advanced here. The caller advances it after the relevant
    processing/send path has completed successfully. This prevents data loss
    when Telegram posting fails.
    """
    clean_channel = normalize_channel_input(channel)
    if not clean_channel:
        log.warning(f"Invalid channel name: {channel}")
        return [], [], ""

    last_msg_id = get_stream_last_message_id(profile_id, clean_channel, stream)
    base_url = f"https://t.me/s/{clean_channel.lstrip('@')}"
    all_configs = []
    all_proxies = []
    current_url = base_url
    page_count = 0
    newest_seen_id = ""
    stopped = False

    # None/0 means exhaustive scan; never artificially stop at 2/3/5 pages.
    effective_max_pages = None if max_pages in (None, 0) else int(max_pages)
    if stream == "proxy" and not last_msg_id:
        # Existing installations may have no proxy cursor. Scan a small recent
        # window so proxies on the newest few pages are not missed. Dedup/state
        # protection prevents reposting already published proxies.
        effective_max_pages = None if max_pages in (None, 0) else int(max_pages)

    log.info(
        f"🔍 [profile={profile_id}][stream={stream}] Starting scrape for "
        f"{clean_channel} (max {effective_max_pages} pages, last_msg_id={last_msg_id or 'NONE'})"
    )

    scrape_client = await _get_scrape_client()
    while (effective_max_pages is None or page_count < effective_max_pages) and not stopped:
        page_count += 1
        log.info(
            f"🔍 [profile={profile_id}][stream={stream}] Scraping page "
            f"{page_count} for {clean_channel}: {current_url}"
        )

        _page_configs, _page_proxies, msg_ids, msg_content_map = \
            await _scrape_single_page_with_messages(current_url, clean_channel, scrape_client)

        if not msg_ids:
            log.info(f"⚠️ [profile={profile_id}][stream={stream}] No messages on page {page_count} for {clean_channel}")
            break

        # Telegram normally returns newest -> oldest. Keep the newest ID we
        # actually encountered for this scan, but do not advance DB state yet.
        if not newest_seen_id:
            newest_seen_id = msg_ids[0]

        new_msg_ids = []
        for mid in msg_ids:
            if last_msg_id and str(mid) == str(last_msg_id):
                stopped = True
                break
            new_msg_ids.append(mid)

        if not new_msg_ids:
            log.info(
                f"✅ [profile={profile_id}][stream={stream}] Reached cursor for "
                f"{clean_channel}; no newer messages on page {page_count}."
            )
            break

        for mid in new_msg_ids:
            content = msg_content_map.get(mid, "")
            if not content:
                continue

            configs = extract_links_from_text(content)
            proxies = extract_proxy_links_from_text(content)
            all_configs.extend(configs)
            all_proxies.extend(proxies)

            # The stream cursor is the authoritative processing state.
            # Do NOT write one processed_messages row per scraped message: that
            # turns a read-only scrape into thousands of synchronous SQLite
            # writes and directly slows the bot. Failed posts remain behind the
            # cursor and are retried on the next cycle.

        numeric_ids = []
        for mid in msg_ids:
            parts = str(mid).split('/')
            if len(parts) == 2 and parts[1].isdigit():
                numeric_ids.append(int(parts[1]))
        if numeric_ids:
            oldest = min(numeric_ids)
            current_url = f"{base_url}?before={oldest}"
        else:
            break

        # No fixed per-page delay: source requests are already rate-limited by Telegram.
        await asyncio.sleep(0)

    all_configs = list(dict.fromkeys(all_configs))
    all_proxies = list(dict.fromkeys(all_proxies))

    log.info(
        f"📊 [profile={profile_id}][stream={stream}] {clean_channel}: "
        f"configs={len(all_configs)}, proxies={len(all_proxies)}, newest={newest_seen_id or 'NONE'}"
    )
    return all_configs, all_proxies, newest_seen_id

async def _scrape_single_page_with_messages(url, channel, client=None):
    headers = {
        "User-Agent": _USER_AGENTS[hash(datetime.now(TEHRAN_TZ).timestamp()) % len(_USER_AGENTS)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    own_client = client is None
    cl = client or httpx.AsyncClient(
        timeout=httpx.Timeout(7.0, connect=3.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
    )
    try:
        r = await cl.get(url, headers=headers)
        if r.status_code == 429:
            log.warning(f"Rate limit for {channel}, waiting 10s")
            await asyncio.sleep(2)
            r = await cl.get(url, headers=headers)
        if r.status_code != 200:
            log.warning(f"⚠️ {channel} returned status {r.status_code} for url {url}")
            return [], [], [], {}

        html_text = r.text

        # Telegram channel HTML contains data-post="channel/message_id".
        # Do NOT use a non-greedy </div> regex here: Telegram messages contain
        # nested divs and that regex truncates the message body, which can hide
        # proxy/config links. Instead, use each data-post marker as a boundary
        # and slice until the next message marker.
        markers = list(re.finditer(r'data-post=["\']([^"\']+)["\']', html_text, re.IGNORECASE))
        msg_ids = []
        msg_content_map = {}

        for idx, match in enumerate(markers):
            mid = match.group(1).strip()
            if not mid:
                continue
            body_start = match.start()
            body_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(html_text)
            block = html_text[body_start:body_end]
            # Extract URLs from the raw HTML BEFORE stripping tags. Telegram
            # often stores proxy/config URLs inside <a href="..."> while the
            # visible anchor text contains no URL at all.
            block_proxy_links = extract_proxy_links_from_text(block)
            block_config_links = extract_links_from_text(block)
            content_text = re.sub(r'<script\b[^>]*>.*?</script>', ' ', block, flags=re.IGNORECASE | re.DOTALL)
            content_text = re.sub(r'<style\b[^>]*>.*?</style>', ' ', content_text, flags=re.IGNORECASE | re.DOTALL)
            content_text = re.sub(r'<[^>]+>', ' ', content_text)
            content_text = html.unescape(content_text)
            content_text = re.sub(r'\s+', ' ', content_text).strip()
            # Preserve extracted href URLs for the downstream per-message parser.
            if block_config_links:
                content_text += "\n" + "\n".join(block_config_links)
            if block_proxy_links:
                content_text += "\n" + "\n".join(block_proxy_links)
            msg_ids.append(mid)
            msg_content_map[mid] = content_text

        # Fallback for layouts without data-post markers.
        if not msg_ids:
            alt_ids = re.findall(r'href=["\']/([^/"\']+)/(\d+)["\']', html_text, re.IGNORECASE)
            seen_alt = set()
            for ch, num in alt_ids:
                mid = f"{ch}/{num}"
                if mid not in seen_alt:
                    seen_alt.add(mid)
                    msg_ids.append(mid)
            if msg_ids:
                # We cannot reliably associate each fallback ID with a block,
                # but the whole page is still useful for extracting links.
                page_text = re.sub(r'<[^>]+>', ' ', html.unescape(html_text))
                page_text = re.sub(r'\s+', ' ', page_text).strip()
                msg_content_map = {mid: page_text for mid in msg_ids}

        config_links = list(dict.fromkeys(extract_links_from_text(html_text)))
        proxy_links = list(dict.fromkeys(extract_proxy_links_from_text(html_text)))

        log.debug(
            f"📄 [profile-source={channel}] Page {url}: "
            f"configs={len(config_links)}, proxies={len(proxy_links)}, messages={len(msg_ids)}"
        )
        return config_links, proxy_links, msg_ids, msg_content_map
    finally:
        if own_client:
            await cl.aclose()

def record_channel_post(profile_id, message):
    try:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        message_id = getattr(message, "message_id", None)
        if chat_id is None or message_id is None:
            return
        c.execute("INSERT OR IGNORE INTO channel_posts(profile_id,chat_id,message_id,sent_at,deleted_at) VALUES (?,?,?,?,NULL)",
                  (int(profile_id), int(chat_id), int(message_id), get_tehran_time()))
        conn.commit()
    except Exception:
        log.exception("[CHANNEL-POSTS] record failed")

async def delete_latest_channel_posts(bot, profile_id, count):
    count = max(1, min(int(count), 5000))
    rows = c.execute(
        "SELECT chat_id,message_id FROM channel_posts WHERE profile_id=? AND deleted_at IS NULL ORDER BY message_id DESC LIMIT ?",
        (int(profile_id), count)
    ).fetchall()
    if not rows:
        return 0, 0
    deleted = failed = 0
    for idx, (chat_id, message_id) in enumerate(rows, 1):
        try:
            await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
            deleted += 1
            c.execute("UPDATE channel_posts SET deleted_at=? WHERE chat_id=? AND message_id=?", (get_tehran_time(), int(chat_id), int(message_id)))
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
                deleted += 1
                c.execute("UPDATE channel_posts SET deleted_at=? WHERE chat_id=? AND message_id=?", (get_tehran_time(), int(chat_id), int(message_id)))
            except Exception as retry_exc:
                failed += 1
                log.warning("[CHANNEL-DELETE] retry failed profile=%s message=%s: %s", profile_id, message_id, retry_exc)
        except BadRequest as exc:
            text = str(exc).lower()
            if "message to delete not found" in text or "message can't be deleted" in text or "message not found" in text:
                c.execute("UPDATE channel_posts SET deleted_at=? WHERE chat_id=? AND message_id=?", (get_tehran_time(), int(chat_id), int(message_id)))
            else:
                failed += 1
                log.warning("[CHANNEL-DELETE] profile=%s message=%s failed: %s", profile_id, message_id, exc)
        except Exception as exc:
            failed += 1
            log.warning("[CHANNEL-DELETE] profile=%s message=%s failed: %s", profile_id, message_id, exc)
        if idx % 20 == 0:
            conn.commit()
            await asyncio.sleep(0.15)
    conn.commit()
    return deleted, failed

async def send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True, max_retries=3, return_message=False):
    retry_count = 0
    while retry_count < max_retries:
        try:
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview
            )
            return message if return_message else True
        except RetryAfter as e:
            wait = e.retry_after + 1
            log.warning(f"Flood control, waiting {wait} seconds...")
            await asyncio.sleep(wait)
            retry_count += 1
        except TimedOut:
            log.warning(f"Timeout, retrying... ({retry_count+1}/{max_retries})")
            await asyncio.sleep(2)
            retry_count += 1
        except BadRequest as e:
            if "can't parse entities" in str(e):
                try:
                    message = await bot.send_message(
                        chat_id=chat_id,
                        text=re.sub(r'<[^>]+>', '', text)[:4096],
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview
                    )
                    return message if return_message else True
                except Exception:
                    pass
            log.error(f"BadRequest: {e}")
            return False
        except Exception as e:
            log.error(f"Send error: {e}")
            await asyncio.sleep(1)
            retry_count += 1
    return False

async def send_to_destination(bot, profile_id, text, buttons=None):
    dest = get_profile_dest(profile_id)
    if not dest:
        log.error(f"❌ Profile {profile_id} has no destination!")
        return False

    log.info(f"📤 Sending to {dest} (profile {profile_id})")
    chunks = split_text(text, 4096)
    success = True
    for idx, chunk in enumerate(chunks):
        # Telegram proxy URLs must remain raw. Do not wrap them in inline buttons or
        # HTML anchors; otherwise Telegram may open the bot/admin callback instead.
        contains_tg_proxy = "t.me/proxy?" in chunk.lower() or "tg://proxy?" in chunk.lower()
        # Same sponsor/channel buttons on every chunk, including Quote mode.
        reply_markup = None if contains_tg_proxy else (InlineKeyboardMarkup(buttons) if buttons else None)
        sent_message = await send_with_retry(
            bot, dest, chunk,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            return_message=True
        )
        if sent_message:
            record_channel_post(profile_id, sent_message)
        else:
            plain = re.sub(r'<[^>]+>', '', chunk)
            sent_message = await send_with_retry(
                bot, dest, plain[:4096],
                parse_mode=None,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                return_message=True
            )
            if sent_message:
                record_channel_post(profile_id, sent_message)
        if not sent_message:
            success = False
        if idx < len(chunks) - 1:
            await asyncio.sleep(0.5)
    return success

def split_text(text, max_len=4096):
    if len(text) <= max_len:
        return [text]
    lines = text.split('\n')
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        if len(line) > max_len:
            if current:
                chunks.append('\n'.join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i+max_len])
            continue
        if current_len + len(line) + 1 > max_len:
            chunks.append('\n'.join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append('\n'.join(current))
    return chunks

async def post_configs(bot, profile_id, working, source_for_seen="", is_instant=False, max_post_override=None, extra_button_rows=None, dedup=True, update_auto_state=True):
    if not working:
        return 0

    max_post = max_post_override if max_post_override is not None else get_profile_max_post_config(profile_id)
    # Instant/manual execution must respect the configured max-post exactly.

    blacklist_words = get_blacklist(profile_id)
    filtered_working = []
    local_seen = set()
    reserved_keys = []
    for url, ping, cnt in working:
        identity_hash = _config_identity_hash(url)
        inflight_key=(int(profile_id), identity_hash)
        if identity_hash in local_seen:
            log.info(f"⏭️ Duplicate config skipped inside current send: {str(url)[:70]}...")
            continue
        if dedup and is_already_posted(profile_id, url):
            log.info(f"⏭️ Duplicate config skipped before send: {str(url)[:70]}...")
            continue
        # Process-wide in-flight reservation closes the race between manual and
        # automatic senders in the same process. Reservation is released on exit
        # if Telegram delivery fails, while successful delivery is persisted below.
        if dedup and inflight_key in _INFLIGHT_CONFIG_KEYS:
            log.info(f"⏭️ Duplicate config skipped: already being sent {str(url)[:70]}...")
            continue
        local_seen.add(identity_hash)
        if blacklist_words and is_word_blacklisted(profile_id, url):
            log.info(f"⛔ Blacklisted config skipped: {url[:50]}...")
            continue
        if dedup:
            _INFLIGHT_CONFIG_KEYS.add(inflight_key)
            reserved_keys.append(inflight_key)
        filtered_working.append((url, ping, cnt))

    items = sorted(filtered_working, key=lambda x: x[1])[:max_post]
    # Release reservations for candidates trimmed by max_post.
    selected_keys={(int(profile_id), _config_identity_hash(url)) for url,_,_ in items}
    for _key in reserved_keys:
        if _key not in selected_keys:
            _INFLIGHT_CONFIG_KEYS.discard(_key)
    if not items:
        return 0

    last_n = get_profile_last_num(profile_id)
    show_numbers = get_profile_show_numbers(profile_id)
    custom_query = get_profile_custom_query(profile_id)
    dest = get_profile_dest(profile_id)
    banner_template = get_profile_banner_config(profile_id) or "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    naming_template = get_profile_naming_template(profile_id)
    channel_link = get_profile_channel_link(profile_id)
    if not channel_link:
        channel_link = dest if dest else ""

    country_display = get_profile_country_display(profile_id)
    config_header_enabled = get_profile_config_header_enabled(profile_id)

    # Get appropriate sponsor
    sponsor = get_best_sponsor(profile_id, "config")
    sponsor_button = None
    if sponsor and sponsor.get("enabled"):
        btn_style = sponsor.get("color", "primary") if sponsor.get("color") in ["primary", "success", "danger"] else "primary"
        sponsor_button = InlineKeyboardButton(sponsor["button_text"], url=sponsor["url"], style=btn_style)

    config_blocks = []
    posted_entries = []
    config_mode = get_profile_config_post_mode(profile_id)

    # Resolve DNS/Geo metadata concurrently. The previous sequential path could
    # spend several seconds per config and made manual runs unnecessarily slow.
    _meta_sem = asyncio.Semaphore(24)
    async def _resolve_config_meta(_url):
        async with _meta_sem:
            host, _ = extract_host(_url)
            flag = "🌐"
            country_code = ""
            if host:
                try:
                    cached_ping = _PING_TARGET_IP_CACHE.get(host.strip().lower())
                    ip = cached_ping[1] if cached_ping and time.monotonic() - cached_ping[0] < _PING_TARGET_IP_CACHE_TTL else None
                    if not ip:
                        ip = await host_to_ip(host)
                    if ip:
                        flag, country_code = await get_flag_for_ip(ip)
                except Exception as _e:
                    log.debug(f"[CONFIG] metadata failed for {host}: {_e}")
            return flag, country_code

    _meta_results = await asyncio.gather(
        *[_resolve_config_meta(url) for url, _, _ in items],
        return_exceptions=True,
    )
    config_meta = {}
    for (_url, _ping, _cnt), _meta in zip(items, _meta_results):
        if isinstance(_meta, Exception):
            config_meta[_url] = ("🌐", "")
        else:
            config_meta[_url] = _meta

    for i, (url, ping, node_count) in enumerate(items, 1):
        n = last_n + i

        # MTProto Telegram proxy links are NOT V2Ray configs.
        # Keep them raw: no fragment, no custom query, no channel tag injection.
        # They must stay as https://t.me/proxy?server=...&port=...&secret=...
        if (url or '').strip().lower().startswith(("https://t.me/proxy?", "tg://proxy?")):
            header = "<b>MTPROTO</b>"
            config_blocks.append(header + "\n<pre>" + (url or '').strip() + "</pre>")
            posted_entries.append(((url or '').strip(), source_for_seen, (url or '').strip()))
            continue

        flag, country_code = config_meta.get(url, ("🌐", ""))

        # Config title/header is fully template-driven. Default: [Protocol] [Flag] [Country].
        config_title = detect_config_protocol(url)
        header_parts = []
        config_header_template = get_profile_config_header_template(profile_id)
        if config_header_enabled:
            if not config_title:
                log.warning(f"[CONFIG][profile={profile_id}] Unknown config protocol: {url[:80]}")
                continue

        # Country display
        if country_display == 0:
            # off: no country
            pass
        elif country_display == 1:
            # English only
            flag_emoji = flag if flag != "🌐" else ""
            en_name = COUNTRY_NAMES_EN.get(country_code, "")
            if flag_emoji and en_name:
                header_parts.append(f"{flag_emoji} {en_name}")
            elif flag_emoji:
                header_parts.append(flag_emoji)
        elif country_display == 2:
            # English + Persian
            flag_emoji = flag if flag != "🌐" else ""
            en_name = COUNTRY_NAMES_EN.get(country_code, "")
            fa_name = COUNTRY_NAMES_FA.get(country_code, "")
            if flag_emoji and en_name and fa_name:
                header_parts.append(f"{flag_emoji} {en_name} • <b>{fa_name}</b>")
            elif flag_emoji and en_name:
                header_parts.append(f"{flag_emoji} {en_name}")
            elif flag_emoji:
                header_parts.append(flag_emoji)

        # Render configurable config title. Country mode is controlled independently: 
        # when country is off, [Country] simply becomes empty.
        if config_header_enabled:
            header_values = {
                "Protocol": config_title or "",
                "Flag": flag or "",
                "Country": (f"{COUNTRY_NAMES_EN.get(country_code, '')} • {COUNTRY_NAMES_FA.get(country_code, '')}".strip(" •")
                            if country_display == 2 else COUNTRY_NAMES_EN.get(country_code, "") if country_display == 1 else ""),
                "COUNTRY_EN": COUNTRY_NAMES_EN.get(country_code, "") if country_display in (1,2) else "",
                "COUNTRY_FA": COUNTRY_NAMES_FA.get(country_code, "") if country_display == 2 else "",
                "CHANNEL_ID": channel_link or dest or "",
                "COUNT": str(n), "PING": "",
            }
            header = config_header_template
            for _key, _value in header_values.items():
                header = header.replace("{"+_key+"}", str(_value)).replace("["+_key+"]", str(_value))
            header = re.sub(r"[ \t]{2,}", " ", header).strip(" -•|")
            if show_numbers:
                header = f"<b>#{n}</b> {header}" if header else f"<b>#{n}</b>"
        else:
            header = ""

        fragment_text = render_naming_template(
            naming_template, protocol=config_title, flag=flag,
            country_code=country_code, channel_link=channel_link, count=n
        )
        encoded_fragment = quote(fragment_text, safe='')
        protocol = url.split('://')[0].lower() if '://' in url else ''

        # VMess fix: never append #name to vmess://.
        # Most clients expect the name inside JSON ps. A fragment after base64
        # makes the payload invalid for strict clients.
        if protocol == "vmess":
            modified_url = apply_display_name_to_config(url, fragment_text)
        else:
            base_url = strip_url_fragment(url)
            modified_url = base_url + "#" + encoded_fragment
            if custom_query and protocol not in ('https', 'tg'):
                modified_url = add_custom_query_to_url(modified_url, custom_query, protocol)

        if config_mode == 1:
            config_blocks.append(html.escape(modified_url, quote=False))
        else:
            block = f"<pre>{modified_url}</pre>"
            config_blocks.append((header + "\n" if header else "") + block)
        posted_entries.append((modified_url, source_for_seen, url))

    # Absolute safety gate: NEVER send a banner by itself. A Telegram message
    # is considered publishable only when at least one real config block was
    # successfully built. This also protects against unknown/unsupported
    # protocols being filtered after the candidate list was selected.
    if not config_blocks or not posted_entries:
        log.warning(
            f"⛔ [CONFIG][profile={profile_id}] refusing empty banner send: "
            f"items={len(items)} blocks={len(config_blocks)} entries={len(posted_entries)}"
        )
        for _key in reserved_keys:
            _INFLIGHT_CONFIG_KEYS.discard(_key)
        return 0

    if config_mode == 1:
        # Quote mode ONLY: one expandable quote + one code block for ALL configs.
        # This makes the whole group one copyable payload and removes blank lines
        # before/after the first/last config inside the quote.
        quote_payload = "\n".join(config_blocks)
        configs_text = f"<blockquote expandable><code>{quote_payload}</code></blockquote>"
    else:
        # Normal mode is intentionally unchanged.
        configs_text = "\n\n".join(config_blocks)
    try:
        full_text = banner_template.format(configs=configs_text)
    except KeyError:
        full_text = f"✦ V2Ray Config List\n\n{configs_text}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

    # Optional proxy glass-button rows are inserted ABOVE the sponsor.
    # Each row is already a list of InlineKeyboardButton objects.
    buttons = []
    if extra_button_rows:
        buttons.extend(extra_button_rows)
    channel_link_display = get_profile_channel_link(profile_id)
    if channel_link_display:
        channel_url = f"https://t.me/{channel_link_display}"
        buttons.append([InlineKeyboardButton("📢 کانال", url=channel_url, style="primary")])
    if sponsor_button:
        buttons.append([sponsor_button])
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    # Telegram has a hard 4096-character limit for text messages. Build the
    # largest possible messages for BOTH config modes instead of relying on a
    # failed send + truncated fallback. A small safety margin avoids edge cases
    # caused by HTML entity expansion. The per-profile max_post remains the
    # upper bound on configs in this call (set it to 30 to target 30/config post).
    TELEGRAM_TEXT_LIMIT = 4096
    TELEGRAM_SAFE_LIMIT = 4050

    marker = "__CONFIGS__"
    try:
        probe = banner_template.format(configs=marker)
    except KeyError:
        probe = f"✦ V2Ray Config List\n\n{marker}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    before, sep, after = probe.partition(marker)
    if not sep:
        before, after = "", ""

    html_messages = []
    message_entry_batches = []
    batch = []
    entry_batch = []

    def _render_batch(lines):
        if config_mode == 1:
            payload = "\n".join(lines)
            return before + "<blockquote expandable><code>" + payload + "</code></blockquote>" + after
        # Normal mode: keep the existing visual formatting, but pack as many
        # complete config blocks as Telegram permits.
        payload = "\n\n".join(lines)
        return before + payload + after

    for line, entry in zip(config_blocks, posted_entries):
        candidate = batch + [line]
        candidate_text = _render_batch(candidate)
        if batch and len(candidate_text) > TELEGRAM_SAFE_LIMIT:
            html_messages.append(_render_batch(batch))
            message_entry_batches.append(list(entry_batch))
            batch = [line]
            entry_batch = [entry]
        else:
            batch = candidate
            entry_batch.append(entry)

    if batch:
        html_messages.append(_render_batch(batch))
        message_entry_batches.append(list(entry_batch))

    # Never silently truncate a config message. If a single rendered block is
    # itself too large, Telegram cannot carry it as one text message; log it and
    # leave it to the normal retry/state path rather than corrupting the URL.
    for _idx, _msg in enumerate(html_messages, 1):
        if len(_msg) > TELEGRAM_TEXT_LIMIT:
            log.warning(
                f"⚠️ [CONFIG][profile={profile_id}] message {_idx} exceeds Telegram limit "
                f"({len(_msg)} chars); this usually means one config block is too large."
            )

    ok = True
    sent_count = 0
    for idx, message_text in enumerate(html_messages):
        sent_message = await send_with_retry(
            bot, dest, message_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            max_retries=3,
            return_message=True
        )
        if not sent_message:
            plain_text = re.sub(r'<[^>]+>', '', message_text)
            sent_message = await send_with_retry(
                bot, dest, plain_text[:4096],
                parse_mode=None,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                max_retries=2,
                return_message=True
            )
        if not sent_message:
            ok = False
            break
        # Channel-message history is useful for the per-profile deletion tool,
        # but manual sends must never alter the automatic dedup/cursor state.
        record_channel_post(profile_id, sent_message)
        batch_entries = message_entry_batches[idx] if idx < len(message_entry_batches) else []
        if batch_entries:
            if update_auto_state:
                # Record ONLY after Telegram confirmed delivery.
                mark_configs_posted_batch(profile_id, batch_entries)
            sent_count += len(batch_entries)
        if idx < len(html_messages) - 1:
            await asyncio.sleep(0.25)

    if not ok:
        log.error(f"❌ Config send stopped after partial success: sent={sent_count}")
        if update_auto_state and sent_count > 0:
            set_profile_last_num(profile_id, last_n + sent_count)
        for _key in reserved_keys:
            _INFLIGHT_CONFIG_KEYS.discard(_key)
        return sent_count

    if update_auto_state and sent_count > 0:
        set_profile_last_num(profile_id, last_n + sent_count)

    log.info(f"✅ Sent {sent_count} configs in one message to {dest}")
    for _key in reserved_keys:
        _INFLIGHT_CONFIG_KEYS.discard(_key)
    return sent_count

async def post_proxies(bot, profile_id, proxies_with_ping, is_instant=False, max_proxies_override=None, dedup=True):
    """Build a proxy post. Does NOT mark anything posted; caller does that only after Telegram success."""
    if not proxies_with_ping:
        return 0, None, []
    max_proxies = max_proxies_override if max_proxies_override is not None else get_profile_max_post_proxy(profile_id)
    try:
        max_proxies = max(1, int(max_proxies))
    except Exception:
        max_proxies = 10
    # Instant mode changes scheduling responsiveness only; it must never
    # silently reduce the profile's configured posting limit.
    mode = get_profile_proxy_post_mode(profile_id)
    show_date = get_profile_show_date_proxy(profile_id)
    country_display = get_profile_country_display(profile_id)
    selected = []
    entries = []
    local_proxy_keys = set()
    for item in proxies_with_ping:
        try:
            raw = item[0]
            flag = item[2] if len(item) > 2 else "🌐"
            country_code = item[3] if len(item) > 3 else ""
        except Exception:
            continue
        norm = canonical_telegram_proxy_url(raw)
        if not norm:
            continue
        if dedup and is_proxy_posted(profile_id, norm):
            continue
        proxy_identity_hash = _proxy_identity_hash(norm)
        if any(proxy_identity_hash == existing for existing in local_proxy_keys):
            continue
        local_proxy_keys.add(proxy_identity_hash)
        if len(entries) >= max_proxies:
            break
        channel = get_profile_channel_link(profile_id)
        if channel:
            channel_label = f"@{channel.lstrip('@')}"
        else:
            # Per-profile fallback: never use a hard-coded @VaslZone for another profile.
            prof = get_profile(profile_id) or {}
            fallback = str(prof.get("dest_name") or "").strip()
            channel_label = fallback if fallback.startswith("@") else (f"@{fallback}" if fallback else "@Channel")
        # IMPORTANT: proxy_post_mode controls ONLY the publication layout (normal vs glass).
        # The proxy header naming is controlled independently by proxy_header_mode.
        # Therefore switching to Glass MUST NOT change @ChannelName into a protocol.
        _config_header_mode, proxy_header_mode = get_header_modes(profile_id)
        if proxy_header_mode == "protocol":
            proxy_title = detect_proxy_protocol(norm) or "MTPROTO"
        else:
            proxy_title = channel_label
        header_parts = [proxy_title]
        if country_display == 1:
            en = COUNTRY_NAMES_EN.get(country_code, "")
            if flag and flag != "🌐":
                header_parts.append(f"{flag} {en}" if en else flag)
        elif country_display == 2:
            en = COUNTRY_NAMES_EN.get(country_code, "")
            fa = COUNTRY_NAMES_FA.get(country_code, "")
            if flag and flag != "🌐":
                if en and fa:
                    header_parts.append(f"{flag} {en} • <b>{fa}</b>")
                elif en:
                    header_parts.append(f"{flag} {en}")
                elif fa:
                    header_parts.append(f"{flag} <b>{fa}</b>")
                else:
                    header_parts.append(flag)
        entries.append((norm, " ".join(header_parts), flag))
        selected.append(norm)
    if not entries:
        return 0, None, []

    banner = get_profile_banner_proxy(profile_id) or "🌐 <b>Proxies</b>\n\n{proxies}"
    if mode == 0:
        # Plain URL lines are intentionally used. Telegram clients recognize
        # t.me/proxy links reliably, while HTML href can be rejected for some
        # proxy URL variants.
        proxy_blocks = [f"{header}\n{norm}" for norm, header, _flag in entries]
        proxy_text = "\n\n".join(proxy_blocks)
        try:
            text = banner.format(date=get_tehran_date() if show_date else "", count=len(entries), proxies=proxy_text)
        except Exception:
            text = f"🌐 <b>Proxies</b>\n\n{proxy_text}"
        rows = []
        sponsor = get_best_sponsor(profile_id)
        if sponsor and sponsor.get("enabled"):
            style = sponsor.get("color") if sponsor.get("color") in ("primary", "success", "danger") else "primary"
            rows.append([InlineKeyboardButton(str(sponsor.get("button_text") or "Advertisement"), url=str(sponsor.get("url") or "https://t.me/"), style=style)])
        return len(entries), (text, rows), selected

    # Glass mode: maximum 3 proxy buttons per row. Sponsor is always isolated
    # in the final row and never shares a row with proxy buttons.
    proxy_buttons = []
    for i, (norm, header, proxy_flag) in enumerate(entries):
        style = ("primary", "success", "danger")[(i) % 3]
        # The button MUST reuse the exact flag already resolved for this proxy.
        # This keeps header country and glass-button country perfectly consistent.
        raw_lower = str(norm).lower()
        if "cloudflare" in header.lower() or "cloudflare" in raw_lower:
            button_label = "☁️ Proxy"
        elif proxy_flag and proxy_flag not in ("🌐", ""):
            button_label = f"Proxy {proxy_flag}"
        else:
            button_label = "🌐 Proxy"
        # Keep URL buttons clickable for real proxy links.
        proxy_buttons.append(InlineKeyboardButton(button_label, url=norm, style=style))
    rows = [proxy_buttons[i:i+3] for i in range(0, len(proxy_buttons), 3)]
    visible = "\n".join(header for _norm, header, _flag in entries)
    try:
        text = banner.format(date=get_tehran_date() if show_date else "", count=len(entries), proxies=visible)
    except Exception:
        text = f"🌐 <b>Proxies</b>\n\n{visible}"
    sponsor = get_best_sponsor(profile_id)
    if sponsor and sponsor.get("enabled"):
        style = sponsor.get("color") if sponsor.get("color") in ("primary", "success", "danger") else "primary"
        rows.append([InlineKeyboardButton(str(sponsor.get("button_text") or "Advertisement"), url=str(sponsor.get("url") or "https://t.me/"), style=style)])
    return len(entries), (text, rows), selected

def get_best_sponsor(profile_id, apply_type="both"):
    """Select the best sponsor based on priority and schedule."""
    sponsors = get_sponsors(profile_id, apply_type)
    if not sponsors:
        return None
    return sponsors[0]
