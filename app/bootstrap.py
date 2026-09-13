from __future__ import annotations

# Execute the original top-level initialization after every function module is loaded.
from app.loader import load_all_modules

_loaded_modules, _namespace = load_all_modules()
# Make all original names available in this module, preserving the old monolith's
# single-namespace behavior for initialization and lifecycle code.
globals().update(_namespace)

APP_VERSION = "4.1.19"

APP_VERSION_MAJOR = 4

APP_VERSION_MINOR = 1

APP_VERSION_PATCH = 19

APP_VERSION_LABEL = "4.1.19-stable"

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

import subprocess

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

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

MAIN_ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not MAIN_ADMIN_ID:
    raise ValueError("ADMIN_ID environment variable not set")

DATA_DIR = "/app/data"

os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "bot.db")

BACKUP_DIR = os.path.join(DATA_DIR, "backups")

os.makedirs(BACKUP_DIR, exist_ok=True)

from logging.handlers import RotatingFileHandler

logging.Formatter.converter = lambda *args: datetime.now(pytz.timezone("Asia/Tehran")).timetuple()

_LOG_FILE = os.path.join(DATA_DIR, "bot.log")

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

logging.getLogger("httpx").setLevel(logging.WARNING)

logging.getLogger("telegram").setLevel(logging.WARNING)

TEHRAN_TZ = pytz.timezone('Asia/Tehran')

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

# The database functions live in app.storage.database, so bind the live
# connection/cursor there as well. The old monolith exposed these as globals.
import app.storage.database as _database_module
_database_module.conn = conn
_database_module.c = c

# Keep compatibility globals synchronized across extracted modules.
# The original monolith shared conn/c/TEHRAN_TZ in one namespace.
for _mod in _loaded_modules:
    vars(_mod)["conn"] = conn
    vars(_mod)["c"] = c
    vars(_mod)["TEHRAN_TZ"] = TEHRAN_TZ

DB_REPLACE_MAX_BYTES = 45 * 1024 * 1024

DB_REPLACE_LOCK = asyncio.Lock()

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

c.execute("""CREATE TABLE IF NOT EXISTS batch_test_cache (
    profile_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, kind, identity_hash))""")

c.execute("CREATE INDEX IF NOT EXISTS idx_batch_test_cache_time ON batch_test_cache(profile_id, kind, tested_at)")

conn.commit()

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

ensure_column("profiles", "config_test_mode", "INTEGER DEFAULT 0", 0)

ensure_column("profiles", "proxy_test_mode", "INTEGER DEFAULT 0", 0)

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

fix_column_types()

migrate_tables_for_profile_isolation()

migrate_old_config()

migrate_header_modes()

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

PROXY_PROTOCOLS = ("MTPROTO", "SOCKS5")

CONFIG_PROTOCOLS = ("VLESS", "VMESS", "TROJAN", "SHADOWSOCKS", "SOCKS", "HYSTERIA", "HYSTERIA2", "HY2", "WIREGUARD", "WG")

CONFIG_RETENTION_HOURS = 48

RUNTIME_RETENTION_HOURS = 12

DB_CLEAN_INTERVAL = 15 * 60

DB_SOFT_LIMIT_BYTES = 9 * 1024 * 1024

DB_HARD_LIMIT_BYTES = 10 * 1024 * 1024

SEEN_ROWS_PER_PROFILE = 2500

PROXY_ROWS_PER_PROFILE = 1500

PENDING_ROWS_PER_PROFILE_KIND = 1000

CHANNEL_POST_ROWS_PER_PROFILE = 300

ADMIN_ACTIVITY_MAX_ROWS = 300

try:
    c.execute("SELECT v FROM cfg WHERE k='ping_default_iran_v1'")
    _ping_default_migrated = c.fetchone()
except Exception:
    _ping_default_migrated = None

if not _ping_default_migrated:
    c.execute("UPDATE profiles SET ping_mode='iran', config_ping_mode='iran', proxy_ping_mode='iran'")
    c.execute("INSERT OR REPLACE INTO cfg (k, v) VALUES ('ping_default_iran_v1', '1')")
else:
    c.execute("UPDATE profiles SET ping_mode='iran' WHERE ping_mode IS NULL OR TRIM(ping_mode)=''")
    c.execute("UPDATE profiles SET config_ping_mode='iran' WHERE config_ping_mode IS NULL OR TRIM(config_ping_mode)=''")
    c.execute("UPDATE profiles SET proxy_ping_mode='iran' WHERE proxy_ping_mode IS NULL OR TRIM(proxy_ping_mode)=''")

conn.commit()

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

SUPPORTED_SCHEMES = (
    "vless", "vmess", "trojan", "ss", "ssr", "socks", "socks5",
    "socks5h", "hy2", "hysteria", "hysteria2", "wg", "wireguard", "https://t.me/proxy"
)

_SEEN_CONFIG_KEYS = set()

_SEEN_PROXY_KEYS = set()

_DEDUP_CACHE_READY = False

_INFLIGHT_CONFIG_KEYS = set()

_DNS_CACHE = {}

_DNS_CACHE_TTL = 300

_PING_CLIENT = None

_PING_CLIENT_LOCK = asyncio.Lock()

_PING_TARGET_IP_CACHE = {}

_PING_TARGET_IP_CACHE_TTL = 900

_CHECK_HOST_SUBMIT_LOCK = asyncio.Lock()

_CHECK_HOST_LAST_SUBMIT = 0.0

_CHECK_HOST_MIN_SUBMIT_INTERVAL = 0.80

_CHECK_HOST_MAX_429_RETRIES = 2

try:
    row = c.execute("SELECT v FROM cfg WHERE k='iran_ping_min_ok'").fetchone()
    if not row:
        c.execute("INSERT OR REPLACE INTO cfg (k, v) VALUES ('iran_ping_min_ok', '2')")
        conn.commit()
except Exception:
    conn.rollback()

_FULL_TEST_URL = "https://www.gstatic.com/generate_204"

_FULL_TEST_TIMEOUT = 18.0

_FULL_CORE_CACHE = {}

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

_SCRAPE_CLIENT = None

_SCRAPE_CLIENT_LOCK = asyncio.Lock()

_PROFILE_CYCLE_LOCKS = {}

_MANUAL_QUEUE_LOCKS = {}

_MANUAL_RUN_LOCKS = {}

_MANUAL_RUN_TASKS = {}

_active_tasks = {}

AUTO_SCAN_INTERVAL_SECONDS = 12.0

AUTO_TEST_INTERVAL_SECONDS = 8.0

AUTO_INSTANT_POST_POLL_SECONDS = 2.0

AUTO_CONFIG_TEST_BATCH = 8

AUTO_PROXY_TEST_BATCH = 8

_auto_next_runs = {}

backup_locks = {}

BOT_START_TIME = datetime.now(TEHRAN_TZ)

_LOG_PRUNE_LOCK = threading.Lock()

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
                            "تعداد ادمین‌ها: {admins_count}\n"
                            "🎯 حداقل Ping ایران: {iran_ping_min_ok}/4",
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
        "btn_ping_mode": "🇮🇷 پینگ ایران",
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
        "naming_template_prompt": "🏷️ قالب نام‌گذاری را وارد کنید.\n\nتوکن‌ها: `{Protocol}`، `{Flag}`، `{Country}`، `{COUNTRY_EN}`، `{COUNTRY_FA}`، `{CHANNEL_ID}`، `{COUNT}`، `{INDEX}`، `{NUMBER}`، `{HOST}`، `{PORT}`، `{DATE}`، `{TIME}`، `{PING}`، `{SOURCE}`\nفرمت `{TOKEN}` یا `[TOKEN]` هر دو قابل استفاده‌اند.\nمثال: `[Protocol] [Flag] [COUNTRY_EN] • [HOST]:[PORT] • [PING]`",
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

PROFILE_PAGE_SIZE = 20

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

SOURCE_PAGE_SIZE = 20

_WORKER_TASKS = {}

_WORKER_HEARTBEATS = {}

# Keep worker registries shared across extracted modules.
for _mod in _loaded_modules:
    vars(_mod)["_WORKER_TASKS"] = _WORKER_TASKS
    vars(_mod)["_WORKER_HEARTBEATS"] = _WORKER_HEARTBEATS

BOT_REF = None

ENABLE_AUTO = True

# Final compatibility sync: loader can only merge names that already exist when
# modules are imported. Bootstrap defines many runtime/config globals later,
# while extracted modules still resolve those names from their own globals.
# Inject only missing shared names so existing module-local values are untouched.
for _mod in _loaded_modules:
    _mod_globals = vars(_mod)
    for _name, _value in list(globals().items()):
        if (_name.isupper() or _name.startswith("_")) and _name not in _mod_globals:
            _mod_globals[_name] = _value
