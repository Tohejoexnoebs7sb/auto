from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

# Bootstrap later replaces these with the shared registries.
_WORKER_TASKS = {}
_WORKER_HEARTBEATS = {}

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

def automatic_database_cleanup():
    """Bound disposable DB history so long-running Railway instances stay small."""
    db = None
    try:
        db = get_conn(); cur = db.cursor(); deleted = {}
        runtime_cutoff = (datetime.now(TEHRAN_TZ) - timedelta(hours=RUNTIME_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        for table, cap in (("seen", SEEN_ROWS_PER_PROFILE), ("proxies_seen", PROXY_ROWS_PER_PROFILE)):
            try:
                cur.execute(f"DELETE FROM {table} WHERE last_posted IS NOT NULL AND last_posted < ?", (runtime_cutoff,))
                deleted[table+"_old"] = max(0, cur.rowcount)
                pids = [r[0] for r in cur.execute(f"SELECT DISTINCT profile_id FROM {table}").fetchall()]
                trimmed = 0
                for pid in pids:
                    cur.execute(f"DELETE FROM {table} WHERE profile_id=? AND rowid NOT IN (SELECT rowid FROM {table} WHERE profile_id=? ORDER BY last_posted DESC LIMIT ?)", (int(pid),int(pid),int(cap)))
                    trimmed += max(0,cur.rowcount)
                deleted[table+"_trimmed"] = trimmed
            except sqlite3.Error:
                deleted[table] = 0
        for table, sql, args in [
            ("processed_messages", "DELETE FROM processed_messages", ()),
            ("posts", "DELETE FROM posts WHERE created_at < ?", (runtime_cutoff,)),
            ("manual_send_queue", "DELETE FROM manual_send_queue WHERE status IN ('done','cancelled','failed') AND updated_at < ?", (runtime_cutoff,)),
            ("pending_batch_items", "DELETE FROM pending_batch_items WHERE added_at < ?", (runtime_cutoff,)),
        ]:
            try:
                cur.execute(sql,args); deleted[table]=max(0,cur.rowcount)
            except sqlite3.Error:
                deleted[table]=0
        try:
            pids = cur.execute("SELECT DISTINCT profile_id,kind FROM pending_batch_items").fetchall(); trimmed=0
            for pid,kind in pids:
                cur.execute("DELETE FROM pending_batch_items WHERE profile_id=? AND kind=? AND rowid NOT IN (SELECT rowid FROM pending_batch_items WHERE profile_id=? AND kind=? ORDER BY added_at ASC LIMIT ?)", (int(pid),kind,int(pid),kind,int(PENDING_ROWS_PER_PROFILE_KIND)))
                trimmed += max(0,cur.rowcount)
            deleted["pending_trimmed"] = trimmed
        except sqlite3.Error: deleted["pending_trimmed"] = 0
        try:
            cache_cutoff=(datetime.now(TEHRAN_TZ)-timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("DELETE FROM batch_test_cache WHERE tested_at < ?",(cache_cutoff,)); deleted["batch_test_cache"]=max(0,cur.rowcount)
        except sqlite3.Error: deleted["batch_test_cache"]=0
        try:
            cur.execute("DELETE FROM channel_posts WHERE sent_at < ?",(runtime_cutoff,)); deleted["channel_posts_old"]=max(0,cur.rowcount)
            for pid in [r[0] for r in cur.execute("SELECT DISTINCT profile_id FROM channel_posts").fetchall()]:
                cur.execute("DELETE FROM channel_posts WHERE profile_id=? AND rowid NOT IN (SELECT rowid FROM channel_posts WHERE profile_id=? ORDER BY sent_at DESC LIMIT ?)",(int(pid),int(pid),int(CHANNEL_POST_ROWS_PER_PROFILE)))
        except sqlite3.Error: pass
        try:
            cur.execute("DELETE FROM admin_activity WHERE id NOT IN (SELECT id FROM admin_activity ORDER BY id DESC LIMIT ?)",(int(ADMIN_ACTIVITY_MAX_ROWS),))
            deleted["admin_activity_trimmed"]=max(0,cur.rowcount)
        except sqlite3.Error: pass
        db.commit()
        try: cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error: pass
        try: cur.execute("PRAGMA optimize")
        except sqlite3.Error: pass
        db.commit()
        log.info("[DB CLEANER] bounded cleanup: %s", deleted)
        return deleted
    except sqlite3.Error:
        if db: db.rollback()
        log.exception("[DB CLEANER] failed"); return {}
    finally:
        if db: db.close()

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
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        if db_size < DB_SOFT_LIMIT_BYTES and free_pages * page_size < 256 * 1024:
            log.info("[DB COMPACT] skipped; size=%d KiB free=%d KiB", db_size//1024, (free_pages*page_size)//1024)
            return False
        log.info("[DB COMPACT] starting compaction: size=%d KiB reclaimable=%d MiB", db_size//1024, (free_pages * page_size) // (1024 * 1024))
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
    log.info('[DB CLEANER] worker started | bounded URL history | runtime-retention=12h | compact-after-cleanup')
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
    """Legacy supervisor intentionally disabled; decoupled pipeline owns AUTO timing now."""
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

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
        new_id = create_profile("", sources="")
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
        log.info("⏰ Decoupled AUTO pipeline starting: scanner/tester/poster")
        start_worker(app, "auto_pipeline_supervisor", lambda: _auto_pipeline_supervisor(app))
        log.info("🛡️ AUTO pipeline supervisor enabled (10s reconciliation)")

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

def _build_application():
    app = Application.builder().token(TOKEN).post_init(post_init).post_stop(_post_stop_cleanup).build()
    return app
