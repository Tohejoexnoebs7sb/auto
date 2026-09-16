from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

def safe_debug_exception(context=""):
    """Compact admin-safe debug helper. Keeps traceback available without noisy loops."""
    try:
        log.exception("[DEBUG] %s", context)
    except Exception:
        pass

def header_modes_menu(profile_id):
    """Per-profile independent header display settings for configs and Telegram proxies."""
    cm, pm = get_header_modes(profile_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⚙️ حالت نمایش کانفیگ: {'نام کانال' if cm == 'channel' else 'پروتکل'}",
            callback_data=f"hm_config_menu_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton(
            f"⚙️ حالت نمایش پروکسی: {'نام کانال' if pm == 'channel' else 'پروتکل'}",
            callback_data=f"hm_proxy_menu_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"prof_{profile_id}", style="primary")]
    ])

def header_mode_keyboard(profile_id, kind):
    config_mode, proxy_mode = get_header_modes(profile_id)
    mode = config_mode if kind == "config" else proxy_mode
    title = "کانفیگ" if kind == "config" else "پروکسی"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'◉' if mode == 'channel' else '○'} نام کانال",
            callback_data=f"hm_{kind}_channel_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton(
            f"{'◉' if mode == 'protocol' else '○'} پروتکل",
            callback_data=f"hm_{kind}_protocol_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"prof_{profile_id}", style="primary")]
    ])

def get_header_modes(profile_id):
    # Fast path: reuse the process connection for tiny settings reads.
    # This avoids opening/configuring a new SQLite connection on every button press.
    try:
        row = c.execute(
            "SELECT config_header_mode, proxy_header_mode FROM profiles WHERE id=?",
            (int(profile_id),)
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning("header mode read failed for profile %s: %s", profile_id, exc)
        return "channel", "channel"
    if not row:
        return "channel", "channel"
    return (row[0] or "channel"), (row[1] or "channel")

def set_header_mode(profile_id, kind, mode):
    if kind not in ("config", "proxy") or mode not in ("channel", "protocol"):
        return False
    column = "config_header_mode" if kind == "config" else "proxy_header_mode"
    try:
        c.execute(f"UPDATE profiles SET {column}=? WHERE id=?", (mode, int(profile_id)))
        changed = c.rowcount > 0
        conn.commit()
        return changed
    except sqlite3.Error as exc:
        conn.rollback()
        log.exception("header mode write failed for profile %s", profile_id)
        return False

def header_mode_label(mode):
    return "نام کانال" if mode == "channel" else "پروتکل"

def proxy_button_style(proxy_url):
    """Telegram button color for proxy types."""
    p = detect_proxy_protocol(proxy_url)
    if p == "SOCKS5":
        return "success"
    if p == "MTPROTO":
        return "primary"
    return "danger"

def detect_proxy_protocol(proxy_url):
    """Detect ONLY supported Telegram proxy types: MTPROTO and SOCKS5."""
    u = html.unescape(str(proxy_url or "")).strip().lower()
    if u.startswith(("tg://proxy?", "https://t.me/proxy?")):
        return "MTPROTO"
    if u.startswith(("socks5://", "socks5h://")):
        return "SOCKS5"
    return ""

def detect_config_protocol(config_url):
    """Detect subscription/config protocols. socks:// is a valid config type."""
    u = (config_url or "").strip().lower()
    if u.startswith("socks://"):
        return "SOCKS"
    if u.startswith("vless://"):
        return "VLESS"
    if u.startswith("vmess://"):
        return "VMESS"
    if u.startswith("trojan://"):
        return "TROJAN"
    if u.startswith(("ss://", "ssr://")):
        return "SHADOWSOCKS"
    if u.startswith(("hysteria2://", "hy2://")):
        return "HYSTERIA2"
    if u.startswith("hysteria://"):
        return "HYSTERIA"
    if u.startswith("tuic://"):
        return "TUIC"
    if u.startswith(("juicity://", "juic://")):
        return "JUICITY"
    if u.startswith(("wireguard://", "wg://")):
        return "WIREGUARD"
    return ""

def format_server_header(profile_id, country_text="", item_url="", kind="config"):
    """
    Header mode is selected independently for configs and proxies.
    Channel mode: @ChannelName + country.
    Protocol mode: detected protocol + country.
    """
    config_mode, proxy_mode = get_header_modes(profile_id)
    mode = config_mode if kind == "config" else proxy_mode

    channel = ""
    try:
        profile = get_profile(profile_id)
        channel = (profile.get("channel_link") or "").strip().lstrip("@")
    except Exception:
        channel = ""

    if mode == "protocol":
        title = detect_config_protocol(item_url) if kind == "config" else detect_proxy_protocol(item_url)
    else:
        title = "@" + channel if channel else "@Channel"

    return f"{title}{(' ' + country_text) if country_text else ''}"

def get_tehran_time() -> str:
    return datetime.now(TEHRAN_TZ).strftime('%Y-%m-%d %H:%M:%S')

def get_tehran_date() -> str:
    return datetime.now(TEHRAN_TZ).strftime('%Y-%m-%d')

def normalize_datetime_value(value):
    """Convert mixed naive/aware datetimes safely before comparison."""
    if value is None:
        return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                value = datetime.strptime(value, fmt)
                break
            except Exception:
                pass
        else:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=TEHRAN_TZ)
    return value.astimezone(TEHRAN_TZ)
