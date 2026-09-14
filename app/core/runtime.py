# Shared runtime environment and imports.
# bot.py — 4.1.18
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

# Foundational timezone constant: this module is imported before bootstrap.py
# finishes loading, so modules must be able to resolve it during import.
TEHRAN_TZ = pytz.timezone("Asia/Tehran")
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
# bootstrap.py installs the actual handlers after all compatibility modules
# are imported. This module only exposes the shared logger and log path.
_LOG_FILE = os.path.join(DATA_DIR, "bot.log")
log = logging.getLogger("bot")

