# bot.py — 4.1.15
APP_VERSION = "4.1.15"
APP_VERSION_MAJOR = 4
APP_VERSION_MINOR = 1
APP_VERSION_PATCH = 15
APP_VERSION_LABEL = "4.1.15-stable"
BOT_VERSION = APP_VERSION
import os
import glob
import re
import asyncio
import sqlite3
import shutil
import tempfile
import socket
import base64
import hashlib
import json
import logging
import traceback
import sys
import html
import random
import time
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse, parse_qs, urlencode, urlunparse
import httpx
import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import BadRequest, TimedOut, RetryAfter

# ======================================================================
# متغیرهای محیطی
# ======================================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")
MAIN_ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
if not MAIN_ADMIN_ID:
    raise ValueError("ADMIN_ID environment variable not set")


# ======================================================================
# مسیر پایدار
# ======================================================================
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

# ======================================================================
# تنظیم لاگ
# ======================================================================
from logging.handlers import RotatingFileHandler

# All bot-visible/logged timestamps use Tehran time.
logging.Formatter.converter = lambda *args: datetime.now(pytz.timezone("Asia/Tehran")).timetuple()

_LOG_FILE = os.path.join(DATA_DIR, "bot.log")
# Hard reset logging on every process start: remove the previous active log
# and any legacy rotated copies. DB/backups are intentionally untouched.
try:
    for _old_log in glob.glob(_LOG_FILE + ".*"):
        try:
            os.remove(_old_log)
        except OSError:
            pass
    try:
        os.remove(_LOG_FILE)
    except FileNotFoundError:
        pass
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Logs are intentionally reset on every process/redeploy.
        # The database is untouched; only bot.log is truncated.
        logging.FileHandler(
            _LOG_FILE,
            mode='w',
            encoding='utf-8'
        )
    ]
)
log = logging.getLogger("bot")

def safe_debug_exception(context=""):
    """Compact admin-safe debug helper. Keeps traceback available without noisy loops."""
    try:
        log.exception("[DEBUG] %s", context)
    except Exception:
        pass

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ======================================================================
# منطقه زمانی تهران
# ======================================================================
TEHRAN_TZ = pytz.timezone('Asia/Tehran')





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
        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"profile_{profile_id}", style="primary")]
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
        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"profile_{profile_id}", style="primary")]
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


# ======================================================================
# اتصال به دیتابیس
# ======================================================================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA wal_autocheckpoint=200")
conn.execute("PRAGMA journal_size_limit=1048576")
conn.execute("PRAGMA busy_timeout=10000")
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA mmap_size=134217728")
c = conn.cursor()

# مستقل از ترتیب تعریف توابع، تمام عملیات DB از این helper استفاده می‌کنند.
def get_conn():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA wal_autocheckpoint=200")
    db.execute("PRAGMA journal_size_limit=1048576")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA cache_size=-65536")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA mmap_size=134217728")
    return db


# ======================================================================
# جایگزینی کامل دیتابیس از داخل پنل ادمین
# ======================================================================
DB_REPLACE_MAX_BYTES = 45 * 1024 * 1024
DB_REPLACE_LOCK = asyncio.Lock()

def _sqlite_signature(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False

def _validate_sqlite_database(path):
    if not _sqlite_signature(path):
        return False, "فایل SQLite معتبر نیست."
    db = None
    try:
        db = sqlite3.connect(path)
        db.execute("PRAGMA query_only=ON")
        result = db.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            return False, f"integrity_check: {result[0] if result else 'unknown'}"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return False, "دیتابیس جدول قابل استفاده ندارد."
        return True, f"SQLite سالم ({len(tables)} جدول)"
    except Exception as e:
        return False, f"خطا در خواندن SQLite: {e}"
    finally:
        if db:
            db.close()

def _looks_like_sql_dump(path):
    try:
        with open(path, "rb") as f:
            sample = f.read(1024 * 1024)
        text = sample.decode("utf-8-sig", errors="ignore")
        upper = text.upper()
        return ("CREATE TABLE" in upper and ("INSERT INTO" in upper or "CREATE TABLE" in upper))
    except Exception:
        return False

def _prepare_sql_dump(path):
    fd, target = tempfile.mkstemp(prefix="db_import_", suffix=".db", dir=DATA_DIR)
    os.close(fd)
    try:
        with open(path, "r", encoding="utf-8-sig", errors="strict") as f:
            sql = f.read()
        db = sqlite3.connect(target)
        db.executescript(sql)
        db.commit()
        db.close()
        ok, detail = _validate_sqlite_database(target)
        if not ok:
            raise RuntimeError(detail)
        return target, None
    except Exception as e:
        try:
            os.remove(target)
        except OSError:
            pass
        return None, str(e)

def _backup_current_database():
    if not os.path.exists(DB_PATH):
        return None
    stamp = datetime.now(TEHRAN_TZ).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"before_replace_{stamp}.db")
    shutil.copy2(DB_PATH, backup_path)
    return backup_path

def _reopen_database_after_replace():
    global conn, c
    try:
        conn.close()
    except Exception:
        pass
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    c = conn.cursor()

def prepare_replaced_database():
    """Run the same compatibility migrations needed by an imported DB."""
    # These functions use the global conn/c, which are reopened before this call.
    migrate_sponsors()
    migrate_tables_for_profile_isolation()
    migrate_old_config()
    migrate_header_modes()
    fix_column_types()
    ensure_column("profiles", "config_header_enabled", "INTEGER DEFAULT 1", 1)
    ensure_column("profiles", "config_header_template", "TEXT DEFAULT '[Protocol] [Flag] [Country]'", "[Protocol] [Flag] [Country]")
    ensure_column("profiles", "low_cost_mode", "INTEGER DEFAULT 1", 1)
    ensure_column("profiles", "batch_posting", "INTEGER DEFAULT 0", 0)
    try:
        c.execute("UPDATE profiles SET batch_posting=0 WHERE batch_posting IS NULL")
        conn.commit()
    except Exception:
        pass
async def replace_database_from_file(update, source_path, original_name="database"):
    """Safely replace the live DB with a validated SQLite DB or SQL dump.

    The uploaded file's extension is ignored; the actual contents are detected.
    The current DB is backed up first, and the replacement is atomic.
    """
    global conn, c
    if not os.path.isfile(source_path):
        return False, "فایل پیدا نشد."
    size = os.path.getsize(source_path)
    if size <= 0:
        return False, "فایل خالی است."
    if size > DB_REPLACE_MAX_BYTES:
        return False, f"حجم فایل بیش از {DB_REPLACE_MAX_BYTES // (1024 * 1024)}MB است."

    async with DB_REPLACE_LOCK:
        import_path = source_path
        generated_path = None
        try:
            if _sqlite_signature(source_path):
                ok, detail = _validate_sqlite_database(source_path)
                if not ok:
                    return False, detail
            elif _looks_like_sql_dump(source_path):
                generated_path, err = _prepare_sql_dump(source_path)
                if err:
                    return False, f"SQL dump قابل import نیست: {err[:250]}"
                import_path = generated_path
            else:
                return False, "فرمت فایل قابل تشخیص نیست. فقط SQLite (با هر پسوندی) یا SQL Dump پشتیبانی می‌شود."

            # Make sure the imported DB can be opened before touching the live DB.
            test_db = sqlite3.connect(import_path)
            test_db.execute("PRAGMA query_only=ON")
            test_db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            test_db.close()

            current_backup = _backup_current_database()
            if current_backup:
                log.info(f"Database replacement backup created: {current_backup}")

            # Put the incoming database in place. Existing WAL/SHM files can otherwise
            # make SQLite reopen an old journal.
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(DB_PATH + suffix)
                except FileNotFoundError:
                    pass
            os.replace(import_path, DB_PATH)
            generated_path = None
            _reopen_database_after_replace()
            prepare_replaced_database()

            # Final integrity check after migrations.
            ok, detail = _validate_sqlite_database(DB_PATH)
            if not ok:
                raise RuntimeError(f"دیتابیس جایگزین‌شده معتبر نیست: {detail}")

            log.info(f"✅ Database replaced successfully from {original_name}")
            return True, detail
        except Exception as e:
            log.exception("Database replacement failed")
            # If replacement failed after the swap, restore the latest pre-replace backup.
            try:
                backups = sorted(
                    [os.path.join(BACKUP_DIR, x) for x in os.listdir(BACKUP_DIR) if x.startswith("before_replace_") and x.endswith(".db")],
                    key=lambda x: os.path.getmtime(x), reverse=True
                )
                if backups:
                    restore = backups[0]
                    _reopen_database_after_replace()
                    shutil.copy2(restore, DB_PATH)
                    _reopen_database_after_replace()
                    log.warning(f"Restored database from {restore}")
            except Exception:
                log.exception("Automatic database rollback failed")
            return False, f"جایگزینی انجام نشد: {str(e)[:300]}"
        finally:
            if generated_path and os.path.exists(generated_path):
                try:
                    os.remove(generated_path)
                except OSError:
                    pass

def migrate_header_modes():
    """Ensure per-profile header mode settings exist without resetting the database."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(profiles)")
        cols = {row[1] for row in cur.fetchall()}
        if "config_header_mode" not in cols:
            cur.execute("ALTER TABLE profiles ADD COLUMN config_header_mode TEXT DEFAULT 'channel'")
        if "proxy_header_mode" not in cols:
            cur.execute("ALTER TABLE profiles ADD COLUMN proxy_header_mode TEXT DEFAULT 'channel'")
        conn.commit()
        conn.close()
    except Exception:
        log.exception("header mode migration failed")


def ensure_column(table, column, col_type, default=None):
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
        if default is not None:
            c.execute(f"UPDATE {table} SET {column}=?", (default,))
            conn.commit()
    except sqlite3.OperationalError:
        pass

# ======================================================================
# ایجاد جداول (با تغییرات جدید)
# ======================================================================
c.execute("""CREATE TABLE IF NOT EXISTS seen (
    uuid TEXT,
    address TEXT,
    source TEXT DEFAULT '',
    first_seen TEXT,
    last_posted TEXT,
    profile_id INTEGER DEFAULT 1,
    full_url TEXT DEFAULT '',
    backup_num INTEGER DEFAULT 0,
    UNIQUE(uuid, address, profile_id))""")

c.execute("""CREATE TABLE IF NOT EXISTS cfg (
    k TEXT PRIMARY KEY,
    v TEXT)""")

c.execute("""CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    count INTEGER,
    created_at TEXT)""")

ensure_column("seen", "source", "TEXT DEFAULT ''")
ensure_column("seen", "profile_id", "INTEGER DEFAULT 1", 1)
ensure_column("seen", "full_url", "TEXT DEFAULT ''", "")
ensure_column("seen", "backup_num", "INTEGER DEFAULT 0", 0)

c.execute("""CREATE TABLE IF NOT EXISTS country_cache (
    ip TEXT PRIMARY KEY,
    country TEXT,
    flag TEXT)""")

# Table for sponsors - with new schema (no start/end time, uses duration/unlimited)
c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    button_text TEXT DEFAULT 'Advertisement',
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 0,
    duration_hours INTEGER DEFAULT 0,
    unlimited INTEGER DEFAULT 1,
    created_at TEXT,
    expires_at TEXT,
    apply_config INTEGER DEFAULT 1,
    apply_proxy INTEGER DEFAULT 1,
    color TEXT DEFAULT 'primary',
    updated_at TEXT,
    UNIQUE(profile_id, name) ON CONFLICT REPLACE)""")

# Migrate old sponsors if they exist
def migrate_sponsors():
    # Check if old table has columns that need migration
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sponsors'")
    row = c.fetchone()
    if row:
        sql = row[0]
        # If old table has start_time or end_time, we need to migrate
        if "start_time" in sql or "end_time" in sql:
            log.warning("Migrating sponsors table to new schema...")
            # Rename old table
            c.execute("ALTER TABLE sponsors RENAME TO sponsors_old")
            # Create new table
            c.execute("""CREATE TABLE sponsors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                button_text TEXT DEFAULT 'Advertisement',
                enabled INTEGER DEFAULT 1,
                priority INTEGER DEFAULT 0,
                duration_hours INTEGER DEFAULT 0,
                unlimited INTEGER DEFAULT 1,
                created_at TEXT,
                expires_at TEXT,
                apply_config INTEGER DEFAULT 1,
                apply_proxy INTEGER DEFAULT 1,
                color TEXT DEFAULT 'primary',
                updated_at TEXT,
                UNIQUE(profile_id, name) ON CONFLICT REPLACE)""")
            # Copy old data, convert start/end to duration/unlimited if possible
            # For simplicity, set unlimited=1 for all old sponsors (they were always active)
            c.execute("SELECT profile_id, name, url, button_text, enabled, color, created_at FROM sponsors_old")
            for row in c.fetchall():
                profile_id, name, url, button_text, enabled, color, created_at = row
                c.execute("""INSERT INTO sponsors
                    (profile_id, name, url, button_text, enabled, priority, duration_hours, unlimited,
                     created_at, expires_at, apply_config, apply_proxy, color, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, name, url, button_text, enabled, 0, 0, 1, created_at, None, 1, 1, color, get_tehran_time()))
            conn.commit()
            c.execute("DROP TABLE sponsors_old")
            log.info("✅ Sponsors migrated to new schema.")
        else:
            # Ensure new columns exist
            ensure_column("sponsors", "duration_hours", "INTEGER DEFAULT 0", 0)
            ensure_column("sponsors", "unlimited", "INTEGER DEFAULT 1", 1)
            ensure_column("sponsors", "expires_at", "TEXT", None)
            ensure_column("sponsors", "updated_at", "TEXT", get_tehran_time())
    else:
        # Create if missing
        c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            button_text TEXT DEFAULT 'Advertisement',
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0,
            duration_hours INTEGER DEFAULT 0,
            unlimited INTEGER DEFAULT 1,
            created_at TEXT,
            expires_at TEXT,
            apply_config INTEGER DEFAULT 1,
            apply_proxy INTEGER DEFAULT 1,
            color TEXT DEFAULT 'primary',
            updated_at TEXT,
            UNIQUE(profile_id, name) ON CONFLICT REPLACE)""")

migrate_sponsors()

c.execute("""CREATE TABLE IF NOT EXISTS last_scrape (
    source TEXT,
    last_scrape_time TEXT,
    profile_id INTEGER DEFAULT 1,
    last_message_id TEXT DEFAULT '',
    UNIQUE(source, profile_id))""")
ensure_column("last_scrape", "last_message_id", "TEXT DEFAULT ''", "")

c.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
    source TEXT,
    message_id INTEGER,
    profile_id INTEGER DEFAULT 1,
    PRIMARY KEY(source, message_id, profile_id))""")

# Independent scrape cursor for each profile/source/content stream.
# This prevents the config worker from consuming the messages before the
# proxy worker gets a chance to process the same source messages.
c.execute("""CREATE TABLE IF NOT EXISTS source_stream_state (
    profile_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    stream TEXT NOT NULL,
    last_message_id TEXT DEFAULT '',
    updated_at TEXT,
    PRIMARY KEY(profile_id, source, stream))""")
ensure_column("processed_messages", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS proxies_seen (
    proxy_url TEXT,
    first_seen TEXT,
    last_posted TEXT,
    profile_id INTEGER DEFAULT 1,
    UNIQUE(proxy_url, profile_id))""")
ensure_column("proxies_seen", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_header_mode TEXT DEFAULT 'channel',
    proxy_header_mode TEXT DEFAULT 'channel',
    dest_name TEXT UNIQUE NOT NULL,
    sources TEXT DEFAULT '',
    banner_config TEXT,
    banner_proxy TEXT,
    interval_min INTEGER DEFAULT 5,
    max_post INTEGER DEFAULT 8,
    max_proxies INTEGER DEFAULT 10,
    post_configs INTEGER DEFAULT 1,
    post_proxies INTEGER DEFAULT 1,
    ping_mode TEXT DEFAULT 'global',
    config_ping_mode TEXT DEFAULT 'global',
    proxy_ping_mode TEXT DEFAULT 'global',
    last_num INTEGER DEFAULT 0,
    created_at TEXT,
    show_numbers INTEGER DEFAULT 1,
    custom_query TEXT DEFAULT '',
    show_date_config INTEGER DEFAULT 1,
    show_date_proxy INTEGER DEFAULT 1,
    schedule_cron TEXT DEFAULT '',
    last_backup_count INTEGER DEFAULT 0,
    timer_expiry TEXT DEFAULT NULL,
    timer_duration INTEGER DEFAULT 0,
    backup_interval INTEGER DEFAULT 1000,
    interval_config INTEGER DEFAULT 5,
    interval_proxy INTEGER DEFAULT 5,
    max_post_config INTEGER DEFAULT 8,
    max_post_proxy INTEGER DEFAULT 10,
    naming_template TEXT DEFAULT '{Flag} | ⚡️Telegram = {CHANNEL_ID}',
    channel_link TEXT DEFAULT '',
    ping_enabled INTEGER DEFAULT 1,
    profile_enabled INTEGER DEFAULT 1,
    country_display INTEGER DEFAULT 2,
    show_ping INTEGER DEFAULT 1,
    proxy_banner_template TEXT DEFAULT '',
    config_post_mode INTEGER DEFAULT 0,
    ping_testing INTEGER DEFAULT 1,
    batch_posting INTEGER DEFAULT 0
)""")
conn.commit()

# Robust schema self-healing: old Railway SQLite files may predate batch_posting.
# The migration is intentionally executed AFTER the profiles table exists and is
# also verified below, so callbacks can never hit "no such column".
try:
    ensure_column("profiles", "batch_posting", "INTEGER DEFAULT 0", 0)
    c.execute("PRAGMA table_info(profiles)")
    _profile_columns = {row[1] for row in c.fetchall()}
    if "batch_posting" not in _profile_columns:
        raise RuntimeError("profiles.batch_posting migration failed")
    c.execute("UPDATE profiles SET batch_posting=0 WHERE batch_posting IS NULL")
    conn.commit()
except Exception:
    log.exception("[DB SCHEMA] batch_posting migration failed")
    raise

# Persistent strict-batch queue. This is NOT a posted ledger: rows here are
# only healthy candidates waiting for a full batch. They are removed only after
# Telegram posting succeeds (or when their permanent posted hash is detected).
c.execute("""CREATE TABLE IF NOT EXISTS pending_batch_items (
    profile_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT DEFAULT '',
    ping REAL DEFAULT 0,
    ping_count INTEGER DEFAULT 0,
    flag TEXT DEFAULT '🌐',
    country_code TEXT DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, kind, identity_hash))""")
c.execute("CREATE INDEX IF NOT EXISTS idx_pending_batch_profile_kind ON pending_batch_items(profile_id, kind, added_at)")

# Short-lived negative test cache prevents the strict-batch scanner from
# re-testing the same dead candidates forever and allows it to move through a
# large source backlog across cycles. It contains no profile settings.
c.execute("""CREATE TABLE IF NOT EXISTS batch_test_cache (
    profile_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, kind, identity_hash))""")
c.execute("CREATE INDEX IF NOT EXISTS idx_batch_test_cache_time ON batch_test_cache(profile_id, kind, tested_at)")
conn.commit()

# Permanent, compact once-only ledgers. The large URL history tables are only
# runtime/backup history; these SHA-256 keys are the authoritative permanent
# dedup state and are intentionally tiny.
c.execute("""CREATE TABLE IF NOT EXISTS posted_config_keys (
    profile_id INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    first_posted TEXT,
    source TEXT DEFAULT '',
    PRIMARY KEY(profile_id, identity_hash))""")
c.execute("""CREATE TABLE IF NOT EXISTS posted_proxy_keys (
    profile_id INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    first_posted TEXT,
    PRIMARY KEY(profile_id, identity_hash))""")

# Bounded ledger of messages actually sent by this bot to each profile destination.
c.execute("""CREATE TABLE IF NOT EXISTS channel_posts (
    profile_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id))""")
c.execute("CREATE INDEX IF NOT EXISTS idx_channel_posts_profile_latest ON channel_posts(profile_id, message_id DESC)")
c.execute("CREATE INDEX IF NOT EXISTS idx_channel_posts_chat_latest ON channel_posts(chat_id, message_id DESC)")
ensure_column("channel_posts", "deleted_at", "TEXT", None)
try:
    c.execute("CREATE INDEX IF NOT EXISTS idx_channel_posts_active_latest ON channel_posts(profile_id, deleted_at, message_id DESC)")
except sqlite3.Error:
    pass
conn.commit()

# Performance indexes and lightweight maintenance for long-running Railway workers.
for _idx in [
    "CREATE INDEX IF NOT EXISTS idx_seen_full_url ON seen(full_url)",
    "CREATE INDEX IF NOT EXISTS idx_seen_last_posted ON seen(last_posted)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_seen_posted ON proxies_seen(last_posted)",
]:
    try:
        c.execute(_idx)
    except Exception:
        log.exception("index creation failed")
conn.commit()

# High-value indexes: keep duplicate checks and 7-day cleanup O(log n).
for _idx in [
    "CREATE INDEX IF NOT EXISTS idx_seen_profile_uuid_address ON seen(profile_id, uuid, address)",
    "CREATE INDEX IF NOT EXISTS idx_seen_profile_last_posted ON seen(profile_id, last_posted)",
    "CREATE INDEX IF NOT EXISTS idx_seen_profile_full_url ON seen(profile_id, full_url)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_seen_profile_url ON proxies_seen(profile_id, proxy_url)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_seen_profile_last_posted ON proxies_seen(profile_id, last_posted)",
    "CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at)",
]:
    try:
        c.execute(_idx)
    except sqlite3.Error:
        log.exception("performance index creation failed: %s", _idx)
conn.commit()

# Add new columns if missing
ensure_column("profiles", "show_numbers", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "custom_query", "TEXT DEFAULT ''", "")
ensure_column("profiles", "show_date_config", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "show_date_proxy", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "schedule_cron", "TEXT DEFAULT ''", "")
ensure_column("profiles", "last_backup_count", "INTEGER DEFAULT 0", 0)
ensure_column("profiles", "timer_expiry", "TEXT DEFAULT NULL", None)
ensure_column("profiles", "timer_duration", "INTEGER DEFAULT 0", 0)
ensure_column("profiles", "backup_interval", "INTEGER DEFAULT 1000", 1000)
ensure_column("profiles", "interval_config", "INTEGER DEFAULT 5", 5)
ensure_column("profiles", "interval_proxy", "INTEGER DEFAULT 5", 5)
ensure_column("profiles", "max_post_config", "INTEGER DEFAULT 8", 8)
ensure_column("profiles", "max_post_proxy", "INTEGER DEFAULT 10", 10)
ensure_column("profiles", "naming_template", "TEXT DEFAULT '{Flag} | ⚡️Telegram = {CHANNEL_ID}'", "{Flag} | ⚡️Telegram = {CHANNEL_ID}")
ensure_column("profiles", "channel_link", "TEXT DEFAULT ''", "")
ensure_column("profiles", "config_ping_mode", "TEXT DEFAULT 'global'", "global")
ensure_column("profiles", "proxy_ping_mode", "TEXT DEFAULT 'global'", "global")
ensure_column("profiles", "ping_enabled", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "profile_enabled", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "country_display", "INTEGER DEFAULT 2", 2)
ensure_column("profiles", "show_ping", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "proxy_banner_template", "TEXT DEFAULT ''", "")
ensure_column("profiles", "proxy_post_mode", "INTEGER DEFAULT 0", 0)
ensure_column("profiles", "config_post_mode", "INTEGER DEFAULT 0", 0)
try:
    c.execute("UPDATE profiles SET config_post_mode=0 WHERE config_post_mode IS NULL")
    conn.commit()
except Exception:
    pass
ensure_column("profiles", "ping_testing", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "config_header_enabled", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "low_cost_mode", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    word TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(profile_id, word))""")

c.execute("""CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT)""")
ensure_column("admins", "added_by", "INTEGER", 0)
ensure_column("admins", "added_at", "TEXT", "")
conn.commit()

# ======================================================================
# تعمیر نوع ستون custom_query و مهاجرت‌ها (بدون تغییر)
# ======================================================================
def fix_column_types():
    c.execute("PRAGMA table_info(profiles)")
    cols = c.fetchall()
    for col in cols:
        if col[1] == "custom_query" and "TEXT" not in col[2].upper():
            log.warning("custom_query column is not TEXT, fixing...")
            c.execute("""
                CREATE TABLE profiles_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dest_name TEXT UNIQUE NOT NULL,
                    sources TEXT DEFAULT '',
                    banner_config TEXT,
                    banner_proxy TEXT,
                    interval_min INTEGER DEFAULT 5,
                    max_post INTEGER DEFAULT 8,
                    max_proxies INTEGER DEFAULT 10,
                    post_configs INTEGER DEFAULT 1,
                    post_proxies INTEGER DEFAULT 1,
                    ping_mode TEXT DEFAULT 'global',
                    config_ping_mode TEXT DEFAULT 'global',
                    proxy_ping_mode TEXT DEFAULT 'global',
                    last_num INTEGER DEFAULT 0,
                    created_at TEXT,
                    show_numbers INTEGER DEFAULT 1,
                    custom_query TEXT DEFAULT '',
                    show_date_config INTEGER DEFAULT 1,
                    show_date_proxy INTEGER DEFAULT 1,
                    schedule_cron TEXT DEFAULT '',
                    last_backup_count INTEGER DEFAULT 0,
                    timer_expiry TEXT DEFAULT NULL,
                    timer_duration INTEGER DEFAULT 0,
                    backup_interval INTEGER DEFAULT 1000,
                    interval_config INTEGER DEFAULT 5,
                    interval_proxy INTEGER DEFAULT 5,
                    max_post_config INTEGER DEFAULT 8,
                    max_post_proxy INTEGER DEFAULT 10,
                    naming_template TEXT DEFAULT '{Flag} | ⚡️Telegram = {CHANNEL_ID}',
                    channel_link TEXT DEFAULT '',
                    ping_enabled INTEGER DEFAULT 1,
                    profile_enabled INTEGER DEFAULT 1,
                    country_display INTEGER DEFAULT 2,
                    show_ping INTEGER DEFAULT 1,
                    proxy_banner_template TEXT DEFAULT '',
                    ping_testing INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                INSERT INTO profiles_new
                    (id, dest_name, sources, banner_config, banner_proxy, interval_min,
                     max_post, max_proxies, post_configs, post_proxies, ping_mode, config_ping_mode, proxy_ping_mode, last_num,
                     created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                     schedule_cron, last_backup_count, timer_expiry, timer_duration, backup_interval,
                     interval_config, interval_proxy, max_post_config, max_post_proxy,
                     naming_template, channel_link, ping_enabled, profile_enabled,
                     country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
                SELECT id, dest_name, sources, banner_config, banner_proxy, interval_min,
                       max_post, max_proxies, post_configs, post_proxies, ping_mode, config_ping_mode, proxy_ping_mode, last_num,
                       created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                       schedule_cron, last_backup_count, timer_expiry, timer_duration, backup_interval,
                       interval_config, interval_proxy, max_post_config, max_post_proxy,
                       naming_template, channel_link, ping_enabled, profile_enabled,
                       country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing
                FROM profiles
            """)
            c.execute("DROP TABLE profiles")
            c.execute("ALTER TABLE profiles_new RENAME TO profiles")
            conn.commit()
            log.info("✅ custom_query column fixed to TEXT.")
            return
    log.info("✅ custom_query column is TEXT (no fix needed).")

fix_column_types()

def migrate_tables_for_profile_isolation():
    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='seen'")
        row = c.fetchone()
        if row and "UNIQUE(uuid, address, profile_id)" not in row[0]:
            log.warning("Migrating seen table...")
            c.execute("ALTER TABLE seen RENAME TO seen_old")
            c.execute("""
                CREATE TABLE seen (
                    uuid TEXT,
                    address TEXT,
                    source TEXT DEFAULT '',
                    first_seen TEXT,
                    last_posted TEXT,
                    profile_id INTEGER DEFAULT 1,
                    full_url TEXT DEFAULT '',
                    backup_num INTEGER DEFAULT 0,
                    UNIQUE(uuid, address, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO seen (uuid, address, source, first_seen, last_posted, profile_id, full_url, backup_num)
                SELECT uuid, address, source, first_seen, last_posted, profile_id, full_url, backup_num FROM seen_old
            """)
            c.execute("DROP TABLE seen_old")
            conn.commit()
            log.info("✅ seen table migrated.")
    except Exception as e:
        log.error(f"seen migration failed: {e}")

    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='proxies_seen'")
        row = c.fetchone()
        if row and "UNIQUE(proxy_url, profile_id)" not in row[0]:
            log.warning("Migrating proxies_seen table...")
            c.execute("ALTER TABLE proxies_seen RENAME TO proxies_seen_old")
            c.execute("""
                CREATE TABLE proxies_seen (
                    proxy_url TEXT,
                    first_seen TEXT,
                    last_posted TEXT,
                    profile_id INTEGER DEFAULT 1,
                    UNIQUE(proxy_url, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO proxies_seen (proxy_url, first_seen, last_posted, profile_id)
                SELECT proxy_url, first_seen, last_posted, profile_id FROM proxies_seen_old
            """)
            c.execute("DROP TABLE proxies_seen_old")
            conn.commit()
            log.info("✅ proxies_seen table migrated.")
    except Exception as e:
        log.error(f"proxies_seen migration failed: {e}")

    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='processed_messages'")
        row = c.fetchone()
        if row and "PRIMARY KEY(source, message_id, profile_id)" not in row[0]:
            log.warning("Migrating processed_messages table...")
            c.execute("ALTER TABLE processed_messages RENAME TO processed_messages_old")
            c.execute("""
                CREATE TABLE processed_messages (
                    source TEXT,
                    message_id INTEGER,
                    profile_id INTEGER DEFAULT 1,
                    PRIMARY KEY(source, message_id, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO processed_messages (source, message_id, profile_id)
                SELECT source, message_id, profile_id FROM processed_messages_old
            """)
            c.execute("DROP TABLE processed_messages_old")
            conn.commit()
            log.info("✅ processed_messages table migrated.")
    except Exception as e:
        log.error(f"processed_messages migration failed: {e}")

    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='last_scrape'")
        row = c.fetchone()
        if row and "UNIQUE(source, profile_id)" not in row[0]:
            log.warning("Migrating last_scrape table...")
            c.execute("ALTER TABLE last_scrape RENAME TO last_scrape_old")
            c.execute("""
                CREATE TABLE last_scrape (
                    source TEXT,
                    last_scrape_time TEXT,
                    profile_id INTEGER DEFAULT 1,
                    last_message_id TEXT DEFAULT '',
                    UNIQUE(source, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO last_scrape (source, last_scrape_time, profile_id, last_message_id)
                SELECT source, last_scrape_time, profile_id, '' FROM last_scrape_old
            """)
            c.execute("DROP TABLE last_scrape_old")
            conn.commit()
            log.info("✅ last_scrape table migrated.")
    except Exception as e:
        log.error(f"last_scrape migration failed: {e}")

migrate_tables_for_profile_isolation()

def migrate_old_config():
    existing = c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    if existing > 0:
        return

    def old_cfg(k, default=""):
        r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    old_dests = old_cfg("destinations", "@VaslZone")
    old_sources = old_cfg("sources", "@Cfox_Server")
    old_banner_config = old_cfg("banner_config", "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری")
    old_banner_proxy = old_cfg("banner_proxy", "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━")
    old_interval = int(old_cfg("interval_min", "5"))
    old_max_post = int(old_cfg("max_post", "8"))
    old_max_proxies = int(old_cfg("max_proxies", "10"))
    old_post_configs = int(old_cfg("post_configs", "1"))
    old_post_proxies = int(old_cfg("post_proxies", "1"))
    old_ping_mode = old_cfg("ping_mode", "iran")
    old_last_num = int(old_cfg("last_num", "0"))

    dest_list = [x.strip() for x in old_dests.split(",") if x.strip()]
    if not dest_list:
        dest_list = ["@VaslZone"]

    for dest in dest_list:
        c.execute("""INSERT INTO profiles
            (dest_name, sources, banner_config, banner_proxy, interval_min,
             max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at,
             show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count,
             timer_expiry, timer_duration, backup_interval,
             interval_config, interval_proxy, max_post_config, max_post_proxy,
             naming_template, channel_link, ping_enabled, profile_enabled,
             country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             get_tehran_time(), 1, "", 1, 1, "", 0, None, 0, 1000,
             old_interval, old_interval, old_max_post, old_max_proxies,
             "{Flag} | ⚡️Telegram = {CHANNEL_ID}", "", 1, 1, 2, 1, "", 0, 1))
    conn.commit()
    log.info(f"✅ Migrated {len(dest_list)} profiles.")

migrate_old_config()
# Header display modes are per-profile and migrated safely after profiles exists.
migrate_header_modes()

# ======================================================================
# صف ارسال دستی زمان‌بندی‌شده (Persistent Manual Queue)
# ======================================================================
# این صف عمداً در SQLite ذخیره می‌شود تا با restart/redeploy زمان‌بندی‌ها از بین نروند.
c.execute("""CREATE TABLE IF NOT EXISTS manual_send_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    items_json TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    batch_size INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT DEFAULT '',
    sent_count INTEGER NOT NULL DEFAULT 0
)""")
ensure_column("manual_send_queue", "last_error", "TEXT DEFAULT ''", "")
ensure_column("manual_send_queue", "sent_count", "INTEGER DEFAULT 0", 0)
ensure_column("manual_send_queue", "queue_name", "TEXT DEFAULT ''", "")
c.execute("UPDATE manual_send_queue SET queue_name = 'صف #' || id WHERE queue_name IS NULL OR TRIM(queue_name) = ''")
conn.commit()
ensure_column("manual_send_queue", "fail_count", "INTEGER DEFAULT 0", 0)
c.execute("CREATE INDEX IF NOT EXISTS idx_manual_queue_due ON manual_send_queue(status, next_run_at)")
c.execute("CREATE INDEX IF NOT EXISTS idx_manual_queue_profile ON manual_send_queue(profile_id, status)")
conn.commit()


# ======================================================================
# Protocol control isolation v1.0
# ======================================================================
PROXY_PROTOCOLS = ("MTPROTO", "SOCKS5")
CONFIG_PROTOCOLS = ("VLESS", "VMESS", "TROJAN", "SHADOWSOCKS", "SOCKS", "HYSTERIA", "HYSTERIA2", "HY2", "WIREGUARD", "WG")

def migrate_protocol_settings():
    db=get_conn(); cur=db.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS profile_protocol_settings(
        profile_id INTEGER NOT NULL,
        protocol_name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT,
        updated_at TEXT,
        PRIMARY KEY(profile_id, protocol_name))""")
    for prof in cur.execute("SELECT id FROM profiles").fetchall():
        for proto in PROXY_PROTOCOLS+CONFIG_PROTOCOLS:
            cur.execute("INSERT OR IGNORE INTO profile_protocol_settings (profile_id, protocol_name, enabled, created_at, updated_at) VALUES(?,?,?,?,?)",(prof[0],proto,1,get_tehran_time(),get_tehran_time()))
    db.commit(); db.close()

def is_protocol_enabled(profile_id, protocol):
    row=get_conn().execute("SELECT enabled FROM profile_protocol_settings WHERE profile_id=? AND protocol_name=?",(profile_id,protocol)).fetchone()
    return True if not row else bool(row[0])

def set_protocol_enabled(profile_id, protocol, enabled):
    db=get_conn(); db.execute("INSERT INTO profile_protocol_settings (profile_id, protocol_name, enabled, created_at, updated_at) VALUES(?,?,?,?,?) ON CONFLICT(profile_id,protocol_name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",(profile_id,protocol,int(enabled),get_tehran_time(),get_tehran_time())); db.commit(); db.close()

def _queue_now():
    return datetime.now(TEHRAN_TZ)

def _queue_iso(dt):
    return dt.astimezone(TEHRAN_TZ).isoformat()

def _queue_parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = TEHRAN_TZ.localize(dt)
        return dt.astimezone(TEHRAN_TZ)
    except Exception:
        return _queue_now()


def extract_supported_links_from_message(message):
    """Extract configs/proxies from text, entities and inline buttons."""
    found=[]
    parts=[]

    for attr in ("text", "caption"):
        value=getattr(message, attr, None)
        if value:
            parts.append(value)

    for ent in list(getattr(message, "entities", None) or []) + list(getattr(message, "caption_entities", None) or []):
        try:
            if getattr(ent, "url", None):
                parts.append(ent.url)
        except Exception:
            pass

    def collect_buttons(markup):
        try:
            for row in getattr(markup, "inline_keyboard", []) or []:
                for btn in row:
                    if getattr(btn, "url", None):
                        parts.append(btn.url)
        except Exception:
            pass

    collect_buttons(getattr(message, "reply_markup", None))

    # Some Telegram clients expose forwarded message markup separately
    for attr in ("forward_origin", "forward_from_message", "forwarded_message"):
        obj=getattr(message, attr, None)
        if obj:
            collect_buttons(getattr(obj, "reply_markup", None))
            for ent in list(getattr(obj, "entities", None) or []) + list(getattr(obj, "caption_entities", None) or []):
                if getattr(ent, "url", None):
                    parts.append(ent.url)
            if getattr(obj, "text", None): parts.append(obj.text)
            if getattr(obj, "caption", None): parts.append(obj.caption)

    blob="\n".join(str(x) for x in parts)
    pattern=r'''(?:vmess|vless|trojan|ss|shadowsocks|socks|hy2|hysteria2?|wireguard|wg)://[^\s<>"']+|(?:tg://proxy\?[^\s<>"']+|https://t\.me/proxy\?[^\s<>"']+)'''
    for item in re.findall(pattern, blob, re.I):
        item=item.strip(".,);]}")
        if detect_config_protocol(item) or detect_proxy_protocol(item):
            found.append(item)
    return list(dict.fromkeys(found))

def filter_enabled_protocols(profile_id, items):
    """Remove disabled protocols before manual/automatic processing."""
    result=[]
    for item in items or []:
        proto = detect_config_protocol(item) or detect_proxy_protocol(item)
        if proto and is_protocol_enabled(profile_id, proto):
            result.append(item)
    return list(dict.fromkeys(result))


def paginate_items(items, page=1, per_page=20):
    try:
        page=max(1,int(page))
    except Exception:
        page=1
    try:
        per_page=max(1,int(per_page))
    except Exception:
        per_page=20
    start=(page-1)*per_page
    return list(items or [])[start:start+per_page]


def create_manual_queue_job(profile_id, kind, items, interval_minutes=0, batch_size=1, first_delay_minutes=0):
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    items = filter_enabled_protocols(profile_id, items)
    if not items:
        return None
    kind = str(kind).lower().strip()
    detected=[]
    allowed_items=[]
    for item in items:
        proto = detect_config_protocol(item) or detect_proxy_protocol(item)
        if not proto:
            continue
        if not is_protocol_enabled(profile_id, proto):
            continue
        allowed_items.append(item)
        if detect_config_protocol(item): detected.append("config")
        elif detect_proxy_protocol(item): detected.append("proxy")
    items = allowed_items
    if not items:
        raise ValueError("all protocols disabled for this profile")
    if detected and all(x=="proxy" for x in detected):
        kind="proxy"
    elif detected and all(x=="config" for x in detected):
        kind="config"
    else:
        raise ValueError("mixed proxy/config input is not allowed")
    if kind not in ("config", "proxy"):
        raise ValueError("invalid queue kind")
    # Never store duplicate/already-posted configs in a queue. Permanent posted
    # hashes are kept separately, so this does not delete any history.
    if kind == "config":
        filtered=[]; local=set()
        for item in items:
            key=_config_identity_hash(item)
            if key in local or is_already_posted(profile_id, item):
                continue
            local.add(key); filtered.append(item)
        items=filtered
    else:
        filtered=[]; local=set()
        for item in items:
            norm=normalize_proxy_url(item); key=_proxy_identity_hash(norm)
            if key in local or is_proxy_posted(profile_id, norm):
                continue
            local.add(key); filtered.append(norm)
        items=filtered
    if not items:
        return None
    interval_minutes = max(0, int(interval_minutes or 0))
    batch_size = max(1, int(batch_size or 1))
    # Telegram text has a practical ceiling; keep one queue batch bounded.
    batch_size = min(batch_size, 50)
    now = _queue_now()
    first_run = now + timedelta(minutes=max(0, int(first_delay_minutes or 0)))
    stamp = _queue_iso(now)
    c.execute("""INSERT INTO manual_send_queue
        (profile_id, kind, items_json, interval_minutes, batch_size, next_run_at, status, created_at, updated_at, queue_name)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (profile_id, kind, json.dumps(items, ensure_ascii=False), interval_minutes,
         batch_size, _queue_iso(first_run), "pending", stamp, stamp, ""))
    job_id = c.lastrowid
    c.execute("UPDATE manual_send_queue SET queue_name=? WHERE id=? AND profile_id=?",
              (f"صف #{job_id}", job_id, profile_id))
    conn.commit()
    return job_id

def get_manual_queue(profile_id, include_done=False):
    # The queue UI must retain cancelled jobs so an admin can resume them.
    # By default we show pending + cancelled; done/failed remain hidden unless
    # include_done=True. The worker itself still processes ONLY pending rows.
    if include_done:
        rows = c.execute(
            "SELECT * FROM manual_send_queue WHERE profile_id=? ORDER BY next_run_at, id", (profile_id,)
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM manual_send_queue WHERE profile_id=? AND status IN ('pending','cancelled') ORDER BY next_run_at, id",
            (profile_id,)
        ).fetchall()
    cols = [d[0] for d in c.description]
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        try:
            d["items"] = json.loads(d.get("items_json") or "[]")
        except Exception:
            d["items"] = []
        out.append(d)
    return out

def get_manual_queue_job(job_id, profile_id=None):
    if profile_id is None:
        row = c.execute("SELECT * FROM manual_send_queue WHERE id=?", (job_id,)).fetchone()
    else:
        row = c.execute("SELECT * FROM manual_send_queue WHERE id=? AND profile_id=?", (job_id, profile_id)).fetchone()
    if not row:
        return None
    cols = [d[0] for d in c.description]
    d = dict(zip(cols, row))
    try:
        d["items"] = json.loads(d.get("items_json") or "[]")
    except Exception:
        d["items"] = []
    return d

def update_manual_queue_job(job_id, profile_id, **changes):
    job = get_manual_queue_job(job_id, profile_id)
    if not job:
        return False
    allowed = {"interval_minutes", "batch_size", "next_run_at", "status", "last_error", "items_json", "sent_count", "fail_count", "queue_name"}
    clauses, params = [], []
    for k, v in changes.items():
        if k in allowed:
            clauses.append(f"{k}=?")
            params.append(v)
    if not clauses:
        return False
    clauses.append("updated_at=?")
    params.append(_queue_iso(_queue_now()))
    params.extend([job_id, profile_id])
    c.execute(f"UPDATE manual_send_queue SET {', '.join(clauses)} WHERE id=? AND profile_id=?", params)
    conn.commit()
    return c.rowcount > 0

def cancel_manual_queue_job(job_id, profile_id):
    job = get_manual_queue_job(job_id, profile_id)
    if not job or job.get("status") not in ("pending", "running"):
        return False
    return update_manual_queue_job(job_id, profile_id, status="cancelled", last_error="")

def resume_manual_queue_job(job_id, profile_id):
    job = get_manual_queue_job(job_id, profile_id)
    if not job or job.get("status") != "cancelled" or not (job.get("items") or []):
        return False
    return update_manual_queue_job(
        job_id, profile_id, status="pending", next_run_at=_queue_iso(_queue_now()),
        fail_count=0, last_error=""
    )

def delete_manual_queue_job(job_id, profile_id):
    c.execute("DELETE FROM manual_send_queue WHERE id=? AND profile_id=?", (job_id, profile_id))
    conn.commit()
    return c.rowcount > 0

def remove_manual_queue_item(job_id, profile_id, index):
    job = get_manual_queue_job(job_id, profile_id)
    if not job or job["status"] != "pending":
        return False
    items = list(job.get("items") or [])
    if not (0 <= index < len(items)):
        return False
    items.pop(index)
    if not items:
        # Removing the final item must NOT destroy the queue. Only the explicit
        # "delete completely" action is allowed to remove the queue row.
        return update_manual_queue_job(
            job_id, profile_id,
            items_json="[]",
            status="cancelled",
            last_error="صف خالی شد؛ برای حذف کامل از گزینه حذف کامل استفاده کنید."
        )
    return update_manual_queue_job(job_id, profile_id, items_json=json.dumps(items, ensure_ascii=False))


def _manual_queue_identity(item, kind):
    try:
        return _config_identity_hash(clean_config_url(item)) if kind == "config" else _proxy_identity_hash(normalize_proxy_url(item))
    except Exception:
        return hashlib.sha256(str(item).strip().encode("utf-8", errors="ignore")).hexdigest()

def _manual_queue_unposted_items(profile_id, kind, items):
    """Return the exact unique queue items that are still unposted."""
    out=[]; seen_local=set()
    for raw in items or []:
        item=str(raw).strip()
        if not item:
            continue
        key=_manual_queue_identity(item, kind)
        if key in seen_local:
            continue
        seen_local.add(key)
        if kind == "config":
            if is_already_posted(profile_id, item):
                continue
        else:
            if is_proxy_posted(profile_id, item):
                continue
        out.append(item)
    return out

def _manual_queue_active_count(profile_id, job):
    """Count exactly what the TXT export will contain."""
    if not job:
        return 0
    return len(_manual_queue_unposted_items(profile_id, str(job.get("kind") or ""), job.get("items") or []))

def add_manual_queue_items(job_id, profile_id, items):
    """Append configs/proxies to an existing profile-owned queue job."""
    job = get_manual_queue_job(job_id, profile_id)
    if not job or job.get("status") != "pending":
        return False
    new_items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not new_items:
        return False
    current = list(job.get("items") or [])
    kind = str(job.get("kind") or "")
    for item in new_items:
        detected = "proxy" if detect_proxy_protocol(item) else "config" if detect_config_protocol(item) else ""
        if detected and kind and detected != kind:
            return False
    merged = _manual_queue_unposted_items(profile_id, kind, current + new_items)
    return update_manual_queue_job(
        job_id, profile_id,
        items_json=json.dumps(merged, ensure_ascii=False),
        status="pending",
        last_error=""
    )

def _manual_queue_take_batch(job):
    items = list(job.get("items") or [])
    if not items:
        return []
    return items[:max(1, min(50, int(job.get("batch_size") or 1)))]

def _manual_queue_commit_batch(job_id, profile_id, sent_items, error="", consumed_items=None):
    job = get_manual_queue_job(job_id, profile_id)
    if not job:
        return
    # If an admin cancelled while Telegram was processing the request, preserve
    # the queue contents. Cancellation is a pause, never an implicit deletion.
    if job.get("status") == "cancelled":
        log.info(f"[MANUAL_QUEUE] job={job_id} remains cancelled; preserving queue after send attempt")
        return
    remaining = list(job.get("items") or [])
    consumed_set = set(consumed_items if consumed_items is not None else (sent_items or []))
    # Preserve order while removing only items that were successfully published or
    # were proven duplicate at send time. Duplicates are consumed but never sent.
    remaining = [x for x in remaining if x not in consumed_set]
    sent_count = int(job.get("sent_count") or 0) + len(sent_items or [])
    if error and not sent_items:
        # Permanent failures must never create an infinite hot loop.
        failures = int(job.get("fail_count") or 0) + 1
        if failures >= 3:
            update_manual_queue_job(
                job_id, profile_id,
                status="failed",
                fail_count=failures,
                last_error=str(error)[:500]
            )
        else:
            update_manual_queue_job(
                job_id, profile_id,
                status="pending",
                fail_count=failures,
                last_error=str(error)[:500],
                next_run_at=_queue_iso(_queue_now() + timedelta(seconds=60 * failures))
            )
        return
    if not remaining:
        update_manual_queue_job(job_id, profile_id, items_json="[]", status="done",
                                sent_count=sent_count, last_error="")
    else:
        interval_minutes = max(0, int(job.get("interval_minutes") or 0))
        # Keep the configured cadence anchored to the actual successful send time.
        # For interval=0 the queue completes in one send; never spin forever.
        next_run = _queue_now() + timedelta(minutes=interval_minutes)
        update_manual_queue_job(
            job_id, profile_id,
            items_json=json.dumps(remaining, ensure_ascii=False),
            next_run_at=_queue_iso(next_run),
            status="pending",
            sent_count=sent_count,
            last_error=str(error or "")[:500]
        )

async def _send_manual_config_items(bot, profile_id, items):
    """Send explicit manual configs without touching automatic dedup/cursors."""
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        return 0
    # Legacy compatibility helper: permanent once-only dedup is still enforced
    # by post_configs; automatic cursor state remains untouched.
    unique = []
    keys = set()
    for url in items:
        key = _config_identity_hash(url)
        if key in keys:
            continue
        keys.add(key)
        unique.append(url)
    working = [(url, 0, 0) for url in unique]
    return await post_configs(
        bot, profile_id, working, source_for_seen="manual",
        is_instant=True, max_post_override=len(working),
        dedup=False, update_auto_state=False
    )

async def _send_manual_proxy_items(bot, profile_id, items):
    """Send explicit manual MTProto proxies without touching AUTO dedup state."""
    unique=[]; keys=set()
    for raw in items or []:
        norm = canonical_telegram_proxy_url(raw)
        if not norm:
            continue
        key = _proxy_identity_hash(norm)
        if key in keys:
            continue
        keys.add(key); unique.append(norm)
    if not unique:
        return 0
    flag_rows=[]
    for norm in unique[:50]:
        host,_ = extract_host(norm)
        flag,country_code = "🌐",""
        if host:
            try:
                ip=await host_to_ip(host)
                if ip:
                    flag,country_code=await get_flag_for_ip(ip)
            except Exception:
                pass
        flag_rows.append((norm,0,flag,country_code))
    cnt,payload,selected = await post_proxies(
        bot, profile_id, flag_rows, is_instant=True,
        max_proxies_override=len(flag_rows), dedup=True
    )
    if cnt <= 0 or not payload:
        return 0
    text_p,buttons=payload
    ok=await send_to_destination(bot, profile_id, text_p, buttons)
    if ok:
        # Deliberately do not call mark_proxies_posted_batch here. Manual traffic
        # must not change the automatic profile's dedup ledger.
        return len(selected)
    return 0

async def _send_manual_queue_batch(bot, job):
    profile_id = int(job["profile_id"])
    kind = job["kind"]
    batch = _manual_queue_take_batch(job)
    # Hard protocol gate: queued items are rechecked at send time too.
    batch = filter_enabled_protocols(profile_id, batch)
    if not batch:
        delete_manual_queue_job(job["id"], profile_id)
        return 0

    if kind == "config":
        # Manual queues also obey the permanent once-only dedup ledger. This does NOT
        # advance automatic cursors/state; it only prevents a duplicate from ever being sent.
        sendable=[]; duplicate_items=[]; local=set()
        for url in batch:
            key=_manual_queue_identity(url, "config")
            if key in local or is_already_posted(profile_id, url):
                duplicate_items.append(url)
                continue
            local.add(key); sendable.append(url)
        if not sendable:
            _manual_queue_commit_batch(job["id"], profile_id, [], "", consumed_items=duplicate_items)
            return 0
        working = [(url, 0, 0) for url in sendable]
        sent = await post_configs(
            bot, profile_id, working, source_for_seen="manual",
            is_instant=True, max_post_override=len(sendable),
            dedup=True, update_auto_state=False
        )
        if sent <= 0:
            _manual_queue_commit_batch(job["id"], profile_id, [], "Telegram config send failed", consumed_items=duplicate_items)
            return 0
        successful = sendable[:sent]
        _manual_queue_commit_batch(job["id"], profile_id, successful, "", consumed_items=duplicate_items + successful)
        return sent

    sent = await _send_manual_proxy_items(bot, profile_id, batch)
    if sent <= 0:
        _manual_queue_commit_batch(job["id"], profile_id, [], "Telegram proxy send failed")
        return 0
    _manual_queue_commit_batch(job["id"], profile_id, batch[:sent], "")
    return sent


CONFIG_RETENTION_HOURS = 24 * 7
RUNTIME_RETENTION_HOURS = 24

def cleanup_expired_runtime_data():
    """Remove disposable runtime history after 7 days; profiles/settings and permanent dedup ledgers stay forever."""
    config_cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=CONFIG_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    runtime_cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=RUNTIME_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    deleted = {}
    for table, where in (("posts", "created_at < ?"), ("processed_messages", "updated_at < ?"), ("manual_send_queue", "status IN (\'done\',\'cancelled\',\'failed\') AND updated_at < ?")):
        try:
            cur = c.execute(f"DELETE FROM {table} WHERE {where}", (runtime_cutoff,))
            deleted[table] = cur.rowcount
        except sqlite3.OperationalError:
            deleted[table] = 0
    try:
        # country_cache contains country/flag metadata and is intentionally permanent.
        deleted["country_cache"] = 0
    except sqlite3.OperationalError:
        deleted["country_cache"] = 0
    conn.commit()
    try: c.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.DatabaseError: pass
    try: c.execute("PRAGMA optimize")
    except sqlite3.DatabaseError: pass
    return deleted

def purge_duplicate_ledgers():
    """Repair legacy duplicate rows; never delete profiles or configuration."""
    try:
        c.execute("DELETE FROM seen WHERE rowid NOT IN (SELECT MAX(rowid) FROM seen GROUP BY profile_id, COALESCE(full_url,\'\'), uuid, address)")
        c.execute("DELETE FROM proxies_seen WHERE rowid NOT IN (SELECT MAX(rowid) FROM proxies_seen GROUP BY profile_id, proxy_url)")
        conn.commit()
    except sqlite3.DatabaseError:
        log.exception("dedup ledger repair failed")

def sqlite_maintenance_cycle():
    return cleanup_expired_runtime_data()

async def _run_manual_queue_batch_isolated(bot, job):
    """Send one manual batch in its own per-profile lane.

    IMPORTANT: this lock is intentionally separate from _PROFILE_CYCLE_LOCKS.
    Manual queue traffic never blocks, waits for, or mutates the automatic cycle.
    """
    profile_id = int(job["profile_id"])
    lock = _MANUAL_QUEUE_LOCKS.get(profile_id)
    if lock is None:
        lock = asyncio.Lock()
        _MANUAL_QUEUE_LOCKS[profile_id] = lock
    async with lock:
        fresh = get_manual_queue_job(int(job["id"]), profile_id)
        if not fresh or fresh.get("status") != "running":
            return 0
        return await _send_manual_queue_batch(bot, fresh)

async def manual_queue_worker(bot):
    """Persistent manual scheduler with second-level deadline accuracy.

    Queue timestamps are stored as timezone-aware Tehran ISO timestamps. Sleeping
    uses the event loop's monotonic clock so NTP/system-clock changes cannot make
    a due job wait an extra polling interval.
    """
    log.info("⏱️ Persistent manual-send scheduler started | timezone=Asia/Tehran")
    loop = asyncio.get_running_loop()
    while True:
        try:
            _WORKER_HEARTBEATS["manual_queue"] = time.time()
            now = _queue_now()
            # Recover jobs left running by a crashed/restarted process.
            c.execute(
                "UPDATE manual_send_queue SET status='pending', next_run_at=?, updated_at=? "
                "WHERE status='running' AND updated_at<?",
                (_queue_iso(now), _queue_iso(now), _queue_iso(now - timedelta(minutes=5)))
            )
            conn.commit()

            rows = c.execute(
                "SELECT id, next_run_at FROM manual_send_queue "
                "WHERE status='pending' ORDER BY next_run_at, id LIMIT 50"
            ).fetchall()
            if not rows:
                await asyncio.sleep(0.5)
                continue

            due_ids = []
            nearest = None
            for job_id, next_run_at in rows:
                due = _queue_parse_time(next_run_at)
                if due <= now:
                    due_ids.append(job_id)
                elif nearest is None or due < nearest:
                    nearest = due

            if not due_ids:
                # Convert the Tehran wall-clock deadline to a monotonic sleep
                # duration once, with a tiny guard against float rounding.
                wait = max(0.01, (nearest - now).total_seconds()) if nearest else 0.5
                await asyncio.sleep(wait)
                continue

            for job_id in due_ids:
                job = get_manual_queue_job(job_id)
                if not job or job.get("status") != "pending":
                    continue
                claimed = update_manual_queue_job(job_id, int(job["profile_id"]), status="running")
                if not claimed:
                    continue
                try:
                    sent = await _run_manual_queue_batch_isolated(bot, job)
                    _WORKER_HEARTBEATS["manual_queue"] = time.time()
                    log.info(
                        f"[MANUAL_QUEUE] job={job_id} profile={job['profile_id']} "
                        f"kind={job['kind']} sent={sent} at={get_tehran_time()}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception(f"[MANUAL_QUEUE] job={job_id} failed")
                    update_manual_queue_job(
                        job_id, int(job["profile_id"]), status="pending",
                        last_error=str(exc)[:500],
                        next_run_at=_queue_iso(_queue_now() + timedelta(seconds=10))
                    )

        except asyncio.CancelledError:
            log.info("🛑 Persistent manual-send scheduler cancelled")
            return
        except Exception:
            log.exception("manual_queue_worker error")
            await asyncio.sleep(0.5)

async def _force_manual_queue_send(bot, profile_id, job_id):
    try:
        job=get_manual_queue_job(job_id, profile_id)
        if not job or job.get("status") != "running":
            return
        sent=await _run_manual_queue_batch_isolated(bot, job)
        log.info(f"[MANUAL_FORCE] job={job_id} profile={profile_id} sent={sent}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception(f"[MANUAL_FORCE] job={job_id} profile={profile_id} failed")
        update_manual_queue_job(job_id, profile_id, status="pending", last_error=str(exc)[:500], next_run_at=_queue_iso(_queue_now()+timedelta(seconds=10)))

# Normalize missing Ping mode to the new default (Global) without overwriting explicit user choices.
c.execute("UPDATE profiles SET ping_mode=? WHERE ping_mode IS NULL OR TRIM(ping_mode)=?", ("global", ""))
c.execute("UPDATE profiles SET config_ping_mode=CASE WHEN LOWER(TRIM(COALESCE(ping_mode, 'global')))='iran' THEN 'iran' ELSE 'global' END WHERE config_ping_mode IS NULL OR TRIM(config_ping_mode)=?", ("",))
c.execute("UPDATE profiles SET proxy_ping_mode=CASE WHEN LOWER(TRIM(COALESCE(ping_mode, 'global')))='iran' THEN 'iran' ELSE 'global' END WHERE proxy_ping_mode IS NULL OR TRIM(proxy_ping_mode)=?", ("",))
conn.commit()

# ======================================================================
# توابع کمکی
# ======================================================================
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

# ======================================================================
# توابع پروفایل (با اضافه شدن تنظیمات جدید)
# ======================================================================
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
                   post_configs=1, post_proxies=1, ping_mode="global", last_num=0,
                   show_numbers=1, custom_query="",
                   show_date_config=1, show_date_proxy=1, schedule_cron="", backup_interval=1000,
                   interval_config=5, interval_proxy=5, max_post_config=8, max_post_proxy=10,
                   naming_template="{Flag} | ⚡️Telegram = {CHANNEL_ID}", channel_link="",
                   ping_enabled=1, profile_enabled=1,
                   country_display=2, show_ping=1, proxy_banner_template="", ping_testing=1):
    if not banner_config:
        banner_config = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    if not banner_proxy:
        banner_proxy = "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━"
    c.execute("""INSERT INTO profiles
        (dest_name, sources, banner_config, banner_proxy, interval_min,
         max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at,
         show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count,
         timer_expiry, timer_duration, backup_interval,
         interval_config, interval_proxy, max_post_config, max_post_proxy,
         naming_template, channel_link, ping_enabled, profile_enabled,
         country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, last_num,
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
    update_profile(profile_id, interval_config=val)

def get_profile_interval_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof.get("interval_proxy", 5) if prof else 5

def set_profile_interval_proxy(profile_id, val):
    update_profile(profile_id, interval_proxy=val)

def get_profile_max_post_config(profile_id):
    prof = get_profile(profile_id)
    return prof.get("max_post_config", 8) if prof else 8

def set_profile_max_post_config(profile_id, val):
    update_profile(profile_id, max_post_config=val)

def get_profile_max_post_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof.get("max_post_proxy", 10) if prof else 10

def set_profile_max_post_proxy(profile_id, val):
    update_profile(profile_id, max_post_proxy=val)

def get_profile_sources(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return []
    s = prof["sources"]
    items = [x.strip() for x in s.split(",") if x.strip()]
    items = [normalize_channel_input(x) for x in items if normalize_channel_input(x)]
    return items

def set_profile_sources(profile_id, sources_list):
    normalized = [normalize_channel_input(s) for s in sources_list if normalize_channel_input(s)]
    s = ",".join(normalized)
    update_profile(profile_id, sources=s)

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

def _normalize_ping_mode(mode):
    return "iran" if str(mode).lower().strip() == "iran" else "global"

def get_profile_ping_mode(profile_id):
    """Backward-compatible master ping mode; new UI uses per-stream modes."""
    prof = get_profile(profile_id)
    return _normalize_ping_mode(prof.get("ping_mode", "global")) if prof else "global"

def set_profile_ping_mode(profile_id, mode):
    mode = _normalize_ping_mode(mode)
    update_profile(profile_id, ping_mode=mode, config_ping_mode=mode, proxy_ping_mode=mode)

def get_profile_config_ping_mode(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return "global"
    return _normalize_ping_mode(prof.get("config_ping_mode", "global"))

def set_profile_config_ping_mode(profile_id, mode):
    update_profile(profile_id, config_ping_mode=_normalize_ping_mode(mode))

def get_profile_proxy_ping_mode(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return "global"
    return _normalize_ping_mode(prof.get("proxy_ping_mode", "global"))

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
    allowed = ("Protocol", "Flag", "Country", "COUNTRY_EN", "COUNTRY_FA", "CHANNEL_ID", "COUNT", "PING")
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
    update_profile(profile_id, country_display=mode)

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

# Smart batch mode: independently stored per profile and OFF by default.
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
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        log.exception("pending batch write failed for profile %s/%s", profile_id, kind)

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

def render_naming_template(template, *, protocol, flag, country_code, channel_link, count):
    """Render {TOKEN} and [TOKEN] placeholders."""
    template = str(template or "{Flag} | ⚡️Telegram = {CHANNEL_ID}")
    values = {
        "Protocol": protocol or "", "PROTOCOL": protocol or "",
        "Flag": flag or "", "FLAG": flag or "",
        "COUNTRY_EN": COUNTRY_NAMES_EN.get(country_code, ""),
        "COUNTRY_FA": COUNTRY_NAMES_FA.get(country_code, ""),
        "Country": COUNTRY_NAMES_EN.get(country_code, ""),
        "COUNTRY": COUNTRY_NAMES_EN.get(country_code, ""),
        "CHANNEL_ID": channel_link or "", "COUNT": str(count), "PING": "",
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

# ======================================================================
# توابع لیست سیاه و اسپانسر (با تغییرات جدید)
# ======================================================================
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

# Proxy post mode: 0 = normal text, 1 = inline glass buttons
def get_profile_proxy_post_mode(profile_id):
    prof = get_profile(profile_id)
    return int(prof.get("proxy_post_mode", 0)) if prof else 0

def set_profile_proxy_post_mode(profile_id, mode):
    mode = 1 if int(mode) else 0
    update_profile(profile_id, proxy_post_mode=mode)
    return mode

# Config post mode: 0 = normal COPY CODE/pre blocks, 1 = one collapsed expandable quote
# with one copyable <code> config per line. Default is always normal.
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

# New sponsor functions
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

# ======================================================================
# توابع کمکی (پینگ، استخراج لینک و ...) - بهبود یافته
# ======================================================================
# Country name mappings - complete list
COUNTRY_NAMES_FA = {
    'AD': 'آندورا',
    'AE': 'امارات',
    'AF': 'افغانستان',
    'AG': 'آنتیگوا و باربودا',
    'AI': 'آنگویلا',
    'AL': 'آلبانی',
    'AM': 'ارمنستان',
    'AO': 'آنگولا',
    'AQ': 'جنوبگان',
    'AR': 'آرژانتین',
    'AS': 'ساموآی آمریکا',
    'AT': 'اتریش',
    'AU': 'استرالیا',
    'AW': 'آروبا',
    'AX': 'جزایر آلاند',
    'AZ': 'آذربایجان',
    'BA': 'بوسنی و هرزگوین',
    'BB': 'باربادوس',
    'BD': 'بنگلادش',
    'BE': 'بلژیک',
    'BF': 'بورکینافاسو',
    'BG': 'بلغارستان',
    'BH': 'بحرین',
    'BI': 'بوروندی',
    'BJ': 'بنین',
    'BL': 'سن بارتلمی',
    'BM': 'برمودا',
    'BN': 'برونئی',
    'BO': 'بولیوی',
    'BQ': 'جزایر کارائیب هلند',
    'BR': 'برزیل',
    'BS': 'باهاما',
    'BT': 'بوتان',
    'BV': 'جزیرهٔ بووه',
    'BW': 'بوتسوانا',
    'BY': 'بلاروس',
    'BZ': 'بلیز',
    'CA': 'کانادا',
    'CC': 'جزایر کوکوس',
    'CD': 'کنگو - کینشاسا',
    'CF': 'جمهوری افریقای مرکزی',
    'CG': 'کنگو - برازویل',
    'CH': 'سوئیس',
    'CI': 'ساحل عاج',
    'CK': 'جزایر کوک',
    'CL': 'شیلی',
    'CM': 'کامرون',
    'CN': 'چین',
    'CO': 'کلمبیا',
    'CR': 'کاستاریکا',
    'CU': 'کوبا',
    'CV': 'کیپ\u200cورد',
    'CW': 'کوراسائو',
    'CX': 'جزیره کریسمس',
    'CY': 'قبرس',
    'CZ': 'جمهوری چک',
    'DE': 'آلمان',
    'DJ': 'جیبوتی',
    'DK': 'دانمارک',
    'DM': 'دومینیکا',
    'DO': 'جمهوری دومینیکن',
    'DZ': 'الجزایر',
    'EC': 'اکوادور',
    'EE': 'استونی',
    'EG': 'مصر',
    'EH': 'صحرای غربی',
    'ER': 'اریتره',
    'ES': 'اسپانیا',
    'ET': 'اتیوپی',
    'FI': 'فنلاند',
    'FJ': 'فیجی',
    'FK': 'جزایر فالکلند',
    'FM': 'میکرونزی',
    'FO': 'جزایر فارو',
    'FR': 'فرانسه',
    'GA': 'گابن',
    'GB': 'بریتانیا',
    'GD': 'گرنادا',
    'GE': 'گرجستان',
    'GF': 'گویان فرانسه',
    'GG': 'گرنزی',
    'GH': 'غنا',
    'GI': 'جبل\u200cالطارق',
    'GL': 'گرینلند',
    'GM': 'گامبیا',
    'GN': 'گینه',
    'GP': 'گوادلوپ',
    'GQ': 'گینهٔ استوایی',
    'GR': 'یونان',
    'GS': 'جورجیای جنوبی و جزایر ساندویچ جنوبی',
    'GT': 'گواتمالا',
    'GU': 'گوام',
    'GW': 'گینهٔ بیسائو',
    'GY': 'گویان',
    'HK': 'هنگ\u200cکنگ',
    'HM': 'هرد و جزایر مک\u200cدونالد',
    'HN': 'هندوراس',
    'HR': 'کرواسی',
    'HT': 'هائیتی',
    'HU': 'مجارستان',
    'ID': 'اندونزی',
    'IE': 'ایرلند',
    'IL': 'اسرائیل',
    'IM': 'جزیره من',
    'IN': 'هند',
    'IO': 'قلمرو اقیانوس هند بریتانیا',
    'IQ': 'عراق',
    'IR': 'ایران',
    'IS': 'ایسلند',
    'IT': 'ایتالیا',
    'JE': 'جرزی',
    'JM': 'جامائیکا',
    'JO': 'اردن',
    'JP': 'ژاپن',
    'KE': 'کنیا',
    'KG': 'قرقیزستان',
    'KH': 'کامبوج',
    'KI': 'کیریباتی',
    'KM': 'کومور',
    'KN': 'سنت کیتس و نویس',
    'KP': 'کرهٔ شمالی',
    'KR': 'کره جنوبی',
    'KW': 'کویت',
    'KY': 'جزایر کیمن',
    'KZ': 'قزاقستان',
    'LA': 'لائوس',
    'LB': 'لبنان',
    'LC': 'سنت لوسیا',
    'LI': 'لیختن\u200cاشتاین',
    'LK': 'سری\u200cلانکا',
    'LR': 'لیبریا',
    'LS': 'لسوتو',
    'LT': 'لیتوانی',
    'LU': 'لوکزامبورگ',
    'LV': 'لتونی',
    'LY': 'لیبی',
    'MA': 'مراکش',
    'MC': 'موناکو',
    'MD': 'مولداوی',
    'ME': 'مونته\u200cنگرو',
    'MF': 'سن مارتن',
    'MG': 'ماداگاسکار',
    'MH': 'جزایر مارشال',
    'MK': 'مقدونیه شمالی',
    'ML': 'مالی',
    'MM': 'میانمار',
    'MN': 'مغولستان',
    'MO': 'ماکائو، منطقهٔ ویژهٔ اداری چین',
    'MP': 'جزایر ماریانای شمالی',
    'MQ': 'مارتینیک',
    'MR': 'موریتانی',
    'MS': 'مونت\u200cسرات',
    'MT': 'مالت',
    'MU': 'موریس',
    'MV': 'مالدیو',
    'MW': 'مالاوی',
    'MX': 'مکزیک',
    'MY': 'مالزی',
    'MZ': 'موزامبیک',
    'NA': 'نامیبیا',
    'NC': 'کالدونیای جدید',
    'NE': 'نیجر',
    'NF': 'جزیره نورفک',
    'NG': 'نیجریه',
    'NI': 'نیکاراگوئه',
    'NL': 'هلند',
    'NO': 'نروژ',
    'NP': 'نپال',
    'NR': 'نائورو',
    'NU': 'نیوئه',
    'NZ': 'نیوزیلند',
    'OM': 'عمان',
    'PA': 'پاناما',
    'PE': 'پرو',
    'PF': 'پلی\u200cنزی فرانسه',
    'PG': 'پاپوآ گینه نو',
    'PH': 'فیلیپین',
    'PK': 'پاکستان',
    'PL': 'لهستان',
    'PM': 'سن پیر و میکلن',
    'PN': 'پیتکرن',
    'PR': 'پورتوریکو',
    'PS': 'فلسطین',
    'PT': 'پرتغال',
    'PW': 'پالائو',
    'PY': 'پاراگوئه',
    'QA': 'قطر',
    'RE': 'رئونیون',
    'RO': 'رومانی',
    'RS': 'صربستان',
    'RU': 'روسیه',
    'RW': 'رواندا',
    'SA': 'عربستان سعودی',
    'SB': 'جزایر سلیمان',
    'SC': 'سیشل',
    'SD': 'سودان',
    'SE': 'سوئد',
    'SG': 'سنگاپور',
    'SH': 'سنت هلنا',
    'SI': 'اسلوونی',
    'SJ': 'سوالبارد و یان ماین',
    'SK': 'اسلواکی',
    'SL': 'سیرالئون',
    'SM': 'سان مارینو',
    'SN': 'سنگال',
    'SO': 'سومالی',
    'SR': 'سورینام',
    'SS': 'سودان جنوبی',
    'ST': 'سائوتومه و پرینسیپ',
    'SV': 'السالوادور',
    'SX': 'سینت مارتن',
    'SY': 'سوریه',
    'SZ': 'اسواتینی',
    'TC': 'جزایر تورکس و کایکوس',
    'TD': 'چاد',
    'TF': 'سرزمین\u200cهای جنوبی فرانسه',
    'TG': 'توگو',
    'TH': 'تایلند',
    'TJ': 'تاجیکستان',
    'TK': 'توکلائو',
    'TL': 'تیمور شرقی',
    'TM': 'ترکمنستان',
    'TN': 'تونس',
    'TO': 'تونگا',
    'TR': 'ترکیه',
    'TT': 'ترینیداد و توباگو',
    'TV': 'تووالو',
    'TW': 'تایوان',
    'TZ': 'تانزانیا',
    'UA': 'اوکراین',
    'UG': 'اوگاندا',
    'UM': 'جزایر کوچک حاشیه\u200cای آمریکا',
    'US': 'آمریکا',
    'UY': 'اروگوئه',
    'UZ': 'ازبکستان',
    'VA': 'واتیکان',
    'VC': 'سنت وینسنت و گرنادین',
    'VE': 'ونزوئلا',
    'VG': 'جزایر ویرجین بریتانیا',
    'VI': 'جزایر ویرجین آمریکا',
    'VN': 'ویتنام',
    'VU': 'وانواتو',
    'WF': 'والیس و فوتونا',
    'WS': 'ساموآ',
    'YE': 'یمن',
    'YT': 'مایوت',
    'ZA': 'آفریقای جنوبی',
    'ZM': 'زامبیا',
    'ZW': 'زیمبابوه',
}
COUNTRY_NAMES_EN = {
    'AD': 'Andorra',
    'AE': 'United Arab Emirates',
    'AF': 'Afghanistan',
    'AG': 'Antigua & Barbuda',
    'AI': 'Anguilla',
    'AL': 'Albania',
    'AM': 'Armenia',
    'AO': 'Angola',
    'AQ': 'Antarctica',
    'AR': 'Argentina',
    'AS': 'American Samoa',
    'AT': 'Austria',
    'AU': 'Australia',
    'AW': 'Aruba',
    'AX': 'Åland Islands',
    'AZ': 'Azerbaijan',
    'BA': 'Bosnia & Herzegovina',
    'BB': 'Barbados',
    'BD': 'Bangladesh',
    'BE': 'Belgium',
    'BF': 'Burkina Faso',
    'BG': 'Bulgaria',
    'BH': 'Bahrain',
    'BI': 'Burundi',
    'BJ': 'Benin',
    'BL': 'St. Barthélemy',
    'BM': 'Bermuda',
    'BN': 'Brunei',
    'BO': 'Bolivia',
    'BQ': 'Caribbean Netherlands',
    'BR': 'Brazil',
    'BS': 'Bahamas',
    'BT': 'Bhutan',
    'BV': 'Bouvet Island',
    'BW': 'Botswana',
    'BY': 'Belarus',
    'BZ': 'Belize',
    'CA': 'Canada',
    'CC': 'Cocos (Keeling) Islands',
    'CD': 'Democratic Republic of the Congo',
    'CF': 'Central African Republic',
    'CG': 'Republic of the Congo',
    'CH': 'Switzerland',
    'CI': 'Côte d’Ivoire',
    'CK': 'Cook Islands',
    'CL': 'Chile',
    'CM': 'Cameroon',
    'CN': 'China',
    'CO': 'Colombia',
    'CR': 'Costa Rica',
    'CU': 'Cuba',
    'CV': 'Cape Verde',
    'CW': 'Curaçao',
    'CX': 'Christmas Island',
    'CY': 'Cyprus',
    'CZ': 'Czech Republic',
    'DE': 'Germany',
    'DJ': 'Djibouti',
    'DK': 'Denmark',
    'DM': 'Dominica',
    'DO': 'Dominican Republic',
    'DZ': 'Algeria',
    'EC': 'Ecuador',
    'EE': 'Estonia',
    'EG': 'Egypt',
    'EH': 'Western Sahara',
    'ER': 'Eritrea',
    'ES': 'Spain',
    'ET': 'Ethiopia',
    'FI': 'Finland',
    'FJ': 'Fiji',
    'FK': 'Falkland Islands',
    'FM': 'Micronesia',
    'FO': 'Faroe Islands',
    'FR': 'France',
    'GA': 'Gabon',
    'GB': 'United Kingdom',
    'GD': 'Grenada',
    'GE': 'Georgia',
    'GF': 'French Guiana',
    'GG': 'Guernsey',
    'GH': 'Ghana',
    'GI': 'Gibraltar',
    'GL': 'Greenland',
    'GM': 'Gambia',
    'GN': 'Guinea',
    'GP': 'Guadeloupe',
    'GQ': 'Equatorial Guinea',
    'GR': 'Greece',
    'GS': 'South Georgia & South Sandwich Islands',
    'GT': 'Guatemala',
    'GU': 'Guam',
    'GW': 'Guinea-Bissau',
    'GY': 'Guyana',
    'HK': 'Hong Kong SAR China',
    'HM': 'Heard & McDonald Islands',
    'HN': 'Honduras',
    'HR': 'Croatia',
    'HT': 'Haiti',
    'HU': 'Hungary',
    'ID': 'Indonesia',
    'IE': 'Ireland',
    'IL': 'Israel',
    'IM': 'Isle of Man',
    'IN': 'India',
    'IO': 'British Indian Ocean Territory',
    'IQ': 'Iraq',
    'IR': 'Iran',
    'IS': 'Iceland',
    'IT': 'Italy',
    'JE': 'Jersey',
    'JM': 'Jamaica',
    'JO': 'Jordan',
    'JP': 'Japan',
    'KE': 'Kenya',
    'KG': 'Kyrgyzstan',
    'KH': 'Cambodia',
    'KI': 'Kiribati',
    'KM': 'Comoros',
    'KN': 'St. Kitts & Nevis',
    'KP': 'North Korea',
    'KR': 'South Korea',
    'KW': 'Kuwait',
    'KY': 'Cayman Islands',
    'KZ': 'Kazakhstan',
    'LA': 'Laos',
    'LB': 'Lebanon',
    'LC': 'St. Lucia',
    'LI': 'Liechtenstein',
    'LK': 'Sri Lanka',
    'LR': 'Liberia',
    'LS': 'Lesotho',
    'LT': 'Lithuania',
    'LU': 'Luxembourg',
    'LV': 'Latvia',
    'LY': 'Libya',
    'MA': 'Morocco',
    'MC': 'Monaco',
    'MD': 'Moldova',
    'ME': 'Montenegro',
    'MF': 'St. Martin',
    'MG': 'Madagascar',
    'MH': 'Marshall Islands',
    'MK': 'North Macedonia',
    'ML': 'Mali',
    'MM': 'Myanmar (Burma)',
    'MN': 'Mongolia',
    'MO': 'Macao SAR China',
    'MP': 'Northern Mariana Islands',
    'MQ': 'Martinique',
    'MR': 'Mauritania',
    'MS': 'Montserrat',
    'MT': 'Malta',
    'MU': 'Mauritius',
    'MV': 'Maldives',
    'MW': 'Malawi',
    'MX': 'Mexico',
    'MY': 'Malaysia',
    'MZ': 'Mozambique',
    'NA': 'Namibia',
    'NC': 'New Caledonia',
    'NE': 'Niger',
    'NF': 'Norfolk Island',
    'NG': 'Nigeria',
    'NI': 'Nicaragua',
    'NL': 'Netherlands',
    'NO': 'Norway',
    'NP': 'Nepal',
    'NR': 'Nauru',
    'NU': 'Niue',
    'NZ': 'New Zealand',
    'OM': 'Oman',
    'PA': 'Panama',
    'PE': 'Peru',
    'PF': 'French Polynesia',
    'PG': 'Papua New Guinea',
    'PH': 'Philippines',
    'PK': 'Pakistan',
    'PL': 'Poland',
    'PM': 'St. Pierre & Miquelon',
    'PN': 'Pitcairn Islands',
    'PR': 'Puerto Rico',
    'PS': 'Palestine',
    'PT': 'Portugal',
    'PW': 'Palau',
    'PY': 'Paraguay',
    'QA': 'Qatar',
    'RE': 'Réunion',
    'RO': 'Romania',
    'RS': 'Serbia',
    'RU': 'Russia',
    'RW': 'Rwanda',
    'SA': 'Saudi Arabia',
    'SB': 'Solomon Islands',
    'SC': 'Seychelles',
    'SD': 'Sudan',
    'SE': 'Sweden',
    'SG': 'Singapore',
    'SH': 'St. Helena',
    'SI': 'Slovenia',
    'SJ': 'Svalbard & Jan Mayen',
    'SK': 'Slovakia',
    'SL': 'Sierra Leone',
    'SM': 'San Marino',
    'SN': 'Senegal',
    'SO': 'Somalia',
    'SR': 'Suriname',
    'SS': 'South Sudan',
    'ST': 'São Tomé & Príncipe',
    'SV': 'El Salvador',
    'SX': 'Sint Maarten',
    'SY': 'Syria',
    'SZ': 'Eswatini',
    'TC': 'Turks & Caicos Islands',
    'TD': 'Chad',
    'TF': 'French Southern Territories',
    'TG': 'Togo',
    'TH': 'Thailand',
    'TJ': 'Tajikistan',
    'TK': 'Tokelau',
    'TL': 'Timor-Leste',
    'TM': 'Turkmenistan',
    'TN': 'Tunisia',
    'TO': 'Tonga',
    'TR': 'Türkiye',
    'TT': 'Trinidad & Tobago',
    'TV': 'Tuvalu',
    'TW': 'Taiwan',
    'TZ': 'Tanzania',
    'UA': 'Ukraine',
    'UG': 'Uganda',
    'UM': 'U.S. Outlying Islands',
    'US': 'United States',
    'UY': 'Uruguay',
    'UZ': 'Uzbekistan',
    'VA': 'Vatican City',
    'VC': 'St. Vincent & Grenadines',
    'VE': 'Venezuela',
    'VG': 'British Virgin Islands',
    'VI': 'U.S. Virgin Islands',
    'VN': 'Vietnam',
    'VU': 'Vanuatu',
    'WF': 'Wallis & Futuna',
    'WS': 'Samoa',
    'YE': 'Yemen',
    'YT': 'Mayotte',
    'ZA': 'South Africa',
    'ZM': 'Zambia',
    'ZW': 'Zimbabwe',
}

def get_country_info(code):
    """Return (flag, english_name, persian_name) for a country code."""
    if not code or len(code) != 2:
        return "🌐", "Unknown", "ناشناخته"
    flag = country_to_flag(code)
    en = COUNTRY_NAMES_EN.get(code, code)
    fa = COUNTRY_NAMES_FA.get(code, code)
    return flag, en, fa

def country_to_flag(code):
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

async def get_flag_for_ip(ip):
    cached = c.execute(
        "SELECT country, flag FROM country_cache WHERE ip=?", (ip,)
    ).fetchone()
    if cached and len(cached[1]) > 1:
        return cached[1], cached[0]  # flag, country_code

    try:
        async with httpx.AsyncClient(timeout=3) as cl:
            r = await cl.get(
                f"http://ip-api.com/json/{ip}?fields=countryCode",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data = r.json()
                country = data.get("countryCode", "").upper()
                if country:
                    flag = country_to_flag(country)
                    c.execute(
                        "INSERT OR REPLACE INTO country_cache VALUES (?,?,?)",
                        (ip, country, flag))
                    conn.commit()
                    return flag, country
    except Exception as e:
        log.warning(f"flag API fail for {ip}: {e}")

    return "🌐", ""

def clean_proxy_link(url):
    if not url:
        return url
    url = url.strip()
    while url and url[-1] in ".,;:!؟\"'`(){}[]<>":
        url = url[:-1]
    return url

def normalize_telegram_proxy(url):
    if not url:
        return url
    url = clean_proxy_link(url)
    url = url.replace('&amp;', '&')
    url = re.sub(r'&amp;', '&', url, flags=re.IGNORECASE)
    try:
        url = unquote(url)
    except:
        pass
    return url

def clean_config_url(url: str) -> str:
    """Normalize a config URI without decoding reserved characters globally.

    Trojan passwords are frequently percent-encoded (for example ``%23`` for
    ``#``). Decoding the complete URI before parsing would turn an encoded
    password character into a URL delimiter and corrupt the netloc. Only HTML
    entities and surrounding whitespace are normalized here; percent-encoding
    is intentionally preserved.
    """
    if not url:
        return url
    url = html.unescape(str(url)).strip()
    url = url.replace("&amp;", "&")
    return url

# ======================================================================
# VALIDATION FUNCTIONS - FIX PARSING
# ======================================================================
def validate_vless(url):
    """Validate VLESS URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "vless":
            return False, "not vless"
        # Check for userinfo (UUID)
        if not parsed.username or len(parsed.username) < 8:
            return False, "missing or invalid UUID"
        # Check host
        if not parsed.hostname:
            return False, "missing host"
        # Check port
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def normalize_vmess_url(url, name=""):
    """Normalize VMess to standard JSON-base64 form. Name is stored only in ps."""
    try:
        raw = url.split("vmess://", 1)[1].strip()
        raw = raw.split("#", 1)[0]
        raw = raw.strip()
        raw += "=" * (-len(raw) % 4)
        obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
        if name:
            obj["ps"] = str(name).strip()
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        return "vmess://" + base64.b64encode(payload).decode()
    except Exception as e:
        log.warning(f"VMESS normalize failed: {e}")
        return None


def apply_display_name_to_config(url, name):
    """
    Change display name safely.
    VMess has no URI fragment name in many clients; the name must be written
    into JSON field ps and the vmess payload must be rebuilt.
    Other protocols keep their normal #fragment behaviour.
    """
    if not name:
        return url
    try:
        if (url or "").lower().startswith("vmess://"):
            fixed = normalize_vmess_url(url, name)
            return fixed or url
    except Exception:
        pass
    return url

def validate_vmess(url):
    """Validate VMESS URL structure (base64 encoded JSON)."""
    try:
        if not url.startswith("vmess://"):
            return False, "not vmess"
        b64 = url[8:]
        # Add padding if needed
        b64 += "=" * (-len(b64) % 4)
        data = base64.b64decode(b64, validate=True).decode('utf-8', errors='ignore')
        # Parse JSON
        obj = json.loads(data)
        required = ['add', 'port', 'id', 'aid', 'net', 'type', 'host', 'path', 'tls']
        for key in ['add', 'port', 'id']:
            if key not in obj or not obj[key]:
                return False, f"missing {key}"
        if not str(obj['port']).isdigit() or int(obj['port']) < 1 or int(obj['port']) > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception as e:
        return False, f"decode error: {str(e)}"

def validate_trojan(url):
    """Validate standard Trojan URIs, including percent-encoded passwords.

    Supported common forms include:
      trojan://PASSWORD@HOST:PORT
      trojan://PASSWORD@HOST:PORT?security=tls&sni=example.com
      trojan://user:PASSWORD@HOST:PORT?...   (accepted for compatibility)

    The credential is opaque data; it must not be URL-decoded before parsing
    because encoded delimiters such as %23 and %40 are valid password bytes.
    """
    try:
        value = clean_config_url(url)
        parsed = urlparse(value)
        if parsed.scheme.lower() != "trojan":
            return False, "not trojan"

        if not parsed.hostname:
            return False, "missing host"

        try:
            port = parsed.port
        except ValueError:
            return False, "invalid port"
        if not port or not (1 <= port <= 65535):
            return False, "invalid port"

        # In the normal Trojan form (PASSWORD@HOST), urllib exposes the
        # password as ``username`` because there is no separate user field.
        credential = parsed.password if parsed.password is not None else parsed.username
        if credential is None or credential == "":
            return False, "missing password"

        # Validate percent escapes in the credential without changing the URI.
        # urllib.parse.unquote tolerates malformed escapes, so explicitly
        # reject a stray '%' followed by a non-hex pair.
        if re.search(r"%(?![0-9A-Fa-f]{2})", credential):
            return False, "invalid percent-encoding in password"

        # A raw '@' in userinfo would be parsed as a delimiter. Encoded %40
        # remains part of the credential and is therefore fully supported.
        return True, "valid"
    except Exception as e:
        return False, f"parse error: {e}"

def validate_ss(url):
    """Validate Shadowsocks URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "ss":
            return False, "not ss"
        # Check for valid base64 userinfo
        if parsed.username and parsed.password:
            return True, "valid"
        else:
            # May be in format ss://base64
            b64 = url[5:]
            b64 += "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64, validate=True).decode('utf-8', errors='ignore')
            if '@' in decoded:
                return True, "valid"
            else:
                return False, "invalid format"
    except Exception:
        return False, "parse error"

def validate_socks(url):
    """Validate SOCKS URL structure (simple)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'socks':
            return False, "not socks"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_hy2(url):
    """Validate Hysteria2 URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "hy2":
            return False, "not hy2"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_tuic(url):
    """Validate TUIC URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "tuic":
            return False, "not tuic"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_wireguard(url):
    """Validate WireGuard URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "wireguard":
            return False, "not wireguard"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_http_proxy(url):
    """Legacy validator only; HTTP/HTTPS is not a supported bot proxy type."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "not http"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or not (1 <= parsed.port <= 65535):
            return False, "invalid port"
        # HTTP proxy URLs normally have credentials or an explicit proxy-like port.
        # Accept explicit host:port URLs because source channels commonly publish them.
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_hysteria(url):
    """Validate Hysteria v1 URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "hysteria":
            return False, "not hysteria"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or not (1 <= parsed.port <= 65535):
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def is_telegram_proxy_url(url):
    """Strictly accept Telegram proxies: MTProto links or SOCKS5 links."""
    if not url:
        return False
    u = html.unescape(str(url)).strip()
    if u.lower().startswith(("tg://proxy?", "https://t.me/proxy?")):
        return validate_telegram_proxy_url(u)[0]
    if u.lower().startswith(("socks5://", "socks5h://")):
        return validate_telegram_proxy_url(u)[0]
    return False


def detect_protocol_name(url):
    """Return a human-readable protocol name for ONLY the supported config/proxy schemes."""
    u = (url or "").strip().lower()
    if u.startswith("vless://"): return "VLESS"
    if u.startswith("vmess://"): return "VMESS"
    if u.startswith("trojan://"): return "TROJAN"
    if u.startswith(("wireguard://", "wg://")): return "WIREGUARD"
    if u.startswith(("hysteria://", "hysteria2://", "hy2://")): return "HYSTERIA2"
    if u.startswith(("wireguard://", "wg://")): return "WIREGUARD"
    if u.startswith(("shadowsocks://", "ss://")): return "SHADOWSOCKS"
    if u.startswith("socks://"): return "SOCKS"
    if is_telegram_proxy_url(u):
        return "MTPROTO" if u.startswith(("tg://proxy", "https://t.me/proxy")) else "SOCKS5"
    return "Unknown"

def validate_config_link(url):
    """Strict validation for the exact config protocols requested by the bot owner."""
    if not url:
        return False, "empty"
    url = clean_config_url(url.strip())
    if is_telegram_proxy_url(url):
        return True, "telegram proxy config"
    scheme = urlparse(url).scheme.lower()
    if scheme == "vless": return validate_vless(url)
    if scheme == "vmess": return validate_vmess(url)
    if scheme == "trojan": return validate_trojan(url)
    if scheme in ("wireguard", "wg"): return validate_wireguard(url.replace("wg://", "wireguard://", 1))
    if scheme in ("shadowsocks", "ss"): return validate_ss(url.replace("shadowsocks://", "ss://", 1))
    if scheme == "socks": return validate_socks(url)
    if scheme in ("hysteria2", "hy2"): return validate_hy2(url.replace("hysteria2://", "hy2://", 1))
    return False, "unsupported protocol"

# ======================================================================
# استخراج لینک‌ها با اعتبارسنجی
# ======================================================================

# ========================= Collector v3 =========================
SUPPORTED_SCHEMES = (
    "vless", "vmess", "trojan", "ss", "ssr", "socks", "socks5",
    "socks5h", "hy2", "hysteria", "hysteria2", "wg", "wireguard", "https://t.me/proxy"
)

def _link_name(url):
    try:
        return unquote(urlparse(url).fragment or "").strip()
    except Exception:
        return ""

def _decode_b64(s):
    try:
        s = s.strip().replace("-", "+").replace("_", "/")
        return base64.b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        return b""

def parse_vmess(url):
    try:
        raw=url.split("vmess://",1)[1].split("#",1)[0].strip()
        obj=json.loads(_decode_b64(raw).decode("utf-8"))
        if not obj.get("add") or not obj.get("port") or not obj.get("id"):
            return {"protocol":"VMESS","url":url,"name":"","valid":False,"metadata":{}}
        obj["ps"]=obj.get("ps") or _link_name(url)
        payload=base64.b64encode(json.dumps(obj,ensure_ascii=False,separators=(",",":")).encode()).decode()
        return {"protocol":"VMESS","url":"vmess://"+payload,"name":obj.get("ps",""),"valid":True,"metadata":obj}
    except Exception:
        return {"protocol":"VMESS","url":url,"name":"","valid":False,"metadata":{}}

def parse_vless(url):
    try:
        p=urlparse(url)
        return {"protocol":"VLESS","url":url,"name":_link_name(url),
                "valid":bool(p.username and p.hostname and p.port),
                "metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"VLESS","url":url,"name":"","valid":False,"metadata":{}}

def parse_trojan(url):
    try:
        p=urlparse(url)
        return {"protocol":"TROJAN","url":url,"name":_link_name(url),
                "valid":bool(p.username and p.hostname and p.port),
                "metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"TROJAN","url":url,"name":"","valid":False,"metadata":{}}

def parse_ss(url):
    try:
        p=urlparse(url)
        return {"protocol":"SHADOWSOCKS","url":url,"name":_link_name(url),
                "valid":bool(p.hostname and p.port),"metadata":{}}
    except Exception:
        return {"protocol":"SHADOWSOCKS","url":url,"name":"","valid":False,"metadata":{}}

def parse_socks(url):
    try:
        raw = str(url).strip()
        # Telegram SOCKS links commonly contain a human name after #. Keep it as
        # part of the display name and never let it break the actual URL parser.
        p=urlparse(raw)
        return {"protocol":"SOCKS","url":raw,"name":_link_name(raw),
                "valid":bool(p.hostname and p.port),
                "metadata":{"user":p.username or "","password":p.password or ""}}
    except Exception:
        return {"protocol":"SOCKS","url":url,"name":"","valid":False,"metadata":{}}

def parse_hysteria(url):
    try:
        p=urlparse(url)
        return {"protocol":"HYSTERIA","url":url,"name":_link_name(url),
                "valid":bool(p.hostname and p.port),"metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"HYSTERIA","url":url,"name":"","valid":False,"metadata":{}}

def parse_wireguard(url):
    try:
        p=urlparse(url)
        return {"protocol":"WIREGUARD","url":url,"name":_link_name(url),
                "valid":bool(p.hostname),"metadata":parse_qs(p.query)}
    except Exception:
        return {"protocol":"WIREGUARD","url":url,"name":"","valid":False,"metadata":{}}

def parse_telegram_proxy(url):
    try:
        p=urlparse(url)
        q=parse_qs(p.query)
        if p.scheme=="tg" and p.netloc=="proxy" or p.path.lower()=="/proxy":
            ok=bool(q.get("server") and q.get("port") and q.get("secret"))
            return {"protocol":"MTPROTO","url":url,"name":"","valid":ok,"metadata":q}
        if p.scheme=="tg" and p.netloc=="socks":
            ok=bool(q.get("server") and q.get("port"))
            return {"protocol":"SOCKS5","url":url,"name":"","valid":ok,"metadata":q}
    except Exception:
        pass
    return {"protocol":"TELEGRAM_PROXY","url":url,"name":"","valid":False,"metadata":{}}

def parse_config_url(url):
    s=url.lower()
    if s.startswith("vmess://"): return parse_vmess(url)
    if s.startswith("vless://"): return parse_vless(url)
    if s.startswith("trojan://"): return parse_trojan(url)
    if s.startswith(("ss://","ssr://")): return parse_ss(url)
    if s.startswith("socks://"): return parse_socks(url)
    if s.startswith(("hy2://","hysteria://","hysteria2://")): return parse_hysteria(url)
    if s.startswith(("wg://","wireguard://", "https://t.me/proxy?", "tg://proxy?")): return parse_wireguard(url)
    return {"protocol":"","url":url,"name":"","valid":False,"metadata":{}}

def extract_links_from_text(text):
    """Extract published configuration links without confusing them with bot proxies."""
    if not text:
        return []
    source = html.unescape(text)
    patterns = [
        r'(?:vless|vmess|trojan|ss|ssr|shadowsocks|socks|socks5|socks5h|hy2|hysteria|hysteria2|wg|wireguard)://[^\s<>"\']+'
    ]
    out=[]; seen=set()
    for pat in patterns:
        for m in re.finditer(pat, source, re.I):
            u=m.group(0).rstrip('.,;:!؟)]}')
            # Telegram proxy links are handled only by extract_proxy_links_from_text()
            # Never count MTProto proxies as VPN configs.
            if u.lower().startswith(('http://t.me/proxy?', 'https://t.me/proxy?')):
                continue
            item=parse_config_url(u)
            if item.get('valid'):
                u=item.get('url',u)
                key=hashlib.sha256(u.encode()).hexdigest()
                if key not in seen:
                    seen.add(key)
                    out.append(u)
    return out

def validate_telegram_proxy_url(url):
    """Strict Telegram proxy validation: MTProto or SOCKS5 only."""
    if not url:
        return False, "empty"
    u = html.unescape(clean_proxy_link(str(url))).strip()
    try:
        p = urlparse(u)
        scheme = p.scheme.lower()
        if scheme == "https" and p.hostname and p.hostname.lower() == "t.me" and p.path.lower() == "/proxy":
            q = parse_qs(p.query, keep_blank_values=True)
            server = (q.get("server") or [""])[0].strip()
            port_raw = (q.get("port") or [""])[0].strip()
            secret = (q.get("secret") or [""])[0].strip()
            if not server or not secret or not port_raw.isdigit():
                return False, "missing server/port/secret"
            port = int(port_raw)
            if not (1 <= port <= 65535):
                return False, "invalid port"
            return True, "MTPROTO"
        if scheme == "tg" and p.netloc.lower() == "proxy":
            q = parse_qs(p.query, keep_blank_values=True)
            server = (q.get("server") or [""])[0].strip()
            port_raw = (q.get("port") or [""])[0].strip()
            secret = (q.get("secret") or [""])[0].strip()
            if not server or not secret or not port_raw.isdigit():
                return False, "missing server/port/secret"
            if not (1 <= int(port_raw) <= 65535):
                return False, "invalid port"
            return True, "MTPROTO"
        if scheme in ("socks", "socks5"):
            if not p.hostname:
                return False, "missing host"
            if p.port is None or not (1 <= p.port <= 65535):
                return False, "invalid port"
            return True, "SOCKS5"
        if scheme == "tg" and p.netloc.lower() == "socks":
            q = parse_qs(p.query, keep_blank_values=True)
            if (q.get("server") or [""])[0] and (q.get("port") or [""])[0].isdigit():
                return True, "SOCKS5"
    except Exception as e:
        return False, f"parse error: {e}"
    return False, "not a Telegram proxy"

def canonical_telegram_proxy_url(url):
    """Canonical Telegram proxy URL. MTProto is normalized to t.me/proxy; SOCKS5 is preserved."""
    norm = normalize_telegram_proxy(url or "")
    if not norm:
        return None
    norm = html.unescape(norm).strip()
    ok, kind = validate_telegram_proxy_url(norm)
    if not ok:
        return None
    if kind == "MTPROTO":
        if norm.lower().startswith("tg://proxy?"):
            norm = "https://t.me/proxy?" + norm.split("?", 1)[1]
        p = urlparse(norm)
        q = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k.lower(), v) for k, vals in q.items() for v in vals), doseq=True)
        return "https://t.me/proxy?" + query
    p = urlparse(norm)
    q = urlencode(sorted((k.lower(), v) for k, vals in parse_qs(p.query, keep_blank_values=True).items() for v in vals), doseq=True)
    return urlunparse(("socks", p.netloc, p.path, p.params, q, ""))

def normalize_proxy_url(url):
    return canonical_telegram_proxy_url(url)

def extract_proxy_links_from_text(text):
    """Extract ONLY Telegram MTProto and Telegram SOCKS5 links, including raw href attributes."""
    if not text:
        return []
    source = html.unescape(text)
    results = []
    seen = set()
    patterns = [
        r'https?://t\.me/proxy\?[^\s<>"\']+',
        r'tg://proxy\?[^\s<>"\']+',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, source, re.IGNORECASE):
            norm = canonical_telegram_proxy_url(clean_proxy_link(m.group(0)).rstrip('.,;:!?)]}\'"'))
            if norm:
                ident = canonical_proxy_identity(norm)
                if ident not in seen:
                    seen.add(ident)
                    results.append(norm)
    return results

def extract_uuid_and_address(url):
    try:
        clean = url.split("#")[0] if "#" in url else url
        clean = clean.split("?")[0] if "?" in clean else clean
        after = clean.split("://", 1)[1] if "://" in clean else clean
        if url.startswith("vmess://"):
            try:
                b64 = after + "=" * (-len(after) % 4)
                data = json.loads(
                    base64.b64decode(b64).decode('utf-8', errors='ignore'))
                uid = data.get("id", "") or data.get("uuid", "")
                host = f"{data.get('add','')}:{data.get('port','')}"
                return uid, host
            except Exception:
                return ("vmess_" +
                        hashlib.md5(after.encode()).hexdigest()[:16]), after
        else:
            if "@" in after:
                uid, host = after.split("@", 1)
            else:
                uid, host = after, ""
            if "/" in host:
                host = host.split("/")[0]
            return (uid.split("?")[0].split("#")[0],
                    host.split("?")[0].split("#")[0])
    except Exception:
        return "", ""

def _legacy_canonical_config_identity(url):
    """Compatibility identity used only for hashes created by <=4.1.3."""
    try:
        url = clean_config_url(url or "").strip()
        p = urlparse(url)
        scheme = p.scheme.lower()
        if scheme == "vmess":
            raw = unquote(p.netloc + p.path)
            raw += "=" * (-len(raw) % 4)
            try:
                obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
                keys = ["v","add","port","id","aid","scy","net","type","host","path","tls","sni","alpn","fp","allowInsecure"]
                obj = {k: str(obj.get(k, "")) for k in keys if k in obj}
                return "vmess|" + json.dumps(obj, sort_keys=True, separators=(",", ":"))
            except Exception:
                pass
        pairs = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k, v) for k, vals in pairs.items() for v in vals), doseq=True)
        return urlunparse((scheme, p.netloc.lower(), p.path or "", p.params, query, ""))
    except Exception:
        return clean_config_url(url or "").split("#", 1)[0].strip()

def _q_first(q, *names):
    for name in names:
        for key in (name, name.lower(), name.upper()):
            if key in q:
                vals = q.get(key) or [""]
                return str(vals[0] or "").strip()
    return ""

def canonical_config_identity(url):
    """Strict connection identity.

    Display-only fragment and the bot's Telegram tagging query are ignored.
    The identity contains protocol, credential/UUID, ADDRESS, PORT, NETWORK and
    the remaining connection parameters. Therefore two links with the same real
    connection details are duplicates even if their Telegram/name tag differs.
    """
    try:
        u = clean_config_url(url or "").strip()
        p = urlparse(u)
        scheme = p.scheme.lower()
        if scheme == "vmess":
            raw = unquote(p.netloc + p.path)
            raw += "=" * (-len(raw) % 4)
            try:
                obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
                # ps/name is display-only. Keep all transport/auth fields that
                # can affect the actual connection.
                ignored = {"ps", "name", "remarks", "remark"}
                fields = {}
                for k, v in obj.items():
                    lk = str(k).strip().lower()
                    if lk in ignored:
                        continue
                    if v is None:
                        v = ""
                    fields[lk] = str(v).strip()
                fields["protocol"] = "vmess"
                fields["address"] = str(obj.get("add", "") or "").strip().lower()
                fields["port"] = str(obj.get("port", "") or "").strip()
                fields["uuid"] = str(obj.get("id", obj.get("uuid", "")) or "").strip().lower()
                fields["network"] = str(obj.get("net", obj.get("type", "")) or "").strip().lower()
                return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                # Fall through to a normalized URI identity; still ignore fragment.
                pass
        q = parse_qs(p.query, keep_blank_values=True)
        # Telegram channel/tag parameters are display metadata, not connection identity.
        ignored_query = {"telegram", "name", "remark", "remarks"}
        clean_q = []
        for k, vals in q.items():
            if str(k).strip().lower() in ignored_query:
                continue
            for v in vals:
                clean_q.append((str(k).strip().lower(), str(v).strip()))
        clean_q.sort()
        fields = {
            "protocol": scheme,
            "uuid": unquote(str(p.username or "")).strip().lower(),
            "password": unquote(str(p.password or "")).strip(),
            "address": str(p.hostname or "").strip().lower(),
            "port": str(p.port or "").strip(),
            "network": _q_first(q, "type", "net").lower(),
            "query": clean_q,
        }
        # For SOCKS/SS, credentials are part of the connection and already live
        # in username/password. For every protocol, the normalized query carries
        # path/host/security/reality/etc. exactly as configured.
        return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return clean_config_url(url or "").split("#", 1)[0].strip().lower()

# Hot-path deduplication cache. The posting workers never query SQLite for each
# candidate; permanent identity hashes are loaded once and checked in O(1).
_SEEN_CONFIG_KEYS = set()
_SEEN_PROXY_KEYS = set()
_DEDUP_CACHE_READY = False
_INFLIGHT_CONFIG_KEYS = set()

def _config_identity_hash(url):
    return hashlib.sha256(canonical_config_identity(url).encode("utf-8", errors="ignore")).hexdigest()

def _legacy_config_identity_hash(url):
    return hashlib.sha256(_legacy_canonical_config_identity(url).encode("utf-8", errors="ignore")).hexdigest()

def _proxy_identity_hash(url):
    return hashlib.sha256(canonical_proxy_identity(url).encode("utf-8", errors="ignore")).hexdigest()

def load_dedup_cache():
    """Warm compact permanent dedup hashes in bounded batches."""
    global _DEDUP_CACHE_READY
    if _DEDUP_CACHE_READY:
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT profile_id,full_url,first_seen,source FROM seen WHERE full_url IS NOT NULL AND full_url!=''")
        while True:
            rows = cur.fetchmany(2000)
            if not rows: break
            cur.executemany(
                "INSERT OR IGNORE INTO posted_config_keys(profile_id,identity_hash,first_posted,source) VALUES (?,?,?,?)",
                [(int(pid), _config_identity_hash(url), fs, src or "") for pid,url,fs,src in rows]
            )
            conn.commit()
        cur.execute("SELECT profile_id,proxy_url,first_seen FROM proxies_seen WHERE proxy_url IS NOT NULL AND proxy_url!=''")
        while True:
            rows = cur.fetchmany(2000)
            if not rows: break
            cur.executemany(
                "INSERT OR IGNORE INTO posted_proxy_keys(profile_id,identity_hash,first_posted) VALUES (?,?,?)",
                [(int(pid), _proxy_identity_hash(url), fs) for pid,url,fs in rows]
            )
            conn.commit()
        cur.execute("SELECT profile_id,identity_hash FROM posted_config_keys")
        while True:
            rows = cur.fetchmany(5000)
            if not rows: break
            _SEEN_CONFIG_KEYS.update((int(pid), h) for pid,h in rows)
        cur.execute("SELECT profile_id,identity_hash FROM posted_proxy_keys")
        while True:
            rows = cur.fetchmany(5000)
            if not rows: break
            _SEEN_PROXY_KEYS.update((int(pid), h) for pid,h in rows)
        _DEDUP_CACHE_READY = True
        log.info("[DB] compact dedup cache ready: configs=%d proxies=%d", len(_SEEN_CONFIG_KEYS), len(_SEEN_PROXY_KEYS))
    except sqlite3.Error:
        log.exception("[DB] compact dedup cache load failed")


def is_already_posted(profile_id, url):
    try:
        pid = int(profile_id)
        new_hash = _config_identity_hash(url)
        legacy_hash = _legacy_config_identity_hash(url)
        if _DEDUP_CACHE_READY:
            return ((pid, new_hash) in _SEEN_CONFIG_KEYS or
                    (pid, legacy_hash) in _SEEN_CONFIG_KEYS)
        return c.execute(
            "SELECT 1 FROM posted_config_keys WHERE profile_id=? AND identity_hash IN (?,?) LIMIT 1",
            (pid, new_hash, legacy_hash)
        ).fetchone() is not None
    except sqlite3.Error as exc:
        log.warning("config duplicate check failed: %s", exc)
        return False

def canonical_proxy_identity(url):
    try:
        norm = canonical_telegram_proxy_url(url or "") or (url or "").strip()
        p = urlparse(norm)
        pairs = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k, v) for k, vals in pairs.items() for v in vals), doseq=True)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.params, query, ""))
    except Exception:
        return (url or "").strip().split("#", 1)[0]

def is_proxy_posted(profile_id, proxy_url):
    try:
        key = (int(profile_id), _proxy_identity_hash(proxy_url))
        if _DEDUP_CACHE_READY:
            return key in _SEEN_PROXY_KEYS
        return c.execute("SELECT 1 FROM posted_proxy_keys WHERE profile_id=? AND identity_hash=? LIMIT 1", key).fetchone() is not None
    except sqlite3.Error as exc:
        log.warning("proxy duplicate check failed: %s", exc)
        return False

def mark_proxy_posted(profile_id, proxy_url):
    norm = canonical_telegram_proxy_url(proxy_url or "") or (proxy_url or "").strip()
    identity_hash = _proxy_identity_hash(norm)
    now = get_tehran_time()
    c.execute("INSERT OR IGNORE INTO posted_proxy_keys(profile_id,identity_hash,first_posted) VALUES (?,?,?)",
              (profile_id, identity_hash, now))
    c.execute("INSERT OR IGNORE INTO proxies_seen (proxy_url,first_seen,last_posted,profile_id) VALUES (?,?,?,?)",
              (norm, now, now, profile_id))
    conn.commit()
    _SEEN_PROXY_KEYS.add((int(profile_id), identity_hash))

def mark_as_posted(profile_id, url, source, full_url=""):
    clean = clean_config_url(url or "")
    identity_hash = _config_identity_hash(clean)
    now = get_tehran_time()
    c.execute("INSERT OR IGNORE INTO posted_config_keys(profile_id,identity_hash,first_posted,source) VALUES (?,?,?,?)",
              (profile_id, identity_hash, now, source or ""))
    # Keep only a short-lived URL copy for backup/export; the hash above is the
    # permanent once-only record.
    uid, host = extract_uuid_and_address(clean)
    if not uid and not host:
        uid = clean[:200]; host = ""
    c.execute("INSERT OR IGNORE INTO seen(uuid,address,source,first_seen,last_posted,profile_id,full_url,backup_num) VALUES (?,?,?,?,?,?,?,0)",
              (uid, host, source or "", now, now, profile_id, clean))
    conn.commit()
    _SEEN_CONFIG_KEYS.add((int(profile_id), identity_hash))

def mark_configs_posted_batch(profile_id, entries):
    if not entries:
        return 0
    now = get_tehran_time()
    cfg_rows = []
    seen_rows = []
    for entry in entries:
        url = entry[0] if len(entry) > 0 else ""
        source = entry[1] if len(entry) > 1 else ""
        # In Quote/normal rendering the display URL can receive a custom query
        # or name fragment. Dedup must use the ORIGINAL scraped URL so changing
        # a display template/query can never make the same config look new.
        identity_url = entry[2] if len(entry) > 2 and entry[2] else url
        clean = clean_config_url(url or "")
        identity_clean = clean_config_url(identity_url or "")
        h = _config_identity_hash(identity_clean)
        cfg_rows.append((profile_id, h, now, source or ""))
        uid, host = extract_uuid_and_address(clean)
        if not uid and not host:
            uid = clean[:200]; host = ""
        seen_rows.append((uid, host, source or "", now, now, profile_id, clean))
    c.executemany("INSERT OR IGNORE INTO posted_config_keys(profile_id,identity_hash,first_posted,source) VALUES (?,?,?,?)", cfg_rows)
    c.executemany("INSERT OR IGNORE INTO seen(uuid,address,source,first_seen,last_posted,profile_id,full_url,backup_num) VALUES (?,?,?,?,?,?,?,0)", seen_rows)
    conn.commit()
    for pid,h,_a,_b in cfg_rows:
        _SEEN_CONFIG_KEYS.add((int(pid), h))
    return len(entries)

def mark_proxies_posted_batch(profile_id, urls):
    if not urls:
        return 0
    now = get_tehran_time()
    key_rows = [(profile_id, _proxy_identity_hash(u), now) for u in urls]
    seen_rows = [(canonical_telegram_proxy_url(u or "") or (u or "").strip(), now, now, profile_id) for u in urls]
    c.executemany("INSERT OR IGNORE INTO posted_proxy_keys(profile_id,identity_hash,first_posted) VALUES (?,?,?)", key_rows)
    c.executemany("INSERT OR IGNORE INTO proxies_seen(proxy_url,first_seen,last_posted,profile_id) VALUES (?,?,?,?)", seen_rows)
    conn.commit()
    for pid,h,_t in key_rows:
        _SEEN_PROXY_KEYS.add((int(pid), h))
    return len(urls)

def is_message_processed(profile_id, source, message_id):
    r = c.execute("SELECT 1 FROM processed_messages WHERE source=? AND message_id=? AND profile_id=?", (source, message_id, profile_id)).fetchone()
    return r is not None

def mark_message_processed(profile_id, source, message_id):
    c.execute("INSERT OR REPLACE INTO processed_messages (source, message_id, profile_id) VALUES (?,?,?)",
              (source, message_id, profile_id))
    conn.commit()

def get_last_scrape_time(profile_id, source):
    r = c.execute("SELECT last_scrape_time FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else None

def update_last_scrape_time(profile_id, source, time_str, last_message_id=""):
    c.execute("INSERT OR REPLACE INTO last_scrape (source, last_scrape_time, profile_id, last_message_id) VALUES (?,?,?,?)",
              (source, time_str, profile_id, last_message_id))
    conn.commit()

def get_last_message_id(profile_id, source):
    r = c.execute("SELECT last_message_id FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else ""

def update_last_message_id(profile_id, source, msg_id):
    c.execute("UPDATE last_scrape SET last_message_id=? WHERE source=? AND profile_id=?", (msg_id, source, profile_id))
    conn.commit()

def get_stream_last_message_id(profile_id, source, stream):
    r = c.execute(
        "SELECT last_message_id FROM source_stream_state WHERE profile_id=? AND source=? AND stream=?",
        (profile_id, source, stream)
    ).fetchone()
    return r[0] if r else ""

def set_stream_last_message_ids_batch(profile_id, stream, updates):
    rows = [(int(profile_id), str(src), str(stream), str(msg_id or ""), get_tehran_time())
            for src, msg_id in updates if msg_id]
    if not rows:
        return
    c.executemany("""INSERT INTO source_stream_state
        (profile_id,source,stream,last_message_id,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(profile_id,source,stream) DO UPDATE SET
        last_message_id=excluded.last_message_id, updated_at=excluded.updated_at""", rows)
    conn.commit()

def set_stream_last_message_id(profile_id, source, stream, msg_id):
    c.execute(
        """INSERT INTO source_stream_state
           (profile_id, source, stream, last_message_id, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(profile_id, source, stream) DO UPDATE SET
             last_message_id=excluded.last_message_id,
             updated_at=excluded.updated_at""",
        (profile_id, source, stream, str(msg_id or ""), get_tehran_time())
    )
    conn.commit()

def strip_url_fragment(url):
    if '#' in url:
        return url.split('#')[0]
    return url

def extract_host(url):
    """
    Extract ONLY the actual target host used by Check-Host.
    VMess is special: its payload is Base64(JSON), so it must be decoded first
    and the JSON `add` field is used as the host. Other supported URI schemes
    use their URI hostname (or the Telegram proxy `server` parameter).
    """
    try:
        raw_url = html.unescape(str(url or "")).strip()
        if not raw_url:
            return None, None

        # VMess: decode the payload first; never try to parse the Base64 text as
        # a URI hostname.
        if raw_url.lower().startswith("vmess://"):
            payload = raw_url.split("://", 1)[1].split("#", 1)[0].strip()
            try:
                decoded = _decode_b64(payload).decode("utf-8", errors="strict")
                obj = json.loads(decoded)
                host = str(obj.get("add") or obj.get("address") or "").strip()
                port_raw = obj.get("port")
                try:
                    port = int(port_raw) if port_raw not in (None, "") else None
                except (TypeError, ValueError):
                    port = None
                if host:
                    return host, port
            except Exception as e:
                log.debug("VMESS host extraction failed: %s", e)
                return None, None

        parsed = urlparse(raw_url)
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            port = None

        # Telegram MTProto/SOCKS proxy links keep the actual server in
        # ?server=... rather than in the URI hostname.
        if parsed.scheme.lower() in ("https", "http", "tg") and (
            parsed.path.lower().startswith("/proxy") or parsed.netloc.lower() == "proxy"
        ):
            query = parse_qs(parsed.query)
            server = (query.get("server") or [None])[0]
            if server:
                server = str(server).strip()
                if server.startswith("[") and "]" in server:
                    close = server.find("]")
                    host = server[1:close]
                    remainder = server[close + 1:]
                    if remainder.startswith(":") and remainder[1:].isdigit():
                        port = int(remainder[1:])
                elif server.count(":") == 1:
                    maybe_host, maybe_port = server.rsplit(":", 1)
                    if maybe_port.isdigit():
                        host, port = maybe_host, int(maybe_port)
                    else:
                        host = server
                else:
                    host = server
                if port is None:
                    port_raw = (query.get("port") or [None])[0]
                    if str(port_raw or "").isdigit():
                        port = int(port_raw)
                return host, port
            return host, port

        if host:
            return host, port

        # Conservative fallback for legacy/non-standard forms.
        candidate = raw_url
        if "://" in candidate:
            candidate = candidate.split("://", 1)[1]
        candidate = candidate.split("?", 1)[0].split("#", 1)[0]
        if "@" in candidate:
            candidate = candidate.rsplit("@", 1)[-1]
        candidate = candidate.strip()

        if candidate.startswith("[") and "]" in candidate:
            close = candidate.find("]")
            host = candidate[1:close]
            rest = candidate[close + 1:]
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
            return host, port

        if ":" in candidate:
            maybe_host, maybe_port = candidate.rsplit(":", 1)
            if maybe_port.isdigit():
                return maybe_host.strip(), int(maybe_port)

        return candidate or None, None
    except Exception as e:
        log.warning(f"extract_host error for {url}: {e}")
        return None, None

def add_custom_query_to_url(url, custom_query, protocol):
    if not custom_query or protocol.lower() == 'vmess':
        return url
    url = clean_config_url(url)
    if '#' in url:
        base, fragment = url.split('#', 1)
    else:
        base = url
        fragment = None
    parsed = urlparse(base)
    existing_params = parse_qs(parsed.query)
    custom_params = parse_qs(custom_query)
    new_query_dict = {}
    for k, v in custom_params.items():
        new_query_dict[k] = v[-1] if v else ""
    for k, v in existing_params.items():
        if k not in new_query_dict:
            new_query_dict[k] = v[-1] if v else ""
    new_query = urlencode(new_query_dict, doseq=True)
    new_base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ''))
    if fragment:
        new_base += '#' + fragment
    return new_base

# ======================================================================
# پینگ (بهینه‌شده) - با لایه‌های تست
# ======================================================================
_DNS_CACHE = {}
_DNS_CACHE_TTL = 300
_PING_CLIENT = None
_PING_CLIENT_LOCK = asyncio.Lock()

async def _get_ping_client():
    global _PING_CLIENT
    if _PING_CLIENT is None or _PING_CLIENT.is_closed:
        async with _PING_CLIENT_LOCK:
            if _PING_CLIENT is None or _PING_CLIENT.is_closed:
                _PING_CLIENT = httpx.AsyncClient(
                    timeout=httpx.Timeout(2.5, connect=1.5),
                    limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
    return _PING_CLIENT

async def _close_ping_client():
    global _PING_CLIENT
    client = _PING_CLIENT
    _PING_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()

async def host_to_ip(host):
    host = (host or "").strip().lower()
    if not host:
        return None
    cached = _DNS_CACHE.get(host)
    now = time.monotonic()
    if cached and now - cached[0] < _DNS_CACHE_TTL:
        return cached[1]
    try:
        ip = await asyncio.to_thread(socket.gethostbyname, host)
        _DNS_CACHE[host] = (now, ip)
        return ip
    except Exception as e:
        log.debug(f"DNS resolution failed for {host}: {e}")
        return None

_PING_TARGET_IP_CACHE = {}
_PING_TARGET_IP_CACHE_TTL = 900

async def ping_from_iran_only(host, port=None, allow_tcp_fallback=False):
    """
    Check ONLY the extracted target host with Check-Host's normal Ping page/API.

    No TCP fallback, no HTTP test, no DNS test, and no manual node selection.
    Check-Host chooses its normal set of checking locations; we inspect ONLY
    the rows whose location country is Iran.

    Publish rule requested by the user:
      - at least one Iranian row must be 3/4 or 4/4 successful ICMP packets;
      - other countries do not matter at all;
      - 0/4, 1/4, 2/4, stuck, missing, or no Iranian result => FAIL.
    """
    target = str(host or "").strip()
    if not target:
        return 0, False, 0

    try:
        cl = await _get_ping_client()

        # Deliberately do NOT pass node=... parameters. This keeps the test
        # identical to Check-Host's normal web result page, including all
        # available countries. We only filter the returned rows to Iran.
        r = await cl.get(
            "https://check-host.net/check-ping",
            params={"host": target},
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            log.warning("check-host.net ping submit status: %s", r.status_code)
            return 0, False, 0

        try:
            created = r.json()
        except json.JSONDecodeError:
            log.warning("⚠️ check-host.net ping submit returned invalid JSON for %s", target)
            return 0, False, 0

        if not isinstance(created, dict) or not created.get("ok") or not created.get("request_id"):
            log.warning("⚠️ check-host.net ping submit failed for %s: %s", target, created)
            return 0, False, 0

        node_meta = created.get("nodes") or {}
        iran_nodes = []
        for node_name, info in node_meta.items():
            if not isinstance(info, list) or not info:
                continue
            country_code = str(info[0] or "").strip().lower()
            if country_code == "ir":
                iran_nodes.append(str(node_name))

        if not iran_nodes:
            log.warning("⚠️ Check-Host returned no Iranian rows for %s", target)
            return 0, False, 0

        request_id = str(created["request_id"])
        result_url = f"https://check-host.net/check-result/{quote(request_id, safe='')}"
        deadline = time.monotonic() + 10.0
        last_result = None

        while time.monotonic() < deadline:
            rr = await cl.get(result_url, headers={"Accept": "application/json"})
            if rr.status_code == 200:
                try:
                    result = rr.json()
                except json.JSONDecodeError:
                    result = None

                if isinstance(result, dict):
                    last_result = result
                    any_iran_pending = False
                    any_iran_result = False

                    for node_name in iran_nodes:
                        node_result = result.get(node_name)
                        if node_result is None:
                            any_iran_pending = True
                            continue

                        any_iran_result = True
                        packets = None
                        if isinstance(node_result, list) and node_result:
                            first = node_result[0]
                            if isinstance(first, list):
                                packets = first

                        if not isinstance(packets, list) or not packets:
                            continue

                        ok_times = []
                        resolved_ip = None
                        for packet in packets:
                            if not isinstance(packet, list) or not packet:
                                continue
                            status = str(packet[0] or "").upper()
                            if status == "OK":
                                try:
                                    seconds = float(packet[1])
                                    ok_times.append(seconds)
                                except (IndexError, TypeError, ValueError):
                                    pass
                                if len(packet) >= 3 and packet[2]:
                                    resolved_ip = str(packet[2]).strip()

                        ok_count = len(ok_times)

                        # The user's required threshold is based on the
                        # packet count shown by Check-Host: 3/4 or 4/4.
                        if ok_count >= 3:
                            if resolved_ip:
                                _PING_TARGET_IP_CACHE[target.lower()] = (
                                    time.monotonic(), resolved_ip
                                )
                            avg_ms = int(round(sum(ok_times) / ok_count * 1000)) if ok_times else 0
                            log.info(
                                "✅ Iran Check-Host PASS %d/4 for %s (%s) -> avg %sms",
                                min(ok_count, 4), target, node_name, avg_ms
                            )
                            return avg_ms, True, min(ok_count, 4)

                    # If every Iranian row has already produced a concrete
                    # result and none reached 3/4, there is no reason to wait.
                    if any_iran_result and not any_iran_pending:
                        log.info("❌ Iran Check-Host FAIL for %s: no Iran row reached 3/4", target)
                        return 0, False, 0

            await asyncio.sleep(0.65)

        log.warning("⏳ Check-Host ping stuck/incomplete for %s", target)
        return 0, False, 0

    except Exception as e:
        log.warning("check-host.net ping request failed for %s: %s", target, e)
        return 0, False, 0

async def check_full_link_ping(url, ping_mode="global", perform_ping=True):
    """When ping testing is enabled, use ONLY Check-Host Ping on the real host."""
    if not perform_ping:
        return 0, True, 0

    host, _port = extract_host(url)
    if not host:
        return 0, False, 0

    # ping_mode is retained only for compatibility with existing profile
    # settings. It no longer selects a different test method.
    return await asyncio.wait_for(
        ping_from_iran_only(host, allow_tcp_fallback=False),
        timeout=12.0,
    )

# ======================================================================
# اسکرپ (بهینه‌شده: استفاده از last_message_id برای توقف)
# ======================================================================
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

_SCRAPE_CLIENT = None
_SCRAPE_CLIENT_LOCK = asyncio.Lock()

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

# ======================================================================
# ثبت پیام‌های واقعی کانال + حذف جدیدترین پست‌های هر پروفایل
# ======================================================================
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

# ======================================================================
# ارسال (حالت‌های عادی دست‌نخورده)
# ======================================================================
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

# ======================================================================
# ارسال کانفیگ‌ها و پروکسی‌ها (با بهبودهای جدید)
# ======================================================================
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

    html_messages = [full_text]
    message_entry_batches = [list(posted_entries)]
    if config_mode == 1 and len(full_text) > 4096:
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
        for line, entry in zip(config_blocks, posted_entries):
            candidate = batch + [line]
            quote_block = "<blockquote expandable><code>" + "\n".join(candidate) + "</code></blockquote>"
            if batch and len(before + quote_block + after) > 4096:
                quote_block = "<blockquote expandable><code>" + "\n".join(batch) + "</code></blockquote>"
                html_messages.append(before + quote_block + after)
                message_entry_batches.append(list(entry_batch))
                batch = [line]
                entry_batch = [entry]
            else:
                batch = candidate
                entry_batch.append(entry)
        if batch:
            quote_block = "<blockquote expandable><code>" + "\n".join(batch) + "</code></blockquote>"
            html_messages.append(before + quote_block + after)
            message_entry_batches.append(list(entry_batch))

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

# ======================================================================
# چرخه اصلی (با بهبود پروکسی و تست)
# ======================================================================
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
        # OFF means OFF: no persistent batch queue is allowed to participate in
        # automatic posting. Clear only transient batch state; profiles/settings
        # and permanent dedup ledgers are never touched.
        try:
            _pending_batch_clear(profile_id, "config")
            _pending_batch_clear(profile_id, "proxy")
            conn.commit()
        except Exception:
            log.exception(f"[BATCH][profile={profile_id}] failed clearing disabled batch state")
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
        sem=asyncio.Semaphore(24 if low_cost_mode else 60)

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
                rs = await asyncio.gather(*[_check(item) for item in chunk], return_exceptions=True)
                for item, r in zip(chunk, rs):
                    identity_hash = _config_identity_hash(item[0])
                    _batch_mark_tested(profile_id, "config", identity_hash)
                    if not isinstance(r, Exception) and r[1]:
                        _pending_batch_upsert(profile_id, "config", identity_hash, r[0], r[4], r[2], r[3])
                        working.append((r[0], r[2], r[3]))
                conn.commit()
                log.info(f"📦 [AUTO-CONFIG] strict queue tested={len(chunk)}/{len(fresh)} pending={len(working)} target={desired}")
        else:
            # Normal mode: when ping testing is OFF, do not spend time in the
            # health-test pipeline. Any structurally valid, new config is
            # publishable and at least one must be selected when available.
            if not ping_testing:
                working = [(u, 0, 0) for u, _src in new_configs[:desired]]
                log.info(
                    f"📊 Ping testing OFF for profile={profile_id}; "
                    f"publishing {len(working)} new configs without health filtering"
                )
            else:
                # Do not stop after only desired*2 candidates. A temporary
                # outage in the first few sources must not make the profile
                # look broken while hundreds of later candidates are available.
                # Test a bounded but sufficiently large window and stop only
                # after collecting the requested number of healthy configs.
                test_limit = min(len(new_configs), max(60, min(240, desired * 30)))
                to_test = new_configs[:test_limit]
                log.info(f"📊 Testing {len(to_test)} configs... low_cost={low_cost_mode}")
                rs = await asyncio.gather(*[_check(item) for item in to_test], return_exceptions=True)
                for r in rs:
                    if not isinstance(r, Exception) and r[1]:
                        working.append((r[0], r[2], r[3]))
                        if len(working) >= desired:
                            break
                log.info(f"📊 Working configs: {len(working)}")
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
            sem = asyncio.Semaphore(24 if low_cost_mode else 60)

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
                    results = await asyncio.gather(*[check_proxy(p) for p in chunk], return_exceptions=True)
                    for p, r in zip(chunk, results):
                        identity_hash = _proxy_identity_hash(p)
                        _batch_mark_tested(profile_id, "proxy", identity_hash)
                        if not isinstance(r, Exception) and (not ping_testing or r[1] > 0):
                            _pending_batch_upsert(profile_id, "proxy", identity_hash, r[0], "", r[1], 0, r[2], r[3])
                            proxy_with_ping.append(r)
                    conn.commit()
                    log.info(f"📦 [AUTO-PROXY] strict queue tested={len(chunk)}/{len(fresh)} pending={len(proxy_with_ping)} target={desired_proxy}")
            else:
                results = await asyncio.gather(*[check_proxy(p) for p in valid_proxies], return_exceptions=True)
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
    config_ok = (not enable_configs) or (not new_configs) or (total_configs >= len(new_configs))
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

# One cycle at a time per profile. Automatic and manual triggers share this
# lock, so two callers can never publish the same candidate concurrently.
# Automatic locks are per profile + stream. Config and proxy of the same
# profile must never block each other. Combined/manual lanes use their own key.
_PROFILE_CYCLE_LOCKS = {}
# Manual queue is completely isolated from automatic cycles.
_MANUAL_QUEUE_LOCKS = {}
_MANUAL_RUN_LOCKS = {}
# Callback handlers may be invoked before any worker has been created; keep the
# registry defined at module load time so run-now can never raise NameError.
_MANUAL_RUN_TASKS = {}

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

# ======================================================================
# حلقه‌های خودکار (با مدیریت بهتر)
# ======================================================================
# Task registry to prevent duplicate loops
_active_tasks = {}  # profile_id -> dict of tasks

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


# ======================================================================
# stable scheduler
# - fixed interval scheduling using monotonic next_run timestamps
# - auto loops are independent from manual queue worker
# - slow source scan no longer shifts the next execution window
# ======================================================================

_auto_next_runs = {}

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
                # First automatic run is immediate. Future runs use a fixed
                # monotonic cadence independent of network duration.
                next_deadline = loop.time()

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

# Scheduler entrypoints are defined once above.

# ======================================================================
# بک‌آپ خودکار، گزارش روزانه، Railway و پاکسازی (بدون تغییر)
# ======================================================================
backup_locks = {}

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
            total = c.execute(
                "SELECT COUNT(*) FROM seen WHERE profile_id=? AND full_url != '' AND backup_num > 0",
                (profile_id,)).fetchone()[0]
            last_backup = get_profile_last_backup_count(profile_id)

            last_backup_block = last_backup // backup_interval if backup_interval > 0 else 0
            current_block = total // backup_interval if backup_interval > 0 else 0

            if current_block <= last_backup_block:
                return

            for block in range(last_backup_block + 1, current_block + 1):
                start_num = (block - 1) * backup_interval + 1
                end_num = block * backup_interval

                rows = c.execute(
                    "SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' AND backup_num BETWEEN ? AND ? ORDER BY backup_num",
                    (profile_id, start_num, end_num)
                ).fetchall()
                links = [r[0] for r in rows if r[0]]
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

BOT_START_TIME = datetime.now(TEHRAN_TZ)

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

_LOG_PRUNE_LOCK = threading.Lock()

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

# ======================================================================
# مدیریت ادمین و زبان (بدون تغییر)
# ======================================================================
def is_admin(user_id: int) -> bool:
    if user_id == MAIN_ADMIN_ID:
        return True
    row = c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def add_admin(user_id: int, added_by: int):
    c.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?,?,?)",
              (user_id, added_by, get_tehran_time()))
    conn.commit()

def remove_admin(user_id: int):
    if user_id == MAIN_ADMIN_ID:
        return False
    c.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    return True

def list_admins():
    rows = c.execute("SELECT user_id, added_by, added_at FROM admins ORDER BY added_at").fetchall()
    admins = []
    for row in rows:
        admins.append({"user_id": row[0], "added_by": row[1], "added_at": row[2]})
    return admins

def get_lang() -> str:
    row = c.execute("SELECT v FROM cfg WHERE k='lang'").fetchone()
    if row and row[0] in ['fa', 'en']:
        return row[0]
    return 'fa'

def set_lang(lang: str):
    if lang not in ['fa', 'en']:
        return
    c.execute("INSERT OR REPLACE INTO cfg (k, v) VALUES ('lang', ?)", (lang,))
    conn.commit()

# ======================================================================
# کیبوردها و پیام‌ها (با اضافه شدن دکمه‌های جدید)
# ======================================================================
BOT_REF = None

T = {
    "fa": {
        "welcome": "🤖 **بات جمع‌آوری کانفیگ و پروکسی**\n\n"
                  "📡 تعداد پروفایل‌ها: {profiles}\n"
                  "🔢 بعدی: #{next_n}\n"
                  "💰 اعتبار: {credit}",
        "admin_panel": "🔐 **پنل مدیریت پروفایل**\n\n"
                       "📡 منابع: {srcs} | 🎯 مقصد: {dest}\n"
                       "🎨 نام: {name} | 🔢 #{num}\n"
                       "⏰ بازه کانفیگ: {cfg_interval}m | بازه پروکسی: {prx_interval}m\n"
                       "📊 حداکثر کانفیگ: {max_cfg} | حداکثر پروکسی: {max_prx}\n"
                       "📢 اسپانسر: {sponsor}\n"
                       "🌍 حالت پینگ: {ping_mode}\n"
                       "📡 کانفیگ: {cfg_status} | 🌐 پروکسی: {prx_status}\n"
                       "🔢 شماره‌گذاری: {numbers_status}\n"
                       "🔗 کوئری سفارشی: {custom_query}\n"
                       "📅 تاریخ کانفیگ: {date_cfg}\n"
                       "📅 تاریخ پروکسی: {date_prx}\n"
                       "⏰ کرون: {cron}\n"
                       "⏱️ تایمر: {timer_status}\n"
                       "📦 بک‌آپ هر {backup_interval} عدد\n"
                       "🏷️ قالب نام: {naming}\n"
                       "🔗 لینک کانال: {channel_link}\n"
                       "🌍 پینگ: {ping_status}\n"
                       "🔘 وضعیت: {profile_status}\n"
                       "🌐 کشور: {country_display}\n"
                       "🧩 عنوان کانفیگ: {config_header_status} | ⚡ کم‌مصرف: {low_cost_status}\n",
        "general_settings": "⚙️ **تنظیمات عمومی**\n\n"
                            "زبان فعلی: {lang}\n"
                            "تعداد ادمین‌ها: {admins_count}",
        "btn_back": "🔙 برگشت",
        "btn_add_source": "➕ منبع",
        "btn_add_dest": "➕ مقصد جدید",
        "btn_dest_list": "📋 مقصدها",
        "btn_sponsors": "📢 اسپانسر",
        "btn_set_dest": "🎯 تنظیم مقصد",
        "btn_set_name": "🎨 نام",
        "btn_set_banner": "📝 بنر",
        "btn_set_banner_config": "📝 بنر کانفیگ",
        "btn_set_banner_proxy": "📝 بنر پروکسی",
        "btn_set_time": "⏰ زمان‌بندی",
        "btn_set_max": "🎯 حداکثر پست",
        "btn_stats": "📊 آمار",
        "btn_test": "🧪 تست",
        "btn_clear": "🗑 پاک DB",
        "btn_reset": "🔢 ریست شماره",
        "btn_ping_mode": "🌍 ایران‌فقط",
        "btn_runnow": "▶️ اجرا کن",
        "btn_instant": "⚡ اپدیت لحظه‌ای",
        "btn_manual_send": "📤 ارسال دستی",
        "btn_manage_sources": "📡 مدیریت منابع",
        "btn_toggle_numbers": "🔢 شماره‌گذاری: {status}",
        "btn_set_custom_query": "🔗 تنظیم کوئری سفارشی",
        "btn_empty": "🧹 خالی کردن",
        "send_prompt": "📝 نام کانال (با @ یا بدون):",
        "added": "✅ {item}",
        "removed": "✅ حذف شد",
        "test_ok": "✅ به {dest} ارسال شد",
        "test_err": "❌ خطا:\n<code>{err}</code>",
        "no_pings": "❌ پینگ نداد",
        "clear_q1": "⚠️ پاک کنم؟ (۱/۲)\n⛔ غیرقابل برگشت",
        "dest_set": "✅ مقصد: {dest}",
        "name_set": "✅ نام: {name}",
        "banner_ok": "✅ بنر ذخیره شد",
        "banner_err": "❌ باید {configs} یا {proxies} داشته باشه",
        "interval_ok": "✅ هر {n} دقیقه",
        "interval_err": "❌ ۱ تا ۱۴۴۰ دقیقه (۰ برای لحظه‌ای)",
        "interval_wrong": "❌ فقط عدد",
        "max_ok": "✅ حداکثر {n}",
        "max_err": "❌ ۱ تا ۵۰",
        "src_title": "📡 منابع ({n}):",
        "src_none": "خالی",
        "reset_ok": "✅ ریست شد (#۱)",
        "sp_prompt": "📢 اسپانسر:\nفرمت: نام|url|متن دکمه|رنگ\nرنگ‌ها: primary (آبی), success (سبز), danger (قرمز)",
        "sp_added": "✅ '{name}' اضافه شد",
        "sp_removed": "✅ حذف شد",
        "sp_title": "📢 اسپانسر:",
        "sp_none": "خالی",
        "sp_err": "❌ فرمت: name|url|text|color (primary/success/danger)",
        "doc_select": "این فایل از کدوم منبعه؟",
        "doc_no_src": "❌ منبعی نیست، اول اضافه کن",
        "doc_decoding": "🔐 رمزگشایی...",
        "doc_no_pw": "❌ هیچ رمزی جواب نداد",
        "doc_no_links": "❌ لینکی پیدا نشد",
        "doc_done": "🎉 {n} کانفیگ و {p} پروکسی پست شد",
        "doc_dup": "همه تکراری بودن",
        "no_sources": "❌ هیچ منبعی تنظیم نشده",
        "test_link_prompt": "🔗 لینک کانفیگ رو بفرست (مثل vless:// یا vmess://)",
        "btn_toggle_configs": "📡 کانفیگ: {status}",
        "btn_toggle_proxies": "🌐 پروکسی: {status}",
        "toggle_configs": "✅ ارسال کانفیگ {'فعال' if status else 'غیرفعال'} شد",
        "toggle_proxies": "✅ ارسال پروکسی {'فعال' if status else 'غیرفعال'} شد",
        "profile_list": "📋 **لیست پروفایل‌ها**\n\n{list}\n\nبرای مدیریت هر کدام کلیک کنید.",
        "profile_add_prompt": "📝 نام مقصد جدید را وارد کنید (با @ یا بدون):",
        "profile_added": "✅ پروفایل '{name}' ساخته شد.",
        "profile_deleted": "❌ پروفایل حذف شد.",
        "profile_not_found": "❌ پروفایل یافت نشد.",
        "manual_send_prompt": "📤 لطفاً پیام (متن یا فایل) حاوی لینک‌های کانفیگ/پروکسی را ارسال کنید.\n\n⏳ بات به‌طور خودکار تشخیص داده و با بنر مناسب ارسال می‌کند.\n\n⚠️ **توجه:** در حالت دستی، تست پینگ انجام نمی‌شود و همه لینک‌ها حتی اگر قبلاً پست شده باشند، دوباره ارسال می‌شوند.",
        "manual_send_cancel": "❌ ارسال دستی لغو شد.",
        "manual_send_processing": "⏳ در حال پردازش...",
        "manual_send_done": "✅ ارسال دستی کامل شد.",
        "custom_query_set": "✅ کوئری سفارشی تنظیم شد: {query}",
        "custom_query_prompt": "🔗 کوئری سفارشی را وارد کنید (مثلا Telegram=@MyChannel) یا دکمه خالی را بزنید:",
        "source_list": "📡 **منابع پروفایل {name}**\n\n{sources}\n\nبرای حذف هر کدام روی دکمه مربوطه کلیک کنید.",
        "source_deleted": "✅ منبع حذف شد.",
        "toggle_numbers_ok": "✅ شماره‌گذاری {'فعال' if status else 'غیرفعال'} شد.",
        "date_cfg_toggle": "✅ نمایش تاریخ در بنر کانفیگ {'فعال' if status else 'غیرفعال'} شد.",
        "date_prx_toggle": "✅ نمایش تاریخ در بنر پروکسی {'فعال' if status else 'غیرفعال'} شد.",
        "sp_edit_prompt": "📢 **ویرایش اسپانسر**\n\nنام: {name}\nلینک: {url}\nمتن: {text}\nرنگ: {color}\nوضعیت: {'فعال' if enabled else 'غیرفعال'}\n\nبرای ویرایش هر بخش، دکمه مربوطه را بزنید.",
        "sp_edit_name": "نام جدید (خالی برای عدم تغییر):",
        "sp_edit_url": "لینک جدید (خالی برای عدم تغییر):",
        "sp_edit_text": "متن جدید دکمه (خالی برای عدم تغییر):",
        "sp_edit_color": "رنگ جدید (primary/success/danger) یا خالی برای عدم تغییر:",
        "sp_updated": "✅ اسپانسر به‌روزرسانی شد.",
        "btn_edit_sponsor": "✏️ ویرایش",
        "delete_confirm1": "⚠️ **آیا مطمئن هستید که می‌خواهید این پروفایل را حذف کنید؟**\n\nنام: {name}\nشناسه: {id}\n\nاین عملیات غیرقابل برگشت است و تمام داده‌های مربوط به این پروفایل (منابع، اسپانسرها، تاریخچه) پاک می‌شود.\n\nبرای تأیید، دکمه **«بله، حذف شود»** را بزنید.",
        "delete_confirm2": "⚠️ **تأیید نهایی حذف پروفایل**\n\nنام: {name}\nشناسه: {id}\n\n**آیا از حذف این پروفایل اطمینان دارید؟**\n\nبرای حذف نهایی، دکمه **«حذف نهایی»** را بزنید.",
        "delete_cancelled": "❌ حذف پروفایل لغو شد.",
        "btn_blacklist": "🚫 مدیریت لیست سیاه",
        "blacklist_title": "🚫 **لیست سیاه پروفایل {name}**\n\nکلمات ممنوعه:\n{words}\n\nهر کانفیگی که شامل این کلمات باشد، پست نمی‌شود.",
        "blacklist_empty": "هیچ کلمه‌ای در لیست سیاه نیست.",
        "blacklist_add_prompt": "📝 کلمه یا عبارت ممنوع را وارد کنید (چند مورد با کاما یا خط جدید):",
        "blacklist_added": "✅ کلمات اضافه شدند: {words}",
        "blacklist_removed": "✅ کلمه حذف شد.",
        "blacklist_clear": "✅ لیست سیاه پاک شد.",
        "btn_blacklist_add": "➕ افزودن",
        "btn_blacklist_clear": "🗑 پاک کردن همه",
        "btn_backup": "💾 بک‌آپ دیتابیس",
        "btn_replace_database": "🔄 جایگزینی کامل دیتابیس",
        "backup_sent": "✅ فایل دیتابیس ارسال شد.",
        "backup_failed": "❌ ارسال بک‌آپ ناموفق.",
        "database_replace_prompt": "🔄 <b>جایگزینی کامل دیتابیس</b>\n\nفایل دیتابیس قدیمی را همینجا ارسال کنید. پسوند فایل مهم نیست؛ محتوای فایل تشخیص داده می‌شود.\n\nپشتیبانی: SQLite با هر پسوند + SQL Dump.\n⚠️ قبل از جایگزینی، از دیتابیس فعلی بک‌آپ گرفته می‌شود.",
        "database_replace_confirm": "⚠️ <b>هشدار مهم</b>\n\nبا تأیید، کل دیتابیس فعلی با فایل انتخاب‌شده جایگزین می‌شود و اطلاعات فعلی دیگر دیتابیس فعال نخواهد بود.\n\nابتدا بک‌آپ خودکار گرفته می‌شود. ادامه می‌دهید؟",
        "btn_set_schedule_cron": "⏰ زمان‌بندی پیشرفته (cron)",
        "schedule_cron_prompt": "⏰ عبارت cron را وارد کنید (مثلاً `*/5 * * * *` برای هر ۵ دقیقه).\n\nخالی بگذارید تا از بازه‌ی دقیقه‌ای استفاده شود.",
        "schedule_cron_set": "✅ زمان‌بندی cron تنظیم شد: {cron}",
        "btn_backup_export": "📤 بک‌آپ کانفیگ/پروکسی",
        "backup_export_type": "📤 **بک‌آپ**\n\nکدام نوع را می‌خواهید؟",
        "backup_export_scope": "📤 **محدوده**\n\nهمه، ۱۰۰ تای آخر، یا تعداد دلخواه؟",
        "backup_export_count_prompt": "🔢 تعداد دلخواه را وارد کنید (عدد):",
        "backup_export_scope_all": "همه",
        "backup_export_scope_100": "۱۰۰ تای آخر",
        "backup_export_scope_custom": "تعداد دلخواه",
        "btn_timer": "⏱️ تایمر",
        "timer_menu": "⏱️ **مدیریت تایمر پروفایل {name}**\n\nوضعیت فعلی: {status}\n\nمدت زمان مکث قبل از شروع خودکار را انتخاب کنید.",
        "timer_set": "✅ تایمر {minutes} دقیقه‌ای تنظیم شد. ارسال خودکار تا پایان تایمر متوقف می‌شود.",
        "timer_cleared": "✅ تایمر لغو شد.",
        "timer_expired_notify": "⏰ تایمر پروفایل {name} (ID: {id}) به پایان رسید. ارسال خودکار از سر گرفته شد.",
        "timer_option_30m": "⏱️ ۳۰ دقیقه",
        "timer_option_1h": "⏱️ ۱ ساعت",
        "timer_option_2h": "⏱️ ۲ ساعت",
        "timer_option_4h": "⏱️ ۴ ساعت",
        "timer_option_8h": "⏱️ ۸ ساعت",
        "timer_option_custom": "⏱️ سفارشی",
        "timer_disable": "⛔ غیرفعال",
        "timer_custom_prompt": "⏱️ تعداد دقیقه را وارد کنید:",
        "timer_status_active": "⏳ {remaining} دقیقه باقی‌مانده",
        "timer_status_inactive": "غیرفعال",
        "btn_log_menu": "📋 دریافت لاگ",
        "log_menu_title": "📋 **دریافت لاگ**\n\nنوع لاگ مورد نظر را انتخاب کنید:",
        "log_range_title": "📋 **دریافت لاگ {log_type}**\n\nبازه‌ی زمانی مورد نظر را انتخاب کنید:",
        "log_range_30m": "📅 ۳۰ دقیقه",
        "log_range_1h": "📅 ۱ ساعت",
        "log_range_6h": "📅 ۶ ساعت",
        "log_range_24h": "📅 ۲۴ ساعت",
        "btn_set_backup_interval": "📦 تنظیم بازه بک‌آپ",
        "backup_interval_prompt": "📦 تعداد کانفیگ در هر فایل بک‌آپ (پیش‌فرض ۱۰۰۰):",
        "backup_interval_set": "✅ بازه بک‌آپ به {n} عدد تنظیم شد.",
        "btn_balance": "💰 مانده اعتبار",
        "balance_info": "💰 **مانده اعتبار:** {balance}",
        "credit_low": "⚠️ اعتبار کمتر از {threshold} دلار است. بک‌آپ کامل دیتابیس ارسال شد.",
        "btn_general": "⚙️ تنظیمات عمومی",
        "btn_manage_profiles": "📋 مدیریت پروفایل‌ها",
        "btn_language": "🌐 زبان",
        "lang_changed": "✅ زبان به {lang} تغییر کرد.",
        "btn_admins": "👥 مدیریت ادمین‌ها",
        "admin_list": "👥 **لیست ادمین‌ها**\n\nادمین اصلی: {main}\n\nسایر ادمین‌ها:\n{admins}",
        "admin_add_prompt": "➕ شناسه عددی ادمین جدید را وارد کنید:",
        "admin_added": "✅ ادمین با شناسه {id} اضافه شد.",
        "admin_removed": "✅ ادمین با شناسه {id} حذف شد.",
        "admin_cannot_remove_main": "❌ نمی‌توانید ادمین اصلی را حذف کنید.",
        "btn_add_admin": "➕ افزودن ادمین",
        "btn_remove_admin": "❌ حذف ادمین",
        "btn_list_admins": "📋 لیست ادمین‌ها",
        "only_admin": "❌ فقط ادمین‌ها می‌توانند از این بات استفاده کنند.",
        "btn_set_naming_template": "🏷️ قالب نام‌گذاری",
        "naming_template_prompt": "🏷️ قالب نام‌گذاری را وارد کنید.\n\nمتغیرها: `{Protocol}`، `{Flag}`، `{COUNTRY_EN}`، `{COUNTRY_FA}`، `{CHANNEL_ID}`، `{COUNT}`\nفرمت `{TOKEN}` یا `[TOKEN]` هر دو قابل استفاده‌اند.\nمثال: `[Protocol] [Flag] [COUNTRY_EN] • [COUNTRY_FA]`",
        "naming_template_set": "✅ قالب نام‌گذاری تنظیم شد: {template}",
        "btn_set_channel_link": "🔗 لینک کانال",
        "channel_link_prompt": "🔗 لینک کانال را وارد کنید (مثلاً `MyChannel`):\n\nاین مقدار در قالب نام‌گذاری به جای `{CHANNEL_ID}` قرار می‌گیرد.\nاگر خالی بگذارید، از نام پروفایل استفاده می‌شود.",
        "channel_link_set": "✅ لینک کانال تنظیم شد: {link}",
        "btn_toggle_ping": "🌍 پینگ: {status}",
        "toggle_ping": "✅ پینگ {'فعال' if status else 'غیرفعال'} شد.",
        "btn_toggle_profile": "🔘 پروفایل: {status}",
        "toggle_profile": "✅ پروفایل {'فعال' if status else 'غیرفعال'} شد.",
        # New keys
        "btn_country_display": "🌐 کشور: {mode}",
        "country_display_off": "خاموش",
        "country_display_en": "انگلیسی",
        "country_display_enfa": "انگلیسی+فارسی",
        "country_display_set": "✅ نمایش کشور به {mode} تغییر کرد.",
        "btn_show_ping": "📡 نمایش پینگ: {status}",
        "show_ping_toggle": "✅ نمایش پینگ {'فعال' if status else 'غیرفعال'} شد.",
        "btn_sponsor_list": "📋 لیست اسپانسرها",
        "btn_add_sponsor": "➕ افزودن اسپانسر",
        "sponsor_list_title": "📋 **لیست اسپانسرهای پروفایل {name}**\n\n{sponsors}",
        "sponsor_list_empty": "هیچ اسپانسری تنظیم نشده است.",
        "sponsor_item": "• {name} (اولویت: {priority}) - {'فعال' if enabled else 'غیرفعال'}\n  لینک: {url}\n  دکمه: {text}\n  مدت: {duration}",
        "sponsor_detail": "📢 **جزئیات اسپانسر**\n\nنام: {name}\nلینک: {url}\nمتن دکمه: {text}\nاولویت: {priority}\nوضعیت: {'فعال' if enabled else 'غیرفعال'}\nمدت: {duration}\nرنگ: {color}",
        "sp_add_name": "نام اسپانسر را وارد کنید:",
        "sp_add_url": "لینک اسپانسر را وارد کنید (مثلاً https://example.com):",
        "sp_add_text": "متن دکمه را وارد کنید (پیش‌فرض 'Advertisement'):",
        "sp_add_priority": "اولویت را وارد کنید (عدد، بالاتر = اولویت بیشتر، پیش‌فرض 0):",
        "sp_add_duration": "مدت زمان بر حسب ساعت (۰ برای نامحدود):",
        "sp_add_unlimited": "آیا نامحدود باشد؟ (بله/خیر)",
        "sp_add_apply": "",
        "sp_add_color": "رنگ دکمه را انتخاب کنید:",
        "sp_added_done": "✅ اسپانسر '{name}' با موفقیت اضافه شد.",
        "sp_edit_list": "برای ویرایش، روی اسپانسر کلیک کنید:",
        "sp_edit_select": "لطفاً یک اسپانسر را از لیست انتخاب کنید.",
        "sp_edit_field_prompt": "مقدار جدید برای {field} را وارد کنید (خالی برای عدم تغییر):",
        "sp_edit_done": "✅ اسپانسر به‌روزرسانی شد.",
        "sp_delete_confirm": "⚠️ آیا از حذف اسپانسر '{name}' اطمینان دارید؟",
        "sp_deleted": "✅ اسپانسر حذف شد.",
        "btn_sponsor_edit": "✏️ ویرایش",
        "btn_sponsor_delete": "🗑 حذف",
        "btn_sponsor_toggle": "🔘 {status}",
        "btn_ping_testing": "📡 تست پینگ: {status}",
        "ping_testing_toggle": "✅ تست پینگ {'فعال' if status else 'غیرفعال'} شد.",
        "btn_channel_link_edit": "🔗 ویرایش لینک کانال",
        "btn_banner_config_edit": "📝 ویرایش بنر کانفیگ",
        "btn_banner_proxy_edit": "🌐 ویرایش بنر پروکسی",
    },
    "en": {
        # ... (simplified for brevity, but should be complete)
        "welcome": "🤖 **Config & Proxy Bot**\n\n"
                  "📡 Profiles: {profiles}\n"
                  "🔢 Next: #{next_n}\n"
                  "💰 Credit: {credit}",
        "admin_panel": "🔐 **Profile Management**\n\n"
                       "📡 Sources: {srcs} | 🎯 Dest: {dest}\n"
                       "🎨 Name: {name} | 🔢 #{num}\n"
                       "⏰ Config interval: {cfg_interval}m | Proxy interval: {prx_interval}m\n"
                       "📊 Max configs: {max_cfg} | Max proxies: {max_prx}\n"
                       "📢 Sponsor: {sponsor}\n"
                       "🌍 Ping mode: {ping_mode}\n"
                       "📡 Configs: {cfg_status} | 🌐 Proxies: {prx_status}\n"
                       "🔢 Numbering: {numbers_status}\n"
                       "🔗 Custom query: {custom_query}\n"
                       "📅 Date config: {date_cfg}\n"
                       "📅 Date proxy: {date_prx}\n"
                       "⏰ Cron: {cron}\n"
                       "⏱️ Timer: {timer_status}\n"
                       "📦 Backup every {backup_interval}\n"
                       "🏷️ Naming: {naming}\n"
                       "🔗 Channel link: {channel_link}\n"
                       "🌍 Ping: {ping_status}\n"
                       "🔘 Status: {profile_status}\n"
                       "🌐 Country: {country_display}\n"
        "🧩 Config title: {config_header_status} | ⚡ Low-cost: {low_cost_status}\n"
        # ... (other keys)
    }
}

def msg(key, **kwargs):
    lang = get_lang()
    text = T[lang].get(key, T["fa"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

# ======================================================================
# کیبوردها (با اضافه شدن دکمه‌های جدید)
# ======================================================================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_profiles"), callback_data="profiles_list", style="primary")],
        [InlineKeyboardButton(msg("btn_general"), callback_data="general_settings", style="primary")],
        [InlineKeyboardButton(msg("btn_balance"), callback_data="show_balance", style="primary")],
    ])

PROFILE_PAGE_SIZE = 20

def profiles_kb(page=1):
    profiles = get_profiles()
    try: page=max(1,int(page))
    except (TypeError,ValueError): page=1
    total_pages=max(1,(len(profiles)+PROFILE_PAGE_SIZE-1)//PROFILE_PAGE_SIZE)
    page=min(page,total_pages)
    btns=[]
    start=(page-1)*PROFILE_PAGE_SIZE
    for p in profiles[start:start+PROFILE_PAGE_SIZE]:
        status = "✅" if get_profile_enabled(p['id']) else "⛔"
        btns.append([InlineKeyboardButton(f"{status} {p['dest_name']} (ID:{p['id']})", callback_data=f"prof_{p['id']}", style="primary")])
    if total_pages>1:
        nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"profiles_page_{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page}/{total_pages}", callback_data="dummy", style="primary"))
        if page<total_pages: nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"profiles_page_{page+1}", style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton("➕ Add Profile", callback_data="prof_add", style="success")])
    btns.append([InlineKeyboardButton("📜 فعالیت ۵۰ عمل آخر", callback_data="activity_log", style="primary")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")])
    return InlineKeyboardMarkup(btns)



def protocol_settings_kb(profile_id):
    rows=[]
    rows.append([InlineKeyboardButton("📡 پروتکل‌های کانفیگ", callback_data=f"proto_cfg_{profile_id}", style="primary")])
    rows.append([InlineKeyboardButton("🌐 پروتکل‌های پروکسی", callback_data=f"proto_prx_{profile_id}", style="primary")])
    rows.append([InlineKeyboardButton("↩️ بازگشت", callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(rows)

def protocol_toggle_kb(profile_id, kind):
    protos = CONFIG_PROTOCOLS if kind == "cfg" else PROXY_PROTOCOLS
    rows=[]
    for proto in protos:
        enabled=is_protocol_enabled(profile_id, proto)
        rows.append([InlineKeyboardButton(f"{'✅' if enabled else '❌'} {proto}", callback_data=f"proto_toggle_{profile_id}_{kind}_{proto}", style="success" if enabled else "danger")])
    rows.append([InlineKeyboardButton("↩️ بازگشت", callback_data=f"proto_menu_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(rows)

def channel_delete_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 حذف ۱۰۰ پست آخر", callback_data=f"delposts_{profile_id}_100", style="danger")],
        [InlineKeyboardButton("🗑 حذف ۵۰۰ پست آخر", callback_data=f"delposts_{profile_id}_500", style="danger")],
        [InlineKeyboardButton("🗑 حذف ۱۰۰۰ پست آخر", callback_data=f"delposts_{profile_id}_1000", style="danger")],
        [InlineKeyboardButton("🔢 تعداد دلخواه", callback_data=f"delposts_custom_{profile_id}", style="primary")],
        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"prof_{profile_id}", style="primary")],
    ])

def channel_delete_confirm_kb(profile_id, count):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ تایید حذف {count} پست", callback_data=f"delconfirm_{profile_id}_{count}", style="danger")],
        [InlineKeyboardButton("❌ لغو", callback_data=f"delcancel_{profile_id}", style="primary")],
    ])


def profile_admin_kb(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return None
    ping_mode = prof["ping_mode"]
    ping_label = "🌍 ایران‌فقط" if ping_mode == "iran" else "🌍 جهانی"
    ping_testing = get_profile_ping_enabled(profile_id)
    ping_testing_label = "✅" if ping_testing else "❌"
    profile_enabled = get_profile_enabled(profile_id)
    profile_status = "✅" if profile_enabled else "❌"

    post_cfg = prof["post_configs"] == 1
    post_prx = prof["post_proxies"] == 1
    show_num = prof["show_numbers"] == 1
    show_date_cfg = prof["show_date_config"] == 1
    show_date_prx = prof["show_date_proxy"] == 1
    cfg_status = "✅" if post_cfg else "❌"
    prx_status = "✅" if post_prx else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"

    country_display = prof.get("country_display", 2)
    country_display_modes = {0: "خاموش", 1: "انگلیسی", 2: "انگلیسی+فارسی"}
    country_label = country_display_modes.get(country_display, "انگلیسی+فارسی")

    sponsors = get_sponsors(profile_id, include_disabled=True)
    sponsor_count = len(sponsors)
    sponsor_status = f"{sponsor_count} اسپانسر" if sponsor_count > 0 else "خالی"

    expiry, remaining = get_profile_timer(profile_id)
    if expiry:
        timer_status = msg("timer_status_active", remaining=remaining)
    else:
        timer_status = msg("timer_status_inactive")

    cfg_btn = msg("btn_toggle_configs", status=cfg_status)
    prx_btn = msg("btn_toggle_proxies", status=prx_status)
    num_btn = msg("btn_toggle_numbers", status=num_status)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_sources"), callback_data=f"src_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_dest_list"), callback_data=f"dl_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📢 اسپانسر: {sponsor_status}", callback_data=f"sponsor_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_name"), callback_data=f"ac_{profile_id}", style="primary")],
        [InlineKeyboardButton("⚙️ مدیریت پروتکل‌ها", callback_data=f"proto_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🧩 عنوان کانفیگ: {'✅ فعال' if prof.get('config_header_enabled', 1) else '❌ حذف'}", callback_data=f"tgl_cfg_header_{profile_id}", style="success" if prof.get('config_header_enabled', 1) else "danger"),
         InlineKeyboardButton(f"⚡ کم‌مصرف: {'✅ فعال' if prof.get('low_cost_mode', 1) else '❌ خاموش'}", callback_data=f"tgl_low_cost_{profile_id}", style="success" if prof.get('low_cost_mode', 1) else "danger")],
        [InlineKeyboardButton(f"🏷 قالب عنوان: {get_profile_config_header_template(profile_id)}", callback_data=f"cfg_header_tpl_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_banner_config"), callback_data=f"ab_config_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_banner_proxy"), callback_data=f"ab_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton("⏰ بازه کانفیگ", callback_data=f"set_cfg_interval_{profile_id}", style="primary"),
         InlineKeyboardButton("⏰ بازه پروکسی", callback_data=f"set_prx_interval_{profile_id}", style="primary")],
        [InlineKeyboardButton("📊 تعداد کانفیگ", callback_data=f"set_cfg_max_{profile_id}", style="primary"),
         InlineKeyboardButton("📊 تعداد پروکسی", callback_data=f"set_prx_max_{profile_id}", style="primary")],
        [InlineKeyboardButton(
            f"⚙️ حالت نمایش کانفیگ: {'نام کانال' if get_header_modes(profile_id)[0] == 'channel' else 'پروتکل'}",
            callback_data=f"hm_config_menu_{profile_id}", style="primary"
        ),
         InlineKeyboardButton(
            f"⚙️ حالت نمایش پروکسی: {'نام کانال' if get_header_modes(profile_id)[1] == 'channel' else 'پروتکل'}",
            callback_data=f"hm_proxy_menu_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton(f"🌐 حالت انتشار پروکسی: {'شیشه‌ای' if get_profile_proxy_post_mode(profile_id) == 1 else 'عادی'}", callback_data=f"tgl_prx_mode_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📡 تست Ping: {'✅' if ping_testing else '❌'}", callback_data=f"tgl_ping_test_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📦 تست و ارسال تجمیعی: {'✅ فعال' if get_profile_batch_posting(profile_id) else '❌ خاموش'}", callback_data=f"tgl_batch_post_{profile_id}", style="success" if get_profile_batch_posting(profile_id) else "danger")],
        [InlineKeyboardButton(
            f"🧩 حالت انتشار کانفیگ: {'عادی (COPY CODE)' if get_profile_config_post_mode(profile_id) == 0 else 'Quote جمع‌شونده'}  🔄",
            callback_data=f"tgl_cfg_post_mode_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton(f"👁 نمایش Ping: {'✅' if prof.get('show_ping', 1) else '❌'}", callback_data=f"tgl_show_ping_{profile_id}", style="primary"),
         InlineKeyboardButton("📍 تنظیم مناطق Ping", callback_data=f"ping_regions_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📍 Ping ویتوری: {'🇮🇷 ایران' if get_profile_config_ping_mode(profile_id) == 'iran' else '🌍 جهانی'}", callback_data=f"ping_region_config_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📍 Ping پروکسی: {'🇮🇷 ایران' if get_profile_proxy_ping_mode(profile_id) == 'iran' else '🌍 جهانی'}", callback_data=f"ping_region_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_toggle_profile", status=profile_status), callback_data=f"tgl_profile_{profile_id}", style="danger")],
        [InlineKeyboardButton(cfg_btn, callback_data=f"tglcfg_{profile_id}", style="primary"),
         InlineKeyboardButton(prx_btn, callback_data=f"tglproxy_{profile_id}", style="primary")],
        [InlineKeyboardButton(num_btn, callback_data=f"togglenum_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📅 تاریخ کانفیگ: {date_cfg_status}", callback_data=f"tgl_date_cfg_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📅 تاریخ پروکسی: {date_prx_status}", callback_data=f"tgl_date_prx_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_custom_query"), callback_data=f"setquery_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_stats"), callback_data=f"ast_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_backup_interval"), callback_data=f"setbackupinterval_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_test"), callback_data=f"sendtest_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_runnow"), callback_data=f"runnow_{profile_id}", style="success"),
         InlineKeyboardButton(msg("btn_instant"), callback_data=f"instant_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_manual_send"), callback_data=f"manual_{profile_id}", style="primary"),
         InlineKeyboardButton("📋 صف ارسال دستی", callback_data=f"mq_list_{profile_id}", style="primary")],
        [InlineKeyboardButton("🗑 حذف پست‌های کانال", callback_data=f"delposts_menu_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_blacklist"), callback_data=f"bl_list_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_set_schedule_cron"), callback_data=f"setcron_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_backup"), callback_data=f"backup_{profile_id}", style="success")],
        [InlineKeyboardButton(msg("btn_backup_export"), callback_data=f"backup_export_menu_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_timer"), callback_data=f"timer_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"⏱️ {timer_status}", callback_data="dummy", style="primary"),
         InlineKeyboardButton(msg("btn_log_menu"), callback_data=f"log_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton("🧪 دیباگ عمیق", callback_data="run_deep_debug", style="primary")],
        [InlineKeyboardButton(msg("btn_set_naming_template"), callback_data=f"set_naming_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_channel_link"), callback_data=f"set_channel_link_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🌐 کشور: {country_label}", callback_data=f"tgl_country_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_reset"), callback_data=f"rn_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_clear"), callback_data=f"cd1_{profile_id}", style="danger")],
        [InlineKeyboardButton("❌ Delete Profile", callback_data=f"delprof_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list", style="primary")],
    ])

def ping_regions_kb(profile_id):
    cfg = get_profile_config_ping_mode(profile_id)
    prx = get_profile_proxy_ping_mode(profile_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🧩 ویتوری: {'🇮🇷 ایران' if cfg == 'iran' else '🌍 جهانی'}", callback_data=f"ping_region_config_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🌐 پروکسی: {'🇮🇷 ایران' if prx == 'iran' else '🌍 جهانی'}", callback_data=f"ping_region_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton("🌍 هر دو = جهانی", callback_data=f"ping_region_all_global_{profile_id}", style="success")],
        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"prof_{profile_id}", style="primary")],
    ])

def ping_region_choice_kb(profile_id, kind):
    current = get_profile_config_ping_mode(profile_id) if kind == "config" else get_profile_proxy_ping_mode(profile_id)
    title = "ویتوری/کانفیگ" if kind == "config" else "پروکسی"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'◉' if current == 'global' else '○'} 🌍 جهانی", callback_data=f"set_ping_region_{kind}_global_{profile_id}", style="success" if current == "global" else "primary")],
        [InlineKeyboardButton(f"{'◉' if current == 'iran' else '○'} 🇮🇷 ایران", callback_data=f"set_ping_region_{kind}_iran_{profile_id}", style="success" if current == "iran" else "primary")],
        [InlineKeyboardButton("↩️ بازگشت به مناطق Ping", callback_data=f"ping_regions_{profile_id}", style="primary")],
    ])

def destinations_kb(profile_id):
    dest = get_profile_dest(profile_id)
    btns = []
    if dest:
        btns.append([InlineKeyboardButton(f"❌ {dest}", callback_data=f"dd_{profile_id}", style="danger")])
    btns.append([
        InlineKeyboardButton(msg("btn_add_dest"), callback_data=f"da_{profile_id}", style="success"),
    ])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def sponsor_list_kb(profile_id, page=1):
    sponsors=get_sponsors(profile_id,include_disabled=True); per_page=20
    try: page=max(1,int(page))
    except (TypeError,ValueError): page=1
    total_pages=max(1,(len(sponsors)+per_page-1)//per_page); page=min(page,total_pages); start=(page-1)*per_page
    btns=[]
    for sp in sponsors[start:start+per_page]:
        status="✅" if sp["enabled"] else "❌"
        btns.append([InlineKeyboardButton(f"{status} {sp['name']} (اولویت:{sp['priority']})",callback_data=f"sp_detail_{sp['id']}",style="primary")])
    if total_pages>1:
        nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️ قبلی",callback_data=f"sponsor_list_{profile_id}_{page-1}",style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page}/{total_pages}",callback_data="dummy",style="primary"))
        if page<total_pages: nav.append(InlineKeyboardButton("بعدی ▶️",callback_data=f"sponsor_list_{profile_id}_{page+1}",style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton("➕ افزودن اسپانسر",callback_data=f"sp_add_step_{profile_id}_name",style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"),callback_data=f"prof_{profile_id}",style="primary")])
    return InlineKeyboardMarkup(btns)

def sponsor_detail_kb(sponsor_id, profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"sp_edit_{sponsor_id}", style="success"),
         InlineKeyboardButton("🗑 حذف", callback_data=f"sp_delete_{sponsor_id}", style="danger")],
        [InlineKeyboardButton("🔘 فعال / غیرفعال", callback_data=f"sp_toggle_{sponsor_id}", style="primary")],
        [InlineKeyboardButton("🔙 برگشت", callback_data=f"sponsor_list_{profile_id}", style="primary")],
    ])

def sponsor_edit_kb(sponsor_id, profile_id):
    c.execute("SELECT enabled, unlimited FROM sponsors WHERE id=?", (sponsor_id,))
    row = c.fetchone()
    enabled = bool(row[0]) if row else False
    unlimited = bool(row[1]) if row else True
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ نام", callback_data=f"sp_edit_field_{sponsor_id}_name", style="primary"),
         InlineKeyboardButton("🔗 لینک", callback_data=f"sp_edit_field_{sponsor_id}_url", style="primary")],
        [InlineKeyboardButton("📝 متن دکمه", callback_data=f"sp_edit_field_{sponsor_id}_button_text", style="primary"),
         InlineKeyboardButton("🔢 اولویت", callback_data=f"sp_edit_field_{sponsor_id}_priority", style="primary")],
        [InlineKeyboardButton("⏱ مدت (ساعت)", callback_data=f"sp_edit_field_{sponsor_id}_duration_hours", style="primary")],
        [InlineKeyboardButton("♾ نامحدود" if not unlimited else "⏱ محدود کردن", callback_data=f"sp_toggle_unlimited_{sponsor_id}", style="success" if unlimited else "primary")],
        [InlineKeyboardButton("🎨 رنگ", callback_data=f"sp_edit_color_{sponsor_id}", style="primary")],
        [InlineKeyboardButton("🔘 فعال" if enabled else "🔘 غیرفعال", callback_data=f"sp_toggle_{sponsor_id}", style="success" if enabled else "danger")],
        [InlineKeyboardButton("🔙 برگشت", callback_data=f"sp_detail_{sponsor_id}", style="primary")],
    ])

# ======================================================================
# Admin audit log (last 50 actions, Tehran time)
# ======================================================================

c.execute("""CREATE TABLE IF NOT EXISTS admin_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
)""")
c.execute("CREATE INDEX IF NOT EXISTS idx_admin_activity_created ON admin_activity(id DESC)")
conn.commit()

_AUDIT_TABLES = ("profiles", "sponsors", "blacklist", "admins", "profile_protocol_settings", "manual_send_queue")

def _audit_snapshot():
    snap = {}
    db = get_conn()
    try:
        for table in _AUDIT_TABLES:
            try:
                rows = db.execute(f"SELECT * FROM {table}").fetchall()
                cols = [x[1] for x in db.execute(f"PRAGMA table_info({table})").fetchall()]
                snap[table] = {str(row[0]): dict(zip(cols,row)) for row in rows}
            except Exception:
                snap[table] = {}
    finally:
        db.close()
    return snap

def _fmt_audit_value(v):
    if v is None: return "NULL"
    text = str(v)
    return text if len(text) <= 1200 else text[:1200] + "…[truncated]"

def _audit_diff(before, after):
    out=[]
    for table in _AUDIT_TABLES:
        b=before.get(table,{}) ; a=after.get(table,{})
        for key in sorted(set(b)-set(a)):
            out.append(f"[حذف] جدول={table} شناسه={key} | داده={json.dumps(b[key],ensure_ascii=False,default=str)}")
        for key in sorted(set(a)-set(b)):
            out.append(f"[افزودن] جدول={table} شناسه={key} | داده={json.dumps(a[key],ensure_ascii=False,default=str)}")
        for key in sorted(set(a)&set(b)):
            changes=[]
            for col in a[key]:
                if a[key].get(col) != b[key].get(col):
                    changes.append(f"{col}: {_fmt_audit_value(b[key].get(col))} -> {_fmt_audit_value(a[key].get(col))}")
            if changes:
                out.append(f"[تغییر] جدول={table} شناسه={key} | " + " | ".join(changes))
    return out

def _record_admin_activity(admin_id, action, before=None, after=None, extra=""):
    try:
        details = []
        if before is not None and after is not None:
            details.extend(_audit_diff(before, after))
        if extra: details.append(str(extra))
        if not details: details.append("بدون تغییر در دیتابیس؛ عمل مدیریتی/ناوبری ثبت شد.")
        text = "\n".join(details)
        db=get_conn()
        db.execute("INSERT INTO admin_activity(admin_id,action,details,created_at) VALUES(?,?,?,?)", (int(admin_id), str(action)[:200], text, get_tehran_time()))
        db.execute("DELETE FROM admin_activity WHERE id NOT IN (SELECT id FROM admin_activity ORDER BY id DESC LIMIT 500)")
        db.commit(); db.close()
    except Exception:
        log.exception("admin audit write failed")

def _activity_report(limit=50):
    db=get_conn()
    try:
        rows=db.execute("SELECT id,admin_id,action,details,created_at FROM admin_activity ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    finally: db.close()
    lines=[f"BOT ADMIN ACTIVITY | version={APP_VERSION}", f"Timezone: Asia/Tehran", f"Generated: {get_tehran_time()}", "="*90]
    for i,(aid,admin_id,action,details,created_at) in enumerate(rows,1):
        lines += [f"\n#{i} | Activity ID: {aid}", f"زمان تهران: {created_at}", f"ادمین: {admin_id}", f"عمل: {action}", "جزئیات:", details, "-"*90]
    if not rows: lines.append("هیچ فعالیتی ثبت نشده است.")
    return "\n".join(lines)

async def _send_activity_file(message):
    path=os.path.join(DATA_DIR, f"admin_activity_{datetime.now(TEHRAN_TZ).strftime('%Y%m%d_%H%M%S')}.txt")
    try:
        with open(path,"w",encoding="utf-8") as f: f.write(_activity_report(50))
        with open(path,"rb") as f: await message.reply_document(document=f, filename="admin_activity_50.txt", caption=f"📜 ۵۰ فعالیت آخر ادمین — زمان تهران: {get_tehran_time()}")
    finally:
        try: os.remove(path)
        except OSError: pass

SOURCE_PAGE_SIZE = 20

def source_list_kb(profile_id, page=1):
    sources = get_profile_sources(profile_id)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    total_pages = max(1, (len(sources) + SOURCE_PAGE_SIZE - 1) // SOURCE_PAGE_SIZE)
    page = min(page, total_pages)
    btns = []
    start = (page - 1) * SOURCE_PAGE_SIZE
    for offset, src in enumerate(sources[start:start + SOURCE_PAGE_SIZE]):
        idx = start + offset
        label = src if len(src) <= 48 else src[:45] + "..."
        btns.append([InlineKeyboardButton(f"❌ {idx + 1}. {label}", callback_data=f"src_del_{profile_id}_{idx}_{page}", style="danger")])
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"src_list_{profile_id}_{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page}/{total_pages}", callback_data="dummy", style="primary"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"src_list_{profile_id}_{page+1}", style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def empty_button_kb(profile_id, callback):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_empty"), callback_data=callback, style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]
    ])

def blacklist_kb(profile_id,page=1):
    words=get_blacklist(profile_id); per_page=20
    try: page=max(1,int(page))
    except (TypeError,ValueError): page=1
    total_pages=max(1,(len(words)+per_page-1)//per_page); page=min(page,total_pages); start=(page-1)*per_page
    btns=[]
    for w in words[start:start+per_page]:
        # Encode word safely in callback data; callback size is limited, so use row index.
        idx=words.index(w,start) if False else start + words[start:start+per_page].index(w)
        btns.append([InlineKeyboardButton(f"❌ {idx+1}. {w}",callback_data=f"bl_del_{profile_id}_{idx}_{page}",style="danger")])
    if total_pages>1:
        nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️ قبلی",callback_data=f"bl_list_{profile_id}_{page-1}",style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page}/{total_pages}",callback_data="dummy",style="primary"))
        if page<total_pages: nav.append(InlineKeyboardButton("بعدی ▶️",callback_data=f"bl_list_{profile_id}_{page+1}",style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton("➕ افزودن",callback_data=f"bl_add_{profile_id}",style="success")])
    if words: btns.append([InlineKeyboardButton("🗑 پاک کردن همه",callback_data=f"bl_clear_{profile_id}",style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_back"),callback_data=f"prof_{profile_id}",style="primary")])
    return InlineKeyboardMarkup(btns)

def backup_export_type_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 کانفیگ", callback_data=f"backup_export_type_{profile_id}_configs", style="primary")],
        [InlineKeyboardButton("🌐 پروکسی", callback_data=f"backup_export_type_{profile_id}_proxies", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def backup_export_scope_kb(profile_id, backup_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("backup_export_scope_all"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_all", style="primary")],
        [InlineKeyboardButton(msg("backup_export_scope_100"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_100", style="primary")],
        [InlineKeyboardButton(msg("backup_export_scope_custom"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_custom", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"backup_export_menu_{profile_id}", style="primary")],
    ])

def manual_schedule_kb(profile_id):
    """Default manual scheduling menu (no draft context required)."""
    return manual_schedule_kb_with_draft(profile_id, None)

def manual_schedule_kb_with_draft(profile_id, draft=None):
    draft = draft or {}
    interval = max(0, int(draft.get("interval", 0) or 0))
    batch = max(1, min(50, int(draft.get("batch", 1) or 1)))
    buttons = [
        [InlineKeyboardButton("⚡️ همین الان", callback_data=f"mqs_{profile_id}_0_{min(50, batch)}_0", style="success")],
        [InlineKeyboardButton("⏱️ هر ۳۰ دقیقه", callback_data=f"mqs_{profile_id}_30_{min(50, batch)}_0", style="primary"),
         InlineKeyboardButton("⏱️ هر ۱ ساعت", callback_data=f"mqs_{profile_id}_60_{min(50, batch)}_0", style="primary")],
        [InlineKeyboardButton("⏱️ هر ۱۲ ساعت", callback_data=f"mqs_{profile_id}_720_{min(50, batch)}_0", style="primary"),
         InlineKeyboardButton("⚙️ تغییر فاصله", callback_data=f"mq_custom_interval_{profile_id}", style="primary")],
        [InlineKeyboardButton("📦 تغییر تعداد در هر پست", callback_data=f"mq_batch_{profile_id}", style="primary")],
        [InlineKeyboardButton("📋 صف ارسال‌های دستی", callback_data=f"mq_list_{profile_id}", style="primary")],
    ]
    if draft:
        buttons.append([InlineKeyboardButton(
            f"✅ ثبت: {'فوری' if interval == 0 else f'هر {interval} دقیقه'} • {batch} مورد",
            callback_data=f"mq_apply_{profile_id}",
            style="success"
        )])
    buttons.append([InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}", style="danger")])
    return InlineKeyboardMarkup(buttons)


def manual_queue_list_kb(profile_id, page=1):
    """Paginated queue list with explicit, validated page state."""
    jobs=get_manual_queue(profile_id)
    try: page=max(1,int(page))
    except (TypeError,ValueError): page=1
    per_page=20
    total_pages=max(1,(len(jobs)+per_page-1)//per_page)
    page=min(page,total_pages)
    btns=[]
    for job in paginate_items(jobs,page,per_page):
        kind="📡" if job["kind"]=="config" else "🌐"
        count=_manual_queue_active_count(profile_id, job)
        interval=int(job.get("interval_minutes") or 0)
        interval_text="فوری" if interval==0 else f"هر {interval}د"
        status = str(job.get("status") or "pending")
        status_icon = {"pending": "🟢", "cancelled": "⛔", "running": "🔵", "done": "✅", "failed": "⚠️"}.get(status, "❔")
        qname=html.escape(str(job.get("queue_name") or f"صف #{job['id']}"))
        btns.append([InlineKeyboardButton(f"{status_icon} {qname} • {kind} • {count} باقی • {interval_text}",callback_data=f"mq_detail_{profile_id}_{job['id']}",style="primary")])
    if not btns: btns.append([InlineKeyboardButton("صف خالی است",callback_data="dummy",style="primary")])
    if total_pages>1:
        nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️ قبلی",callback_data=f"mq_list_{profile_id}_{page-1}",style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page}/{total_pages}",callback_data="dummy",style="primary"))
        if page<total_pages: nav.append(InlineKeyboardButton("بعدی ▶️",callback_data=f"mq_list_{profile_id}_{page+1}",style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 بازگشت",callback_data=f"prof_{profile_id}",style="primary")])
    return InlineKeyboardMarkup(btns)


def manual_queue_detail_kb(profile_id, job_id, items, item_page=1):
    btns = []
    per_page = 25
    try: item_page = max(1, int(item_page))
    except (TypeError, ValueError): item_page = 1
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    item_page = min(item_page, total_pages)
    start = (item_page - 1) * per_page
    for offset, item in enumerate(items[start:start + per_page]):
        idx = start + offset
        label = str(item)
        if len(label) > 42:
            label = label[:39] + "..."
        btns.append([InlineKeyboardButton(f"🗑 حذف {idx+1}: {label}", callback_data=f"mq_rm_{profile_id}_{job_id}_{idx}_{item_page}", style="danger")])
    if total_pages > 1:
        nav = []
        if item_page > 1: nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"mq_detail_{profile_id}_{job_id}_{item_page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {item_page}/{total_pages}", callback_data="dummy", style="primary"))
        if item_page < total_pages: nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"mq_detail_{profile_id}_{job_id}_{item_page+1}", style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton("✏️ تغییر فاصله زمانی", callback_data=f"mq_edit_interval_{profile_id}_{job_id}", style="primary"),
                 InlineKeyboardButton("📦 تغییر تعداد در هر پست", callback_data=f"mq_edit_batch_{profile_id}_{job_id}", style="primary")])
    btns.append([InlineKeyboardButton("✏️ نام‌گذاری صف", callback_data=f"mq_rename_{profile_id}_{job_id}", style="primary")])
    if str((get_manual_queue_job(job_id, profile_id) or {}).get("kind") or "") == "config":
        btns.append([InlineKeyboardButton("📄 دریافت کانفیگ‌های فعالِ پست‌نشده TXT", callback_data=f"mq_export_{profile_id}_{job_id}", style="success")])
    btns.append([InlineKeyboardButton("➕ افزودن سرور به این صف", callback_data=f"mq_add_{profile_id}_{job_id}", style="success")])
    status = str((get_manual_queue_job(job_id, profile_id) or {}).get("status") or "pending")
    if status == "cancelled":
        btns.append([InlineKeyboardButton("▶️ خارج کردن از لغو و ادامه ارسال", callback_data=f"mq_resume_{profile_id}_{job_id}", style="success")])
    elif status == "pending":
        btns.append([InlineKeyboardButton("⏩ ارسال همین پست الان", callback_data=f"mq_force_{profile_id}_{job_id}", style="success")])
        btns.append([InlineKeyboardButton("⛔ لغو ارسال", callback_data=f"mq_cancel_{profile_id}_{job_id}", style="danger")])
    btns.append([InlineKeyboardButton("🗑 حذف کامل صف", callback_data=f"mq_delete_{profile_id}_{job_id}", style="danger")])
    btns.append([InlineKeyboardButton("🔙 صف", callback_data=f"mq_list_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def manual_queue_text(job, profile_id):
    kind = "کانفیگ" if job["kind"] == "config" else "پروکسی"
    items = _manual_queue_unposted_items(profile_id, str(job.get("kind") or ""), job.get("items") or [])
    interval = int(job.get("interval_minutes") or 0)
    batch = int(job.get("batch_size") or 1)
    status = job.get("status", "pending")
    status_labels = {"pending": "🟢 در صف", "cancelled": "⛔ لغوشده", "running": "🔵 در حال ارسال", "done": "✅ تکمیل‌شده", "failed": "⚠️ ناموفق"}
    status_label = status_labels.get(status, status)
    next_run = job.get("next_run_at", "")
    preview = "\n".join(f"{i+1}. {html.escape(str(x)[:100])}" for i, x in enumerate(items[:12]))
    if len(items) > 12:
        preview += f"\n... و {len(items)-12} مورد دیگر"
    queue_name=html.escape(str(job.get("queue_name") or f"صف #{job['id']}"))
    return (f"📋 <b>{queue_name}</b> • #{job['id']}\n"
            f"نوع: {kind}\nباقی‌مانده: <b>{len(items)}</b>\n"
            f"تعداد هر پست: <b>{batch}</b>\n"
            f"فاصله: <b>{'فوری' if interval == 0 else str(interval) + ' دقیقه'}</b>\n"
            f"وضعیت: <b>{status_label}</b>\n"
            f"اجرای بعدی: <code>{html.escape(next_run)}</code>\n\n{preview}")

def timer_menu_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("timer_option_30m"), callback_data=f"timer_set_{profile_id}_30", style="primary")],
        [InlineKeyboardButton(msg("timer_option_1h"), callback_data=f"timer_set_{profile_id}_60", style="primary")],
        [InlineKeyboardButton(msg("timer_option_2h"), callback_data=f"timer_set_{profile_id}_120", style="primary")],
        [InlineKeyboardButton(msg("timer_option_4h"), callback_data=f"timer_set_{profile_id}_240", style="primary")],
        [InlineKeyboardButton(msg("timer_option_8h"), callback_data=f"timer_set_{profile_id}_480", style="primary")],
        [InlineKeyboardButton(msg("timer_option_custom"), callback_data=f"timer_custom_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("timer_disable"), callback_data=f"timer_clear_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def log_range_kb(profile_id, log_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("log_range_30m"), callback_data=f"log_range_{profile_id}_{log_type}_30", style="primary")],
        [InlineKeyboardButton(msg("log_range_1h"), callback_data=f"log_range_{profile_id}_{log_type}_60", style="primary")],
        [InlineKeyboardButton(msg("log_range_6h"), callback_data=f"log_range_{profile_id}_{log_type}_360", style="primary")],
        [InlineKeyboardButton(msg("log_range_24h"), callback_data=f"log_range_{profile_id}_{log_type}_1440", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def log_menu_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لاگ کامل", callback_data=f"log_full_{profile_id}", style="primary")],
        [InlineKeyboardButton("🚨 فقط خطاها", callback_data=f"log_errors_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def general_settings_kb():
    lang = get_lang()
    lang_text = "فارسی" if lang == "fa" else "English"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 زبان: {lang_text}", callback_data="toggle_lang", style="primary")],
        [InlineKeyboardButton(msg("btn_admins"), callback_data="manage_admins", style="primary")],
        [InlineKeyboardButton(msg("btn_backup"), callback_data="backup_db", style="primary")],
        [InlineKeyboardButton(msg("btn_replace_database"), callback_data="replace_db", style="danger")],
        [InlineKeyboardButton("🧪 دیباگ عمیق با شماره لاین", callback_data="run_deep_debug", style="primary")],
        [InlineKeyboardButton("📜 فعالیت ۵۰ عمل آخر ادمین", callback_data="activity_log", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")],
    ])

def manage_admins_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_add_admin"), callback_data="add_admin", style="success")],
        [InlineKeyboardButton(msg("btn_remove_admin"), callback_data="remove_admin", style="danger")],
        [InlineKeyboardButton(msg("btn_list_admins"), callback_data="list_admins", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="general_settings", style="primary")],
    ])

def admin_list_kb(page=1):
    admins=list_admins(); per_page=20
    try: page=max(1,int(page))
    except (TypeError,ValueError): page=1
    total_pages=max(1,(len(admins)+per_page-1)//per_page); page=min(page,total_pages)
    btns=[]
    if total_pages>1:
        nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️ قبلی",callback_data=f"list_admins_{page-1}",style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page}/{total_pages}",callback_data="dummy",style="primary"))
        if page<total_pages: nav.append(InlineKeyboardButton("بعدی ▶️",callback_data=f"list_admins_{page+1}",style="primary"))
        btns.append(nav)
    btns.append([InlineKeyboardButton("➕ افزودن ادمین",callback_data="add_admin",style="success")])
    btns.append([InlineKeyboardButton("🗑 حذف ادمین",callback_data="remove_admin",style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_back"),callback_data="manage_admins",style="primary")])
    return InlineKeyboardMarkup(btns)

# ======================================================================
# دستورات (بدون تغییر)
# ======================================================================
async def cmd_start(u, ctx):
    if not is_admin(u.effective_user.id):
        return await u.message.reply_text(msg("only_admin"))
    profiles = get_profiles()
    total = len(profiles)
    next_n = 0
    if profiles:
        last_num = max(p["last_num"] for p in profiles)
        next_n = last_num + 1
    balance = None
    credit_str = f"${balance:.2f}" if balance is not None else "نامشخص"
    txt = msg("welcome", profiles=total, next_n=next_n, credit=credit_str)
    await u.message.reply_text(txt, parse_mode="HTML", reply_markup=main_menu_kb())

async def cmd_admin(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    await show_profiles_list(u.message)

async def cmd_balance(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    balance = None
    if balance is not None:
        txt = msg("balance_info", balance=f"${balance:.2f}")
    else:
        txt = "💰 اعتبار سرویس در دسترس نیست."
    await u.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")]]))

async def show_profiles_list(msg_or_q, page=1):
    profiles = get_profiles()
    if not profiles:
        txt = "❌ هیچ پروفایلی وجود ندارد.\nبرای ساخت، دکمه Add را بزنید."
    else:
        lines = []
        for p in profiles:
            status = "✅" if get_profile_enabled(p['id']) else "⛔"
            lines.append(f"• {status} `{p['dest_name']}` (ID: {p['id']}) – {len(get_profile_sources(p['id']))} منبع, بازه کانفیگ:{p.get('interval_config',5)}m, بازه پروکسی:{p.get('interval_proxy',5)}m")
        txt = msg("profile_list", list="\n".join(lines))
    kb = profiles_kb(page)
    try:
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_q.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise

async def cmd_runnow(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    p = await u.message.reply_text("⏳ در حال اجرا برای همه پروفایل‌ها...")
    try:
        profiles = get_profiles()
        results = []
        for prof in profiles:
            if not get_profile_enabled(prof['id']):
                results.append(f"{prof['dest_name']}: غیرفعال")
                continue
            log.info(f"🚀 /runnow for profile {prof['id']}")
            n, m = await run_cycle_for_profile(u.get_bot(), prof['id'], enable_configs=True, enable_proxies=True, is_instant=False)
            results.append(f"{prof['dest_name']}: {n} - {m}")
        await p.edit_text("✅ Done:\n" + "\n".join(results))
    except Exception as e:
        log.error(f"❌ /runnow error: {e}")
        log.error(traceback.format_exc())
        await p.edit_text(f"❌ {str(e)[:200]}")

async def cmd_runall(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    p = await u.message.reply_text("⏳ در حال اجرا (همه) برای همه پروفایل‌ها...")
    try:
        profiles = get_profiles()
        results = []
        for prof in profiles:
            if not get_profile_enabled(prof['id']):
                results.append(f"{prof['dest_name']}: غیرفعال")
                continue
            log.info(f"🚀 /runall for profile {prof['id']}")
            n, m = await run_cycle_for_profile(u.get_bot(), prof['id'], enable_configs=True, enable_proxies=True, is_instant=False)
            results.append(f"{prof['dest_name']}: {n} - {m}")
        await p.edit_text("✅ Done:\n" + "\n".join(results))
    except Exception as e:
        log.error(f"❌ /runall error: {e}")
        log.error(traceback.format_exc())
        await p.edit_text(f"❌ {str(e)[:200]}")

async def cmd_sendtest(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    profiles = get_profiles()
    if not profiles:
        return await u.message.reply_text("❌ No profiles!")
    for prof in profiles:
        dest = prof["dest_name"]
        try:
            await u.get_bot().send_message(dest, f"Test {get_tehran_time()}")
        except Exception as e:
            await u.message.reply_text(f"❌ Failed for {dest}: {e}")
    await u.message.reply_text(f"✅ Test sent to {len(profiles)} destinations")

def _debug_static_report():
    import ast
    from collections import Counter
    path=os.path.abspath(__file__)
    try: source=open(path,"r",encoding="utf-8",errors="replace").read()
    except Exception as exc: return f"FILE READ FAILED: {exc}"
    lines=source.splitlines(); out=[f"BOT DEEP DEBUG | version={globals().get('APP_VERSION', 'unknown')}",f"FILE: {path}",f"LINES: {len(lines)}"]
    try: tree=ast.parse(source,filename=path); out.append("SYNTAX: PASS")
    except SyntaxError as exc:
        out.append(f"SYNTAX: FAIL | line={exc.lineno} col={exc.offset} | {exc.msg}"); return "\n".join(out)
    funcs=[(n.name,n.lineno) for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
    c=Counter(n for n,_ in funcs)
    for name,count in c.items():
        if count>1: out.append(f"[WARN][DUPLICATE] {name}: lines {[ln for nm,ln in funcs if nm==name]}")
    imported=set(); defined=set()
    for n in tree.body:
        if isinstance(n,ast.Import): imported.update(a.asname or a.name.split('.')[0] for a in n.names)
        elif isinstance(n,ast.ImportFrom): imported.update(a.asname or a.name for a in n.names)
        elif isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): defined.add(n.name)
        elif isinstance(n,(ast.Assign,ast.AnnAssign)):
            ts=n.targets if isinstance(n,ast.Assign) else [n.target]
            for t in ts:
                if isinstance(t,ast.Name): defined.add(t.id)
    import builtins
    known=set(dir(builtins))|imported|defined
    loads=[]
    for n in ast.walk(tree):
        if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id not in known: loads.append((n.id,n.lineno))
    uc=Counter(n for n,_ in loads); out.append(f"POTENTIAL UNDEFINED NAMES: {len(uc)}")
    for name,_ in uc.most_common(100): out.append(f"[CHECK][NAME] {name}: lines {sorted({ln for nm,ln in loads if nm==name})[:20]}")
    for i,line in enumerate(lines,1):
        st=line.strip()
        if st=="except:" or st.startswith("except Exception:"): out.append(f"[WARN][BROAD-EXCEPT] line {i}: {st}")
        if "TODO" in line or "FIXME" in line: out.append(f"[INFO][TODO] line {i}: {st[:180]}")
    out.append("PROXY TYPE TESTS:")
    samples=["https://t.me/proxy?port=8443&secret=EERighJJvXrFGRMCIMJdCQ&server=beer.crona-extra.co.uk","https://t.me/proxy?port=444&secret=dd41b712fe12019c64e281bcb6aeccded7&server=185.84.156.45","socks5://127.0.0.1:1080","http://127.0.0.1:8080"]
    for x in samples: out.append(f"  {x[:60]} => {detect_proxy_protocol(x) or 'REJECTED'}")
    return "\n".join(out)

async def run_deep_debug(update, context):
    user = getattr(update, "effective_user", None) or getattr(update, "from_user", None)
    if not user or not is_admin(user.id): return
    report=_debug_static_report()
    try:
        ok,detail=_validate_sqlite_database(DB_PATH)
        report += f"\n\nDB CHECK: {'PASS' if ok else 'FAIL'} | {detail}"
        report += f"\nLOG FILE: {_LOG_FILE} | exists={os.path.exists(_LOG_FILE)} | size={os.path.getsize(_LOG_FILE) if os.path.exists(_LOG_FILE) else 0}"
        report += f"\nPROFILES: {len(get_profiles())}"
    except Exception: report += "\n\n[RUNTIME ERROR]\n"+traceback.format_exc()
    stamp=datetime.now(TEHRAN_TZ).strftime("%Y%m%d_%H%M%S"); path=os.path.join(DATA_DIR,f"deep_debug_{stamp}.txt")
    with open(path,"w",encoding="utf-8") as f: f.write(report)
    message=getattr(update,"effective_message",None) or getattr(update,"message",None)
    try:
        if message:
            with open(path,"rb") as f: await message.reply_document(document=f,filename=os.path.basename(path),caption="🧪 دیباگ عمیق کامل؛ شماره لاین‌ها و تست پروتکل‌ها داخل فایل است.")
    finally:
        try: os.remove(path)
        except OSError: pass

async def cmd_diag(update: Update, context):
    await run_deep_debug(update, context)

async def cmd_status(update: Update, context):
    if not is_admin(update.effective_user.id):
        return
    profiles = get_profiles()
    lines = ["📊 **وضعیت پروفایل‌ها**"]
    for p in profiles:
        enabled = get_profile_enabled(p['id'])
        post_cfg = get_profile_post_configs(p['id'])
        post_prx = get_profile_post_proxies(p['id'])
        interval_cfg = get_profile_interval_config(p['id'])
        interval_prx = get_profile_interval_proxy(p['id'])
        timer_expiry = p.get('timer_expiry')
        timer = "⏳ فعال" if timer_expiry else "⏹ غیرفعال"
        sources_count = len(get_profile_sources(p['id']))
        lines.append(
            f"• {p['dest_name']} (ID:{p['id']}) – "
            f"فعال: {'✅' if enabled else '❌'}, "
            f"کانفیگ: {'✅' if post_cfg else '❌'}, "
            f"پروکسی: {'✅' if post_prx else '❌'}, "
            f"بازه‌ی کانفیگ: {interval_cfg}m, "
            f"بازه‌ی پروکسی: {interval_prx}m, "
            f"منابع: {sources_count}, "
            f"تایمر: {timer}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ======================================================================
# کالبک (با اضافه شدن هندلرهای جدید)
# ======================================================================
def clear_pending_input_state(ctx):
    """Cancel active text-entry workflows without destroying manual-send data.

    Manual sending is a small state machine: the parsed links live in
    ``manual_pending`` and the temporary input prompts live in
    ``manual_schedule_custom`` / ``manual_queue_edit``. Navigation should
    cancel only the active prompt, not the already parsed links or draft
    scheduling values, so Back can safely return to the previous manual menu.
    """
    for key in (
        "action",
        "sponsor_add",
        "sponsor_edit",
        "backup_export",
        "backup_export_custom",
        "manual_schedule_custom",
        "manual_queue_edit",
    ):
        ctx.user_data.pop(key, None)


async def _run_channel_delete_job(bot, q, profile_id, count):
    try:
        deleted, failed = await delete_latest_channel_posts(bot, profile_id, count)
        result = f"⚠️ {deleted} پست حذف شد؛ {failed} مورد حذف نشد." if failed else f"✅ {deleted} پست آخر حذف شد."
        await q.message.reply_text(result + "\nترتیب: از جدیدترین پست‌ها به سمت قدیمی‌تر.", reply_markup=channel_delete_kb(profile_id))
    except Exception as exc:
        log.exception("[CHANNEL-DELETE] background job failed")
        try:
            await q.message.reply_text(f"❌ خطا در حذف پست‌ها: {str(exc)[:200]}", reply_markup=channel_delete_kb(profile_id))
        except Exception:
            pass


async def _run_channel_delete_job_from_message(u, profile_id, count):
    try:
        deleted, failed = await delete_latest_channel_posts(u.get_bot(), profile_id, count)
        result = f"⚠️ {deleted} پست حذف شد؛ {failed} مورد حذف نشد." if failed else f"✅ {deleted} پست آخر حذف شد."
        await u.message.reply_text(result + "\nترتیب: از جدیدترین پست‌ها به سمت قدیمی‌تر.", reply_markup=channel_delete_kb(profile_id))
    except Exception as exc:
        log.exception("[CHANNEL-DELETE] custom job failed")
        await u.message.reply_text(f"❌ خطا در حذف پست‌ها: {str(exc)[:200]}")


async def _on_callback_impl(u, ctx):
    q = u.callback_query
    try:
        if not is_admin(q.from_user.id):
            await q.answer(msg("only_admin"), show_alert=True)
            return
        await q.answer()
    except Exception as e:
        log.warning(f"Failed to answer callback query: {e}")

    try:
        d = q.data or ""
        log.info(f"📨 Callback data: {d}")

        if d == "run_deep_debug":
            await run_deep_debug(q, ctx)
            return

        # Any navigation/back/cancel action must terminate the previous text-entry
        # state before rendering the destination page. This is global across all bot sections.
        navigation_prefixes = (
            "back_", "prof_", "profiles_list", "general_settings", "manage_admins",
            "list_admins", "sponsor_list_", "sp_detail_", "sp_delete_",
            "src_list_", "dl_", "bl_list_", "backup_", "ast_", "home_", "delposts_"
        )
        if ("cancel" in d.lower() or "back" in d.lower() or d in ("back_home", "profiles_list", "general_settings", "manage_admins", "list_admins")
                or any(d.startswith(p) for p in navigation_prefixes)):
            # Preserve multi-step Sponsor Add selections and explicit field-selection callbacks.
            if not (d.startswith("sp_add_") or d.startswith("sp_color_") or d.startswith("sp_edit_color_")):
                clear_pending_input_state(ctx)

        if d == "dummy":
            return

        if d == "back_home":
            profiles = get_profiles()
            total = len(profiles)
            next_n = 0
            if profiles:
                last_num = max(p["last_num"] for p in profiles)
                next_n = last_num + 1
            balance = None
            credit_str = f"${balance:.2f}" if balance is not None else "نامشخص"
            txt = msg("welcome", profiles=total, next_n=next_n, credit=credit_str)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=main_menu_kb())
            return

        if d == "profiles_list":
            await show_profiles_list(q.message, 1)
            return

        if d.startswith("profiles_page_"):
            try: page=int(d.split("_")[-1])
            except ValueError: page=1
            await show_profiles_list(q.message, page)
            return

        if d == "activity_log":
            await _send_activity_file(q.message)
            await q.answer("📜 فایل فعالیت ارسال شد.")
            return

        if d == "general_settings":
            lang = get_lang()
            lang_text = "فارسی" if lang == "fa" else "English"
            admins = list_admins()
            txt = msg("general_settings", lang=lang_text, admins_count=len(admins)+1)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=general_settings_kb())
            return

        if d == "replace_db":
            ctx.user_data["action"] = "replace_database"
            await q.edit_message_text(
                msg("database_replace_prompt"),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="general_settings", style="primary")]])
            )
            return

        if d == "show_balance":
            balance = None
            if balance is not None:
                txt = msg("balance_info", balance=f"${balance:.2f}")
            else:
                txt = "💰 اعتبار سرویس در دسترس نیست."
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")]]))
            return

        if d == "toggle_lang":
            current = get_lang()
            new_lang = "en" if current == "fa" else "fa"
            set_lang(new_lang)
            await q.answer(msg("lang_changed", lang=new_lang))
            lang_text = "فارسی" if new_lang == "fa" else "English"
            admins = list_admins()
            txt = msg("general_settings", lang=lang_text, admins_count=len(admins)+1)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=general_settings_kb())
            return

        if d == "manage_admins":
            admins=list_admins(); main=MAIN_ADMIN_ID
            admin_list="\n".join([f"• {a['user_id']} (added by {a['added_by']})" for a in admins]) if admins else "هیچ"
            txt=msg("admin_list",main=main,admins=admin_list)
            await q.edit_message_text(txt,parse_mode="HTML",reply_markup=admin_list_kb(1)); return

        if d.startswith("list_admins_"):
            try: page=int(d.split("_")[-1])
            except ValueError: page=1
            admins=list_admins(); main=MAIN_ADMIN_ID; per_page=20; total_pages=max(1,(len(admins)+per_page-1)//per_page); page=min(max(1,page),total_pages); start=(page-1)*per_page
            shown=admins[start:start+per_page]
            admin_list="\n".join([f"• {a['user_id']} (added by {a['added_by']})" for a in shown]) if shown else "هیچ"
            txt=msg("admin_list",main=main,admins=admin_list)+f"\n\n📄 صفحه {page}/{total_pages} | کل ادمین‌های فرعی: {len(admins)}"
            await q.edit_message_text(txt,parse_mode="HTML",reply_markup=admin_list_kb(page)); return

        if d == "add_admin":
            ctx.user_data["action"] = "add_admin"
            await q.edit_message_text(msg("admin_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="manage_admins", style="primary")]]))
            return

        if d == "list_admins":
            admins=list_admins(); main=MAIN_ADMIN_ID; per_page=20; shown=admins[:per_page]
            admin_list="\n".join([f"• {a['user_id']} (added by {a['added_by']})" for a in shown]) if shown else "هیچ"
            total_pages=max(1,(len(admins)+per_page-1)//per_page)
            txt=msg("admin_list",main=main,admins=admin_list)+f"\n\n📄 صفحه 1/{total_pages} | کل ادمین‌های فرعی: {len(admins)}"
            await q.edit_message_text(txt,parse_mode="HTML",reply_markup=admin_list_kb(1)); return

        if d == "remove_admin":
            ctx.user_data["action"] = "remove_admin"
            await q.edit_message_text("❌ شناسه ادمین مورد نظر برای حذف را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="manage_admins", style="primary")]]))
            return

        if d == "backup_db":
            try:
                await q.edit_message_text("⏳ در حال تهیه بک‌آپ...")
                with open(DB_PATH, "rb") as f:
                    await q.message.reply_document(
                        document=f,
                        filename=f"bot_backup_{get_tehran_date()}.db",
                        caption=f"💾 بک‌آپ دیتابیس - {get_tehran_time()}"
                    )
                await q.message.edit_text("✅ " + msg("backup_sent"))
            except Exception as e:
                log.error(f"Backup error: {e}")
                await q.message.edit_text("❌ " + msg("backup_failed"))
            return

        if d == "prof_add":
            ctx.user_data["action"] = "prof_add"
            await q.edit_message_text(msg("profile_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list", style="primary")]]))
            return

        if d.startswith("prof_"):
            ctx.user_data.pop("manual_pending", None)
            ctx.user_data.pop("manual_schedule_custom", None)
            ctx.user_data.pop("manual_schedule_draft", None)
            ctx.user_data.pop("manual_queue_edit", None)
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("proto_menu_"):
            try:
                profile_id=int(d.split("_")[-1])
            except:
                return
            await q.edit_message_text("⚙️ مدیریت پروتکل‌ها", reply_markup=protocol_settings_kb(profile_id))
            return

        if d.startswith("proto_cfg_") or d.startswith("proto_prx_"):
            try:
                profile_id=int(d.split("_")[-1])
            except:
                return
            kind="cfg" if d.startswith("proto_cfg_") else "prx"
            title="کانفیگ" if kind=="cfg" else "پروکسی"
            await q.edit_message_text(f"{'📡' if kind=='cfg' else '🌐'} پروتکل‌های {title}", reply_markup=protocol_toggle_kb(profile_id, kind))
            return

        if d.startswith("proto_toggle_"):
            parts=d.split("_")
            try:
                profile_id=int(parts[2]); kind=parts[3]; proto="_".join(parts[4:])
            except:
                return
            current=is_protocol_enabled(profile_id, proto)
            set_protocol_enabled(profile_id, proto, not current)
            await q.edit_message_text("⚙️ مدیریت پروتکل‌ها", reply_markup=protocol_settings_kb(profile_id))
            return

        # ===================== INDEPENDENT HEADER DISPLAY MODES =====================
        if d.startswith("hm_config_menu_") or d.startswith("hm_proxy_menu_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True)
                return
            if not get_profile(profile_id):
                await q.answer("⚠️ پروفایل یافت نشد", show_alert=True)
                return
            kind = "config" if d.startswith("hm_config_menu_") else "proxy"
            title = "کانفیگ" if kind == "config" else "پروکسی"
            await q.edit_message_text(
                f"⚙️ حالت نمایش {title} را انتخاب کنید:",
                reply_markup=header_mode_keyboard(profile_id, kind)
            )
            return

        if d.startswith("hm_config_") or d.startswith("hm_proxy_"):
            parts = d.split("_")
            if len(parts) != 4 or parts[0] != "hm":
                await q.answer("⚠️ داده نامعتبر", show_alert=True)
                return
            kind, mode = parts[1], parts[2]
            try:
                profile_id = int(parts[3])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True)
                return
            if kind not in ("config", "proxy") or mode not in ("channel", "protocol") or not get_profile(profile_id):
                await q.answer("⚠️ تنظیم نامعتبر", show_alert=True)
                return
            await q.answer("⏳ در حال ذخیره…")
            if set_header_mode(profile_id, kind, mode):
                title = "کانفیگ" if kind == "config" else "پروکسی"
                label = "نام کانال" if mode == "channel" else "پروتکل"
                await q.edit_message_text(
                    f"⚙️ حالت نمایش {title}: <b>{label}</b>",
                    parse_mode="HTML",
                    reply_markup=header_mode_keyboard(profile_id, kind)
                )
            else:
                await q.answer("⚠️ ذخیره تنظیمات انجام نشد", show_alert=True)
            return

        # ===================== SPONSOR NEW =====================
        if d.startswith("sponsor_list_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); page=int(parts[3]) if len(parts)>=4 else 1
            except (ValueError,IndexError): await q.answer("⚠️ شناسه نامعتبر"); return
            prof=get_profile(profile_id); name=prof["dest_name"] if prof else ""; sponsors=get_sponsors(profile_id,include_disabled=True); per_page=20
            total_pages=max(1,(len(sponsors)+per_page-1)//per_page); page=min(max(1,page),total_pages); start=(page-1)*per_page
            shown=sponsors[start:start+per_page]
            if not shown: txt=msg("sponsor_list_title",name=name,sponsors=msg("sponsor_list_empty"))
            else:
                lines=[]
                for sp in shown:
                    duration_str="نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
                    lines.append(msg("sponsor_item",name=sp["name"],priority=sp["priority"],enabled=sp["enabled"],url=sp["url"],text=sp["button_text"],duration=duration_str))
                txt=msg("sponsor_list_title",name=name,sponsors="\n".join(lines))+f"\n\n📄 صفحه {page}/{total_pages} | کل: {len(sponsors)}"
            await q.edit_message_text(txt,parse_mode="HTML",reply_markup=sponsor_list_kb(profile_id,page))
            return

        if d.startswith("sp_detail_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if not row:
                    await q.answer("اسپانسر یافت نشد.")
                    return
                cols = [d[0] for d in c.description]
                sp = dict(zip(cols, row))
                profile_id = sp["profile_id"]
                duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
                txt = msg("sponsor_detail", name=sp["name"], url=sp["url"], text=sp["button_text"],
                          priority=sp["priority"], enabled=bool(sp["enabled"]),
                          duration=duration_str, color=sp["color"])
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_detail_kb(sponsor_id, profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_toggle_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT enabled, profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    new_enabled = 0 if row[0] else 1
                    update_sponsor(sponsor_id, enabled=new_enabled)
                    await q.answer(f"اسپانسر {'فعال' if new_enabled else 'غیرفعال'} شد.")
                    # Refresh detail
                    c.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,))
                    row2 = c.fetchone()
                    if row2:
                        cols = [d[0] for d in c.description]
                        sp = dict(zip(cols, row2))
                        profile_id = sp["profile_id"]
                        duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
                        txt = msg("sponsor_detail", name=sp["name"], url=sp["url"], text=sp["button_text"],
                                  priority=sp["priority"], enabled=bool(sp["enabled"]),
                                  duration=duration_str, color=sp["color"])
                        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_detail_kb(sponsor_id, profile_id))
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ===================== PER-PROFILE CHANNEL POST DELETE =====================
        if d.startswith("delposts_menu_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            if not get_profile(profile_id):
                await q.answer("⚠️ پروفایل یافت نشد", show_alert=True); return
            await q.edit_message_text(
                "🗑 <b>حذف پست‌های کانال</b>\n\n"
                "حذف از <b>جدیدترین پست‌ها</b> شروع می‌شود.\n"
                "این ابزار پیام‌هایی را حذف می‌کند که همین بات برای این پروفایل ارسال و ثبت کرده است.",
                parse_mode="HTML", reply_markup=channel_delete_kb(profile_id)
            )
            return

        if d.startswith("delposts_custom_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            if not get_profile(profile_id):
                await q.answer("⚠️ پروفایل یافت نشد", show_alert=True); return
            ctx.user_data["action"] = f"delete_channel_posts_{profile_id}"
            await q.edit_message_text(
                "🔢 تعداد پست را وارد کن.\n\nمثال: <code>250</code>\n"
                "حداکثر 5000؛ حذف دقیقاً از جدیدترین پست‌های ثبت‌شده شروع می‌شود.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ لغو", callback_data=f"delposts_menu_{profile_id}", style="primary")]])
            )
            return

        if d.startswith("delposts_"):
            parts = d.split("_")
            if len(parts) == 3:
                try:
                    profile_id = int(parts[1]); count = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ مقدار نامعتبر", show_alert=True); return
                if count not in (100, 500, 1000):
                    await q.answer("⚠️ مقدار نامعتبر", show_alert=True); return
                if not get_profile(profile_id):
                    await q.answer("⚠️ پروفایل یافت نشد", show_alert=True); return
                await q.edit_message_text(
                    f"⚠️ <b>تأیید حذف پست‌ها</b>\n\n"
                    f"تعداد: <b>{count}</b>\n"
                    "ترتیب حذف: <b>جدیدترین پست‌های کانال → قدیمی‌تر</b>.\n\n"
                    "آیا مطمئنی؟",
                    parse_mode="HTML", reply_markup=channel_delete_confirm_kb(profile_id, count)
                )
                return

        if d.startswith("delconfirm_"):
            parts = d.split("_")
            if len(parts) != 3:
                await q.answer("⚠️ داده نامعتبر", show_alert=True); return
            try:
                profile_id, count = int(parts[1]), int(parts[2])
            except ValueError:
                await q.answer("⚠️ داده نامعتبر", show_alert=True); return
            if not get_profile(profile_id) or not (1 <= count <= 5000):
                await q.answer("⚠️ مقدار نامعتبر", show_alert=True); return
            await q.answer(f"⏳ حذف {count} پست تأیید شد…")
            asyncio.create_task(_run_channel_delete_job(u.get_bot(), q, profile_id, count), name=f"delete_posts_{profile_id}_{count}")
            return

        if d.startswith("delcancel_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            await q.answer("❌ حذف لغو شد")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("sp_delete_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT name, profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    name, profile_id = row
                    await q.edit_message_text(
                        msg("sp_delete_confirm", name=name),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"sp_delete_confirm_{sponsor_id}", style="danger")],
                            [InlineKeyboardButton("❌ لغو", callback_data=f"sp_detail_{sponsor_id}", style="primary")],
                        ])
                    )
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_delete_confirm_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    sponsor_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    profile_id = row[0]
                    delete_sponsor(sponsor_id)
                    await q.answer(msg("sp_deleted"))
                    await q.edit_message_text("✅ اسپانسر حذف شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_color_"):
            try:
                sponsor_id=int(d.rsplit("_",1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
            row=c.fetchone()
            if not row:
                await q.answer("اسپانسر یافت نشد.")
                return
            profile_id=row[0]
            await q.edit_message_text("🎨 رنگ دکمه اسپانسر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_color_{sponsor_id}_primary", style="primary")],
                [InlineKeyboardButton("🟢 Success", callback_data=f"sp_color_{sponsor_id}_success", style="success")],
                [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_color_{sponsor_id}_danger", style="danger")],
                [InlineKeyboardButton("🔙 برگشت", callback_data=f"sp_edit_{sponsor_id}", style="primary")]
            ]))
            return

        if d.startswith("sp_color_"):
            parts=d.split("_")
            try:
                sponsor_id=int(parts[2]); color=parts[3]
            except Exception:
                await q.answer("⚠️ داده نامعتبر")
                return
            c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
            row=c.fetchone()
            if not row:
                await q.answer("اسپانسر یافت نشد.")
                return
            update_sponsor(sponsor_id, color=color)
            profile_id=row[0]
            await q.answer("✅ رنگ ذخیره شد.")
            await q.edit_message_text("✏️ ویرایش اسپانسر", reply_markup=sponsor_edit_kb(sponsor_id, profile_id))
            return

        if d.startswith("sp_toggle_unlimited_"):
            try:
                sponsor_id = int(d.rsplit("_", 1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            c.execute("SELECT profile_id, unlimited, duration_hours FROM sponsors WHERE id=?", (sponsor_id,))
            row = c.fetchone()
            if not row:
                await q.answer("اسپانسر یافت نشد.")
                return
            profile_id, current_unlimited, current_duration = row
            new_unlimited = 0 if current_unlimited else 1
            if new_unlimited:
                update_sponsor(sponsor_id, unlimited=1, expires_at=None)
            else:
                duration = int(current_duration or 1)
                update_sponsor(sponsor_id, unlimited=0, duration_hours=duration)
            await q.answer("♾ نامحدود فعال شد." if new_unlimited else "⏱ محدود شد.")
            c.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,))
            row2 = c.fetchone()
            cols = [d[0] for d in c.description]
            sp = dict(zip(cols, row2))
            duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
            txt = msg("sponsor_detail", name=sp["name"], url=sp["url"], text=sp["button_text"],
                      priority=sp["priority"], enabled=bool(sp["enabled"]),
                      duration=duration_str, color=sp["color"])
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_edit_kb(sponsor_id, profile_id))
            return

        if d.startswith("sp_edit_field_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    sponsor_id = int(parts[3])
                    field = "_".join(parts[4:])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["sponsor_edit"] = {"sponsor_id": sponsor_id, "field": field}
                await q.edit_message_text(
                    msg("sp_edit_field_prompt", field=field),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 لغو", callback_data=f"sp_edit_{sponsor_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_") and not d.startswith("sp_edit_field_") and not d.startswith("sp_edit_color_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    profile_id = row[0]
                    await q.edit_message_text(
                        "✏️ **ویرایش اسپانسر**\n\nکدام فیلد را می‌خواهید ویرایش کنید؟",
                        parse_mode="HTML",
                        reply_markup=sponsor_edit_kb(sponsor_id, profile_id)
                    )
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Sponsor add steps (multi-step)
        if d.startswith("sp_add_step_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                    step = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if step == "name":
                    ctx.user_data["sponsor_add"] = {"profile_id": profile_id, "step": "name"}
                    await q.edit_message_text(msg("sp_add_name"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "url":
                    ctx.user_data["sponsor_add"]["step"] = "url"
                    await q.edit_message_text(msg("sp_add_url"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "button_text":
                    ctx.user_data["sponsor_add"]["step"] = "button_text"
                    await q.edit_message_text(msg("sp_add_text"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "priority":
                    ctx.user_data["sponsor_add"]["step"] = "priority"
                    await q.edit_message_text(msg("sp_add_priority"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "duration":
                    ctx.user_data["sponsor_add"]["step"] = "duration"
                    await q.edit_message_text(msg("sp_add_duration"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "unlimited":
                    ctx.user_data["sponsor_add"]["step"] = "unlimited"
                    await q.edit_message_text(msg("sp_add_unlimited"), reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("بله", callback_data=f"sp_add_unlimited_yes_{profile_id}", style="primary")],
                        [InlineKeyboardButton("خیر", callback_data=f"sp_add_unlimited_no_{profile_id}", style="primary")],
                        [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                    ]))
                elif step == "color":
                    ctx.user_data["sponsor_add"]["step"] = "color"
                    await q.edit_message_text(msg("sp_add_color"), reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_add_color_{profile_id}_primary", style="primary")],
                        [InlineKeyboardButton("🟢 Success", callback_data=f"sp_add_color_{profile_id}_success", style="success")],
                        [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_add_color_{profile_id}_danger", style="danger")],
                        [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                    ]))
                else:
                    await q.answer("مرحله نامعتبر")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_unlimited_yes_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if "sponsor_add" not in ctx.user_data:
                    ctx.user_data["sponsor_add"] = {}
                ctx.user_data["sponsor_add"]["unlimited"] = 1
                ctx.user_data["sponsor_add"]["duration_hours"] = 0
                ctx.user_data["sponsor_add"]["step"] = "color"
                await q.edit_message_text(msg("sp_add_color"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_add_color_{profile_id}_primary", style="primary")],
                    [InlineKeyboardButton("🟢 Success", callback_data=f"sp_add_color_{profile_id}_success", style="success")],
                    [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_add_color_{profile_id}_danger", style="danger")],
                    [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_unlimited_no_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if "sponsor_add" not in ctx.user_data:
                    ctx.user_data["sponsor_add"] = {}
                ctx.user_data["sponsor_add"]["unlimited"] = 0
                ctx.user_data["sponsor_add"]["step"] = "duration"
                await q.edit_message_text(msg("sp_add_duration"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_color_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                    color = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if "sponsor_add" in ctx.user_data and ctx.user_data["sponsor_add"].get("profile_id") == profile_id:
                    data = ctx.user_data["sponsor_add"]
                    name = data.get("name", "Advertisement")
                    url = data.get("url", "")
                    button_text = data.get("button_text", "Advertisement")
                    priority = int(data.get("priority", 0))
                    duration_hours = int(data.get("duration_hours", 0))
                    unlimited = data.get("unlimited", 1)
                    apply_config = data.get("apply_config", 1)
                    apply_proxy = data.get("apply_proxy", 1)
                    if url:
                        add_sponsor(profile_id, name, url, button_text, enabled=1, priority=priority,
                                    duration_hours=duration_hours, unlimited=unlimited,
                                    apply_config=apply_config, apply_proxy=apply_proxy, color=color)
                        await q.answer(msg("sp_added_done", name=name))
                        await q.edit_message_text("✅ اسپانسر اضافه شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                    else:
                        await q.answer("❌ لینک اسپانسر معتبر نیست.")
                    del ctx.user_data["sponsor_add"]
                else:
                    await q.answer("❌ داده‌ها منقضی شده‌اند.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # End sponsor new

        # Legacy sponsor handling (keep for compatibility)
        if d.startswith("sp_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text("📢 **مدیریت اسپانسرها**", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 لیست اسپانسرها", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                    [InlineKeyboardButton("➕ افزودن اسپانسر", callback_data=f"sp_add_step_{profile_id}_name", style="success")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"prof_{profile_id}", style="primary")],
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ---------- Existing callbacks (unchanged) ----------
        # Sources
        if d.startswith("src_list_"):
            parts = d.split("_")
            try:
                profile_id = int(parts[2]); page = int(parts[3]) if len(parts) >= 4 else 1
            except (ValueError, IndexError):
                await q.answer("⚠️ شناسه نامعتبر"); return
            prof = get_profile(profile_id); name = prof["dest_name"] if prof else ""
            sources = get_profile_sources(profile_id)
            start=(max(1,page)-1)*SOURCE_PAGE_SIZE
            shown=sources[start:start+SOURCE_PAGE_SIZE]
            src_text = "\n".join([f"• {start+i+1}. {s}" for i,s in enumerate(shown)]) if shown else "هیچ منبعی"
            txt = msg("source_list", name=name, sources=src_text) + f"\n\n📄 صفحه {min(max(1,page),max(1,(len(sources)+SOURCE_PAGE_SIZE-1)//SOURCE_PAGE_SIZE))}/{max(1,(len(sources)+SOURCE_PAGE_SIZE-1)//SOURCE_PAGE_SIZE)} | کل منابع: {len(sources)}"
            await q.edit_message_text(txt, reply_markup=source_list_kb(profile_id,page))
            return

        if d.startswith("src_del_"):
            parts=d.split("_")
            try:
                profile_id=int(parts[2]); idx=int(parts[3]); page=int(parts[4]) if len(parts)>=5 else 1
            except (ValueError,IndexError):
                await q.answer("⚠️ شناسه نامعتبر"); return
            sources=get_profile_sources(profile_id)
            if 0 <= idx < len(sources):
                removed=sources.pop(idx); set_profile_sources(profile_id,sources)
                await q.answer(msg("source_deleted"))
                prof=get_profile(profile_id); name=prof["dest_name"] if prof else ""
                total_pages=max(1,(len(sources)+SOURCE_PAGE_SIZE-1)//SOURCE_PAGE_SIZE); page=min(max(1,page),total_pages)
                start=(page-1)*SOURCE_PAGE_SIZE; shown=sources[start:start+SOURCE_PAGE_SIZE]
                src_text="\n".join([f"• {start+i+1}. {s}" for i,s in enumerate(shown)]) if shown else "هیچ منبعی"
                txt=msg("source_list",name=name,sources=src_text)+f"\n\n📄 صفحه {page}/{total_pages} | کل منابع: {len(sources)}"
                await q.edit_message_text(txt,reply_markup=source_list_kb(profile_id,page))
            else:
                await q.answer("❌ منبع پیدا نشد؛ لیست به‌روز شده است.",show_alert=True)
            return

        if d.startswith("sa_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"sa_{profile_id}"
                await q.edit_message_text(msg("send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"src_list_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Destinations
        if d.startswith("dl_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                dest = get_profile_dest(profile_id)
                body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
                await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("da_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"da_{profile_id}"
                await q.edit_message_text("📝 کانال مقصد جدید رو بفرست (با @ یا بدون):\nمثال: `@MyChannel`", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"dl_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("dd_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_dest(profile_id, "")
                await q.answer(msg("removed"))
                dest = get_profile_dest(profile_id)
                body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
                await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Settings: name, banners, intervals, max, etc.
        if d.startswith("ac_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ac_{profile_id}"
                current_name = get_profile_dest(profile_id) or "نامشخص"
                await q.edit_message_text(f"نام فعلی: {current_name}\nنام جدید را بفرست (یا دکمه خالی):", reply_markup=empty_button_kb(profile_id, f"empty_ac_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_ac_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_dest(profile_id, "")
                await q.answer("✅ نام پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ab_config_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ab_config_{profile_id}"
                cur = html.escape(get_profile_banner_config(profile_id))
                await q.edit_message_text(f"Current Config Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{configs}}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ab_proxy_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ab_proxy_{profile_id}"
                cur = html.escape(get_profile_banner_proxy(profile_id))
                await q.edit_message_text(f"Current Proxy Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{proxies}}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_cfg_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_cfg_interval_{profile_id}"
                current = get_profile_interval_config(profile_id)
                await q.edit_message_text(f"بازه فعلی کانفیگ: {current} دقیقه\n\nعدد جدید (۰ تا ۱۴۴۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_cfg_interval_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_cfg_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_prx_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_prx_interval_{profile_id}"
                current = get_profile_interval_proxy(profile_id)
                await q.edit_message_text(f"بازه فعلی پروکسی: {current} دقیقه\n\nعدد جدید (۰ تا ۱۴۴۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_prx_interval_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_prx_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_cfg_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_cfg_max_{profile_id}"
                current = get_profile_max_post_config(profile_id)
                await q.edit_message_text(f"حداکثر تعداد کانفیگ فعلی: {current}\n\nعدد جدید (۱ تا ۵۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_cfg_max_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_cfg_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_prx_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_prx_max_{profile_id}"
                current = get_profile_max_post_proxy(profile_id)
                await q.edit_message_text(f"حداکثر تعداد پروکسی فعلی: {current}\n\nعدد جدید (۱ تا ۵۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_prx_max_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_prx_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ast_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                n_seen = c.execute("SELECT COUNT(*) FROM seen WHERE profile_id=?", (profile_id,)).fetchone()[0]
                n_sp = c.execute("SELECT COUNT(*) FROM sponsors WHERE profile_id=?", (profile_id,)).fetchone()[0]
                next_n = get_profile_last_num(profile_id) + 1
                dest = get_profile_dest(profile_id)
                txt = f"📊 مقصد: {dest}\nمنابع: {len(get_profile_sources(profile_id))}\nاسپانسر: {n_sp}\nبعدی: #{next_n}\nحداکثر کانفیگ: {get_profile_max_post_config(profile_id)}\nحداکثر پروکسی: {get_profile_max_post_proxy(profile_id)}"
                await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sendtest_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                dest = get_profile_dest(profile_id)
                if not dest:
                    await q.answer("❌ No destination set!", show_alert=True)
                    return
                try:
                    await u.get_bot().send_message(dest, f"Test {get_tehran_time()}")
                    await q.answer("✅ Test sent")
                except Exception as e:
                    await q.answer(f"❌ {str(e)[:80]}", show_alert=True)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("runnow_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if not get_profile_enabled(profile_id):
                    await q.answer("⛔ پروفایل غیرفعال است!", show_alert=True)
                    return
                await q.answer("🚀 اجرا شروع شد؛ نتیجه بعد از پایان ارسال می‌شود.")
                try:
                    await q.edit_message_text("⏳ اجرای دستی شروع شد...\n\n⚡ پردازش سریع فعال است؛ بات در حال کار است و پنل را قفل نمی‌کند.")
                except Exception:
                    pass
                async def _run_manual_fast():
                    try:
                        _started_mono = asyncio.get_running_loop().time()
                        _WORKER_HEARTBEATS[f"manual_runnow_{profile_id}"] = time.time()
                        n, m = await asyncio.wait_for(
                            _run_manual_runnow_isolated(
                                u.get_bot(), profile_id
                            ),
                            timeout=300.0
                        )
                        _elapsed = asyncio.get_running_loop().time() - _started_mono
                        _WORKER_HEARTBEATS.pop(f"manual_runnow_{profile_id}", None)
                        try:
                            await u.get_bot().send_message(
                                MAIN_ADMIN_ID,
                                f"✅ اجرای دستی تمام شد\n📊 {n}\n📝 {m}\n⏱ زمان: {_elapsed:.1f} ثانیه"
                            )
                        except Exception:
                            pass
                    except asyncio.TimeoutError:
                        _WORKER_HEARTBEATS.pop(f"manual_runnow_{profile_id}", None)
                        log.error(f"❌ runnow timeout for profile {profile_id}")
                        try:
                            await u.get_bot().send_message(MAIN_ADMIN_ID, "⚠️ اجرای دستی بیش از ۵ دقیقه طول کشید و متوقف شد.\nجزئیات در لاگ ثبت شده است.")
                        except Exception:
                            pass
                    except Exception as e:
                        _WORKER_HEARTBEATS.pop(f"manual_runnow_{profile_id}", None)
                        log.exception(f"❌ runnow error for profile {profile_id}")
                        try:
                            await u.get_bot().send_message(MAIN_ADMIN_ID, f"❌ خطای اجرای دستی: {str(e)[:300]}")
                        except Exception:
                            pass
                task_key = f"manual_runnow_{profile_id}"
                existing = _MANUAL_RUN_TASKS.get(task_key)
                if existing is None or existing.done():
                    _MANUAL_RUN_TASKS[task_key] = asyncio.create_task(_run_manual_fast())
                else:
                    try:
                        await u.get_bot().send_message(MAIN_ADMIN_ID, f"⚠️ اجرای دستی پروفایل {profile_id} هنوز در حال انجام است؛ اجرای تکراری شروع نشد.")
                    except Exception:
                        pass
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("instant_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_interval_config(profile_id, 0)
                set_profile_interval_proxy(profile_id, 0)
                await q.answer("⚡ حالت اپدیت لحظه‌ای برای کانفیگ و پروکسی فعال شد")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_show_ping_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            current = get_profile_show_ping(profile_id)
            new_val = not bool(current)
            set_profile_show_ping(profile_id, new_val)
            await q.answer(f"👁 نمایش Ping {'فعال' if new_val else 'غیرفعال'} شد.")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("ping_regions_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            await q.edit_message_text("📍 <b>تنظیم مستقل مناطق Ping</b>\n\nبرای ویتوری و پروکسی می‌توانی منطقه جداگانه انتخاب کنی.\nپیش‌فرض هر دو: 🌍 جهانی", parse_mode="HTML", reply_markup=ping_regions_kb(profile_id))
            return

        if d.startswith("ping_region_config_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            await q.edit_message_text("🧩 منطقه Ping ویتوری/کانفیگ را انتخاب کن:", reply_markup=ping_region_choice_kb(profile_id, "config"))
            return

        if d.startswith("ping_region_proxy_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            await q.edit_message_text("🌐 منطقه Ping پروکسی را انتخاب کن:", reply_markup=ping_region_choice_kb(profile_id, "proxy"))
            return

        if d.startswith("set_ping_region_"):
            parts = d.split("_")
            if len(parts) != 5:
                await q.answer("⚠️ داده نامعتبر"); return
            _, _, kind, mode, profile_raw = parts
            try:
                profile_id = int(profile_raw)
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            mode = _normalize_ping_mode(mode)
            if kind == "config":
                set_profile_config_ping_mode(profile_id, mode)
                label = "ویتوری/کانفیگ"
            elif kind == "proxy":
                set_profile_proxy_ping_mode(profile_id, mode)
                label = "پروکسی"
            else:
                await q.answer("⚠️ نوع نامعتبر"); return
            await q.answer(f"✅ منطقه Ping {label}: {'جهانی' if mode == 'global' else 'ایران'}")
            await q.edit_message_text(f"🧩 منطقه Ping {label} تنظیم شد.", reply_markup=ping_region_choice_kb(profile_id, kind))
            return

        if d.startswith("ping_region_all_global_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            set_profile_config_ping_mode(profile_id, "global")
            set_profile_proxy_ping_mode(profile_id, "global")
            await q.answer("🌍 هر دو منطقه Ping روی جهانی قرار گرفت")
            await q.edit_message_text("🌍 منطقه Ping ویتوری و پروکسی هر دو روی جهانی تنظیم شدند.", reply_markup=ping_regions_kb(profile_id))
            return

        if d.startswith("tglping_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_ping_mode(profile_id)
                new_mode = "global" if current == "iran" else "iran"
                set_profile_ping_mode(profile_id, new_mode)
                await q.answer(f"حالت پینگ: {'جهانی' if new_mode == 'global' else 'ایران'}")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_ping_test_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_ping_enabled(profile_id)
                new_val = not current
                set_profile_ping_enabled(profile_id, new_val)
                await q.answer(msg("ping_testing_toggle", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_batch_post_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            if not get_profile(profile_id):
                await q.answer("⚠️ پروفایل یافت نشد", show_alert=True); return
            current = get_profile_batch_posting(profile_id)
            new_val = not current
            set_profile_batch_posting(profile_id, new_val)
            # Read back from SQLite: the UI must reflect the persisted value,
            # never an optimistic/in-memory value.
            persisted = bool(get_profile_batch_posting(profile_id))
            if not persisted:
                try:
                    _pending_batch_clear(profile_id, "config")
                    _pending_batch_clear(profile_id, "proxy")
                    conn.commit()
                except Exception:
                    log.exception(f"[BATCH][profile={profile_id}] cleanup after OFF failed")
            log.info(f"[BATCH][profile={profile_id}] toggle requested={int(new_val)} persisted={int(persisted)}")
            await q.answer("📦 ارسال تجمیعی فعال شد." if persisted else "📦 ارسال تجمیعی خاموش شد.")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tgl_profile_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_enabled(profile_id)
                new_val = not current
                set_profile_enabled(profile_id, new_val)
                await q.answer(msg("toggle_profile", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tglcfg_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_post_configs(profile_id)
                new_val = not current
                set_profile_post_configs(profile_id, new_val)
                await q.answer(msg("toggle_configs", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_cfg_post_mode_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            await q.answer("⏳ در حال ذخیره…")
            current = get_profile_config_post_mode(profile_id)
            new_mode = 0 if current == 1 else 1
            set_profile_config_post_mode(profile_id, new_mode)
            persisted = get_profile_config_post_mode(profile_id)
            if persisted != new_mode:
                await q.answer("❌ ذخیره حالت انجام نشد.", show_alert=True)
                return
            await q.answer("🧩 Quote جمع‌شونده فعال شد." if persisted else "🧩 حالت عادی فعال شد.")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tgl_prx_mode_"):
            try:
                profile_id=int(d.rsplit("_",1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            mode=0 if get_profile_proxy_post_mode(profile_id)==1 else 1
            set_profile_proxy_post_mode(profile_id, mode)
            await q.answer("🌐 حالت پروکسی شیشه‌ای فعال شد." if mode else "🌐 حالت پروکسی عادی فعال شد.")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tglproxy_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_post_proxies(profile_id)
                new_val = not current
                set_profile_post_proxies(profile_id, new_val)
                await q.answer(msg("toggle_proxies", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("togglenum_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_show_numbers(profile_id)
                new_val = not current
                set_profile_show_numbers(profile_id, new_val)
                await q.answer(msg("toggle_numbers_ok", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_date_cfg_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_show_date_config(profile_id)
                set_profile_show_date_config(profile_id, not current)
                await q.answer(msg("date_cfg_toggle", status=not current))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_date_prx_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_show_date_proxy(profile_id)
                set_profile_show_date_proxy(profile_id, not current)
                await q.answer(msg("date_prx_toggle", status=not current))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("clearquery_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_custom_query(profile_id, "")
                await q.answer("✅ کوئری سفارشی پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setquery_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setquery_{profile_id}"
                current = get_profile_custom_query(profile_id) or "خالی"
                await q.edit_message_text(f"کوئری فعلی: {current}\n" + msg("custom_query_prompt"), reply_markup=empty_button_kb(profile_id, f"empty_query_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_query_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_custom_query(profile_id, "")
                await q.answer("✅ کوئری پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("rn_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_last_num(profile_id, 0)
                await q.answer(msg("reset_ok"))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("cd1_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(msg("clear_q1"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("YES", callback_data=f"cd2_{profile_id}", style="danger")], [InlineKeyboardButton("NO", callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("cd2_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM posts")
                c.execute("DELETE FROM country_cache")
                c.execute("DELETE FROM last_scrape WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM processed_messages WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM manual_send_queue WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM blacklist WHERE profile_id=?", (profile_id,))
                set_profile_last_num(profile_id, 0)
                conn.commit()
                await q.answer("پاک شد")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Persistent manual queue: scheduling, inspection, editing, deletion and forced send.
        if d.startswith("mqs_"):
            parts = d.split("_")
            try:
                profile_id = int(parts[1])
                interval = max(0, int(parts[2]))
                batch = max(1, min(50, int(parts[3])))
                delay = max(0, int(parts[4]))
            except (ValueError, IndexError):
                await q.answer("⚠️ تنظیمات صف نامعتبر است", show_alert=True)
                return
            pending = ctx.user_data.get("manual_pending")
            if not pending or int(pending.get("profile_id", -1)) != profile_id:
                await q.answer("⚠️ داده ارسال دستی منقضی شده؛ دوباره لینک‌ها را وارد کن.", show_alert=True)
                return
            # Preset buttons commit immediately, preserving the selected batch size.
            created = []
            configs = list(pending.get("configs") or [])
            proxies = list(pending.get("proxies") or [])
            if configs:
                created.append(create_manual_queue_job(profile_id, "config", configs, interval, batch, (delay if delay > 0 else (interval if interval > 0 else 0))))
            if proxies:
                created.append(create_manual_queue_job(profile_id, "proxy", proxies, interval, batch, (delay if delay > 0 else (interval if interval > 0 else 0))))
            ctx.user_data.pop("manual_pending", None)
            ctx.user_data.pop("manual_schedule_custom", None)
            ctx.user_data.pop("manual_schedule_draft", None)
            ctx.user_data.pop("action", None)
            await q.answer("✅ در صف قرار گرفت")
            await q.edit_message_text(
                f"✅ زمان‌بندی ثبت شد.\n\n📋 شناسه صف: {', '.join('#'+str(x) for x in created if x)}\n"
                f"⏱ فاصله: {'فوری' if interval == 0 else str(interval)+' دقیقه'}\n📦 تعداد هر پست: {batch}",
                reply_markup=manual_queue_list_kb(profile_id)
            )
            return

        if d.startswith("mq_apply_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True)
                return
            pending = ctx.user_data.get("manual_pending")
            draft = ctx.user_data.get("manual_schedule_draft") or {}
            if not pending or int(pending.get("profile_id", -1)) != profile_id:
                await q.answer("⚠️ داده ارسال دستی منقضی شده؛ دوباره لینک‌ها را وارد کن.", show_alert=True)
                return
            interval = max(0, int(draft.get("interval", 0) or 0))
            batch = max(1, min(50, int(draft.get("batch", 1) or 1)))
            created = []
            if pending.get("configs"):
                created.append(create_manual_queue_job(profile_id, "config", pending["configs"], interval, batch, interval if interval > 0 else 0))
            if pending.get("proxies"):
                created.append(create_manual_queue_job(profile_id, "proxy", pending["proxies"], interval, batch, interval if interval > 0 else 0))
            ctx.user_data.pop("manual_pending", None)
            ctx.user_data.pop("manual_schedule_custom", None)
            ctx.user_data.pop("manual_schedule_draft", None)
            ctx.user_data.pop("action", None)
            await q.answer("✅ در صف قرار گرفت")
            await q.edit_message_text(
                f"✅ زمان‌بندی ثبت شد.\n\n📋 شناسه صف: {', '.join('#'+str(x) for x in created if x)}\n"
                f"⏱ فاصله: {'فوری' if interval == 0 else str(interval)+' دقیقه'}\n📦 تعداد هر پست: {batch}",
                reply_markup=manual_queue_list_kb(profile_id)
            )
            return

        if d.startswith("mq_custom_interval_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            if not ctx.user_data.get("manual_pending"):
                await q.answer("⚠️ داده ارسال دستی منقضی شده است.", show_alert=True)
                return
            draft = ctx.user_data.setdefault("manual_schedule_draft", {"profile_id": profile_id, "interval": 0, "batch": 1})
            draft["profile_id"] = profile_id
            ctx.user_data["manual_schedule_custom"] = {"profile_id": profile_id, "step": "interval"}
            await q.edit_message_text(
                "⏱ فاصله زمانی را فقط به دقیقه وارد کن. مثال: 30 یا 60 یا 720",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 بازگشت", callback_data=f"mq_schedule_back_{profile_id}", style="primary")
                ]])
            )
            return

        if d.startswith("mq_batch_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            if not ctx.user_data.get("manual_pending"):
                await q.answer("⚠️ داده ارسال دستی منقضی شده است.", show_alert=True)
                return
            draft = ctx.user_data.setdefault("manual_schedule_draft", {"profile_id": profile_id, "interval": 0, "batch": 1})
            draft["profile_id"] = profile_id
            ctx.user_data["manual_schedule_custom"] = {"profile_id": profile_id, "step": "pair"}
            await q.edit_message_text(
                "📦 تعداد در هر پست را وارد کن (1 تا 50).\n"
                "اگر فاصله هم می‌خواهی تغییر کند، فرمت «دقیقه,تعداد» را بفرست؛ مثال: 30,5",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 بازگشت", callback_data=f"mq_schedule_back_{profile_id}", style="primary")
                ]])
            )
            return

        if d.startswith("mq_schedule_back_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            pending = ctx.user_data.get("manual_pending")
            if not pending or int(pending.get("profile_id", -1)) != profile_id:
                await q.answer("⚠️ داده ارسال دستی منقضی شده است.", show_alert=True)
                return
            ctx.user_data.pop("manual_schedule_custom", None)
            draft = ctx.user_data.get("manual_schedule_draft") or {"profile_id": profile_id, "interval": 0, "batch": 1}
            await q.edit_message_text(
                "📋 تنظیمات ارسال دستی\n\n"
                "می‌توانی چند مورد را تنظیم کنی و بعد «ثبت» را بزنی.",
                reply_markup=manual_schedule_kb_with_draft(profile_id, draft)
            )
            return

        if d.startswith("mq_list_"):
            ctx.user_data.pop("manual_queue_edit", None)
            ctx.user_data.pop("manual_schedule_custom", None)
            parts=d.split("_")
            try:
                profile_id=int(parts[2]); page=max(1,int(parts[3])) if len(parts)>3 else 1
            except (ValueError,IndexError):
                await q.answer("⚠️ شناسه/صفحه نامعتبر",show_alert=True); return
            jobs=get_manual_queue(profile_id)
            await q.edit_message_text(f"📋 <b>صف ارسال‌های دستی</b>\nتعداد صف‌ها: <b>{len(jobs)}</b>\nصفحه: <b>{page}</b>",parse_mode="HTML",reply_markup=manual_queue_list_kb(profile_id,page))
            return

        if d.startswith("mq_detail_"):
            ctx.user_data.pop("manual_queue_edit", None)
            ctx.user_data.pop("manual_schedule_custom", None)
            parts = d.split("_")
            try:
                profile_id = int(parts[2]); job_id = int(parts[3]); item_page = int(parts[4]) if len(parts) >= 5 else 1
            except (ValueError, IndexError):
                await q.answer("⚠️ داده نامعتبر"); return
            job = get_manual_queue_job(job_id, profile_id)
            if not job or job.get("status") not in ("pending", "cancelled", "running"):
                await q.answer("این صف دیگر قابل مدیریت نیست", show_alert=True)
                await q.edit_message_text("📋 صف ارسال‌های دستی", reply_markup=manual_queue_list_kb(profile_id)); return
            await q.edit_message_text(manual_queue_text(job, profile_id), parse_mode="HTML", reply_markup=manual_queue_detail_kb(profile_id, job_id, job.get("items") or [], item_page))
            return

        if d.startswith("mq_rm_"):
            parts = d.split("_")
            try:
                profile_id = int(parts[2]); job_id = int(parts[3]); item_index = int(parts[4]); item_page = int(parts[5]) if len(parts) >= 6 else 1
            except (ValueError, IndexError):
                await q.answer("⚠️ داده نامعتبر"); return
            remove_manual_queue_item(job_id, profile_id, item_index)
            job = get_manual_queue_job(job_id, profile_id)
            if not job:
                await q.answer("✅ مورد حذف شد و صف خالی شد")
                await q.edit_message_text("📋 صف فعال", reply_markup=manual_queue_list_kb(profile_id)); return
            await q.answer("✅ مورد حذف شد")
            await q.edit_message_text(manual_queue_text(job, profile_id), parse_mode="HTML", reply_markup=manual_queue_detail_kb(profile_id, job_id, job.get("items") or [], item_page))
            return

        if d.startswith("mq_cancel_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); job_id=int(parts[3])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر"); return
            if cancel_manual_queue_job(job_id, profile_id):
                await q.answer("⛔ ارسال لغو شد؛ صف حذف نشد")
            else:
                await q.answer("⚠️ این صف دیگر قابل لغو نیست", show_alert=True)
            await q.edit_message_text("📋 صف ارسال‌های دستی", reply_markup=manual_queue_list_kb(profile_id)); return

        if d.startswith("mq_resume_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); job_id=int(parts[3])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر"); return
            if resume_manual_queue_job(job_id, profile_id):
                await q.answer("▶️ صف از حالت لغو خارج شد و آماده ارسال است")
            else:
                await q.answer("⚠️ صف قابل بازیابی نیست", show_alert=True)
            job=get_manual_queue_job(job_id, profile_id)
            if job:
                await q.edit_message_text(manual_queue_text(job, profile_id), parse_mode="HTML", reply_markup=manual_queue_detail_kb(profile_id, job_id, job.get("items") or []))
            else:
                await q.edit_message_text("📋 صف ارسال‌های دستی", reply_markup=manual_queue_list_kb(profile_id))
            return

        if d.startswith("mq_delete_") and not d.startswith("mq_delete_yes_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); job_id=int(parts[3])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر"); return
            if not get_manual_queue_job(job_id, profile_id):
                await q.answer("صف پیدا نشد", show_alert=True); return
            await q.edit_message_text(
                "⚠️ <b>حذف کامل صف</b>\n\nاین کار کل صف و موارد باقی‌مانده آن را برای همیشه حذف می‌کند.\nاگر فقط نمی‌خواهی ارسال شود، از «لغو ارسال» استفاده کن.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 بله، حذف کامل", callback_data=f"mq_delete_yes_{profile_id}_{job_id}", style="danger"),
                    InlineKeyboardButton("↩️ بازگشت", callback_data=f"mq_detail_{profile_id}_{job_id}", style="primary")
                ]])
            )
            return

        if d.startswith("mq_delete_yes_"):
            parts=d.split("_")
            try: profile_id=int(parts[3]); job_id=int(parts[4])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر"); return
            delete_manual_queue_job(job_id, profile_id)
            await q.answer("🗑 صف به‌طور کامل حذف شد")
            await q.edit_message_text("📋 صف ارسال‌های دستی", reply_markup=manual_queue_list_kb(profile_id)); return

        if d.startswith("mq_rename_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); job_id=int(parts[3])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر", show_alert=True); return
            if not get_manual_queue_job(job_id, profile_id):
                await q.answer("صف پیدا نشد", show_alert=True); return
            ctx.user_data["manual_queue_rename"]={"profile_id":profile_id,"job_id":job_id}
            await q.answer("نام جدید را ارسال کن")
            await q.edit_message_text("✏️ نام جدید صف را ارسال کن (حداکثر 60 کاراکتر).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"mq_detail_{profile_id}_{job_id}", style="primary")]]))
            return

        if d.startswith("mq_export_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); job_id=int(parts[3])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر", show_alert=True); return
            job=get_manual_queue_job(job_id, profile_id)
            if not job or job.get("kind") != "config":
                await q.answer("صف کانفیگ پیدا نشد", show_alert=True); return
            items=_manual_queue_unposted_items(profile_id, "config", job.get("items") or [])
            if not items:
                await q.answer("کانفیگ فعال و پست‌نشده‌ای در این صف نیست", show_alert=True); return
            safe_name=re.sub(r"[^A-Za-z0-9_-]+", "_", str(job.get("queue_name") or f"queue_{job_id}"))[:40].strip("_") or f"queue_{job_id}"
            path=os.path.join(DATA_DIR, f"{safe_name}_unposted_{get_tehran_date()}.txt")
            try:
                with open(path,"w",encoding="utf-8") as f:
                    f.write("\n".join(items)+"\n")
                with open(path,"rb") as f:
                    await q.message.reply_document(document=f, filename=os.path.basename(path), caption=f"📄 {len(items)} کانفیگ فعال و پست‌نشده از {job.get('queue_name') or ('صف #'+str(job_id))}")
                await q.answer("✅ فایل آماده شد")
            finally:
                try: os.remove(path)
                except OSError: pass
            return

        if d.startswith("mq_add_"):
            parts = d.split("_")
            try:
                profile_id = int(parts[2]); job_id = int(parts[3])
            except (ValueError, IndexError):
                await q.answer("⚠️ داده نامعتبر"); return
            ctx.user_data["manual_queue_add"] = {"profile_id": profile_id, "job_id": job_id}
            await q.answer("ارسال سرور جدید را بفرست")
            await q.edit_message_text("➕ سرور جدید را به صورت متن یا فایل TXT ارسال کن.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"mq_detail_{profile_id}_{job_id}", style="primary")]]))
            return

        if d.startswith("mq_force_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); job_id=int(parts[3])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر", show_alert=True); return
            job=get_manual_queue_job(job_id, profile_id)
            if not job or job.get("status") not in ("pending", "cancelled"):
                await q.answer("صف فعال نیست", show_alert=True); return
            if not (job.get("items") or []):
                await q.answer("صف خالی است", show_alert=True); return
            update_manual_queue_job(job_id, profile_id, status="running", next_run_at=_queue_iso(_queue_now()), last_error="")
            task_key=f"manual_queue_force_{profile_id}_{job_id}"
            asyncio.create_task(_force_manual_queue_send(u.get_bot(), profile_id, job_id), name=task_key)
            await q.answer("🚀 ارسال همین پست شروع شد")
            return

        if d.startswith("mq_edit_back_"):
            parts = d.split("_")
            try:
                profile_id = int(parts[3]); job_id = int(parts[4])
            except (ValueError, IndexError):
                await q.answer("⚠️ داده نامعتبر"); return
            ctx.user_data.pop("manual_queue_edit", None)
            job = get_manual_queue_job(job_id, profile_id)
            if not job:
                await q.answer("❌ صف پیدا نشد", show_alert=True)
                await q.edit_message_text("📋 صف فعال", reply_markup=manual_queue_list_kb(profile_id))
                return
            await q.edit_message_text(
                manual_queue_text(job, profile_id), parse_mode="HTML",
                reply_markup=manual_queue_detail_kb(profile_id, job_id, job.get("items") or [])
            )
            return

        if d.startswith("mq_edit_interval_"):
            parts=d.split("_")
            try: profile_id=int(parts[3]); job_id=int(parts[4])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر"); return
            ctx.user_data["manual_queue_edit"]={"profile_id":profile_id,"job_id":job_id,"field":"interval"}
            await q.edit_message_text("⏱ فاصله جدید را به دقیقه وارد کن. 0 یعنی فوری.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"mq_edit_back_{profile_id}_{job_id}", style="primary")]])); return

        if d.startswith("mq_edit_batch_"):
            parts=d.split("_")
            try: profile_id=int(parts[3]); job_id=int(parts[4])
            except (ValueError,IndexError): await q.answer("⚠️ داده نامعتبر"); return
            ctx.user_data["manual_queue_edit"]={"profile_id":profile_id,"job_id":job_id,"field":"batch"}
            await q.edit_message_text("📦 تعداد جدید در هر پست را وارد کن (1 تا 50).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"mq_edit_back_{profile_id}_{job_id}", style="primary")]])); return

        if d.startswith("manual_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                pending = ctx.user_data.get("manual_pending")
                if pending and int(pending.get("profile_id", -1)) == profile_id:
                    draft = ctx.user_data.get("manual_schedule_draft") or {"profile_id": profile_id, "interval": 0, "batch": 1}
                    ctx.user_data.pop("manual_schedule_custom", None)
                    ctx.user_data.pop("manual_queue_edit", None)
                    await q.edit_message_text(
                        "📋 تنظیمات ارسال دستی\n\n"
                        "می‌توانی چند مورد را تنظیم کنی و بعد «ثبت» را بزنی.",
                        reply_markup=manual_schedule_kb_with_draft(profile_id, draft)
                    )
                else:
                    ctx.user_data["action"] = f"manual_{profile_id}"
                    await q.edit_message_text(msg("manual_send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"prof_{profile_id}", style="danger")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Backup export, timer, log, cron, blacklist
        if d.startswith("backup_export_menu_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(msg("backup_export_type"), reply_markup=backup_export_type_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("backup_export_type_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    profile_id = int(parts[3])
                    backup_type = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["backup_export"] = {"profile_id": profile_id, "type": backup_type}
                await q.edit_message_text(msg("backup_export_scope"), reply_markup=backup_export_scope_kb(profile_id, backup_type))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("backup_export_scope_"):
            parts = d.split("_")
            if len(parts) >= 6:
                try:
                    profile_id = int(parts[3])
                    backup_type = parts[4]
                    scope = parts[5]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if scope == "all":
                    await export_backup(q, ctx, profile_id, backup_type, -1)
                    await q.edit_message_text("✅ بک‌آپ ارسال شد.")
                elif scope == "100":
                    await export_backup(q, ctx, profile_id, backup_type, 100)
                    await q.edit_message_text("✅ بک‌آپ ارسال شد.")
                elif scope == "custom":
                    ctx.user_data["backup_export_custom"] = {"profile_id": profile_id, "type": backup_type}
                    await q.edit_message_text(msg("backup_export_count_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"backup_export_menu_{profile_id}", style="primary")]]))
                else:
                    await q.answer("⚠️ محدوده نامعتبر")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                expiry, remaining = get_profile_timer(profile_id)
                if expiry:
                    status = msg("timer_status_active", remaining=remaining)
                else:
                    status = msg("timer_status_inactive")
                txt = msg("timer_menu", name=prof["dest_name"], status=status)
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=timer_menu_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_set_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    minutes = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_timer(profile_id, minutes)
                await q.answer(msg("timer_set", minutes=minutes))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_clear_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                clear_profile_timer(profile_id)
                await q.answer(msg("timer_cleared"))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_custom_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"timer_custom_{profile_id}"
                await q.edit_message_text(msg("timer_custom_prompt"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(msg("btn_back"), callback_data=f"timer_menu_{profile_id}", style="primary")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("log_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(
                    msg("log_menu_title"),
                    parse_mode="HTML",
                    reply_markup=log_menu_kb(profile_id)
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("log_full_") or d.startswith("log_errors_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                log_type = "full" if d.startswith("log_full_") else "errors"
                await q.edit_message_text(
                    msg("log_range_title", log_type=log_type),
                    parse_mode="HTML",
                    reply_markup=log_range_kb(profile_id, log_type)
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("log_range_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    profile_id = int(parts[2])
                    log_type = parts[3]
                    minutes = int(parts[4])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await get_logs(q, ctx, profile_id, log_type, minutes)
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setbackupinterval_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setbackupinterval_{profile_id}"
                current = get_profile_backup_interval(profile_id)
                await q.edit_message_text(f"بازه فعلی: {current}\n{msg('backup_interval_prompt')}", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"prof_{profile_id}", style="primary")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setcron_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setcron_{profile_id}"
                current = get_profile_schedule_cron(profile_id) or "خالی"
                await q.edit_message_text(f"⏰ کرون فعلی: {current}\n\n" + msg("schedule_cron_prompt"), reply_markup=empty_button_kb(profile_id, f"empty_cron_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_cron_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_schedule_cron(profile_id, "")
                await q.answer("✅ کرون پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("bl_list_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); page=int(parts[3]) if len(parts)>=4 else 1
            except (ValueError,IndexError): await q.answer("⚠️ شناسه نامعتبر"); return
            prof=get_profile(profile_id); name=prof["dest_name"] if prof else ""; words=get_blacklist(profile_id); per_page=20
            total_pages=max(1,(len(words)+per_page-1)//per_page); page=min(max(1,page),total_pages); start=(page-1)*per_page; shown=words[start:start+per_page]
            words_text="\n".join([f"• {start+i+1}. `{w}`" for i,w in enumerate(shown)]) if shown else msg("blacklist_empty")
            txt=msg("blacklist_title",name=name,words=words_text)+f"\n\n📄 صفحه {page}/{total_pages} | کل: {len(words)}"
            await q.edit_message_text(txt,parse_mode="HTML",reply_markup=blacklist_kb(profile_id,page)); return

        if d.startswith("bl_add_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"bl_add_{profile_id}"
                await q.edit_message_text(msg("blacklist_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"bl_list_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("bl_del_"):
            parts=d.split("_")
            try: profile_id=int(parts[2]); idx=int(parts[3]); page=int(parts[4]) if len(parts)>=5 else 1
            except (ValueError,IndexError): await q.answer("⚠️ شناسه نامعتبر"); return
            words=get_blacklist(profile_id)
            if 0<=idx<len(words):
                removed=words[idx]; remove_blacklist_word(profile_id,removed); await q.answer(msg("blacklist_removed"))
                prof=get_profile(profile_id); name=prof["dest_name"] if prof else ""; words=get_blacklist(profile_id); per_page=20; total_pages=max(1,(len(words)+per_page-1)//per_page); page=min(max(1,page),total_pages); start=(page-1)*per_page; shown=words[start:start+per_page]
                words_text="\n".join([f"• {start+i+1}. `{w}`" for i,w in enumerate(shown)]) if shown else msg("blacklist_empty")
                txt=msg("blacklist_title",name=name,words=words_text)+f"\n\n📄 صفحه {page}/{total_pages} | کل: {len(words)}"
                await q.edit_message_text(txt,parse_mode="HTML",reply_markup=blacklist_kb(profile_id,page))
            else: await q.answer("❌ مورد پیدا نشد؛ لیست به‌روز شده است.",show_alert=True)
            return

        if d.startswith("bl_clear_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                clear_blacklist(profile_id)
                await q.answer(msg("blacklist_clear"))
                prof = get_profile(profile_id)
                name = prof["dest_name"] if prof else ""
                txt = msg("blacklist_title", name=name, words=msg("blacklist_empty"))
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=blacklist_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("backup_"):
            try:
                await q.edit_message_text("⏳ در حال تهیه بک‌آپ...")
                with open(DB_PATH, "rb") as f:
                    await q.message.reply_document(
                        document=f,
                        filename=f"bot_backup_{get_tehran_date()}.db",
                        caption=f"💾 بک‌آپ دیتابیس - {get_tehran_time()}"
                    )
                await q.message.edit_text("✅ " + msg("backup_sent"))
            except Exception as e:
                log.error(f"Backup error: {e}")
                await q.message.edit_text("❌ " + msg("backup_failed"))
            return

        if d.startswith("delprof_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                txt = msg("delete_confirm1", name=prof["dest_name"], id=profile_id)
                await q.edit_message_text(
                    txt,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delprof_confirm1_{profile_id}", style="danger")],
                        [InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("delprof_confirm1_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                txt = msg("delete_confirm2", name=prof["dest_name"], id=profile_id)
                await q.edit_message_text(
                    txt,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 حذف نهایی", callback_data=f"delprof_confirm2_{profile_id}", style="danger")],
                        [InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("delprof_confirm2_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                delete_profile(profile_id)
                await q.answer("✅ پروفایل حذف شد.")
                await q.edit_message_text(msg("profile_deleted"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="profiles_list", style="primary")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_cfg_header_"):
            try:
                profile_id=int(d.rsplit("_",1)[1]); current=get_profile_config_header_enabled(profile_id)
                set_profile_config_header_enabled(profile_id,not current)
                await q.answer("✅ عنوان بخش فعال شد." if not current else "🗑 عنوان بخش حذف شد.")
                await show_profile_admin(q.message,profile_id)
            except (ValueError,IndexError) as exc:
                log.exception("config header toggle failed"); await q.answer(f"⚠️ خطا: {exc}",show_alert=True)
            return
        if d.startswith("tgl_low_cost_"):
            try:
                profile_id=int(d.rsplit("_",1)[1]); current=get_profile_low_cost_mode(profile_id)
                set_profile_low_cost_mode(profile_id,not current)
                await q.answer("⚡ حالت کم‌مصرف فعال شد." if not current else "⚡ حالت کم‌مصرف خاموش شد.")
                await show_profile_admin(q.message,profile_id)
            except (ValueError,IndexError) as exc:
                log.exception("low cost toggle failed"); await q.answer(f"⚠️ خطا: {exc}",show_alert=True)
            return

        # New toggles for country
        if d.startswith("tgl_country_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_country_display(profile_id)
                new_mode = (current + 1) % 3
                set_profile_country_display(profile_id, new_mode)
                mode_names = {0: msg("country_display_off"), 1: msg("country_display_en"), 2: msg("country_display_enfa")}
                await q.answer(msg("country_display_set", mode=mode_names[new_mode]))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Config header template
        if d.startswith("cfg_header_tpl_default_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                await q.answer("⚠️ شناسه پروفایل نامعتبر", show_alert=True); return
            reset_profile_config_header_template(profile_id)
            ctx.user_data.pop("action", None)
            await q.answer("✅ قالب عنوان به پیش‌فرض برگشت")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("cfg_header_tpl_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                await q.answer("⚠️ شناسه پروفایل نامعتبر", show_alert=True); return
            current = get_profile_config_header_template(profile_id)
            ctx.user_data["action"] = f"cfg_header_tpl_{profile_id}"
            await q.edit_message_text(
                "🏷 <b>قالب عنوان کانفیگ</b>\n\n"
                f"قالب فعلی: <code>{html.escape(current)}</code>\n\n"
                "توکن‌ها: [Protocol] [Flag] [Country] [COUNTRY_EN] [COUNTRY_FA] [CHANNEL_ID] [COUNT] [PING]\n\n"
                "قالب پیش‌فرض: <code>[Protocol] [Flag] [Country]</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♻️ بازگردانی پیش‌فرض", callback_data=f"cfg_header_tpl_default_{profile_id}", style="success")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"prof_{profile_id}", style="primary")]
                ])
            )
            return

        # Naming template and channel link
        if d.startswith("set_naming_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_naming_{profile_id}"
                current = get_profile_naming_template(profile_id)
                await q.edit_message_text(
                    f"قالب فعلی:\n`{current}`\n\n" + msg("naming_template_prompt"),
                    parse_mode="HTML",
                    reply_markup=empty_button_kb(profile_id, f"empty_naming_{profile_id}")
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_naming_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_naming_template(profile_id, "{Flag} | ⚡️Telegram = {CHANNEL_ID}")
                await q.answer("✅ قالب به پیش‌فرض برگردانده شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_channel_link_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                await q.answer("⚠️ شناسه پروفایل نامعتبر", show_alert=True)
                return
            if not get_profile(profile_id):
                await q.answer("⚠️ پروفایل یافت نشد", show_alert=True)
                return
            ctx.user_data["action"] = f"set_channel_link_{profile_id}"
            current = get_profile_channel_link(profile_id) or "خالی"
            await q.edit_message_text(
                f"🔗 لینک کانال فعلی: <code>{html.escape(current)}</code>\n\n" + msg("channel_link_prompt"),
                parse_mode="HTML",
                reply_markup=empty_button_kb(profile_id, f"empty_channel_link_{profile_id}")
            )
            return

        if d.startswith("empty_channel_link_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_channel_link(profile_id, "")
                await q.answer("✅ لینک کانال پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        await show_profiles_list(q.message)

    except Exception as e:
        log.error(f"❌ on_callback ERROR: {e}\n{traceback.format_exc()}")
        try:
            await q.edit_message_text(f"⚠️ خطا: {str(e)[:100]}")
        except:
            pass

async def show_profile_admin(msg_or_q, profile_id):
    prof = get_profile(profile_id)
    if not prof:
        txt = msg("profile_not_found")
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt)
        else:
            await msg_or_q.reply_text(txt)
        return
    srcs = get_profile_sources(profile_id)
    dest = prof["dest_name"] or "تنظیم نشده"
    last_num = prof["last_num"]
    interval_cfg = prof.get("interval_config", 5)
    interval_prx = prof.get("interval_proxy", 5)
    max_cfg = prof.get("max_post_config", 8)
    max_prx = prof.get("max_post_proxy", 10)
    show_num = prof["show_numbers"] == 1
    custom_query = prof["custom_query"] or "خالی"
    show_date_cfg = prof["show_date_config"] == 1
    show_date_prx = prof["show_date_proxy"] == 1
    cron = prof["schedule_cron"] or "خالی"
    backup_interval = get_profile_backup_interval(profile_id)
    sponsors = get_sponsors(profile_id)
    sponsor_st = f"{len(sponsors)} اسپانسر" if sponsors else "خالی"
    ping_mode = prof["ping_mode"]
    ping_display = f"ویتوری:{get_profile_config_ping_mode(profile_id)} / پروکسی:{get_profile_proxy_ping_mode(profile_id)}"
    ping_testing = get_profile_ping_enabled(profile_id)
    ping_status = "✅" if ping_testing else "❌"
    profile_enabled = get_profile_enabled(profile_id)
    profile_status = "✅" if profile_enabled else "❌"

    post_cfg = prof["post_configs"] == 1
    post_prx = prof["post_proxies"] == 1
    cfg_status = "✅" if post_cfg else "❌"
    prx_status = "✅" if post_prx else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"

    naming_template = get_profile_naming_template(profile_id)
    channel_link = get_profile_channel_link(profile_id) or "خالی"

    country_display = prof.get("country_display", 2)
    country_display_modes = {0: "خاموش", 1: "انگلیسی", 2: "انگلیسی+فارسی"}
    country_label = country_display_modes.get(country_display, "انگلیسی+فارسی")

    expiry, remaining = get_profile_timer(profile_id)
    if expiry:
        timer_status = msg("timer_status_active", remaining=remaining)
    else:
        timer_status = msg("timer_status_inactive")

    txt = msg(
        "admin_panel",
        srcs=len(srcs), dest=dest,
        name=dest, num=last_num,
        cfg_interval=interval_cfg, prx_interval=interval_prx,
        max_cfg=max_cfg, max_prx=max_prx,
        sponsor=sponsor_st,
        ping_mode=ping_display,
        cfg_status=cfg_status,
        prx_status=prx_status,
        numbers_status=num_status,
        custom_query=custom_query,
        date_cfg=date_cfg_status,
        date_prx=date_prx_status,
        cron=cron,
        timer_status=timer_status,
        backup_interval=backup_interval,
        naming=naming_template,
        channel_link=channel_link,
        ping_status=ping_status,
        profile_status=profile_status,
        country_display=country_label,
        config_header_status="فعال" if get_profile_config_header_enabled(profile_id) else "حذف",
        low_cost_status="فعال" if get_profile_low_cost_mode(profile_id) else "خاموش",
    )
    kb = profile_admin_kb(profile_id)
    try:
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_q.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise

# ======================================================================
# هندلرهای متنی و سند (بدون تغییر، حذف بخش فایل)
# ======================================================================
async def _on_text_impl(u, ctx):
    if not is_admin(u.effective_user.id):
        return

    # Manual queue custom scheduling input.
    custom = ctx.user_data.get("manual_schedule_custom")
    if custom:
        profile_id = int(custom["profile_id"])
        pending = ctx.user_data.get("manual_pending")
        if not pending or int(pending.get("profile_id", -1)) != profile_id:
            ctx.user_data.pop("manual_schedule_custom", None)
            ctx.user_data.pop("manual_schedule_draft", None)
            await u.message.reply_text("❌ داده ارسال دستی منقضی شده است.")
            return
        raw = (u.message.text or "").strip()
        try:
            draft = ctx.user_data.setdefault(
                "manual_schedule_draft",
                {"profile_id": profile_id, "interval": 0, "batch": 1}
            )
            if custom.get("step") == "interval":
                interval = int(raw)
                if interval < 0 or interval > 100000:
                    raise ValueError
                draft["interval"] = interval
            else:
                parts = re.split(r"[,،\s]+", raw)
                if len(parts) == 1:
                    batch = int(parts[0])
                    if not (1 <= batch <= 50):
                        raise ValueError
                    draft["batch"] = batch
                elif len(parts) == 2:
                    interval, batch = int(parts[0]), int(parts[1])
                    if interval < 0 or interval > 100000 or not (1 <= batch <= 50):
                        raise ValueError
                    draft["interval"] = interval
                    draft["batch"] = batch
                else:
                    raise ValueError
        except ValueError:
            await u.message.reply_text("❌ فرمت نامعتبر. فاصله: 30 یا تعداد: 5 یا هر دو: 30,5")
            return

        ctx.user_data["manual_schedule_draft"] = draft
        ctx.user_data.pop("manual_schedule_custom", None)
        await u.message.reply_text(
            "✅ مقدار ذخیره شد؛ هنوز صف ثبت نشده است.\n"
            "می‌توانی تنظیم دیگری را هم تغییر بدهی و بعد «ثبت» را بزن.",
            reply_markup=manual_schedule_kb_with_draft(profile_id, draft)
        )
        return

    rename_state=ctx.user_data.get("manual_queue_rename")
    if rename_state and u.message.text:
        name=" ".join((u.message.text or "").split()).strip()[:60]
        if not name:
            await u.message.reply_text("❌ نام صف نمی‌تواند خالی باشد.")
            return
        if update_manual_queue_job(int(rename_state["job_id"]), int(rename_state["profile_id"]), queue_name=name):
            ctx.user_data.pop("manual_queue_rename",None)
            job=get_manual_queue_job(int(rename_state["job_id"]), int(rename_state["profile_id"]))
            await u.message.reply_text("✅ نام صف ذخیره شد.", reply_markup=manual_queue_detail_kb(int(rename_state["profile_id"]), int(rename_state["job_id"]), job.get("items") or []))
        else:
            await u.message.reply_text("❌ ذخیره نام صف ناموفق بود.")
        return

    add_state = ctx.user_data.get("manual_queue_add")
    if add_state and u.message.text:
        items=[]
        text=u.message.text or ""
        for url in extract_links_from_text(text):
            if detect_config_protocol(url): items.append(clean_config_url(url))
        for url in extract_proxy_links_from_text(text):
            norm=normalize_proxy_url(url)
            if norm: items.append(norm)
        for line in text.splitlines():
            line=line.strip()
            if detect_config_protocol(line): items.append(clean_config_url(line))
            elif detect_proxy_protocol(line): items.append(normalize_proxy_url(line))
        items=list(dict.fromkeys(x for x in items if x))
        try:
            job=get_manual_queue_job(add_state["job_id"], add_state["profile_id"])
            if not job:
                await u.message.reply_text("❌ صف پیدا نشد.")
            else:
                kind=str(job.get("kind") or "")
                items=_manual_queue_unposted_items(add_state["profile_id"], kind, items)
                if items and add_manual_queue_items(add_state["job_id"], add_state["profile_id"], items):
                    await u.message.reply_text(f"✅ {len(items)} مورد جدید به صف اضافه شد")
                else:
                    await u.message.reply_text("⚠️ مورد جدید و تکرارنشده‌ای برای این صف پیدا نشد.")
        finally:
            ctx.user_data.pop("manual_queue_add", None)
        return

    edit = ctx.user_data.get("manual_queue_edit")
    if edit:
        profile_id = int(edit["profile_id"]); job_id = int(edit["job_id"]); field = edit["field"]
        try:
            value = int((u.message.text or "").strip())
            if field == "interval":
                if value < 0 or value > 100000: raise ValueError
                job = get_manual_queue_job(job_id, profile_id)
                if not job: raise ValueError
                next_run = _queue_now() if value == 0 else _queue_now() + timedelta(minutes=value)
                update_manual_queue_job(job_id, profile_id, interval_minutes=value, next_run_at=_queue_iso(next_run), last_error="")
            else:
                if not (1 <= value <= 50): raise ValueError
                update_manual_queue_job(job_id, profile_id, batch_size=value, last_error="")
        except ValueError:
            await u.message.reply_text("❌ مقدار نامعتبر است.")
            return
        ctx.user_data.pop("manual_queue_edit", None)
        job = get_manual_queue_job(job_id, profile_id)
        if job:
            await u.message.reply_text("✅ تنظیم صف تغییر کرد.", reply_markup=manual_queue_detail_kb(profile_id, job_id, job.get("items") or []))
        else:
            await u.message.reply_text("❌ صف پیدا نشد.", reply_markup=manual_queue_list_kb(profile_id))
        return

    if ctx.user_data.get("action", "").startswith("timer_custom_"):
        profile_id = int(ctx.user_data["action"].split("_")[2])
        try:
            minutes = int(u.message.text.strip())
            if minutes <= 0:
                await u.message.reply_text("❌ عدد باید مثبت باشد.")
                return
            set_profile_timer(profile_id, minutes)
            await u.message.reply_text(msg("timer_set", minutes=minutes))
        except ValueError:
            await u.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if ctx.user_data.get("backup_export_custom"):
        data = ctx.user_data["backup_export_custom"]
        profile_id = data["profile_id"]
        backup_type = data["type"]
        try:
            count = int(u.message.text.strip())
            if count < 1:
                raise ValueError
        except:
            await u.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
            return
        await export_backup(u, ctx, profile_id, backup_type, count)
        del ctx.user_data["backup_export_custom"]
        await u.message.reply_text("✅ بک‌آپ ارسال شد.")
        return

    # Sponsor edit field
    if ctx.user_data.get("sponsor_edit"):
        data=ctx.user_data["sponsor_edit"]
        sponsor_id=int(data["sponsor_id"])
        field=data["field"]
        txt=u.message.text.strip()
        c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
        row=c.fetchone()
        if not row:
            await u.message.reply_text("اسپانسر یافت نشد.")
            ctx.user_data.pop("sponsor_edit", None)
            return
        profile_id=row[0]
        if field in ("name","button_text"):
            if not txt:
                await u.message.reply_text("❌ مقدار نمی‌تواند خالی باشد.")
                return
            update_sponsor(sponsor_id, **{field:txt})
        elif field=="url":
            if not re.match(r"^(https?://|tg://)", txt, re.I):
                await u.message.reply_text("❌ لینک معتبر نیست. لینک باید با https:// یا tg:// شروع شود.")
                return
            update_sponsor(sponsor_id, url=txt)
        elif field=="priority":
            try: value=int(txt)
            except Exception:
                await u.message.reply_text("❌ اولویت باید عدد باشد.")
                return
            update_sponsor(sponsor_id, priority=value)
        elif field=="duration_hours":
            try: value=int(txt)
            except Exception:
                await u.message.reply_text("❌ مدت باید عدد غیرمنفی باشد.")
                return
            if value<0:
                await u.message.reply_text("❌ مدت نمی‌تواند منفی باشد.")
                return
            update_sponsor(sponsor_id, duration_hours=value, unlimited=0)
        elif field=="unlimited":
            c.execute("SELECT unlimited FROM sponsors WHERE id=?", (sponsor_id,))
            cur=c.fetchone()
            unlimited=0 if cur and cur[0] else 1
            update_sponsor(sponsor_id, unlimited=unlimited, duration_hours=0 if unlimited else None)
            # None duration means preserve current when turning unlimited off.
            if not unlimited:
                c.execute("SELECT duration_hours FROM sponsors WHERE id=?", (sponsor_id,))
                current=c.fetchone()
                if current and current[0] is None:
                    update_sponsor(sponsor_id, duration_hours=1)
        else:
            await u.message.reply_text("این فیلد با دکمه مخصوص ویرایش می‌شود.")
            return
        ctx.user_data.pop("sponsor_edit", None)
        await u.message.reply_text(msg("sp_edit_done"))
        await show_profile_admin(u.message, profile_id)
        return

    # Sponsor add: if we have step and not handled by callback, process text
    if ctx.user_data.get("sponsor_add"):
        data = ctx.user_data["sponsor_add"]
        step = data.get("step")
        profile_id = data.get("profile_id")
        if step == "name":
            name = u.message.text.strip()
            if name:
                data["name"] = name
                data["step"] = "url"
                await u.message.reply_text(msg("sp_add_url"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            else:
                await u.message.reply_text("❌ نام نمی‌تواند خالی باشد.")
            return
        elif step == "url":
            url = u.message.text.strip()
            if url:
                data["url"] = url
                data["step"] = "button_text"
                await u.message.reply_text(msg("sp_add_text"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            else:
                await u.message.reply_text("❌ لینک نمی‌تواند خالی باشد.")
            return
        elif step == "button_text":
            data["button_text"] = u.message.text.strip() or "Advertisement"
            data["step"] = "priority"
            await u.message.reply_text(msg("sp_add_priority"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            return
        elif step == "priority":
            try:
                priority = int(u.message.text.strip() or "0")
                data["priority"] = priority
                data["step"] = "unlimited"
                await u.message.reply_text(msg("sp_add_unlimited"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("بله", callback_data=f"sp_add_unlimited_yes_{profile_id}", style="primary")],
                    [InlineKeyboardButton("خیر", callback_data=f"sp_add_unlimited_no_{profile_id}", style="primary")],
                    [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                ]))
            except:
                await u.message.reply_text("❌ اولویت باید عدد باشد.")
            return
        elif step == "duration":
            try:
                duration = int(u.message.text.strip())
                if duration < 0:
                    raise ValueError
                data["duration_hours"] = duration
                data["unlimited"] = 0
                data["step"] = "color"
                await u.message.reply_text(msg("sp_add_color"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_add_color_{profile_id}_primary", style="primary")],
                    [InlineKeyboardButton("🟢 Success", callback_data=f"sp_add_color_{profile_id}_success", style="success")],
                    [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_add_color_{profile_id}_danger", style="danger")],
                    [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                ]))
            except:
                await u.message.reply_text("❌ مدت باید عدد غیرمنفی باشد.")
            return
        elif step == "apply":
            # handled by callback
            pass
        elif step == "color":
            # handled by callback
            pass
        else:
            del ctx.user_data["sponsor_add"]
            await u.message.reply_text("❌ خطا در روند افزودن اسپانسر.")
            return

    a = ctx.user_data.get("action")
    if not a:
        return

    t = u.message.text.strip()

    if a.startswith("delete_channel_posts_"):
        try:
            profile_id = int(a.rsplit("_", 1)[1])
            count = int(t)
            if not (1 <= count <= 5000):
                raise ValueError
        except ValueError:
            await u.message.reply_text("❌ تعداد باید بین 1 تا 5000 باشد.")
            return
        ctx.user_data.pop("action", None)
        await u.message.reply_text(
            f"⚠️ <b>تأیید حذف</b>\n\nتعداد: <b>{count}</b> پست\n"
            "حذف از <b>جدیدترین پست‌های کانال</b> شروع می‌شود.\n\nآیا مطمئنی؟",
            parse_mode="HTML", reply_markup=channel_delete_confirm_kb(profile_id, count)
        )
        return

    if a.startswith("setbackupinterval_"):
        profile_id = int(a.split("_")[1])
        try:
            interval = int(t)
            if interval < 1:
                raise ValueError
            set_profile_backup_interval(profile_id, interval)
            await u.message.reply_text(msg("backup_interval_set", n=interval))
        except ValueError:
            await u.message.reply_text("❌ لطفاً یک عدد صحیح بزرگتر از صفر وارد کنید.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("bl_add_"):
        profile_id = int(a.split("_")[2])
        if not t:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', t)
        items = [x.strip().lower() for x in items if x.strip()]
        added = []
        for word in items:
            if add_blacklist_word(profile_id, word):
                added.append(word)
        if added:
            await u.message.reply_text(msg("blacklist_added", words=", ".join(added)))
        else:
            await u.message.reply_text("❌ هیچ کلمه‌ای اضافه نشد (تکراری یا نامعتبر).")
        del ctx.user_data["action"]
        prof = get_profile(profile_id)
        name = prof["dest_name"] if prof else ""
        words = get_blacklist(profile_id)
        words_text = "\n".join([f"• `{w}`" for w in words]) if words else msg("blacklist_empty")
        txt = msg("blacklist_title", name=name, words=words_text)
        await u.message.reply_text(txt, parse_mode="HTML", reply_markup=blacklist_kb(profile_id))
        return

    if a.startswith("setcron_"):
        profile_id = int(a.split("_")[1])
        cron = t.strip()
        if cron:
            parts = cron.split()
            if len(parts) == 5:
                set_profile_schedule_cron(profile_id, cron)
                await u.message.reply_text(msg("schedule_cron_set", cron=cron))
            else:
                await u.message.reply_text("❌ فرمت cron نامعتبر. مثال: `*/5 * * * *`")
        else:
            set_profile_schedule_cron(profile_id, "")
            await u.message.reply_text("✅ کرون پاک شد.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a == "add_admin":
        try:
            new_id = int(t.strip())
        except ValueError:
            await u.message.reply_text("❌ شناسه باید عدد باشد.")
            return
        if new_id == MAIN_ADMIN_ID:
            await u.message.reply_text("❌ این ادمین اصلی است و قبلاً وجود دارد.")
            return
        if is_admin(new_id):
            await u.message.reply_text("❌ این کاربر قبلاً ادمین است.")
            return
        add_admin(new_id, u.effective_user.id)
        await u.message.reply_text(msg("admin_added", id=new_id))
        del ctx.user_data["action"]
        await u.message.reply_text("📋 لیست ادمین‌ها:", reply_markup=manage_admins_kb())
        return

    if a == "remove_admin":
        try:
            rem_id = int(t.strip())
        except ValueError:
            await u.message.reply_text("❌ شناسه باید عدد باشد.")
            return
        if rem_id == MAIN_ADMIN_ID:
            await u.message.reply_text(msg("admin_cannot_remove_main"))
            return
        if not is_admin(rem_id):
            await u.message.reply_text("❌ این کاربر ادمین نیست.")
            return
        if remove_admin(rem_id):
            await u.message.reply_text(msg("admin_removed", id=rem_id))
        else:
            await u.message.reply_text("❌ حذف انجام نشد.")
        del ctx.user_data["action"]
        await u.message.reply_text("📋 لیست ادمین‌ها:", reply_markup=manage_admins_kb())
        return

    if a == "prof_add":
        dest_name = t if t else None
        if not dest_name:
            await u.message.reply_text("❌ نام مقصد خالی است.")
            return
        dest_name = normalize_channel_input(dest_name)
        if not dest_name:
            await u.message.reply_text("❌ نام مقصد نامعتبر است.")
            return
        profiles = get_profiles()
        if any(p["dest_name"] == dest_name for p in profiles):
            await u.message.reply_text("❌ این مقصد قبلاً وجود دارد.")
            return
        new_id = create_profile(dest_name)
        await u.message.reply_text(msg("profile_added", name=dest_name))
        del ctx.user_data["action"]
        if ENABLE_AUTO:
            bot = u.get_bot()
            # A new profile must use the same precise scheduler as profiles
            # loaded at startup. Do not call Bot.create_task (Bot has no such
            # API); schedule these coroutines on the running event loop.
            # Use the current Application when available; otherwise schedule on the running loop.
            app_obj = getattr(ctx, "application", None)
            if app_obj is not None:
                start_worker(app_obj, f"auto_config_{new_id}", lambda: _profile_scheduler_v16(bot, new_id, "config"))
                start_worker(app_obj, f"auto_proxy_{new_id}", lambda: _profile_scheduler_v16(bot, new_id, "proxy"))
            else:
                asyncio.create_task(_profile_scheduler_v16(bot, new_id, "config"), name=f"auto_config_{new_id}")
                asyncio.create_task(_profile_scheduler_v16(bot, new_id, "proxy"), name=f"auto_proxy_{new_id}")
            log.info(f"⏰ Started precise auto schedulers for new profile {new_id}")
        await show_profiles_list(u.message)
        return

    if a.startswith("sa_"):
        profile_id = int(a.split("_")[1])
        if not t:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', t)
        normalized_items = []
        for item in items:
            item = normalize_channel_input(item.strip())
            if item:
                normalized_items.append(item)
        if not normalized_items:
            await u.message.reply_text("❌ هیچ منبع معتبری یافت نشد.")
            return
        srcs = get_profile_sources(profile_id)
        added = []
        for item in normalized_items:
            if item not in srcs:
                srcs.append(item)
                added.append(item)
        if added:
            set_profile_sources(profile_id, srcs)
            await u.message.reply_text(msg("added", item=", ".join(added)))
        else:
            await u.message.reply_text("همه موارد تکراری بودند.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("da_"):
        profile_id = int(a.split("_")[1])
        dest = t if t else None
        if not dest:
            set_profile_dest(profile_id, "")
            await u.message.reply_text(msg("removed"))
        else:
            dest = normalize_channel_input(dest)
            if not dest:
                await u.message.reply_text("❌ مقصد نامعتبر است.")
                return
            set_profile_dest(profile_id, dest)
            await u.message.reply_text(msg("dest_set", dest=dest))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ac_"):
        profile_id = int(a.split("_")[1])
        name = t if t else ""
        if name:
            name = normalize_channel_input(name)
        set_profile_dest(profile_id, name)
        await u.message.reply_text(msg("name_set", name=name if name else "حذف شد"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ab_config_"):
        profile_id = int(a.split("_")[2])
        if not t:
            await u.message.reply_text("❌ بنر خالی است.")
            return
        if "{configs}" in t:
            update_profile(profile_id, banner_config=t)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ab_proxy_"):
        profile_id = int(a.split("_")[2])
        if not t:
            await u.message.reply_text("❌ بنر خالی است.")
            return
        if "{proxies}" in t:
            update_profile(profile_id, banner_proxy=t)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_cfg_interval_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 0 <= n <= 1440:
            set_profile_interval_config(profile_id, n)
            await u.message.reply_text(msg("interval_ok", n=n))
        else:
            return await u.message.reply_text(msg("interval_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_prx_interval_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 0 <= n <= 1440:
            set_profile_interval_proxy(profile_id, n)
            await u.message.reply_text(msg("interval_ok", n=n))
        else:
            return await u.message.reply_text(msg("interval_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_cfg_max_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 1 <= n <= 50:
            set_profile_max_post_config(profile_id, n)
            await u.message.reply_text(msg("max_ok", n=n))
        else:
            return await u.message.reply_text(msg("max_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_prx_max_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 1 <= n <= 50:
            set_profile_max_post_proxy(profile_id, n)
            await u.message.reply_text(msg("max_ok", n=n))
        else:
            return await u.message.reply_text(msg("max_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=False, ctx=ctx)
        del ctx.user_data["action"]
        return

    if a.startswith("setquery_"):
        profile_id = int(a.split("_")[1])
        query = t if t else ""
        set_profile_custom_query(profile_id, query)
        await u.message.reply_text(msg("custom_query_set", query=query if query else "خالی"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_naming_"):
        profile_id = int(a.split("_")[2])
        template = t.strip()
        if not template:
            await u.message.reply_text("❌ قالب خالی است.")
            return
        normalized_template = template.replace("[", "{").replace("]", "}")
        allowed_tokens = ("{Flag}", "{FLAG}", "{Protocol}", "{PROTOCOL}", "{COUNTRY_EN}", "{COUNTRY_FA}", "{Country}", "{COUNTRY}", "{CHANNEL_ID}", "{COUNT}", "{PING}")
        if not any(token in normalized_template for token in allowed_tokens):
            await u.message.reply_text("❌ قالب باید حداقل یکی از متغیرهای Protocol / Flag / Country / Channel / Count را داشته باشد.")
            return
        set_profile_naming_template(profile_id, template)
        await u.message.reply_text(msg("naming_template_set", template=template))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_channel_link_"):
        try:
            profile_id = int(a.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await u.message.reply_text("❌ شناسه پروفایل نامعتبر است.")
            return
        channel_link = t.strip()
        if channel_link:
            normalized = channel_link
            normalized = re.sub(r"^https?://t\.me/", "", normalized, flags=re.IGNORECASE)
            normalized = re.sub(r"^t\.me/", "", normalized, flags=re.IGNORECASE)
            normalized = normalized.split("?", 1)[0].split("#", 1)[0].strip()
            normalized = normalized.lstrip("@/")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", normalized):
                await u.message.reply_text("❌ لینک کانال معتبر نیست. مثال: @MyChannel")
                return
            channel_link = normalized
        set_profile_channel_link(profile_id, channel_link)
        saved = get_profile_channel_link(profile_id)
        if saved != channel_link:
            await u.message.reply_text("❌ ذخیره لینک کانال تأیید نشد.")
            return
        await u.message.reply_text(msg("channel_link_set", link=saved if saved else "خالی"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

async def _process_manual_queue_add_document(u, ctx, state):
    """Accept TXT/encoded TXT directly when adding servers to an existing queue."""
    profile_id=int(state["profile_id"]); job_id=int(state["job_id"])
    doc=u.message.document
    if not doc:
        return False
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await u.message.reply_text("❌ فایل بزرگ است؛ حداکثر 5MB")
        return True
    try:
        f=await doc.get_file()
        data=await f.download_as_bytearray()
        text=data.decode("utf-8", errors="ignore")
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", text.strip() or ""):
            try:
                decoded=base64.b64decode(text.strip(), validate=True).decode("utf-8", errors="ignore")
                if decoded.strip(): text=decoded
            except Exception:
                pass
        configs=[]; proxies=[]
        for url in extract_links_from_text(text):
            if detect_config_protocol(url): configs.append(clean_config_url(url))
        for url in extract_proxy_links_from_text(text):
            norm=normalize_proxy_url(url)
            if norm and is_telegram_proxy_url(norm): proxies.append(norm)
        # Also accept one raw URI per line, including formats the generic extractor misses.
        for raw in text.splitlines():
            line=raw.strip()
            if not line: continue
            if detect_config_protocol(line): configs.append(clean_config_url(line))
            elif detect_proxy_protocol(line): proxies.append(normalize_proxy_url(line))
        configs=list(dict.fromkeys(x for x in configs if x))
        proxies=list(dict.fromkeys(x for x in proxies if x))
        job=get_manual_queue_job(job_id, profile_id)
        if not job:
            await u.message.reply_text("❌ صف پیدا نشد.")
            return True
        kind=str(job.get("kind") or "")
        incoming=configs if kind=="config" else proxies
        if kind=="config": incoming=_manual_queue_unposted_items(profile_id, "config", incoming)
        else: incoming=_manual_queue_unposted_items(profile_id, "proxy", incoming)
        if not incoming:
            await u.message.reply_text("⚠️ هیچ مورد جدید و تکرارنشده‌ای برای این صف پیدا نشد.")
            return True
        ok=add_manual_queue_items(job_id, profile_id, incoming)
        await u.message.reply_text(f"✅ {len(incoming)} مورد جدید به صف #{job_id} اضافه شد." if ok else "❌ نوع سرورها با این صف سازگار نیست.")
        return True
    except Exception as exc:
        log.exception("manual queue TXT import failed")
        await u.message.reply_text(f"❌ خطا در خواندن TXT: {str(exc)[:200]}")
        return True

async def _on_document_impl(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    queue_add_state = ctx.user_data.get("manual_queue_add")
    if queue_add_state and u.message.document:
        await _process_manual_queue_add_document(u, ctx, queue_add_state)
        ctx.user_data.pop("manual_queue_add", None)
        return
    a = ctx.user_data.get("action")
    if not a:
        return

    if a == "replace_database":
        doc = u.message.document
        if not doc:
            return
        status = await u.message.reply_text("⏳ فایل دریافت شد؛ در حال بررسی و جایگزینی امن دیتابیس...")
        temp_path = os.path.join(DATA_DIR, f"db_upload_{u.effective_user.id}_{int(time.time())}")
        try:
            file = await doc.get_file()
            await file.download_to_drive(temp_path)
            ok, detail = await replace_database_from_file(u, temp_path, doc.file_name or "database")
            if ok:
                await status.edit_text(f"✅ دیتابیس با موفقیت جایگزین شد.\n\n{detail}\n\n🔄 بات از این دیتابیس ادامه می‌دهد.", parse_mode="HTML")
            else:
                await status.edit_text(f"❌ {html.escape(detail)}", parse_mode="HTML")
        except Exception as e:
            log.exception("Database upload/replacement error")
            await status.edit_text(f"❌ خطا در جایگزینی دیتابیس: {html.escape(str(e)[:300])}", parse_mode="HTML")
        finally:
            ctx.user_data.pop("action", None)
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=True, ctx=ctx)
        del ctx.user_data["action"]
        return

# ======================================================================
# ارسال دستی (بدون دانلود فایل)
# ======================================================================
async def process_manual_text(u, message, profile_id, is_document=False, ctx=None):
    """Parse manual input and hand it to the persistent scheduling UI."""
    if ctx is not None:
        ctx.user_data.pop("manual_queue_edit", None)
        ctx.user_data.pop("manual_schedule_custom", None)
        ctx.user_data.pop("manual_schedule_draft", None)
    pmsg = await message.reply_text(msg("manual_send_processing"))
    try:
        if is_document:
            doc = message.document
            if doc.file_size and doc.file_size > 3 * 1024 * 1024:
                return await pmsg.edit_text(">3MB")
            file = await doc.get_file()
            data = await file.download_as_bytearray()
            text = data.decode('utf-8', errors='ignore')
            if re.match(r'^[A-Za-z0-9+/=\s]+$', text):
                try:
                    decoded = base64.b64decode(text.strip(), validate=True).decode('utf-8', errors='ignore')
                    if decoded:
                        text = decoded
                except Exception:
                    pass
        else:
            text = message.text or ""

        # Include hidden Telegram URLs (buttons/entities) before parsing text.
        extracted_message_links = extract_supported_links_from_message(message)
        config_links = extract_links_from_text(text)
        proxy_links = extract_proxy_links_from_text(text)
        for _url in extracted_message_links:
            if detect_config_protocol(_url):
                config_links.append(_url)
            elif detect_proxy_protocol(_url):
                proxy_links.append(_url)
        config_links = list(dict.fromkeys(config_links))
        proxy_links = list(dict.fromkeys(proxy_links))
        if not config_links and not proxy_links:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for line in lines:
                if line.lower().startswith("http") and "t.me/proxy" in line.lower():
                    proxy_links.append(line)
                else:
                    config_links.extend(extract_links_from_text(line))
            # Stable de-duplication; never use set() because input order matters for scheduling.
            config_links = list(dict.fromkeys(config_links))
            proxy_links = list(dict.fromkeys(proxy_links))

        # Normalize and validate once more at the manual boundary.
        valid_configs = []
        for link in config_links:
            link = clean_config_url(link.strip())
            ok, _ = validate_config_link(link)
            if ok:
                valid_configs.append(link)
        valid_proxies = []
        for pl in proxy_links:
            norm = normalize_proxy_url(pl)
            if norm and is_telegram_proxy_url(norm):
                valid_proxies.append(norm)
        valid_configs = list(dict.fromkeys(valid_configs))
        valid_proxies = list(dict.fromkeys(valid_proxies))

        if not valid_configs and not valid_proxies:
            return await pmsg.edit_text("❌ هیچ لینک معتبر و جدیدی یافت نشد.")

        if ctx is None:
            # Compatibility path for old callers: immediate send, without scheduling.
            for chunk in [valid_configs[i:i+get_profile_max_post_config(profile_id)] for i in range(0, len(valid_configs), get_profile_max_post_config(profile_id))]:
                await _send_manual_queue_batch(u.get_bot(), {
                    "id": 0, "profile_id": profile_id, "kind": "config", "items": chunk,
                    "batch_size": len(chunk), "interval_minutes": 0, "status": "pending", "sent_count": 0
                })
            for chunk in [valid_proxies[i:i+get_profile_max_post_proxy(profile_id)] for i in range(0, len(valid_proxies), get_profile_max_post_proxy(profile_id))]:
                await _send_manual_queue_batch(u.get_bot(), {
                    "id": 0, "profile_id": profile_id, "kind": "proxy", "items": chunk,
                    "batch_size": len(chunk), "interval_minutes": 0, "status": "pending", "sent_count": 0
                })
            return await pmsg.edit_text(f"✅ ارسال شد: {len(valid_configs)} کانفیگ و {len(valid_proxies)} پروکسی")

        ctx.user_data["manual_pending"] = {
            "profile_id": int(profile_id),
            "configs": valid_configs,
            "proxies": valid_proxies,
        }
        ctx.user_data["manual_schedule_draft"] = {"profile_id": int(profile_id), "interval": 0, "batch": 1}
        await pmsg.edit_text(
            f"📋 آماده زمان‌بندی\n\n📡 کانفیگ: {len(valid_configs)}\n🌐 پروکسی: {len(valid_proxies)}\n\n"
            "حالت ارسال را انتخاب کن:",
            reply_markup=manual_schedule_kb_with_draft(profile_id, ctx.user_data["manual_schedule_draft"])
        )
    except Exception as e:
        log.exception("manual send error")
        await pmsg.edit_text(f"❌ {str(e)[:250]}")

async def post_working_configs(bot, profile_id, working, proxies_with_ping, force=False, skip_duplicate=False):
    total_configs = 0
    total_proxies = 0
    if working:
        total_configs = await post_configs(bot, profile_id, working, source_for_seen="manual", is_instant=False)
    if proxies_with_ping:
        cnt, payload, selected_proxy_urls = await post_proxies(bot, profile_id, proxies_with_ping)
        if cnt > 0 and payload:
            text, buttons = payload
            log.info(f"[PROXY][profile={profile_id}] attempting Telegram send: count={cnt}, mode={get_profile_proxy_post_mode(profile_id)}, button_rows={len(buttons or [])}")
            sent = await send_to_destination(bot, profile_id, text, buttons)
            if sent:
                total_proxies = cnt
                log.info(f"[PROXY][profile={profile_id}] Telegram send succeeded for {cnt} proxies")
                mark_proxies_posted_batch(profile_id, selected_proxy_urls)
    return total_configs, total_proxies

async def export_backup(update, context, profile_id, backup_type, count=None):
    try:
        profile = get_profile(profile_id)
        profile_name = profile["dest_name"].replace("@", "").strip() if profile else f"profile_{profile_id}"

        if backup_type == "configs":
            if count is None or count == -1:
                rows = c.execute("SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' ORDER BY last_posted DESC", (profile_id,)).fetchall()
            else:
                rows = c.execute("SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' ORDER BY last_posted DESC LIMIT ?", (profile_id, count)).fetchall()
            links = [row[0] for row in rows if row[0]]
            if not links:
                await update.message.reply_text("❌ هیچ کانفیگی برای بک‌آپ یافت نشد.")
                return
            filename = f"configs_backup_{profile_name}_{get_tehran_date()}.txt"
            content = f"# Backup for {profile_name} (ID: {profile_id})\n# Total: {len(links)}\n\n" + "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(DATA_DIR, filename), "rb") as f:
                await update.message.reply_document(document=f, filename=filename, caption=f"📤 {len(links)} کانفیگ - {profile_name}")
            os.remove(os.path.join(DATA_DIR, filename))
            return

        elif backup_type == "proxies":
            if count is None or count == -1:
                rows = c.execute("SELECT proxy_url FROM proxies_seen WHERE profile_id=? ORDER BY last_posted DESC", (profile_id,)).fetchall()
            else:
                rows = c.execute("SELECT proxy_url FROM proxies_seen WHERE profile_id=? ORDER BY last_posted DESC LIMIT ?", (profile_id, count)).fetchall()
            links = [row[0] for row in rows if row[0]]
            if not links:
                await update.message.reply_text("❌ هیچ پروکسی برای بک‌آپ یافت نشد.")
                return
            filename = f"proxies_backup_{profile_name}_{get_tehran_date()}.txt"
            content = f"# Backup for {profile_name} (ID: {profile_id})\n# Total: {len(links)}\n\n" + "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(DATA_DIR, filename), "rb") as f:
                await update.message.reply_document(document=f, filename=filename, caption=f"📤 {len(links)} پروکسی - {profile_name}")
            os.remove(os.path.join(DATA_DIR, filename))
            return
        else:
            await update.message.reply_text("❌ نوع نامعتبر.")
    except Exception as e:
        log.error(f"Backup export error: {e}")
        await update.message.reply_text(f"❌ خطا در بک‌آپ: {str(e)[:100]}")



# ======================================================================
# Automatic database garbage collector (v1.1.0)
# ======================================================================
DB_CLEAN_INTERVAL = 24 * 60 * 60


def automatic_database_cleanup():
    """Delete disposable runtime data only; permanent profiles/cursors/dedup stay intact."""
    db = None
    try:
        db = get_conn()
        cur = db.cursor()
        config_cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=CONFIG_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        runtime_cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=RUNTIME_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        deleted = {}
        try:
            cur.execute("DELETE FROM manual_send_queue WHERE status IN ('done','cancelled','failed') AND updated_at < ?", (runtime_cutoff,))
            deleted["manual_send_queue"] = max(0, cur.rowcount)
        except sqlite3.Error:
            deleted["manual_send_queue"] = 0
        # Full config/proxy URL history stays 7 days. Permanent compact dedup hashes remain forever.
        for table, col in (("seen", "last_posted"), ("proxies_seen", "last_posted")):
            try:
                cur.execute(f"DELETE FROM {table} WHERE {col} IS NOT NULL AND {col} < ?", (config_cutoff,))
                deleted[table] = max(0, cur.rowcount)
            except sqlite3.Error:
                deleted[table] = 0

        # NEVER delete country/flag cache; flags must remain available.
        deleted["country_cache"] = 0
        # Short-lived runtime history: one day. Scrape cursors are NOT disposable.
        for table, col in (("processed_messages", "rowid"), ("posts", "created_at")):
            try:
                if table == "processed_messages":
                    # processed_messages has no timestamp column; it is safe to remove
                    # the old rows because source_stream_state is the real cursor.
                    cur.execute("DELETE FROM processed_messages")
                else:
                    cur.execute("DELETE FROM posts WHERE created_at < ?", (runtime_cutoff,))
                deleted[table] = max(0, cur.rowcount)
            except sqlite3.Error:
                deleted[table] = 0
        try:
            pending_cutoff = runtime_cutoff
            cur.execute("DELETE FROM pending_batch_items WHERE added_at < ?", (pending_cutoff,))
            deleted["pending_batch_items"] = max(0, cur.rowcount)
        except sqlite3.Error:
            deleted["pending_batch_items"] = 0
        try:
            cache_cutoff = runtime_cutoff
            cur.execute("DELETE FROM batch_test_cache WHERE tested_at < ?", (cache_cutoff,))
            deleted["batch_test_cache"] = max(0, cur.rowcount)
        except sqlite3.Error:
            deleted["batch_test_cache"] = 0
        # Admin activity is useful, but only recent 7-day activity is needed.
        try:
            cur.execute("DELETE FROM admin_activity WHERE created_at < ?", (runtime_cutoff,))
            deleted["admin_activity"] = max(0, cur.rowcount)
        except sqlite3.Error:
            deleted["admin_activity"] = 0
        # channel_posts is the authoritative 7-day record of messages actually
        # sent by each profile, including messages that were later deleted.
        try:
            cur.execute("DELETE FROM channel_posts WHERE sent_at < ?", (config_cutoff,))
            deleted["channel_posts"] = max(0, cur.rowcount)
        except sqlite3.Error:
            deleted["channel_posts"] = 0
        db.commit()
        cur.execute("PRAGMA optimize")
        try:
            cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        db.commit()
        log.info("[DB CLEANER] disposable cleanup completed: %s; permanent ledgers preserved", deleted)
        return deleted
    except sqlite3.Error:
        if db:
            db.rollback()
        log.exception("[DB CLEANER] failed")
        return {}
    finally:
        if db:
            db.close()

def compact_database_once():
    """One-time physical compaction. Runs off the event loop and skips if DB is busy."""
    db = None
    try:
        db = get_conn()
        db.execute("PRAGMA busy_timeout=500")
        # Only compact when the file has enough free pages to justify the work.
        row = db.execute("PRAGMA freelist_count").fetchone()
        free_pages = int(row[0] or 0) if row else 0
        page_size = int((db.execute("PRAGMA page_size").fetchone() or [4096])[0])
        if free_pages * page_size < 256 * 1024:
            log.info("[DB COMPACT] skipped; reclaimable space below 256 KiB")
            return False
        log.info("[DB COMPACT] starting one-time compaction: reclaimable=%d MiB", (free_pages * page_size) // (1024 * 1024))
        db.execute("VACUUM")
        log.info("[DB COMPACT] completed")
        return True
    except sqlite3.Error as exc:
        log.warning("[DB COMPACT] skipped: %s", exc)
        return False
    finally:
        if db:
            db.close()

async def database_compact_worker():
    # Delay compaction until the bot is already responsive. This is a one-time
    # disk operation and is never performed in the polling/event-loop thread.
    await asyncio.sleep(90)
    try:
        await asyncio.to_thread(compact_database_once)
    except Exception:
        log.exception("[DB COMPACT] worker failed")

async def database_cleanup_worker():
    log.info('[DB CLEANER] worker started | config-retention=7d | runtime-retention=1d | flags=permanent | compact-after-cleanup')
    # Never compete with startup/callback initialization.
    await asyncio.sleep(10)
    while True:
        try:
            # Delete disposable rows first, then physically reclaim the space.
            # Both operations run in a worker thread, never on the Telegram event loop.
            await asyncio.to_thread(automatic_database_cleanup)
            await asyncio.to_thread(compact_database_once)
        except Exception:
            log.exception('[DB CLEANER] worker error')
        await asyncio.sleep(DB_CLEAN_INTERVAL)


# ======================================================================
# Worker stability layer (v2.2.1)
# ======================================================================
_WORKER_TASKS = {}
_WORKER_HEARTBEATS = {}

async def _worker_guard(name, coro_factory, restart_delay=5):
    """Run long lived workers forever without allowing one exception to kill them."""
    while True:
        try:
            _WORKER_HEARTBEATS[name] = time.time()
            await coro_factory()
            _WORKER_HEARTBEATS[name] = time.time()
        except asyncio.CancelledError:
            log.info(f"[WATCHDOG] cancelled: {name}")
            raise
        except Exception:
            log.exception(f"[WATCHDOG] worker crashed: {name}; restarting")
            await asyncio.sleep(restart_delay)


def start_worker(app, name, coro_factory):
    if name in _WORKER_TASKS and not _WORKER_TASKS[name].done():
        return _WORKER_TASKS[name]
    task = app.create_task(_worker_guard(name, coro_factory))
    _WORKER_TASKS[name] = task
    log.info(f"[BOOT] Worker started: {name}")
    return task

async def _profile_scheduler_supervisor(app):
    """Continuously ensure every enabled profile has exactly one AUTO scheduler per stream."""
    while True:
        try:
            if ENABLE_AUTO:
                for prof in get_profiles():
                    pid=int(prof["id"])
                    if get_profile_post_configs(pid):
                        start_worker(app, f"auto_config_{pid}", lambda pid=pid: _profile_scheduler_v16(app.bot, pid, "config"))
                    else:
                        log.info(f"[AUTO-SUPERVISOR] profile={pid} config disabled by profile setting")
                    if get_profile_post_proxies(pid):
                        start_worker(app, f"auto_proxy_{pid}", lambda pid=pid: _profile_scheduler_v16(app.bot, pid, "proxy"))
                    else:
                        log.info(f"[AUTO-SUPERVISOR] profile={pid} proxy disabled by profile setting")
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("[AUTO-SUPERVISOR] scheduler reconciliation failed")
            await asyncio.sleep(5)

async def worker_watchdog():
    while True:
        try:
            now=time.time()
            for name, task in list(_WORKER_TASKS.items()):
                if task.done():
                    log.warning(f"[WATCHDOG] dead worker detected: {name}")
                elif now - _WORKER_HEARTBEATS.get(name, now) > 300:
                    log.warning(f"[WATCHDOG] stale worker heartbeat: {name}")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("[WATCHDOG] monitor error")
            await asyncio.sleep(60)

# ======================================================================
# راه‌اندازی
# ======================================================================
BOT_REF = None
ENABLE_AUTO = True

async def post_init(app):
    global BOT_REF, BOT_START_TIME
    BOT_REF = app.bot
    BOT_START_TIME = datetime.now(TEHRAN_TZ)
    # پاکسازی تایمرهای منقضی‌شده در ابتدا
    for prof in get_profiles():
        expiry_str = prof.get("timer_expiry")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if expiry < datetime.now(TEHRAN_TZ):
                    clear_profile_timer(prof['id'])
                    log.info(f"Cleared expired timer for profile {prof['id']}")
            except:
                clear_profile_timer(prof['id'])

    profiles = get_profiles()
    if not profiles:
        new_id = create_profile("@VaslZone", sources="@Cfox_Server")
        log.info(f"✅ Created default profile with id {new_id}.")
        profiles = get_profiles()
    log.info(f"✅ INIT done: {len(profiles)} profiles, AUTO={ENABLE_AUTO}")
    for _prof in profiles:
        log.info(
            f"[PROFILE-BOOT] id={_prof['id']} dest={_prof.get('dest_name','')} "
            f"enabled={int(_prof.get('profile_enabled', 1) or 0)} "
            f"config={int(_prof.get('post_configs', 0) or 0)} "
            f"proxy={int(_prof.get('post_proxies', 0) or 0)} "
            f"intervals=({_prof.get('interval_config',0)},{_prof.get('interval_proxy',0)})"
        )
    # Load permanent dedup identities once. Hot posting paths then avoid SQLite
    # lookups entirely; profiles/settings remain untouched.
    load_dedup_cache()
    try:
        purge_duplicate_ledgers()
    except Exception:
        log.exception("startup ledger repair failed")

    job_queue = app.job_queue
    if job_queue:
        now = datetime.now(TEHRAN_TZ)
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        seconds_until = (target - now).total_seconds()
        job_queue.run_once(send_daily_report, when=seconds_until, chat_id=MAIN_ADMIN_ID)
        log.info(f"📅 Daily report scheduled for {target.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        log.warning("⚠️ JobQueue not available, daily report disabled.")

    # Recover jobs that were being sent when the process restarted.
    c.execute("UPDATE manual_send_queue SET status='pending', updated_at=? WHERE status='running'", (get_tehran_time(),))
    conn.commit()
    # Persistent manual scheduler is always enabled independently from automatic scraping.
    start_worker(app, "manual_queue", lambda: manual_queue_worker(app.bot))
    log.info("⏱️ Manual queue scheduler enabled")

    if ENABLE_AUTO:
        # Use the fixed-deadline scheduler as the ONLY automatic scheduler.
        # The legacy profile_loop_* workers used relative sleeps after each
        # cycle and are intentionally not started: two schedulers for the same
        # profile can race on cursors and make config posting appear broken.
        for prof in profiles:
            pid = int(prof["id"])
            if get_profile_post_configs(pid):
                log.info(f"⏰ Creating precise config scheduler for profile {pid} ({prof['dest_name']})")
                start_worker(app, f"auto_config_{pid}", lambda pid=pid: _profile_scheduler_v16(app.bot, pid, "config"))
            else:
                log.info(f"⏸️ AUTO config scheduler not started for profile {pid}: posting disabled")
            if get_profile_post_proxies(pid):
                log.info(f"⏰ Creating precise proxy scheduler for profile {pid} ({prof['dest_name']})")
                start_worker(app, f"auto_proxy_{pid}", lambda pid=pid: _profile_scheduler_v16(app.bot, pid, "proxy"))
            else:
                log.info(f"⏸️ AUTO proxy scheduler not started for profile {pid}: posting disabled")
        log.info("⏰ Precise automatic scheduler started: only enabled streams are running")
        start_worker(app, "profile_scheduler_supervisor", lambda: _profile_scheduler_supervisor(app))
        log.info("🛡️ AUTO scheduler supervisor enabled (10s reconciliation)")

    start_worker(app, "cleanup", lambda: periodic_cleanup())
    start_worker(app, "database_cleaner", lambda: database_cleanup_worker())
    start_worker(app, "watchdog", lambda: worker_watchdog())
    log.info("🧹 Periodic cleanup task started")


def optimize_database():
    """Low disk SQLite maintenance. Never runs huge vacuum/write operations."""
    db = None
    try:
        db = get_conn()
        cur = db.cursor()

        # keep WAL and temporary files small
        cur.execute("PRAGMA journal_size_limit=262144")
        cur.execute("PRAGMA wal_autocheckpoint=50")
        cur.execute("PRAGMA temp_store=MEMORY")

        # indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_processed_message ON processed_messages(source,message_id,profile_id)")

        cutoff = (datetime.now(TEHRAN_TZ) - timedelta(days=1)).isoformat()
        # delete old duplicate trackers, not actual configs
        cur.execute("DELETE FROM posts WHERE created_at < ?", (cutoff,))
        # NEVER delete seen/proxies_seen: they are permanent once-only posting ledgers.
        cur.execute("DELETE FROM processed_messages WHERE rowid NOT IN (SELECT MIN(rowid) FROM processed_messages GROUP BY source,message_id,profile_id)")

        db.commit()

        # checkpoint after commit
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

        # small incremental reclaim only
        try:
            cur.execute("PRAGMA incremental_vacuum(100)")
        except Exception:
            pass

        db.commit()
    except Exception as e:
        log.error(f"database optimizer failed: {e}")
    finally:
        if db:
            db.close()

async def on_callback(u, ctx):
    before=_audit_snapshot()
    try:
        await _on_callback_impl(u, ctx)
    finally:
        try:
            admin=getattr(getattr(u,"effective_user",None),"id",0)
            action=f"Callback: {getattr(getattr(u,'callback_query',None),'data','')}"
            after=_audit_snapshot(); _record_admin_activity(admin,action,before,after)
        except Exception: log.exception("callback audit wrapper failed")

async def on_text(u, ctx):
    before=_audit_snapshot()
    action_before = (ctx.user_data.get("action", "unknown") if getattr(ctx, "user_data", None) else "unknown")
    try:
        await _on_text_impl(u, ctx)
    finally:
        try:
            admin=getattr(getattr(u,"effective_user",None),"id",0)
            action=f"Text action: {action_before}"
            after=_audit_snapshot(); _record_admin_activity(admin,action,before,after)
        except Exception: log.exception("text audit wrapper failed")

async def on_document(u, ctx):
    before=_audit_snapshot()
    action_before = (ctx.user_data.get("action", "unknown") if getattr(ctx, "user_data", None) else "unknown")
    try:
        await _on_document_impl(u, ctx)
    finally:
        try:
            admin=getattr(getattr(u,"effective_user",None),"id",0)
            action=f"Document action: {action_before}"
            after=_audit_snapshot(); _record_admin_activity(admin,action,before,after)
        except Exception: log.exception("document audit wrapper failed")

def main():
    # Never perform database cleanup/VACUUM before polling starts. A large
    # SQLite file can otherwise block the entire bot for minutes and make the
    # bot look dead. Maintenance runs in its own worker after startup.
    app = _build_application()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("runnow", cmd_runnow))
    app.add_handler(CommandHandler("runall", cmd_runall))
    app.add_handler(CommandHandler("sendtest", cmd_sendtest))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("✅ Bot is ready, polling...")
    app.run_polling()



async def _post_stop_cleanup(app):
    await _close_ping_client()
    await _close_scrape_client()

# Final lifecycle hook: all functions are defined before polling starts.
# This prevents the historical "main() before trailing definitions" layout.

def _build_application():
    app = Application.builder().token(TOKEN).post_init(post_init).post_stop(_post_stop_cleanup).build()
    return app

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("🚀 Starting bot | version=%s", APP_VERSION_LABEL)
    main()
