from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

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

# Module-local database handles. The original monolith exposed these as
# globals; after modularization database functions must have real handles
# in this module's global namespace.
conn = get_conn()
c = conn.cursor()

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
    # The active DB handle changed; refresh compatibility globals in all loaded app modules.
    try:
        import sys
        for _name, _mod in list(sys.modules.items()):
            if _name.startswith("app.") and _mod is not None:
                setattr(_mod, "conn", conn)
                setattr(_mod, "c", c)
    except Exception:
        pass

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
    ensure_column("profiles", "config_test_mode", "INTEGER DEFAULT 0", 0)
    ensure_column("profiles", "proxy_test_mode", "INTEGER DEFAULT 0", 0)
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

def migrate_old_config():
    existing = c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    if existing > 0:
        return

    def old_cfg(k, default=""):
        r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    old_dests = old_cfg("destinations", "")
    old_sources = old_cfg("sources", "")
    old_banner_config = old_cfg("banner_config", "✦ V2Ray Config List\n\n{configs}\n\n◈ #کانفیگ #ویتوری")
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
        dest_list = []

    for dest in dest_list:
        c.execute("""INSERT INTO profiles
            (dest_name, sources, banner_config, banner_proxy, interval_min,
             max_post, max_proxies, post_configs, post_proxies, ping_mode, config_ping_mode, proxy_ping_mode, last_num, created_at,
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
