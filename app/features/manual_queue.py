from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

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
