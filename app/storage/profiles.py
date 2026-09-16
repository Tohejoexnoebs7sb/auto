from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

def clean_source_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip()
    if "t.me/" in name.lower():
        match = re.search(r't\.me/([^/?]+)', name, re.IGNORECASE)
        if match:
            name = match.group(1)
    if name and not name.startswith("@"):
        name = "@" + name
    cleaned = re.sub(r'[^@a-zA-Z0-9_]', '', name)
    if len(cleaned) <= 1:
        if re.match(r'^@[a-zA-Z0-9_]+$', name):
            return name
        return ""
    return cleaned

def normalize_channel_input(text: str) -> str:
    return clean_source_name(text)

def get_profiles():
    c.execute("SELECT * FROM profiles ORDER BY id")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    profiles = []
    for row in rows:
        prof = dict(zip(cols, row))
        profiles.append(prof)
    return profiles

def get_profile(profile_id):
    c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,))
    row = c.fetchone()
    if not row:
        return None
    cols = [d[0] for d in c.description]
    return dict(zip(cols, row))

def create_profile(dest_name, sources="", banner_config=None, banner_proxy=None,
                   interval_min=5, max_post=8, max_proxies=10,
                   post_configs=1, post_proxies=1, ping_mode="iran", last_num=0,
                   show_numbers=1, custom_query="",
                   show_date_config=1, show_date_proxy=1, schedule_cron="", backup_interval=1000,
                   interval_config=5, interval_proxy=5, max_post_config=8, max_post_proxy=10,
                   naming_template="{Flag} | ⚡️Telegram = {CHANNEL_ID}", channel_link="",
                   ping_enabled=1, profile_enabled=1,
                   country_display=2, show_ping=1, proxy_banner_template="", proxy_post_mode=0, ping_testing=1):
    if not banner_config:
        banner_config = "✦ V2Ray Config List\n\n{configs}\n\n◈ #کانفیگ #ویتوری"
    if not banner_proxy:
        banner_proxy = "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━"
    c.execute("""INSERT INTO profiles
        (dest_name, sources, banner_config, banner_proxy, interval_min,
         max_post, max_proxies, post_configs, post_proxies, ping_mode, config_ping_mode, proxy_ping_mode, last_num, created_at,
         show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count,
         timer_expiry, timer_duration, backup_interval,
         interval_config, interval_proxy, max_post_config, max_post_proxy,
         naming_template, channel_link, ping_enabled, profile_enabled,
         country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, ping_mode, ping_mode, last_num,
         get_tehran_time(), show_numbers, custom_query,
         show_date_config, show_date_proxy, schedule_cron, 0, None, 0, backup_interval,
         interval_config, interval_proxy, max_post_config, max_post_proxy,
         naming_template, channel_link, ping_enabled, profile_enabled,
         country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "config_ping_mode", "proxy_ping_mode", "last_num",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy",
               "schedule_cron", "last_backup_count", "timer_expiry", "timer_duration",
               "backup_interval", "interval_config", "interval_proxy", "max_post_config", "max_post_proxy",
               "naming_template", "channel_link", "ping_enabled", "profile_enabled",
               "country_display", "show_ping", "proxy_banner_template", "proxy_post_mode", "config_post_mode", "ping_testing", "config_header_enabled", "config_header_template", "low_cost_mode", "batch_posting"]
    for key, value in kwargs.items():
        if key in allowed:
            c.execute(f"UPDATE profiles SET {key}=? WHERE id=?", (value, profile_id))
    conn.commit()
    log.info(f"Updated profile {profile_id}: {kwargs}")

def delete_profile(profile_id):
    c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM posted_config_keys WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM posted_proxy_keys WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM channel_posts WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM last_scrape WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM source_stream_state WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM processed_messages WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM manual_send_queue WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM pending_batch_items WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM batch_test_cache WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM blacklist WHERE profile_id=?", (profile_id,))
    conn.commit()

def get_profile_interval_config(profile_id):
    prof = get_profile(profile_id)
    return prof.get("interval_config", 5) if prof else 5

def set_profile_interval_config(profile_id, val):
    val = max(0, min(1440, int(val)))
    update_profile(profile_id, interval_config=val)
    return val

def get_profile_interval_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof.get("interval_proxy", 5) if prof else 5

def set_profile_interval_proxy(profile_id, val):
    val = max(0, min(1440, int(val)))
    update_profile(profile_id, interval_proxy=val)
    return val

def get_profile_max_post_config(profile_id):
    prof = get_profile(profile_id)
    return prof.get("max_post_config", 8) if prof else 8

def set_profile_max_post_config(profile_id, val):
    val = max(1, min(50, int(val)))
    update_profile(profile_id, max_post_config=val)
    return val

def get_profile_max_post_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof.get("max_post_proxy", 10) if prof else 10

def set_profile_max_post_proxy(profile_id, val):
    val = max(1, min(50, int(val)))
    update_profile(profile_id, max_post_proxy=val)
    return val

def get_profile_sources(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return []
    result = []
    seen = set()
    for raw in str(prof.get("sources") or "").split(","):
        item = normalize_channel_input(raw)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result

def set_profile_sources(profile_id, sources_list):
    normalized = []
    seen = set()
    for raw in sources_list or []:
        item = normalize_channel_input(raw)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            normalized.append(item)
    update_profile(profile_id, sources=",".join(normalized))
    return normalized

def get_profile_dest(profile_id):
    prof = get_profile(profile_id)
    return prof["dest_name"] if prof else None

def set_profile_dest(profile_id, dest):
    update_profile(profile_id, dest_name=dest)

def get_profile_banner_config(profile_id):
    prof = get_profile(profile_id)
    return prof["banner_config"] if prof else ""

def get_profile_banner_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof["banner_proxy"] if prof else ""

def get_profile_last_num(profile_id):
    prof = get_profile(profile_id)
    return prof["last_num"] if prof else 0

def set_profile_last_num(profile_id, num):
    update_profile(profile_id, last_num=num)

def _normalize_ping_engine_mode(mode):
    try:
        value = int(mode)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(2, value))

def get_ping_engine_mode():
    """0=current Check-Host, 1=Xray real-delay, 2=Xray + Iran Check-Host."""
    try:
        row = c.execute("SELECT v FROM cfg WHERE k='ping_engine_mode'").fetchone()
        return _normalize_ping_engine_mode(row[0] if row else 0)
    except Exception:
        return 0

def set_ping_engine_mode(mode):
    mode = _normalize_ping_engine_mode(mode)
    c.execute("INSERT OR REPLACE INTO cfg (k, v) VALUES ('ping_engine_mode', ?)", (str(mode),))
    conn.commit()
    return mode

def _normalize_ping_mode(mode):
    return "iran" if str(mode).lower().strip() == "iran" else "global"

def get_profile_ping_mode(profile_id):
    """Backward-compatible master ping mode; new UI uses per-stream modes."""
    prof = get_profile(profile_id)
    return _normalize_ping_mode(prof.get("ping_mode", "iran")) if prof else "iran"

def set_profile_ping_mode(profile_id, mode):
    mode = _normalize_ping_mode(mode)
    update_profile(profile_id, ping_mode=mode, config_ping_mode=mode, proxy_ping_mode=mode)

def get_profile_config_ping_mode(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return "iran"
    return _normalize_ping_mode(prof.get("config_ping_mode", "iran"))

def set_profile_config_ping_mode(profile_id, mode):
    update_profile(profile_id, config_ping_mode=_normalize_ping_mode(mode))

def get_profile_proxy_ping_mode(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return "iran"
    return _normalize_ping_mode(prof.get("proxy_ping_mode", "iran"))

def set_profile_proxy_ping_mode(profile_id, mode):
    update_profile(profile_id, proxy_ping_mode=_normalize_ping_mode(mode))

def get_profile_ping_enabled(profile_id):
    # This now represents the master switch for ping testing
    prof = get_profile(profile_id)
    return prof.get("ping_testing", 1) if prof else 1

def set_profile_ping_enabled(profile_id, enabled):
    # This sets ping_testing
    update_profile(profile_id, ping_testing=1 if enabled else 0)

def get_profile_show_ping(profile_id):
    prof = get_profile(profile_id)
    return prof.get("show_ping", 1) if prof else 1

def set_profile_show_ping(profile_id, enabled):
    update_profile(profile_id, show_ping=1 if enabled else 0)

def get_profile_enabled(profile_id):
    prof = get_profile(profile_id)
    return prof.get("profile_enabled", 1) if prof else 1

def set_profile_enabled(profile_id, enabled):
    update_profile(profile_id, profile_enabled=1 if enabled else 0)

def get_profile_post_configs(profile_id):
    prof = get_profile(profile_id)
    return prof["post_configs"] == 1 if prof else True

def set_profile_post_configs(profile_id, enabled):
    update_profile(profile_id, post_configs=1 if enabled else 0)

def get_profile_post_proxies(profile_id):
    prof = get_profile(profile_id)
    return prof["post_proxies"] == 1 if prof else True

def set_profile_post_proxies(profile_id, enabled):
    update_profile(profile_id, post_proxies=1 if enabled else 0)

def get_profile_show_numbers(profile_id):
    prof = get_profile(profile_id)
    return prof["show_numbers"] == 1 if prof else True

def set_profile_show_numbers(profile_id, enabled):
    update_profile(profile_id, show_numbers=1 if enabled else 0)

def get_profile_custom_query(profile_id):
    prof = get_profile(profile_id)
    return prof["custom_query"] if prof else ""

def set_profile_custom_query(profile_id, query):
    query = query.strip()
    c.execute("UPDATE profiles SET custom_query=? WHERE id=?", (query, profile_id))
    conn.commit()

def get_profile_show_date_config(profile_id):
    prof = get_profile(profile_id)
    return prof["show_date_config"] == 1 if prof else True

def set_profile_show_date_config(profile_id, enabled):
    update_profile(profile_id, show_date_config=1 if enabled else 0)

def get_profile_show_date_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof["show_date_proxy"] == 1 if prof else True

def set_profile_show_date_proxy(profile_id, enabled):
    update_profile(profile_id, show_date_proxy=1 if enabled else 0)

def get_profile_schedule_cron(profile_id):
    prof = get_profile(profile_id)
    return prof["schedule_cron"] if prof else ""

def set_profile_schedule_cron(profile_id, cron):
    update_profile(profile_id, schedule_cron=cron)

def get_profile_last_backup_count(profile_id):
    prof = get_profile(profile_id)
    return prof["last_backup_count"] if prof else 0

def set_profile_last_backup_count(profile_id, count):
    update_profile(profile_id, last_backup_count=count)

def get_profile_backup_interval(profile_id):
    prof = get_profile(profile_id)
    return prof.get("backup_interval", 1000) if prof else 1000

def set_profile_backup_interval(profile_id, interval):
    update_profile(profile_id, backup_interval=interval)

def get_profile_config_header_template(profile_id):
    prof = get_profile(profile_id)
    return prof.get("config_header_template", "[Protocol] [Flag] [Country]") if prof else "[Protocol] [Flag] [Country]"

def set_profile_config_header_template(profile_id, template):
    template = (template or "[Protocol] [Flag] [Country]").strip()
    if not template:
        template = "[Protocol] [Flag] [Country]"
    allowed = (
        "Protocol", "Flag", "Country", "COUNTRY_EN", "COUNTRY_FA",
        "CHANNEL_ID", "COUNT", "INDEX", "NUMBER", "HOST", "PORT",
        "DATE", "TIME", "PING", "SOURCE",
    )
    cleaned = template
    # Keep unknown placeholders visible to the admin instead of silently corrupting output.
    unknown = re.findall(r"[\[{]([A-Za-z_]+)[\]}]", cleaned)
    bad = sorted({x for x in unknown if x not in allowed})
    if bad:
        raise ValueError("Placeholder نامعتبر: " + ", ".join(bad))
    update_profile(profile_id, config_header_template=cleaned)

def reset_profile_config_header_template(profile_id):
    set_profile_config_header_template(profile_id, "[Protocol] [Flag] [Country]")

def get_profile_naming_template(profile_id):
    prof = get_profile(profile_id)
    return prof.get("naming_template", "{Flag} | ⚡️Telegram = {CHANNEL_ID}") if prof else "{Flag} | ⚡️Telegram = {CHANNEL_ID}"

def set_profile_naming_template(profile_id, template):
    update_profile(profile_id, naming_template=template)

def get_profile_channel_link(profile_id):
    prof = get_profile(profile_id)
    return prof.get("channel_link", "") if prof else ""

def set_profile_channel_link(profile_id, channel_link):
    channel_link = (channel_link or "").strip()
    if channel_link:
        channel_link = re.sub(r"^https?://t\.me/", "", channel_link, flags=re.IGNORECASE)
        channel_link = re.sub(r"^t\.me/", "", channel_link, flags=re.IGNORECASE)
        channel_link = channel_link.split("?", 1)[0].split("#", 1)[0].strip().lstrip("@/")
    update_profile(profile_id, channel_link=channel_link)
    # Verify persistence immediately; never report success for a stale value.
    saved = get_profile_channel_link(profile_id)
    if saved != channel_link:
        raise RuntimeError(f"channel_link persistence failed for profile {profile_id}")

def set_profile_timer(profile_id, minutes):
    if minutes <= 0:
        clear_profile_timer(profile_id)
        return
    expiry = (datetime.now(TEHRAN_TZ) + timedelta(minutes=minutes)).isoformat()
    update_profile(profile_id, timer_expiry=expiry, timer_duration=minutes)
    log.info(f"Timer set for profile {profile_id}: {minutes} minutes, expires at {expiry}")

def clear_profile_timer(profile_id):
    update_profile(profile_id, timer_expiry=None, timer_duration=0)
    log.info(f"Timer cleared for profile {profile_id}")

def get_profile_timer(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return None, 0
    expiry_str = prof.get("timer_expiry")
    if not expiry_str:
        return None, 0
    try:
        expiry = datetime.fromisoformat(expiry_str)
        now = datetime.now(TEHRAN_TZ)
        if expiry > now:
            remaining = (expiry - now).total_seconds() / 60
            return expiry, int(remaining)
        else:
            clear_profile_timer(profile_id)
            return None, 0
    except Exception:
        clear_profile_timer(profile_id)
        return None, 0

def get_profile_country_display(profile_id):
    prof = get_profile(profile_id)
    return prof.get("country_display", 2) if prof else 2

def set_profile_country_display(profile_id, mode):
    mode = max(0, min(2, int(mode)))
    update_profile(profile_id, country_display=mode)
    return mode

def get_profile_config_header_enabled(profile_id):
    prof = get_profile(profile_id)
    return bool(prof.get("config_header_enabled", 1)) if prof else True

def set_profile_config_header_enabled(profile_id, enabled):
    update_profile(profile_id, config_header_enabled=int(bool(enabled)))

def get_profile_low_cost_mode(profile_id):
    prof = get_profile(profile_id)
    return bool(prof.get("low_cost_mode", 1)) if prof else True

def set_profile_low_cost_mode(profile_id, enabled):
    update_profile(profile_id, low_cost_mode=int(bool(enabled)))

def get_profile_batch_posting(profile_id):
    try:
        prof = get_profile(profile_id)
        return bool(prof.get("batch_posting", 0)) if prof else False
    except sqlite3.OperationalError as exc:
        if "batch_posting" in str(exc).lower():
            ensure_column("profiles", "batch_posting", "INTEGER DEFAULT 0", 0)
            prof = get_profile(profile_id)
            return bool(prof.get("batch_posting", 0)) if prof else False
        raise

def set_profile_batch_posting(profile_id, enabled):
    # Self-heal old databases even if a callback reaches this function after a
    # hot reload or an imported legacy DB.
    try:
        ensure_column("profiles", "batch_posting", "INTEGER DEFAULT 0", 0)
    except Exception:
        pass
    update_profile(profile_id, batch_posting=int(bool(enabled)))
    return bool(enabled)

def _normalize_test_mode(value):
    try:
        return max(0, min(2, int(value)))
    except Exception:
        return 0

def get_profile_config_test_mode(profile_id):
    prof = get_profile(profile_id) or {}
    return _normalize_test_mode(prof.get("config_test_mode", 0))

def set_profile_config_test_mode(profile_id, mode):
    mode = _normalize_test_mode(mode)
    update_profile(profile_id, config_test_mode=mode)
    return mode

def get_profile_proxy_test_mode(profile_id):
    prof = get_profile(profile_id) or {}
    return _normalize_test_mode(prof.get("proxy_test_mode", 0))

def set_profile_proxy_test_mode(profile_id, mode):
    mode = _normalize_test_mode(mode)
    update_profile(profile_id, proxy_test_mode=mode)
    return mode

def _test_mode_label(mode):
    return {0: "🇮🇷 Check-Host", 1: "⚡ Full Config", 2: "⚡ Full Config + 🇮🇷 Host"}.get(_normalize_test_mode(mode), "🇮🇷 Check-Host")

def _pending_batch_rows(profile_id, kind):
    try:
        rows = c.execute(
            "SELECT identity_hash,url,source,ping,ping_count,flag,country_code FROM pending_batch_items "
            "WHERE profile_id=? AND kind=? ORDER BY added_at ASC",
            (int(profile_id), kind)
        ).fetchall()
        return rows
    except sqlite3.Error:
        log.exception("pending batch read failed for profile %s/%s", profile_id, kind)
        return []

def _pending_batch_upsert(profile_id, kind, identity_hash, url, source="", ping=0, ping_count=0, flag="🌐", country_code=""):
    try:
        c.execute(
            "INSERT OR IGNORE INTO pending_batch_items "
            "(profile_id,kind,identity_hash,url,source,ping,ping_count,flag,country_code,added_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(profile_id), kind, identity_hash, url, source or "", float(ping or 0), int(ping_count or 0), flag or "🌐", country_code or "", get_tehran_time())
        )
    except sqlite3.Error:
        conn.rollback()
        log.exception("pending batch write failed for profile %s/%s", profile_id, kind)

def _pending_batch_upsert_many(profile_id, kind, rows):
    if not rows:
        return 0
    now = get_tehran_time()
    payload = [(int(profile_id), kind, h, url, source or "", float(ping or 0), int(ping_count or 0), flag or "🌐", cc or "", now)
               for h, url, source, ping, ping_count, flag, cc in rows]
    try:
        before = conn.total_changes
        c.executemany(
            "INSERT OR IGNORE INTO pending_batch_items "
            "(profile_id,kind,identity_hash,url,source,ping,ping_count,flag,country_code,added_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
        conn.commit()
        return max(0, conn.total_changes - before)
    except sqlite3.Error:
        conn.rollback()
        log.exception("pending batch bulk write failed for profile %s/%s", profile_id, kind)
        return 0

def _pending_batch_remove_posted(profile_id, kind):
    try:
        rows = c.execute(
            "SELECT identity_hash,url FROM pending_batch_items WHERE profile_id=? AND kind=?",
            (int(profile_id), kind)
        ).fetchall()
        removed = 0
        for identity_hash, url in rows:
            posted = is_already_posted(profile_id, url) if kind == "config" else is_proxy_posted(profile_id, url)
            if posted:
                c.execute(
                    "DELETE FROM pending_batch_items WHERE profile_id=? AND kind=? AND identity_hash=?",
                    (int(profile_id), kind, identity_hash)
                )
                removed += c.rowcount
        if removed:
            conn.commit()
        return removed
    except sqlite3.Error:
        conn.rollback()
        log.exception("pending batch cleanup failed for profile %s/%s", profile_id, kind)
        return 0

def _batch_tested_recently(profile_id, kind, identity_hash, hours=2):
    try:
        cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = c.execute(
            "SELECT 1 FROM batch_test_cache WHERE profile_id=? AND kind=? AND identity_hash=? AND tested_at>=? LIMIT 1",
            (int(profile_id), kind, identity_hash, cutoff)
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False

def _batch_mark_tested(profile_id, kind, identity_hash):
    try:
        c.execute(
            "INSERT OR REPLACE INTO batch_test_cache(profile_id,kind,identity_hash,tested_at) VALUES (?,?,?,?)",
            (int(profile_id), kind, identity_hash, get_tehran_time())
        )
    except sqlite3.Error:
        conn.rollback()

def _batch_prune_test_cache(profile_id=None):
    try:
        cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        if profile_id is None:
            c.execute("DELETE FROM batch_test_cache WHERE tested_at < ?", (cutoff,))
        else:
            c.execute("DELETE FROM batch_test_cache WHERE profile_id=? AND tested_at < ?", (int(profile_id), cutoff))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()

def _pending_batch_clear(profile_id, kind=None):
    try:
        if kind:
            c.execute("DELETE FROM pending_batch_items WHERE profile_id=? AND kind=?", (int(profile_id), kind))
        else:
            c.execute("DELETE FROM pending_batch_items WHERE profile_id=?", (int(profile_id),))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()

def render_naming_template(template, *, protocol, flag, country_code, channel_link, count,
                           ping=0, url="", source=""):
    """Render {TOKEN} and [TOKEN] placeholders for config names."""
    template = str(template or "{Flag} | ⚡️Telegram = {CHANNEL_ID}")

    host = ""
    port = ""
    try:
        host, port_value = extract_host(url)
        host = str(host or "")
        port = str(port_value or "")
    except Exception:
        pass

    ping_text = f"{int(round(float(ping)))} ms" if ping and float(ping) > 0 else ""
    values = {
        "Protocol": protocol or "", "PROTOCOL": protocol or "",
        "Flag": flag or "", "FLAG": flag or "",
        "COUNTRY_EN": COUNTRY_NAMES_EN.get(country_code, ""),
        "COUNTRY_FA": COUNTRY_NAMES_FA.get(country_code, ""),
        "Country": COUNTRY_NAMES_EN.get(country_code, ""),
        "COUNTRY": COUNTRY_NAMES_EN.get(country_code, ""),
        "CHANNEL_ID": channel_link or "",
        "COUNT": str(count),
        "INDEX": str(count),
        "NUMBER": str(count),
        "HOST": host,
        "PORT": port,
        "DATE": get_tehran_date(),
        "TIME": get_tehran_time(),
        "PING": ping_text,
        "SOURCE": source or "",
    }
    out = template
    for key,value in values.items():
        out = out.replace("{"+key+"}", str(value)).replace("["+key+"]", str(value))
    return out.strip()

def get_profile_proxy_banner_template(profile_id):
    prof = get_profile(profile_id)
    return prof.get("proxy_banner_template", "") if prof else ""

def set_profile_proxy_banner_template(profile_id, template):
    update_profile(profile_id, proxy_banner_template=template)

def get_blacklist(profile_id):
    rows = c.execute("SELECT word FROM blacklist WHERE profile_id=? ORDER BY id", (profile_id,)).fetchall()
    return [r[0] for r in rows]

def add_blacklist_word(profile_id, word):
    word = word.strip().lower()
    if not word:
        return False
    try:
        c.execute("INSERT INTO blacklist (profile_id, word, created_at) VALUES (?,?,?)",
                  (profile_id, word, get_tehran_time()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def remove_blacklist_word(profile_id, word):
    word = word.strip().lower()
    c.execute("DELETE FROM blacklist WHERE profile_id=? AND word=?", (profile_id, word))
    conn.commit()
    return c.rowcount > 0

def clear_blacklist(profile_id):
    c.execute("DELETE FROM blacklist WHERE profile_id=?", (profile_id,))
    conn.commit()

def is_word_blacklisted(profile_id, text):
    if not text:
        return False
    words = get_blacklist(profile_id)
    if not words:
        return False
    text_lower = text.lower()
    for w in words:
        if w in text_lower:
            return True
    return False

def get_profile_proxy_post_mode(profile_id):
    prof = get_profile(profile_id)
    return int(prof.get("proxy_post_mode", 0)) if prof else 0

def set_profile_proxy_post_mode(profile_id, mode):
    mode = 1 if int(mode) else 0
    update_profile(profile_id, proxy_post_mode=mode)
    return mode

def get_profile_config_post_mode(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return 0
    try:
        return 1 if int(prof.get("config_post_mode", 0) or 0) == 1 else 0
    except (TypeError, ValueError):
        return 0

def set_profile_config_post_mode(profile_id, mode):
    mode = 1 if int(mode) else 0
    update_profile(profile_id, config_post_mode=mode)
    return mode

def get_sponsors(profile_id, apply_type="both", include_disabled=False):
    """Return sponsors for admin or active selection. Disabled sponsors remain editable."""
    now = datetime.now(TEHRAN_TZ)
    query = "SELECT * FROM sponsors WHERE profile_id=?"
    params = [profile_id]
    if not include_disabled:
        query += " AND enabled=1"
    # Legacy DB columns are retained for compatibility, but sponsor application
    # is no longer exposed as an admin setting: every active sponsor is global.
    query += " ORDER BY priority DESC, id ASC"
    c.execute(query, params)
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    sponsors=[]
    for row in rows:
        sponsor=dict(zip(cols,row))
        if not sponsor.get("unlimited",1) and sponsor.get("expires_at"):
            try:
                expires=datetime.fromisoformat(sponsor["expires_at"])
                if expires <= now:
                    continue
            except Exception:
                pass
        sponsors.append(sponsor)
    return sponsors

def get_sponsor(profile_id):
    # Legacy compatibility: return first active sponsor
    sponsors = get_sponsors(profile_id)
    return sponsors[0] if sponsors else None

def add_sponsor(profile_id, name, url, button_text="Advertisement", enabled=1,
                priority=0, duration_hours=0, unlimited=1,
                apply_config=1, apply_proxy=1, color="primary"):
    now = get_tehran_time()
    expires_at = None
    if not unlimited and duration_hours > 0:
        expires_at = (datetime.now(TEHRAN_TZ) + timedelta(hours=duration_hours)).isoformat()
    c.execute("""INSERT INTO sponsors
        (profile_id, name, url, button_text, enabled, priority, duration_hours, unlimited,
         created_at, expires_at, apply_config, apply_proxy, color, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (profile_id, name, url, button_text, enabled, priority, duration_hours, unlimited,
         now, expires_at, apply_config, apply_proxy, color, now))
    conn.commit()
    return c.lastrowid

def update_sponsor(sponsor_id, **kwargs):
    allowed = ["name", "url", "button_text", "enabled", "priority",
               "duration_hours", "unlimited", "apply_config", "apply_proxy", "color", "expires_at"]
    # If unlimited or duration changes, update expires_at
    set_clauses = []
    params = []
    for key, value in kwargs.items():
        if key in allowed:
            set_clauses.append(f"{key}=?")
            params.append(value)
    if not set_clauses:
        return
    # If duration or unlimited changed, recalc expires_at
    if "duration_hours" in kwargs or "unlimited" in kwargs:
        # Need to fetch current values to recompute
        c.execute("SELECT duration_hours, unlimited, created_at FROM sponsors WHERE id=?", (sponsor_id,))
        row = c.fetchone()
        if row:
            cur_duration, cur_unlimited, created_at = row
            new_unlimited = kwargs.get("unlimited", cur_unlimited)
            new_duration = kwargs.get("duration_hours", cur_duration)
            if new_unlimited:
                expires_at = None
            else:
                # Duration is relative to the moment it is changed, not the original creation time.
                expires_at = (datetime.now(TEHRAN_TZ) + timedelta(hours=int(new_duration or 1))).isoformat()
            set_clauses.append("expires_at=?")
            params.append(expires_at)
    params.append(get_tehran_time())
    params.append(sponsor_id)
    c.execute(f"UPDATE sponsors SET {', '.join(set_clauses)}, updated_at=? WHERE id=?", params)
    conn.commit()

def delete_sponsor(sponsor_id):
    c.execute("DELETE FROM sponsors WHERE id=?", (sponsor_id,))
    conn.commit()

def clear_sponsor(profile_id):
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    conn.commit()

def toggle_sponsor(profile_id, sponsor_id=None):
    if sponsor_id:
        c.execute("SELECT enabled FROM sponsors WHERE id=?", (sponsor_id,))
        row = c.fetchone()
        if row:
            new_enabled = 0 if row[0] else 1
            update_sponsor(sponsor_id, enabled=new_enabled)
            return new_enabled
    else:
        # Legacy: toggle first sponsor
        sponsors = get_sponsors(profile_id)
        if sponsors:
            sponsor = sponsors[0]
            new_enabled = 0 if sponsor["enabled"] else 1
            update_sponsor(sponsor["id"], enabled=new_enabled)
            return new_enabled
    return None

def update_sponsor_color(profile_id, color):
    # Legacy: update first sponsor
    sponsors = get_sponsors(profile_id)
    if sponsors:
        update_sponsor(sponsors[0]["id"], color=color)
