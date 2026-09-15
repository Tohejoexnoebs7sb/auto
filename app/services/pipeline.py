from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

_AUTO_STREAM_LOCKS = {}
_AUTO_POST_DEADLINES = {}
_AUTO_SCRAPE_CACHE = {}
_AUTO_SCRAPE_CACHE_LOCK = asyncio.Lock()
_AUTO_GLOBAL_SCRAPE_SEM = asyncio.Semaphore(32)
_AUTO_SCRAPE_CACHE_TTL = 8.0

def _auto_stream_lock(profile_id, stream, operation="shared"):
    # Keep network-heavy scan/test/post operations independent; only same-operation
    # re-entry is serialized so a slow tester cannot starve the poster.
    key = (int(profile_id), str(stream), str(operation))
    lock = _AUTO_STREAM_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _AUTO_STREAM_LOCKS[key] = lock
    return lock

async def _cached_auto_scrape(profile_id, source, stream):
    last_msg_id = get_stream_last_message_id(profile_id, source, stream)
    key = (str(source).casefold(), str(stream), str(last_msg_id or ""))
    now = time.monotonic()
    async with _AUTO_SCRAPE_CACHE_LOCK:
        cached = _AUTO_SCRAPE_CACHE.get(key)
        if cached and now - cached[0] < _AUTO_SCRAPE_CACHE_TTL:
            configs, proxies, newest = cached[1]
            return list(configs), list(proxies), newest
        for old_key, old_value in list(_AUTO_SCRAPE_CACHE.items()):
            if now - old_value[0] >= _AUTO_SCRAPE_CACHE_TTL:
                _AUTO_SCRAPE_CACHE.pop(old_key, None)
    async with _AUTO_GLOBAL_SCRAPE_SEM:
        result = await scrape_channel_paginated(profile_id, source, max_pages=1, stream=stream)
    async with _AUTO_SCRAPE_CACHE_LOCK:
        _AUTO_SCRAPE_CACHE[key] = (time.monotonic(), (list(result[0]), list(result[1]), result[2]))
        if len(_AUTO_SCRAPE_CACHE) > 512:
            oldest = sorted(_AUTO_SCRAPE_CACHE.items(), key=lambda item: item[1][0])[:128]
            for old_key, _ in oldest:
                _AUTO_SCRAPE_CACHE.pop(old_key, None)
    return result

async def _run_cycle_for_profile_unlocked(bot, profile_id, enable_configs=True, enable_proxies=True, is_instant=False):
    log.info("=" * 50)
    log.info(f"🔄 run_cycle for profile {profile_id} (cfg={enable_configs}, prx={enable_proxies}, instant={is_instant})")

    profile = get_profile(profile_id)
    if not profile:
        log.error(f"❌ Profile {profile_id} not found!")
        return 0, "profile not found"

    if not get_profile_enabled(profile_id):
        log.info(f"⏸️ Profile {profile_id} is disabled, skipping cycle.")
        return 0, "profile disabled"

    # Never allow a manual/scheduled caller to bypass per-profile posting switches.
    enable_configs = bool(enable_configs and get_profile_post_configs(profile_id))
    enable_proxies = bool(enable_proxies and get_profile_post_proxies(profile_id))
    if not enable_configs and not enable_proxies:
        log.info(f"⏸️ Both config and proxy posting are disabled for profile {profile_id}.")
        return 0, "config/proxy posting disabled"

    sources = get_profile_sources(profile_id)
    sources = [normalize_channel_input(s) for s in sources if normalize_channel_input(s)]
    if not sources:
        log.error(f"❌ Profile {profile_id} has no valid sources!")
        try:
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"⚠️ پروفایل {profile.get('dest_name', '')} (ID:{profile_id}) هیچ منبعی ندارد. لطفاً یک منبع اضافه کنید."
            )
        except:
            pass
        return 0, "no valid sources"

    dest = get_profile_dest(profile_id)
    if not dest:
        log.error(f"❌ Profile {profile_id} has no destination!")
        try:
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"⚠️ پروفایل {profile.get('dest_name', '')} (ID:{profile_id}) مقصدی ندارد. لطفاً یک مقصد تنظیم کنید."
            )
        except:
            pass
        return 0, "no destination"

    config_ping_mode = get_profile_config_ping_mode(profile_id)
    proxy_ping_mode = get_profile_proxy_ping_mode(profile_id)
    ping_mode = config_ping_mode  # backward-compatible local alias for config checks
    ping_testing = get_profile_ping_enabled(profile_id)
    stream = "combined" if enable_configs and enable_proxies else ("config" if enable_configs else "proxy")
    log.info(f"📡 [profile={profile_id}] Sources: {len(sources)} | 🎯 {dest} | stream={stream} | internal_health_test={ping_testing}")

    all_configs = []
    all_proxies = []
    seen_urls = set()
    seen_config_identities = set()
    seen_proxy_identities = set()
    source_newest_ids = {}

    # Low-cost mode reduces Railway network/CPU usage.
    low_cost_mode=get_profile_low_cost_mode(profile_id)
    batch_posting = bool(get_profile_batch_posting(profile_id))
    if not batch_posting:
        # Normal mode now keeps a small persistent config backlog. This is NOT
        # strict-batch mode: items are posted as soon as they are ready. The
        # backlog exists only so the source cursor can advance immediately and
        # the next cycle never has to rescan already-seen Telegram messages.
        try:
            _pending_batch_clear(profile_id, "proxy")
            conn.commit()
        except Exception:
            log.exception(f"[BATCH][profile={profile_id}] failed clearing disabled proxy batch state")
    log.info(f"📦 [profile={profile_id}] smart_batch={int(batch_posting)} (default OFF)")
    # Responsive incremental scan. A cycle must never walk hundreds of historical
    # Telegram pages. Four pages per source are enough for normal interval runs;
    # an unfinished backlog remains behind the stream cursor and is picked up by
    # the next cycle. This keeps manual/automatic execution bounded and stable.
    scrape_pages = 1
    config_test_limit = None
    # Scrape concurrently, but with a hard per-cycle limit. Launching 100+
    # HTTP requests at the same millisecond (as seen in the log) can saturate
    # Railway/network sockets and make the whole bot look frozen.
    scrape_sem = asyncio.Semaphore(32 if low_cost_mode else 48)
    async def scrape_one(src):
        async with scrape_sem:
            last_exc = None
            for attempt in range(1, 3):
                try:
                    config_links, proxy_links, newest_id = await asyncio.wait_for(
                        scrape_channel_paginated(
                            profile_id, src, max_pages=scrape_pages, stream=stream
                        ),
                        timeout=15.0,
                    )
                    return src, config_links, proxy_links, newest_id
                except Exception as exc:
                    last_exc = exc
                    log.warning(f"⚠️ [profile={profile_id}] source={src} scrape attempt {attempt}/2 failed: {exc}")
                    if attempt < 2:
                        await asyncio.sleep(0.5)
            raise RuntimeError(f"source {src} failed after 2 attempts: {last_exc}")

    scrape_tasks = [scrape_one(src) for src in sources]
    results = await asyncio.gather(*scrape_tasks, return_exceptions=True)
    failed_sources = []

    for res in results:
        if isinstance(res, Exception):
            log.warning(f"Scrape error: {res}")
            failed_sources.append(str(res))
            continue
        src, config_links, proxy_links, newest_id = res
        source_newest_ids[src] = newest_id
        log.debug(f"[profile={profile_id}][{stream}] {src}: {len(config_links)} configs, {len(proxy_links)} proxies from web, newest={newest_id or 'NONE'}")
        for link in config_links:
            identity = canonical_config_identity(link)
            if identity not in seen_config_identities:
                seen_config_identities.add(identity)
                all_configs.append((link, src))
        for link in proxy_links:
            norm = normalize_proxy_url(link)
            if norm:
                identity = canonical_proxy_identity(norm)
                if identity not in seen_proxy_identities:
                    seen_proxy_identities.add(identity)
                    all_proxies.append(norm)

    # Filter duplicates based on database
    new_configs = []
    blacklist_words = get_blacklist(profile_id)
    for u, s in all_configs:
        # Remove blacklisted candidates BEFORE the ping/test window is selected.
        # Otherwise a small max_post/test window can be filled entirely by
        # blacklisted URLs and newer valid configs never get a chance to publish.
        if blacklist_words and any(str(word).lower() in str(u).lower() for word in blacklist_words if word):
            continue
        if not detect_config_protocol(u):
            log.warning(f"[CONFIG][profile={profile_id}] unsupported config scheme skipped before test: {str(u)[:90]}")
            continue
        if not is_already_posted(profile_id, u):
            new_configs.append((u, s))

    new_proxies = []
    for p in all_proxies:
        if not is_proxy_posted(profile_id, p):
            new_proxies.append(p)

    log.info(f"📊 New configs: {len(new_configs)}, New proxies: {len(new_proxies)}")

    working = []
    if enable_configs and new_configs:
        desired = max(1, int(get_profile_max_post_config(profile_id) or 1))
        log.info(f"[AUTO-CONFIG] profile={profile_id} candidates={len(new_configs)} max_post={desired} ping={ping_testing} smart_batch={batch_posting}")
        sem=asyncio.Semaphore(3 if low_cost_mode else 4)

        async def _check(item):
            u, src = item
            async with sem:
                try:
                    if ping_testing:
                        ping, ok, cnt = await check_full_link_ping(u, config_ping_mode, perform_ping=True)
                    else:
                        ping, ok, cnt = 0, True, 0
                    return u, bool(ok), ping, cnt, src
                except Exception as e:
                    log.debug(f"ping failed for {u[:30]}: {e}")
                    return u, False, 0, 0, src

        if batch_posting:
            # Strict persistent batch: existing healthy candidates are loaded first.
            # New candidates are tested in a bounded slice and successful ones are
            # persisted. Nothing is posted until the persisted healthy queue reaches
            # the configured batch size. This survives redeploys and page turnover.
            _pending_batch_remove_posted(profile_id, "config")
            pending_rows = _pending_batch_rows(profile_id, "config")
            pending_hashes = {r[0] for r in pending_rows}
            for identity_hash, url, src, ping, ping_count, _flag, _cc in pending_rows:
                working.append((url, ping, ping_count))

            if len(working) < desired:
                fresh = []
                _batch_prune_test_cache(profile_id)
                for item in new_configs:
                    identity_hash = _config_identity_hash(item[0])
                    if identity_hash not in pending_hashes and not _batch_tested_recently(profile_id, "config", identity_hash):
                        fresh.append(item)
                test_cap = min(len(fresh), max(24, min(120, desired * 12)))
                chunk = fresh[:test_cap]
                rs = []
                for offset in range(0, len(chunk), 4 if not low_cost_mode else 3):
                    part = chunk[offset:offset + (4 if not low_cost_mode else 3)]
                    rs.extend(await asyncio.gather(*[_check(item) for item in part], return_exceptions=True))
                for item, r in zip(chunk, rs):
                    identity_hash = _config_identity_hash(item[0])
                    _batch_mark_tested(profile_id, "config", identity_hash)
                    if not isinstance(r, Exception) and r[1]:
                        _pending_batch_upsert(profile_id, "config", identity_hash, r[0], r[4], r[2], r[3])
                        working.append((r[0], r[2], r[3]))
                conn.commit()
                log.info(f"📦 [AUTO-CONFIG] strict queue tested={len(chunk)}/{len(fresh)} pending={len(working)} target={desired}")
        else:
            # Incremental normal mode: persist every newly discovered config
            # before advancing the source cursor. Then only the pending queue is
            # tested/posted. This is the key change that prevents rescanning old
            # Telegram pages when a source produced more configs than one post can
            # contain.
            _pending_batch_remove_posted(profile_id, "config")
            for u, src in new_configs:
                identity_hash = _config_identity_hash(u)
                if not is_already_posted(profile_id, u):
                    _pending_batch_upsert(profile_id, "config", identity_hash, u, src, 0, 0)
            conn.commit()

            pending_rows = _pending_batch_rows(profile_id, "config")
            if not pending_rows:
                working = []
            elif not ping_testing:
                # No health testing: publish the oldest pending configs first.
                working = [(url, 0, 0) for _h, url, _src, _ping, _pc, _flag, _cc in pending_rows[:desired]]
                log.info(
                    f"📊 Ping testing OFF for profile={profile_id}; "
                    f"publishing {len(working)} pending configs"
                )
            else:
                # Test pending items first. The bounded window is taken from the
                # queue, not from Telegram, so old source messages are never
                # revisited just because the previous post was full.
                test_rows = pending_rows[:max(24, min(120, desired * 15))]
                to_test = []
                for identity_hash, url, src, ping, ping_count, flag, cc in test_rows:
                    if ping and float(ping) > 0:
                        working.append((url, ping, ping_count))
                        if len(working) >= desired:
                            break
                    else:
                        to_test.append((identity_hash, url, src))

                if len(working) < desired and to_test:
                    log.info(f"📊 Testing {len(to_test)} pending configs... low_cost={low_cost_mode}")
                    chunk_size = 4 if not low_cost_mode else 3
                    for offset in range(0, len(to_test), chunk_size):
                        chunk = to_test[offset:offset + chunk_size]
                        rs = await asyncio.gather(*[_check((u, src)) for _h, u, src in chunk], return_exceptions=True)
                        for (identity_hash, url, src), r in zip(chunk, rs):
                            if not isinstance(r, Exception) and r[1]:
                                _pending_batch_upsert(profile_id, "config", identity_hash, r[0], src, r[2], r[3])
                                working.append((r[0], r[2], r[3]))
                                if len(working) >= desired:
                                    break
                        if len(working) >= desired:
                            break
                    conn.commit()

                # Re-read so any healthy item inserted above is available, while
                # preserving queue order and the configured per-post ceiling.
                if len(working) < desired:
                    pending_rows = _pending_batch_rows(profile_id, "config")
                    for _h, url, _src, ping, ping_count, _flag, _cc in pending_rows:
                        if ping and float(ping) > 0 and not any(url == x[0] for x in working):
                            working.append((url, ping, ping_count))
                            if len(working) >= desired:
                                break
                log.info(f"📊 Working pending configs: {len(working)}")
        if not working:
            log.warning(f"[AUTO-CONFIG] profile={profile_id} no publishable configs in current window; cursor retained")
    else:
        log.info("ℹ️ No configs to test")

    proxy_with_ping = []
    if enable_proxies and new_proxies:
        valid_proxies = [p for p in new_proxies if is_telegram_proxy_url(p)]
        if valid_proxies:
            desired_proxy = max(1, int(get_profile_max_post_proxy(profile_id) or 1))
            log.info(f"📊 Processing proxies: candidates={len(valid_proxies)} target={desired_proxy} smart_batch={batch_posting}")
            sem = asyncio.Semaphore(3 if low_cost_mode else 4)

            async def check_proxy(proxy_url):
                async with sem:
                    host, port = extract_host(proxy_url)
                    flag, country_code = "🌐", ""
                    if host:
                        try:
                            cached_ping = _PING_TARGET_IP_CACHE.get(host.strip().lower())
                            ip = cached_ping[1] if cached_ping and time.monotonic() - cached_ping[0] < _PING_TARGET_IP_CACHE_TTL else None
                            if not ip:
                                ip = await host_to_ip(host)
                            if ip:
                                flag, country_code = await get_flag_for_ip(ip)
                        except Exception as e:
                            log.debug(f"[PROXY] GeoIP lookup failed for {host}: {e}")
                    if not host:
                        return proxy_url, 0, flag, country_code
                    if ping_testing:
                        try:
                            ping, ok, _ = await check_full_link_ping(proxy_url, proxy_ping_mode, perform_ping=True)
                            if not ok:
                                return proxy_url, 0, flag, country_code
                            return proxy_url, ping, flag, country_code
                        except Exception as e:
                            log.debug(f"[PROXY] ping failed for {host}:{port}: {e}")
                            return proxy_url, 0, flag, country_code
                    return proxy_url, 0, flag, country_code

            if batch_posting:
                _pending_batch_remove_posted(profile_id, "proxy")
                pending_rows = _pending_batch_rows(profile_id, "proxy")
                pending_hashes = {r[0] for r in pending_rows}
                for identity_hash, url, _src, ping, ping_count, flag, cc in pending_rows:
                    proxy_with_ping.append((url, ping, flag, cc))
                if len(proxy_with_ping) < desired_proxy:
                    fresh = []
                    _batch_prune_test_cache(profile_id)
                    for p in valid_proxies:
                        identity_hash = _proxy_identity_hash(p)
                        if identity_hash not in pending_hashes and not _batch_tested_recently(profile_id, "proxy", identity_hash):
                            fresh.append(p)
                    test_cap = min(len(fresh), max(24, min(120, desired_proxy * 12)))
                    chunk = fresh[:test_cap]
                    results = []
                    for offset in range(0, len(chunk), 4 if not low_cost_mode else 3):
                        part = chunk[offset:offset + (4 if not low_cost_mode else 3)]
                        results.extend(await asyncio.gather(*[check_proxy(p) for p in part], return_exceptions=True))
                    for p, r in zip(chunk, results):
                        identity_hash = _proxy_identity_hash(p)
                        _batch_mark_tested(profile_id, "proxy", identity_hash)
                        if not isinstance(r, Exception) and (not ping_testing or r[1] > 0):
                            _pending_batch_upsert(profile_id, "proxy", identity_hash, r[0], "", r[1], 0, r[2], r[3])
                            proxy_with_ping.append(r)
                    conn.commit()
                    log.info(f"📦 [AUTO-PROXY] strict queue tested={len(chunk)}/{len(fresh)} pending={len(proxy_with_ping)} target={desired_proxy}")
            else:
                results = []
                chunk_size = 4 if not low_cost_mode else 3
                for offset in range(0, len(valid_proxies), chunk_size):
                    part = valid_proxies[offset:offset + chunk_size]
                    results.extend(await asyncio.gather(*[check_proxy(p) for p in part], return_exceptions=True))
                    if len([x for x in results if not isinstance(x, Exception) and (not ping_testing or x[1] > 0)]) >= desired_proxy:
                        break
                for r in results:
                    if not isinstance(r, Exception):
                        proxy_with_ping.append(r)
                log.info(f"📊 Valid Telegram proxies ready for posting: {len(proxy_with_ping)}")
        else:
            log.info("ℹ️ No valid Telegram proxies found.")

    total_configs = 0
    total_proxies = 0
    selected_proxy_urls = []
    glass_proxy_rows = None

    # In combined mode, glass proxies are attached directly BELOW the config
    # message. Build the proxy payload first, but do not send it separately.
    combined_glass = (enable_configs and enable_proxies and get_profile_proxy_post_mode(profile_id) == 1)
    combined_proxy_ready = bool(proxy_with_ping) and (not batch_posting or len(proxy_with_ping) >= desired_proxy) if enable_proxies and new_proxies else False
    if combined_glass and proxy_with_ping and combined_proxy_ready:
        pcnt, ppayload, selected_proxy_urls = await post_proxies(
            bot, profile_id, proxy_with_ping[:desired_proxy] if batch_posting else proxy_with_ping, is_instant=is_instant
        )
        if pcnt > 0 and ppayload:
            _proxy_preview_text, all_proxy_rows = ppayload
            # post_proxies may append Channel/Sponsor rows. Only the first N rows
            # belong to proxy buttons; Channel and Sponsor are rebuilt by the
            # config sender so Sponsor remains the absolute bottom row.
            proxy_row_count=(pcnt + 2)//3
            glass_proxy_rows = all_proxy_rows[:proxy_row_count]
            log.info(f"[PROXY][profile={profile_id}] glass mode: attaching {pcnt} proxy buttons to config message")
        else:
            glass_proxy_rows = None
            selected_proxy_urls = []

    # Strict smart-batch rule:
    # When enabled, NEVER publish a partial batch. The configured maximum is a
    # required batch size, not merely a ceiling. If fewer healthy, non-duplicate
    # configs are ready, keep the stream cursor unchanged and wait for the next
    # cycle to collect the missing items. The normal mode remains unchanged.
    config_batch_ready = bool(working) and (not batch_posting or len(working) >= desired)
    if batch_posting and enable_configs and working and len(working) < desired:
        log.info(
            f"📦 [AUTO-CONFIG] strict batch WAIT profile={profile_id}: "
            f"ready={len(working)}/{desired}; nothing will be posted until the batch is full"
        )
    if config_batch_ready and enable_configs:
        # A config post is legal only if there is at least one candidate.
        # post_configs has a second hard gate, so an empty banner can never be
        # emitted even if a caller accidentally reaches this branch.
        if not working:
            log.warning(f"⛔ [AUTO-CONFIG] profile={profile_id}: no config candidate; skipping post")
        else:
            total_configs = await post_configs(
            bot, profile_id, working[:desired] if batch_posting else working,
            source_for_seen="auto", is_instant=is_instant,
            extra_button_rows=glass_proxy_rows
        )
        # If the config message containing the glass proxy buttons was delivered,
        # those proxies were actually published. Mark them only after success.
        if batch_posting and total_configs > 0:
            _pending_batch_remove_posted(profile_id, "config")
        if combined_glass and glass_proxy_rows and total_configs > 0:
            total_proxies = len(selected_proxy_urls)
            for proxy_url in selected_proxy_urls:
                if not is_proxy_posted(profile_id, proxy_url):
                    mark_proxy_posted(profile_id, proxy_url)
            if batch_posting:
                _pending_batch_remove_posted(profile_id, "proxy")
        elif combined_glass:
            # Config message failed/no configs: do not lose proxy candidates.
            selected_proxy_urls = []

    # Normal mode, or proxy-only mode, sends the proxy banner as its own message.
    # If glass mode was selected but there is no config message to attach to (or
    # the config send failed), fall back to a standalone proxy post so proxies
    # are never silently lost.
    proxy_batch_ready = bool(proxy_with_ping) and (not batch_posting or len(proxy_with_ping) >= desired_proxy) if enable_proxies and new_proxies else False
    if batch_posting and enable_proxies and proxy_with_ping and len(proxy_with_ping) < desired_proxy:
        log.info(
            f"📦 [AUTO-PROXY] strict batch WAIT profile={profile_id}: "
            f"ready={len(proxy_with_ping)}/{desired_proxy}; nothing will be posted until the batch is full"
        )
    if proxy_with_ping and enable_proxies and proxy_batch_ready and (not combined_glass or total_configs == 0):
        cnt, payload, selected_proxy_urls = await post_proxies(
            bot, profile_id, proxy_with_ping[:desired_proxy] if batch_posting else proxy_with_ping, is_instant=is_instant
        )
        if cnt > 0 and payload:
            text, buttons = payload
            log.info(f"[PROXY][profile={profile_id}] attempting Telegram send: count={cnt}, mode={get_profile_proxy_post_mode(profile_id)}, button_rows={len(buttons or [])}")
            sent = await send_to_destination(bot, profile_id, text, buttons)
            if sent:
                total_proxies = cnt
                log.info(f"[PROXY][profile={profile_id}] Telegram send succeeded for {cnt} proxies")
                mark_proxies_posted_batch(profile_id, selected_proxy_urls)
                if batch_posting:
                    _pending_batch_remove_posted(profile_id, "proxy")

    # Advance each stream independently. A failed Telegram send MUST NOT move
    # that stream's cursor, otherwise the failed content would be lost forever.
    # A stream may advance only when there was nothing new to publish OR when
    # every publishable candidate for that stream was actually delivered.
    # IMPORTANT: an existing/new candidate that failed validation or Telegram
    # delivery must remain behind the cursor so the next automatic cycle retries it.
    # Never move a stream cursor past unpublished candidates. If the current
    # window contains more new items than the configured per-post limit, the
    # next cycle must revisit the same window and publish the remainder.
    config_ok = (not enable_configs) or (not new_configs) or (not batch_posting) or (total_configs >= len(new_configs))
    proxy_ok = (not enable_proxies) or (not new_proxies) or (total_proxies >= len(new_proxies))
    if stream == "combined":
        if config_ok:
            set_stream_last_message_ids_batch(profile_id, "config", source_newest_ids.items())
        if proxy_ok:
            set_stream_last_message_ids_batch(profile_id, "proxy", source_newest_ids.items())
    elif stream == "config" and config_ok:
        set_stream_last_message_ids_batch(profile_id, "config", source_newest_ids.items())
    elif stream == "proxy" and proxy_ok:
        set_stream_last_message_ids_batch(profile_id, "proxy", source_newest_ids.items())

    result_msg = f"posted {total_configs} configs and {total_proxies} proxies"
    if failed_sources:
        result_msg += f" | failed sources: {len(failed_sources)} (cursor not advanced for failed sources)"
    if total_configs == 0 and total_proxies == 0:
        result_msg = "no new content to send"

    log.info(f"✅ Cycle result for profile {profile_id}: {result_msg}")
    log.info("=" * 50)
    return total_configs + total_proxies, result_msg

async def run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=True, is_instant=False, wait_if_running=False):
    pid = int(profile_id)
    if enable_configs and enable_proxies:
        stream_key = "combined"
    elif enable_configs:
        stream_key = "config"
    elif enable_proxies:
        stream_key = "proxy"
    else:
        return 0, "nothing enabled"
    lock_key = (pid, stream_key)
    lock = _PROFILE_CYCLE_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _PROFILE_CYCLE_LOCKS[lock_key] = lock
    if lock.locked() and not wait_if_running:
        log.warning("⏭️ [profile=%s][stream=%s] cycle already running; duplicate trigger skipped", pid, stream_key)
        return 0, "cycle already running"
    if lock.locked() and wait_if_running:
        log.info("⏳ [profile=%s][stream=%s] waiting for current cycle", pid, stream_key)
    async with lock:
        return await _run_cycle_for_profile_unlocked(
            bot, pid, enable_configs=enable_configs, enable_proxies=enable_proxies, is_instant=is_instant
        )

async def _run_manual_runnow_isolated(bot, profile_id):
    """Run Now lane independent from AUTO config/proxy locks.

    It intentionally uses the same tested cycle implementation, but acquires a
    dedicated manual lock instead of waiting behind an automatic stream.
    Dedup still protects already-posted items, while the manual trigger cannot
    deadlock on AUTO.
    """
    pid = int(profile_id)
    lock = _MANUAL_RUN_LOCKS.get(pid)
    if lock is None:
        lock = asyncio.Lock()
        _MANUAL_RUN_LOCKS[pid] = lock
    async with lock:
        return await _run_cycle_for_profile_unlocked(
            bot, pid, enable_configs=True, enable_proxies=True, is_instant=True
        )

async def profile_loop_config(bot, profile_id):
    log.info(f"🔄 Starting config loop for profile {profile_id}")
    while True:
        try:
            _WORKER_HEARTBEATS.get(f"auto_config_{profile_id}")
            _WORKER_HEARTBEATS[f"auto_config_{profile_id}"] = time.time()
            profile = get_profile(profile_id)
            if not profile:
                log.error(f"❌ Profile {profile_id} not found, stopping config loop.")
                break

            if not get_profile_enabled(profile_id):
                log.info(f"⏸️ Profile {profile_id} is disabled, sleeping 60s")
                await asyncio.sleep(60)
                continue

            if not get_profile_post_configs(profile_id):
                log.info(f"ℹ️ Config posting disabled for profile {profile_id}, sleeping 60s")
                await asyncio.sleep(60)
                continue

            interval = get_profile_interval_config(profile_id)
            dest_name = profile.get("dest_name", "unknown")
            timer_expiry = profile.get("timer_expiry")
            if timer_expiry:
                try:
                    expiry = datetime.fromisoformat(timer_expiry)
                    now = datetime.now(TEHRAN_TZ)
                    if expiry > now:
                        remaining_seconds = (expiry - now).total_seconds()
                        log.info(f"⏳ Timer active for profile {profile_id}: {remaining_seconds/60:.1f} minutes remaining")
                        await asyncio.sleep(min(remaining_seconds, 30))
                        continue
                    else:
                        clear_profile_timer(profile_id)
                        await bot.send_message(
                            MAIN_ADMIN_ID,
                            f"⏰ تایمر پروفایل {dest_name} (ID: {profile_id}) به پایان رسید. ارسال خودکار از سر گرفته شد."
                        )
                        log.info(f"✅ Timer expired for profile {profile_id}, running cycle immediately")
                        n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=(interval == 0))
                        log.info(f"[config loop] result: {n} - {m}")
                        continue
                except Exception as e:
                    log.error(f"Error parsing timer: {e}")
                    clear_profile_timer(profile_id)

            if interval == 0:
                log.info(f"⚡ INSTANT CONFIG UPDATE for profile {profile_id} ({dest_name})")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=True)
                log.info(f"[instant config] result: {n} - {m}")
                await asyncio.sleep(5)
            else:
                now = datetime.now(TEHRAN_TZ)
                next_run = now + timedelta(minutes=interval)
                sleep_seconds = (next_run - now).total_seconds()
                if sleep_seconds > 0:
                    log.info(f"⏳ Config loop sleeping for {sleep_seconds:.0f}s until {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                    await asyncio.sleep(sleep_seconds)
                else:
                    await asyncio.sleep(1)

                log.info(f"⏰ CONFIG AUTO TICK for profile {profile_id}")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=False)
                log.info(f"[config auto] result: {n} - {m}")

        except asyncio.CancelledError:
            log.info(f"🛑 Config loop for profile {profile_id} cancelled.")
            break
        except Exception as e:
            log.error(f"❌ profile_loop_config error: {e}")
            log.error(traceback.format_exc())
            await asyncio.sleep(60)

async def profile_loop_proxy(bot, profile_id):
    log.info(f"🔄 Starting proxy loop for profile {profile_id}")
    while True:
        try:
            _WORKER_HEARTBEATS.get(f"auto_proxy_{profile_id}")
            _WORKER_HEARTBEATS[f"auto_proxy_{profile_id}"] = time.time()
            profile = get_profile(profile_id)
            if not profile:
                log.error(f"❌ Profile {profile_id} not found, stopping proxy loop.")
                break

            if not get_profile_enabled(profile_id):
                log.info(f"⏸️ Profile {profile_id} is disabled, sleeping 60s")
                await asyncio.sleep(60)
                continue

            if not get_profile_post_proxies(profile_id):
                log.info(f"ℹ️ Proxy posting disabled for profile {profile_id}, sleeping 60s")
                await asyncio.sleep(60)
                continue

            interval = get_profile_interval_proxy(profile_id)
            dest_name = profile.get("dest_name", "unknown")
            timer_expiry = profile.get("timer_expiry")
            if timer_expiry:
                try:
                    expiry = datetime.fromisoformat(timer_expiry)
                    now = datetime.now(TEHRAN_TZ)
                    if expiry > now:
                        remaining_seconds = (expiry - now).total_seconds()
                        log.info(f"⏳ Timer active for profile {profile_id}: {remaining_seconds/60:.1f} minutes remaining")
                        await asyncio.sleep(min(remaining_seconds, 30))
                        continue
                    else:
                        clear_profile_timer(profile_id)
                        await bot.send_message(
                            MAIN_ADMIN_ID,
                            f"⏰ تایمر پروفایل {dest_name} (ID: {profile_id}) به پایان رسید. ارسال خودکار از سر گرفته شد."
                        )
                        log.info(f"✅ Timer expired for profile {profile_id}, running proxy cycle immediately")
                        n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=(interval == 0))
                        log.info(f"[proxy loop] result: {n} - {m}")
                        continue
                except Exception as e:
                    log.error(f"Error parsing timer: {e}")
                    clear_profile_timer(profile_id)

            if interval == 0:
                log.info(f"⚡ INSTANT PROXY UPDATE for profile {profile_id} ({dest_name})")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=True)
                log.info(f"[instant proxy] result: {n} - {m}")
                await asyncio.sleep(5)
            else:
                now = datetime.now(TEHRAN_TZ)
                next_run = now + timedelta(minutes=interval)
                sleep_seconds = (next_run - now).total_seconds()
                if sleep_seconds > 0:
                    log.info(f"⏳ Proxy loop sleeping for {sleep_seconds:.0f}s until {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                    await asyncio.sleep(sleep_seconds)
                else:
                    await asyncio.sleep(1)

                log.info(f"⏰ PROXY AUTO TICK for profile {profile_id}")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=False)
                log.info(f"[proxy auto] result: {n} - {m}")

        except asyncio.CancelledError:
            log.info(f"🛑 Proxy loop for profile {profile_id} cancelled.")
            break
        except Exception as e:
            log.error(f"❌ profile_loop_proxy error: {e}")
            log.error(traceback.format_exc())
            await asyncio.sleep(60)

async def _auto_scan_stream_unlocked(profile_id, stream):
    pid=int(profile_id); profile=get_profile(pid)
    if not profile or not get_profile_enabled(pid): return 0
    if stream=="config":
        if not get_profile_post_configs(pid): return 0
    elif not get_profile_post_proxies(pid): return 0
    expiry_raw=profile.get("timer_expiry")
    if expiry_raw:
        try:
            expiry=datetime.fromisoformat(expiry_raw)
            if expiry.tzinfo is None: expiry=TEHRAN_TZ.localize(expiry)
            if expiry.astimezone(TEHRAN_TZ)>datetime.now(TEHRAN_TZ): return 0
        except Exception: pass
    sources=[normalize_channel_input(x) for x in get_profile_sources(pid)]
    sources=[x for x in sources if x]
    if not sources: return 0
    sem=asyncio.Semaphore(20 if get_profile_low_cost_mode(pid) else 28)
    async def one(src):
        async with sem:
            try:
                return await asyncio.wait_for(_cached_auto_scrape(pid,src,stream),timeout=18.0)
            except Exception as exc:
                log.warning("[AUTO-SCAN][%s][profile=%s] %s failed: %s",stream,pid,src,exc); return None
    results=await asyncio.gather(*[one(src) for src in sources])
    pending_hashes={r[0] for r in _pending_batch_rows(pid,stream)}
    ping_enabled=get_profile_ping_enabled(pid); inserted=0; cursor_updates=[]

    async def _resolve_proxy_geo(proxy_url):
        """Resolve proxy country metadata when a proxy enters the persistent queue."""
        flag, country_code = "🌐", ""
        try:
            host, _port = extract_host(proxy_url)
            if host:
                cached_ping = _PING_TARGET_IP_CACHE.get(host.strip().lower())
                ip = cached_ping[1] if cached_ping and time.monotonic() - cached_ping[0] < _PING_TARGET_IP_CACHE_TTL else None
                if not ip:
                    ip = await host_to_ip(host)
                if ip:
                    flag, country_code = await get_flag_for_ip(ip)
        except Exception as exc:
            log.debug("[AUTO-SCAN][proxy] geo lookup failed for %s: %s", str(proxy_url)[:90], exc)
        return flag, country_code

    for src,result in zip(sources,results):
        if not result: continue
        configs,proxies,newest_id=result
        if newest_id: cursor_updates.append((src,newest_id))
        candidates=configs if stream=="config" else [normalize_proxy_url(x) for x in proxies]
        rows=[]
        for raw in [x for x in candidates if x]:
            h=_config_identity_hash(raw) if stream=="config" else _proxy_identity_hash(raw)
            if h in pending_hashes: continue
            if (is_already_posted(pid,raw) if stream=="config" else is_proxy_posted(pid,raw)): continue
            if stream == "proxy":
                flag, country_code = await _resolve_proxy_geo(raw)
            else:
                flag, country_code = "🌐", ""
            rows.append((h,raw,src,1 if not ping_enabled else 0,0,flag,country_code)); pending_hashes.add(h)
        if rows: inserted += _pending_batch_upsert_many(pid,stream,rows)
    if cursor_updates: set_stream_last_message_ids_batch(pid,stream,cursor_updates)
    if inserted: log.info("[AUTO-SCAN][%s][profile=%s] queued=%d",stream,pid,inserted)
    return inserted

async def _auto_health_test_stream_unlocked(profile_id, stream):
    pid=int(profile_id); profile=get_profile(pid)
    if not profile or not get_profile_enabled(pid): return 0
    if not get_profile_ping_enabled(pid):
        try:
            cur=c.execute("UPDATE pending_batch_items SET ping=1 WHERE profile_id=? AND kind=? AND ping<=0",(pid,stream)); conn.commit(); return max(0,cur.rowcount)
        except sqlite3.Error: conn.rollback(); return 0
    limit=AUTO_CONFIG_TEST_BATCH if stream=="config" else AUTO_PROXY_TEST_BATCH
    rows=c.execute("SELECT identity_hash,url,source FROM pending_batch_items WHERE profile_id=? AND kind=? AND ping<=0 ORDER BY added_at ASC LIMIT ?",(pid,stream,limit)).fetchall()
    if not rows: return 0
    sem=asyncio.Semaphore(3 if get_profile_low_cost_mode(pid) else 4)
    host_mode=get_profile_config_ping_mode(pid) if stream=="config" else get_profile_proxy_ping_mode(pid)
    test_mode=get_profile_config_test_mode(pid) if stream=="config" else get_profile_proxy_test_mode(pid)
    async def test(row):
        h,url,src=row
        async with sem:
            try:
                # 0 = current Check-Host only; 1 = real full-config; 2 = full-config then host.
                if test_mode in (1,2):
                    full_ms, full_ok, _detail = await full_config_ping(url)
                    if not full_ok:
                        return h,0,0,False
                else:
                    full_ms=0
                if test_mode==1:
                    return h,float(full_ms),1,True
                host, _port = extract_host(url)
                if not host:
                    return h,0,0,False
                host_ms,host_ok,count=await _check_host_ping(host,host_mode)
                return h,host_ms,count,host_ok
            except Exception as exc:
                log.debug("[AUTO-TEST] failed %s: %s", h, exc)
                return h,0,0,False
    results=await asyncio.gather(*[test(r) for r in rows])
    passed=failed=0
    for h,ping,count,ok in results:
        if ok and ping>0:
            c.execute("UPDATE pending_batch_items SET ping=?,ping_count=? WHERE profile_id=? AND kind=? AND identity_hash=?",(float(ping),int(count),pid,stream,h)); passed+=1
        else:
            c.execute("DELETE FROM pending_batch_items WHERE profile_id=? AND kind=? AND identity_hash=?",(pid,stream,h)); failed+=1
    conn.commit()
    if passed or failed: log.info("[AUTO-TEST][%s][profile=%s] passed=%d failed=%d",stream,pid,passed,failed)
    return passed

async def _auto_scan_stream(profile_id, stream):
    async with _auto_stream_lock(profile_id, stream, "scan"):
        return await _auto_scan_stream_unlocked(profile_id, stream)

async def _auto_health_test_stream(profile_id, stream):
    async with _auto_stream_lock(profile_id, stream, "test"):
        return await _auto_health_test_stream_unlocked(profile_id, stream)

async def _auto_post_pending(profile_id, stream, bot, force_instant=False):
    async with _auto_stream_lock(profile_id, stream, "post"):
        return await _auto_post_pending_unlocked(profile_id, stream, bot, force_instant)

async def _auto_scanner_worker(profile_id,stream):
    name=f"auto_scan_{stream}_{profile_id}"; log.info("[AUTO-SCAN] started %s",name)
    while True:
        try:
            _WORKER_HEARTBEATS[name]=time.time()
            await _auto_scan_stream(profile_id,stream)
            _WORKER_HEARTBEATS[name]=time.time()
            try:
                raw_interval = get_profile_interval_config(profile_id) if stream == "config" else get_profile_interval_proxy(profile_id)
                post_seconds = max(0, int(raw_interval or 0)) * 60
            except Exception:
                post_seconds = 0
            scan_delay = 30.0 if get_profile_low_cost_mode(profile_id) else 20.0
            if post_seconds > 0:
                scan_delay = min(scan_delay, max(10.0, post_seconds / 4.0))
            await asyncio.sleep(scan_delay)
        except asyncio.CancelledError: return
        except Exception: log.exception("[AUTO-SCAN] worker error %s",name); await asyncio.sleep(2)

async def _auto_tester_worker(profile_id,stream):
    name=f"auto_test_{stream}_{profile_id}"; log.info("[AUTO-TEST] started %s",name)
    while True:
        try:
            _WORKER_HEARTBEATS[name]=time.time()
            await _auto_health_test_stream(profile_id,stream)
            _WORKER_HEARTBEATS[name]=time.time()
            await asyncio.sleep(AUTO_TEST_INTERVAL_SECONDS)
        except asyncio.CancelledError: return
        except Exception: log.exception("[AUTO-TEST] worker error %s",name); await asyncio.sleep(2)

async def _auto_post_pending_unlocked(profile_id,stream,bot,force_instant=False):
    pid=int(profile_id); profile=get_profile(pid)
    if not profile or not get_profile_enabled(pid): return 0
    if stream=="config":
        if not get_profile_post_configs(pid): return 0
        desired=max(1,int(get_profile_max_post_config(pid) or 1))
    else:
        if not get_profile_post_proxies(pid): return 0
        desired=max(1,int(get_profile_max_post_proxy(pid) or 1))
    rows=_pending_batch_rows(pid,stream)
    if stream == "proxy":
        # Repair legacy pending rows created before proxy GeoIP metadata was
        # persisted. This keeps existing queues intact while ensuring proxy
        # posts receive their country flag/name as soon as the data is needed.
        repaired_rows = []
        repair_limit = max(1, desired)
        for row_index, row in enumerate(rows):
            if row_index >= repair_limit:
                repaired_rows.extend(rows[repair_limit:])
                break
            identity_hash, url, source, ping, ping_count, flag, country_code = row
            if not flag or flag == "🌐" or not country_code:
                try:
                    host, _port = extract_host(url)
                    ip = None
                    if host:
                        cached_ping = _PING_TARGET_IP_CACHE.get(host.strip().lower())
                        ip = cached_ping[1] if cached_ping and time.monotonic() - cached_ping[0] < _PING_TARGET_IP_CACHE_TTL else None
                        if not ip:
                            ip = await host_to_ip(host)
                    if ip:
                        resolved_flag, resolved_country = await get_flag_for_ip(ip)
                        if resolved_flag:
                            flag = resolved_flag
                        if resolved_country:
                            country_code = resolved_country
                        c.execute(
                            "UPDATE pending_batch_items SET flag=?, country_code=? WHERE profile_id=? AND kind='proxy' AND identity_hash=?",
                            (flag or "🌐", country_code or "", pid, identity_hash)
                        )
                except Exception as exc:
                    log.debug("[AUTO-POST][proxy] metadata repair failed for %s: %s", str(url)[:90], exc)
            repaired_rows.append((identity_hash, url, source, ping, ping_count, flag, country_code))
        conn.commit()
        rows = repaired_rows
    if get_profile_ping_enabled(pid): rows=[r for r in rows if float(r[3] or 0)>0]
    if not rows: return 0
    if get_profile_batch_posting(pid) and len(rows)<desired: return 0
    rows=rows[:desired]
    if stream=="config":
        sent=await post_configs(bot,pid,[(r[1],float(r[3] or 0),int(r[4] or 0)) for r in rows],source_for_seen="auto",is_instant=force_instant,max_post_override=desired)
        if sent: _pending_batch_remove_posted(pid,"config")
        return sent
    items=[(r[1],float(r[3] or 0),r[5] or "🌐",r[6] or "") for r in rows]
    cnt,payload,urls=await post_proxies(bot,pid,items,is_instant=force_instant,max_proxies_override=desired)
    if cnt and payload:
        text,buttons=payload
        if await send_to_destination(bot,pid,text,buttons):
            mark_proxies_posted_batch(pid,urls); _pending_batch_remove_posted(pid,"proxy"); return cnt
    return 0

async def _auto_exact_poster_worker(profile_id,stream,bot):
    name=f"auto_post_{stream}_{profile_id}"; log.info("[AUTO-POST] started %s | timer independent",name)
    loop=asyncio.get_running_loop(); next_deadline=_AUTO_POST_DEADLINES.get((int(profile_id), str(stream))); interval_seconds=None
    while True:
        try:
            _WORKER_HEARTBEATS[name]=time.time(); profile=get_profile(profile_id)
            if not profile or not get_profile_enabled(profile_id): next_deadline=None; interval_seconds=None; await asyncio.sleep(1); continue
            enabled=get_profile_post_configs(profile_id) if stream=="config" else get_profile_post_proxies(profile_id)
            if not enabled: next_deadline=None; interval_seconds=None; await asyncio.sleep(1); continue
            raw=get_profile_interval_config(profile_id) if stream=="config" else get_profile_interval_proxy(profile_id)
            try: minutes=max(0,int(raw or 0))
            except Exception: minutes=0
            expiry_raw=profile.get("timer_expiry")
            if expiry_raw:
                try:
                    expiry=datetime.fromisoformat(expiry_raw)
                    if expiry.tzinfo is None: expiry=TEHRAN_TZ.localize(expiry)
                    nowt=datetime.now(TEHRAN_TZ)
                    if expiry.astimezone(TEHRAN_TZ)>nowt:
                        next_deadline=None; interval_seconds=None; await asyncio.sleep(min(1,max(.05,(expiry.astimezone(TEHRAN_TZ)-nowt).total_seconds()))); continue
                    clear_profile_timer(profile_id)
                except Exception:
                    clear_profile_timer(profile_id); next_deadline=None; interval_seconds=None
            if minutes==0:
                sent=await _auto_post_pending(profile_id,stream,bot,True)
                _WORKER_HEARTBEATS[name]=time.time()
                if sent: log.info("[AUTO-POST][%s][profile=%s] instant sent=%d",stream,profile_id,sent)
                await asyncio.sleep(AUTO_INSTANT_POST_POLL_SECONDS); continue
            seconds=float(minutes*60)
            if interval_seconds!=seconds or next_deadline is None:
                # The current admin/profile interval is authoritative. If it
                # changes, start a fresh window using the new value. If a worker
                # is merely restarted with the same value, keep its existing
                # deadline so watchdog restarts cannot postpone a scheduled post.
                changed = interval_seconds is not None and interval_seconds != seconds
                interval_seconds=seconds
                if next_deadline is None or changed:
                    next_deadline=loop.time()+seconds
                else:
                    now_mono=loop.time()
                    if next_deadline > now_mono + seconds:
                        next_deadline=now_mono+seconds
                _AUTO_POST_DEADLINES[(int(profile_id), str(stream))]=next_deadline
            wait=next_deadline-loop.time()
            if wait>0: await asyncio.sleep(wait); continue
            scheduled=datetime.now(TEHRAN_TZ); log.info("[AUTO-POST] TICK profile=%s stream=%s scheduled=%s interval=%sm",profile_id,stream,scheduled.isoformat(),minutes)
            now_mono=loop.time(); missed=max(0,int((now_mono-next_deadline)//seconds)); next_deadline+=(missed+1)*seconds
            _AUTO_POST_DEADLINES[(int(profile_id), str(stream))]=next_deadline
            sent=await _auto_post_pending(profile_id,stream,bot,False)
            _WORKER_HEARTBEATS[name]=time.time()
            log.info("[AUTO-POST] DONE profile=%s stream=%s sent=%d next_in=%.1fs",profile_id,stream,sent,max(0,next_deadline-loop.time()))
        except asyncio.CancelledError: return
        except Exception: log.exception("[AUTO-POST] error profile=%s stream=%s",profile_id,stream); await asyncio.sleep(.5)

async def _auto_pipeline_supervisor(app):
    while True:
        try:
            if ENABLE_AUTO:
                for prof in get_profiles():
                    pid=int(prof["id"])
                    if not get_profile_enabled(pid): continue
                    if get_profile_post_configs(pid):
                        start_worker(app,f"auto_scan_config_{pid}",lambda pid=pid:_auto_scanner_worker(pid,"config"))
                        start_worker(app,f"auto_test_config_{pid}",lambda pid=pid:_auto_tester_worker(pid,"config"))
                        start_worker(app,f"auto_post_config_{pid}",lambda pid=pid:_auto_exact_poster_worker(pid,"config",app.bot))
                    if get_profile_post_proxies(pid):
                        start_worker(app,f"auto_scan_proxy_{pid}",lambda pid=pid:_auto_scanner_worker(pid,"proxy"))
                        start_worker(app,f"auto_test_proxy_{pid}",lambda pid=pid:_auto_tester_worker(pid,"proxy"))
                        start_worker(app,f"auto_post_proxy_{pid}",lambda pid=pid:_auto_exact_poster_worker(pid,"proxy",app.bot))
            await asyncio.sleep(10)
        except asyncio.CancelledError: return
        except Exception: log.exception("[AUTO-PIPELINE] supervisor failed"); await asyncio.sleep(5)

async def _run_scheduled_profile_cycle(bot, profile_id, mode):
    """Run exactly one independent automatic stream.

    Config and proxy workers never depend on each other; a proxy success can
    never mask a config failure. Every config tick therefore calls the same
    config-only cycle used by the normal scheduler.
    """
    try:
        if mode == "config":
            result = await run_cycle_for_profile(
                bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=(get_profile_interval_config(profile_id) == 0)
            )
            log.info(f"[AUTO-CONFIG] profile={profile_id} result={result}")
            return result
        result = await run_cycle_for_profile(
            bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=(get_profile_interval_proxy(profile_id) == 0)
        )
        log.info(f"[AUTO-PROXY] profile={profile_id} result={result}")
        return result
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(f"[AUTO-{mode.upper()}] profile={profile_id} cycle failed")
        return 0, "error"

async def _profile_scheduler_v16(bot, profile_id, mode):
    """Stable interval scheduler.

    - Uses Tehran time for all displayed/saved timestamps.
    - Uses the event loop monotonic clock for sleeping, preventing wall-clock
      jumps from making a job run early/late.
    - Keeps a fixed schedule anchor, so a slow scrape/send cycle does not shift
      every future run.
    - Never silently converts an explicit interval=0 setting into 1 minute.
    """
    key = (int(profile_id), str(mode))
    log.info(f"[SCHEDULER] started {key} | timezone=Asia/Tehran")
    loop = asyncio.get_running_loop()
    next_deadline = None
    interval_seconds = None

    while True:
        try:
            _WORKER_HEARTBEATS.get(f"auto_{mode}_{profile_id}")
            _WORKER_HEARTBEATS[f"auto_{mode}_{profile_id}"] = time.time()
            profile = get_profile(profile_id)
            if not profile:
                log.warning(f"[SCHEDULER] profile {profile_id} disappeared; stopping {mode}")
                return

            if not get_profile_enabled(profile_id):
                next_deadline = None
                interval_seconds = None
                await asyncio.sleep(1.0)
                continue

            posting_enabled = (get_profile_post_configs(profile_id) if mode == "config"
                               else get_profile_post_proxies(profile_id))
            if not posting_enabled:
                next_deadline = None
                interval_seconds = None
                await asyncio.sleep(1.0)
                continue

            raw_interval = get_profile_interval_config(profile_id) if mode == "config" else get_profile_interval_proxy(profile_id)
            try:
                interval_minutes = int(raw_interval or 0)
            except Exception:
                interval_minutes = 0
            interval_minutes = max(0, interval_minutes)

            # Profile timer is always interpreted in Tehran timezone.
            timer_expiry = profile.get("timer_expiry")
            if timer_expiry:
                try:
                    expiry = datetime.fromisoformat(timer_expiry)
                    if expiry.tzinfo is None:
                        expiry = TEHRAN_TZ.localize(expiry)
                    expiry = expiry.astimezone(TEHRAN_TZ)
                    now_tehran = datetime.now(TEHRAN_TZ)
                    if expiry > now_tehran:
                        next_deadline = None
                        interval_seconds = None
                        await asyncio.sleep(min(1.0, max(0.05, (expiry - now_tehran).total_seconds())))
                        continue
                    clear_profile_timer(profile_id)
                    try:
                        await bot.send_message(
                            MAIN_ADMIN_ID,
                            f"⏰ تایمر پروفایل {profile.get('dest_name','')} (ID: {profile_id}) به پایان رسید. ارسال خودکار از سر گرفته شد."
                        )
                    except Exception:
                        pass
                    next_deadline = None
                    interval_seconds = None
                except Exception:
                    clear_profile_timer(profile_id)
                    next_deadline = None
                    interval_seconds = None

            # interval=0 is a responsive polling mode, not a busy loop.
            # Public-channel scraping cannot be truly event-driven, so use a
            # bounded 30-second cadence instead of hammering 100+ sources every
            # second. Positive intervals are scheduled precisely.
            if interval_minutes == 0:
                started = datetime.now(TEHRAN_TZ)
                log.info(f"[SCHEDULER] AUTO TICK {key} at {started.isoformat()} (instant mode)")
                await _run_scheduled_profile_cycle(bot, profile_id, mode)
                # Public Telegram channel scraping is polling-based. A short
                # bounded sleep gives near-immediate updates without a busy loop.
                await asyncio.sleep(15.0)
                continue

            seconds = float(interval_minutes * 60)
            if interval_seconds != seconds or next_deadline is None:
                # Preserve the bot's established behavior: one immediate cycle
                # when the worker starts (so enabling AUTO never waits a whole
                # interval), then every configured interval from that fixed
                # anchor. This also keeps the cadence independent of scrape time.
                interval_seconds = seconds
                # Positive intervals are full accumulation windows. The first
                # scheduled publication is after the configured interval.
                next_deadline = loop.time() + seconds

            wait = next_deadline - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
                continue

            scheduled_for = datetime.now(TEHRAN_TZ)
            log.info(
                f"[SCHEDULER] AUTO TICK {key} scheduled={scheduled_for.isoformat()} "
                f"interval={interval_minutes}m"
            )

            # Advance the schedule BEFORE doing network work. If the cycle is
            # slow, skip missed slots rather than firing a burst of late posts.
            now_mono = loop.time()
            missed = max(0, int((now_mono - next_deadline) // seconds))
            next_deadline += (missed + 1) * seconds

            await _run_scheduled_profile_cycle(bot, profile_id, mode)

        except asyncio.CancelledError:
            log.info(f"[SCHEDULER] cancelled {key}")
            return
        except Exception:
            log.exception(f"[SCHEDULER] error in {key}")
            # Never sleep for 30/60 seconds after a transient error; that would
            # destroy exact scheduling. Re-enter the deadline calculation quickly.
            await asyncio.sleep(0.5)
            if interval_seconds and next_deadline is None:
                next_deadline = loop.time() + interval_seconds

async def check_and_auto_backup(profile_id):
    try:
        profile = get_profile(profile_id)
        if not profile:
            return
        profile_name = profile["dest_name"].replace("@", "").strip() or f"profile_{profile_id}"
        backup_interval = get_profile_backup_interval(profile_id) or 1000

        lock = backup_locks.get(profile_id)
        if not lock:
            lock = asyncio.Lock()
            backup_locks[profile_id] = lock

        async with lock:
            archive_rows = export_archive_rows("config", profile_id)
            archive_nums = [(int(r[2] or 0), r[1]) for r in archive_rows if len(r) > 2 and int(r[2] or 0) > 0]
            current_rows = c.execute(
                "SELECT backup_num,full_url FROM seen WHERE profile_id=? AND full_url != '' AND backup_num > 0 ORDER BY backup_num",
                (profile_id,)).fetchall()
            all_numbered = {int(n): url for n,url in archive_nums}
            for n,url in current_rows:
                if int(n) > 0 and url: all_numbered[int(n)] = url
            total = max(all_numbered.keys(), default=0)
            last_backup = get_profile_last_backup_count(profile_id)

            last_backup_block = last_backup // backup_interval if backup_interval > 0 else 0
            current_block = total // backup_interval if backup_interval > 0 else 0

            if current_block <= last_backup_block:
                return

            for block in range(last_backup_block + 1, current_block + 1):
                start_num = (block - 1) * backup_interval + 1
                end_num = block * backup_interval

                links = [all_numbered[n] for n in range(start_num, end_num + 1) if all_numbered.get(n)]
                if not links:
                    continue

                filename = f"configs_backup_{get_tehran_date()}_{profile_name}_{start_num}_{end_num}.txt"
                content = f"# Backup for {profile_name} (ID: {profile_id})\n# Range: {start_num} - {end_num}\n# Total: {len(links)}\n\n" + "\n".join(links)
                filepath = os.path.join(DATA_DIR, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)

                bot = BOT_REF
                if bot:
                    with open(filepath, "rb") as f:
                        await bot.send_document(
                            MAIN_ADMIN_ID,
                            document=f,
                            filename=filename,
                            caption=f"📤 بک‌آپ خودکار پروفایل {profile_name} (ID:{profile_id}) - {start_num} تا {end_num} (تعداد: {len(links)})"
                        )
                    asyncio.create_task(delete_file_after_delay(filepath, 1800))
                else:
                    log.warning("BOT_REF is None, cannot send backup.")

            set_profile_last_backup_count(profile_id, total)
    except Exception as e:
        log.error(f"Auto backup check error for profile {profile_id}: {e}")

async def delete_file_after_delay(filepath, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            log.info(f"🗑️ Deleted file: {filepath}")
    except Exception as e:
        log.error(f"Error deleting file {filepath}: {e}")

async def get_logs(update, context, profile_id, log_type="full", time_range_minutes=30):
    """Export the actual rotating bot.log files; safe for callback buttons too."""
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        log.error("[LOGS] no effective message")
        return
    paths = [p for p in (_LOG_FILE, _LOG_FILE+".1", _LOG_FILE+".2", _LOG_FILE+".3") if os.path.isfile(p)]
    if not paths:
        await message.reply_text("❌ هیچ فایل لاگی پیدا نشد.")
        return
    try:
        minutes=max(1,int(time_range_minutes or 30))
    except (TypeError,ValueError):
        minutes=30
    cutoff=datetime.now(TEHRAN_TZ)-timedelta(minutes=minutes)
    errors=("ERROR","CRITICAL","Traceback","Exception","[DEBUG]")
    selected=[]; read_errors=[]
    for path in paths:
        try:
            with open(path,"r",encoding="utf-8",errors="replace") as f:
                for raw in f:
                    line=raw.rstrip("\n")
                    m=re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",line)
                    if m:
                        ts=normalize_datetime_value(m.group(1))
                        if ts and ts < cutoff: continue
                    if log_type=="errors" and not any(k in line for k in errors): continue
                    selected.append(line)
        except Exception as exc:
            read_errors.append(f"{os.path.basename(path)}: {exc}")
    if not selected:
        extra=("\n"+"; ".join(read_errors)) if read_errors else ""
        await message.reply_text(f"❌ هیچ لاگ {html.escape(str(log_type))} در {minutes} دقیقه اخیر پیدا نشد.{extra}",parse_mode="HTML")
        return
    selected=selected[-2500:]
    content="\n".join(selected)
    if read_errors: content += "\n\n[LOG READ WARNINGS]\n"+"\n".join(read_errors)
    stamp=datetime.now(TEHRAN_TZ).strftime("%Y%m%d_%H%M%S")
    filename=f"bot_logs_{log_type}_{minutes}m_{stamp}.txt"
    filepath=os.path.join(DATA_DIR,filename)
    try:
        with open(filepath,"w",encoding="utf-8") as f: f.write(content)
        with open(filepath,"rb") as f:
            await message.reply_document(document=f,filename=filename,caption=f"📋 لاگ واقعی bot.log ({log_type}) | {len(selected)} خط | {minutes} دقیقه")
    finally:
        try: os.remove(filepath)
        except OSError: pass

async def send_daily_report(app):
    try:
        profiles = get_profiles()
        total_profiles = len(profiles)
        total_seen = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        total_proxies = c.execute("SELECT COUNT(*) FROM proxies_seen").fetchone()[0]
        total_blacklist = c.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]

        lines = []
        lines.append("📊 **گزارش روزانه بات**")
        lines.append(f"📅 تاریخ: {get_tehran_date()}")
        lines.append(f"🕐 زمان: {get_tehran_time()}")
        lines.append("")
        lines.append(f"📌 تعداد پروفایل‌ها: {total_profiles}")
        lines.append(f"📡 کانفیگ‌های دیده‌شده: {total_seen}")
        lines.append(f"🌐 پروکسی‌های دیده‌شده: {total_proxies}")
        lines.append(f"🚫 کلمات لیست سیاه: {total_blacklist}")
        lines.append("")
        for p in profiles:
            src_count = len(get_profile_sources(p['id']))
            last_num = p['last_num']
            interval_cfg = p.get('interval_config', 5)
            interval_prx = p.get('interval_proxy', 5)
            timer = ""
            if p.get('timer_expiry'):
                timer = " (⏳ تایمر فعال)"
            enabled_status = "✅ فعال" if get_profile_enabled(p['id']) else "⛔ غیرفعال"
            lines.append(f"• {p['dest_name']} (ID:{p['id']}) – {src_count} منبع, بازه کانفیگ:{interval_cfg}m, بازه پروکسی:{interval_prx}m, #{last_num+1}{timer} {enabled_status}")

        msg = "\n".join(lines)
        await app.bot.send_message(MAIN_ADMIN_ID, msg, parse_mode="HTML")
        log.info("✅ Daily report sent.")
    except Exception as e:
        log.error(f"❌ Failed to send daily report: {e}")

def _prune_log_to_30_minutes():
    """Keep only timestamped bot.log records from the last 30 minutes."""
    path=os.path.join(DATA_DIR, "bot.log")
    if not os.path.exists(path):
        return 0
    cutoff=datetime.now(TEHRAN_TZ)-timedelta(minutes=30)
    try:
        with _LOG_PRUNE_LOCK:
            with open(path,"r",encoding="utf-8",errors="ignore") as f:
                lines=f.readlines()
            kept=[]
            for line in lines:
                m=re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",line)
                if not m:
                    # Preserve continuation/traceback lines only if the file still has
                    # a recent timestamped record; they are tiny and avoid broken tracebacks.
                    if kept: kept.append(line)
                    continue
                try:
                    ts=normalize_datetime_value(m.group(1))
                    if ts>=cutoff: kept.append(line)
                except Exception:
                    kept.append(line)
            # Truncate/rewrite the same inode because logging.FileHandler keeps
            # its file descriptor open for the lifetime of the bot. Replacing the
            # path would make future log lines continue in an unlinked old inode.
            with open(path,"w",encoding="utf-8") as f: f.writelines(kept)
            return len(kept)
    except Exception as exc:
        log.warning("[LOG PRUNE] skipped: %s",exc)
        return 0

async def periodic_cleanup():
    while True:
        try:
            await asyncio.to_thread(_prune_log_to_30_minutes)

            now_ts = time.time()
            for fname in os.listdir(DATA_DIR):
                if fname == "bot.db":
                    continue
                filepath = os.path.join(DATA_DIR, fname)
                if os.path.isfile(filepath):
                    if fname == "bot.log":
                        continue
                    if (fname.startswith("configs_backup_") or 
                        fname.startswith("proxies_backup_") or 
                        fname.startswith("logs_") or 
                        fname.startswith("bot_backup_")):
                        try:
                            mtime = os.path.getmtime(filepath)
                            if now_ts - mtime > 3600:
                                os.remove(filepath)
                                log.info(f"🗑️ Cleanup: deleted old file {fname}")
                        except Exception as e:
                            log.warning(f"Could not delete {fname}: {e}")

            if os.path.exists(BACKUP_DIR):
                for fname in os.listdir(BACKUP_DIR):
                    filepath = os.path.join(BACKUP_DIR, fname)
                    if os.path.isfile(filepath):
                        try:
                            mtime = os.path.getmtime(filepath)
                            if now_ts - mtime > 3600:
                                os.remove(filepath)
                                log.info(f"🗑️ Cleanup: deleted old backup file {fname}")
                        except Exception as e:
                            log.warning(f"Could not delete {fname}: {e}")

        except Exception as e:
            log.error(f"Error in periodic_cleanup: {e}")
        await asyncio.sleep(300)
