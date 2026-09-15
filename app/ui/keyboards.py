from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

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

def msg(key, **kwargs):
    lang = get_lang()
    text = T[lang].get(key, T["fa"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_profiles"), callback_data="profiles_list", style="primary")],
        [InlineKeyboardButton(msg("btn_general"), callback_data="general_settings", style="primary")],
        [InlineKeyboardButton(msg("btn_balance"), callback_data="show_balance", style="primary")],
    ])

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

def profile_config_settings_kb(profile_id):
    prof = get_profile(profile_id) or {}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📡 انتشار کانفیگ: {'✅' if prof.get('post_configs',1) else '❌'}", callback_data=f"tglcfg_{profile_id}", style="primary"),
         InlineKeyboardButton(f"⏰ زمان کانفیگ: {get_profile_interval_config(profile_id)} دقیقه", callback_data=f"set_cfg_interval_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📊 حداکثر کانفیگ: {get_profile_max_post_config(profile_id)}", callback_data=f"set_cfg_max_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🧩 Ping کانفیگ: {'🇮🇷 ایران' if get_profile_config_ping_mode(profile_id)=='iran' else '🌍 جهانی'}", callback_data=f"toggle_ping_config_{profile_id}", style="primary"),
         InlineKeyboardButton(f"🧪 نوع تست: {_test_mode_label(get_profile_config_test_mode(profile_id))}", callback_data=f"cfg_test_mode_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📡 تست Ping: {'✅' if get_profile_ping_enabled(profile_id) else '❌'}", callback_data=f"tgl_ping_test_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"👁 نمایش Ping: {'✅' if prof.get('show_ping',1) else '❌'}", callback_data=f"tgl_show_ping_{profile_id}", style="primary"),
         InlineKeyboardButton(f"🧩 حالت انتشار: {'Quote جمع‌شونده' if get_profile_config_post_mode(profile_id) else 'عادی'}", callback_data=f"tgl_cfg_post_mode_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🧩 عنوان کانفیگ: {'✅' if prof.get('config_header_enabled',1) else '❌'}", callback_data=f"tgl_cfg_header_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🏷 قالب عنوان: {get_profile_config_header_template(profile_id)}", callback_data=f"cfg_header_tpl_{profile_id}", style="primary")],
        [InlineKeyboardButton("📝 بنر کانفیگ", callback_data=f"ab_config_{profile_id}", style="primary"),
         InlineKeyboardButton("📅 تاریخ: " + ("✅" if prof.get('show_date_config',1) else "❌"), callback_data=f"tgl_date_cfg_{profile_id}", style="primary")],
        [InlineKeyboardButton("↩️ بازگشت به تنظیمات پروفایل", callback_data=f"prof_{profile_id}", style="primary")],
    ])

def profile_proxy_settings_kb(profile_id):
    prof = get_profile(profile_id) or {}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 انتشار پروکسی: {'✅' if prof.get('post_proxies',1) else '❌'}", callback_data=f"tglproxy_{profile_id}", style="primary"),
         InlineKeyboardButton(f"⏰ زمان پروکسی: {get_profile_interval_proxy(profile_id)} دقیقه", callback_data=f"set_prx_interval_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📊 حداکثر پروکسی: {get_profile_max_post_proxy(profile_id)}", callback_data=f"set_prx_max_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🌐 Ping پروکسی: {'🇮🇷 ایران' if get_profile_proxy_ping_mode(profile_id)=='iran' else '🌍 جهانی'}", callback_data=f"toggle_ping_proxy_{profile_id}", style="primary"),
         InlineKeyboardButton(f"🧪 نوع تست: {_test_mode_label(get_profile_proxy_test_mode(profile_id))}", callback_data=f"prx_test_mode_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📡 تست Ping: {'✅' if get_profile_ping_enabled(profile_id) else '❌'}", callback_data=f"tgl_ping_test_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🧩 حالت انتشار: {'شیشه‌ای' if get_profile_proxy_post_mode(profile_id) else 'عادی'}", callback_data=f"tgl_prx_mode_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"👁 نمایش Ping: {'✅' if prof.get('show_ping',1) else '❌'}", callback_data=f"tgl_show_ping_{profile_id}", style="primary"),
         InlineKeyboardButton("📅 تاریخ: " + ("✅" if prof.get('show_date_proxy',1) else "❌"), callback_data=f"tgl_date_prx_{profile_id}", style="primary")],
        [InlineKeyboardButton("📝 بنر پروکسی", callback_data=f"ab_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton("↩️ بازگشت به تنظیمات پروفایل", callback_data=f"prof_{profile_id}", style="primary")],
    ])

def profile_admin_kb(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return None
    profile_status = "✅" if get_profile_enabled(profile_id) else "❌"
    batch = get_profile_batch_posting(profile_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 مدیریت منابع", callback_data=f"src_list_{profile_id}", style="primary"),
         InlineKeyboardButton("🎯 مقصد", callback_data=f"dl_{profile_id}", style="primary")],
        [InlineKeyboardButton("🧩 تنظیمات کانفیگ", callback_data=f"cfg_settings_{profile_id}", style="primary"),
         InlineKeyboardButton("🌐 تنظیمات پروکسی", callback_data=f"prx_settings_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📦 تست و ارسال تجمیعی: {'✅ فعال' if batch else '❌ خاموش'}", callback_data=f"tgl_batch_post_{profile_id}", style="success" if batch else "danger")],
        [InlineKeyboardButton(f"⚡ کم‌مصرف: {'✅' if prof.get('low_cost_mode',1) else '❌'}", callback_data=f"tgl_low_cost_{profile_id}", style="primary"),
         InlineKeyboardButton(f"🔘 پروفایل: {profile_status}", callback_data=f"tgl_profile_{profile_id}", style="primary")],
        [InlineKeyboardButton("⚙️ مدیریت پروتکل‌ها", callback_data=f"proto_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton("📢 اسپانسر", callback_data=f"sponsor_list_{profile_id}", style="primary"),
         InlineKeyboardButton("🎨 نام پروفایل", callback_data=f"ac_{profile_id}", style="primary")],
        [InlineKeyboardButton("🔗 لینک کانال", callback_data=f"set_channel_link_{profile_id}", style="primary"),
         InlineKeyboardButton("🏷 قالب نام", callback_data=f"set_naming_{profile_id}", style="primary")],
        [InlineKeyboardButton("🔢 شماره‌گذاری: " + ("✅" if prof.get('show_numbers',1) else "❌"), callback_data=f"togglenum_{profile_id}", style="primary"),
         InlineKeyboardButton("🌍 کشور: " + str(prof.get('country_display',2)), callback_data=f"tgl_country_{profile_id}", style="primary")],
        [InlineKeyboardButton("🔎 کوئری سفارشی", callback_data=f"setquery_{profile_id}", style="primary"),
         InlineKeyboardButton("📊 آمار", callback_data=f"ast_{profile_id}", style="primary")],
        [InlineKeyboardButton("⏱️ تایمر", callback_data=f"timer_menu_{profile_id}", style="primary"),
         InlineKeyboardButton("📜 لاگ", callback_data=f"log_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton("💾 فاصله بک‌آپ", callback_data=f"setbackupinterval_{profile_id}", style="primary"),
         InlineKeyboardButton("⏰ زمان‌بندی Cron", callback_data=f"setcron_{profile_id}", style="primary")],
        [InlineKeyboardButton("📤 ارسال دستی", callback_data=f"manual_{profile_id}", style="primary"),
         InlineKeyboardButton("📋 صف ارسال دستی", callback_data=f"mq_list_{profile_id}", style="primary")],
        [InlineKeyboardButton("🗑 حذف پست‌های کانال", callback_data=f"delposts_menu_{profile_id}", style="danger"),
         InlineKeyboardButton("🚫 بلک‌لیست", callback_data=f"bl_list_{profile_id}", style="danger")],
        [InlineKeyboardButton("💾 بک‌آپ", callback_data=f"backup_{profile_id}", style="success"),
         InlineKeyboardButton("🧪 تست", callback_data=f"sendtest_{profile_id}", style="primary")],
        [InlineKeyboardButton("▶️ اجرا کن", callback_data=f"runnow_{profile_id}", style="success"),
         InlineKeyboardButton("⚡ آپدیت لحظه‌ای", callback_data=f"instant_{profile_id}", style="primary")],
        [InlineKeyboardButton("🗑 پاک DB", callback_data=f"cd1_{profile_id}", style="danger"),
         InlineKeyboardButton("❌ حذف پروفایل", callback_data=f"delprof_{profile_id}", style="danger")],
        [InlineKeyboardButton("↩️ بازگشت", callback_data="profiles_list", style="primary")],
    ])

def ping_regions_kb(profile_id):
    """Exactly two Ping mode buttons: one for configs and one for proxies."""
    cfg = get_profile_config_ping_mode(profile_id)
    prx = get_profile_proxy_ping_mode(profile_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🧩 Ping کانفیگ: {'🇮🇷 ایران' if cfg == 'iran' else '🌍 جهانی'}",
            callback_data=f"toggle_ping_config_{profile_id}", style="primary"
        )],
        [InlineKeyboardButton(
            f"🌐 Ping پروکسی: {'🇮🇷 ایران' if prx == 'iran' else '🌍 جهانی'}",
            callback_data=f"toggle_ping_proxy_{profile_id}", style="primary"
        )],
    ])

def ping_region_choice_kb(profile_id, kind):
    # Backward-compatible entry point for old callbacks. The UI intentionally
    # exposes only the two direct stream buttons now.
    return ping_regions_kb(profile_id)

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

def data_management_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 بک‌آپ آرشیو کانفیگ‌ها", callback_data="backup_config_archive", style="primary")],
        [InlineKeyboardButton("🔄 جایگزینی آرشیو کانفیگ‌ها", callback_data="replace_config_archive", style="danger")],
        [InlineKeyboardButton("🌐 بک‌آپ آرشیو پروکسی‌ها", callback_data="backup_proxy_archive", style="primary")],
        [InlineKeyboardButton("🔄 جایگزینی آرشیو پروکسی‌ها", callback_data="replace_proxy_archive", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="general_settings", style="primary")],
    ])

def general_settings_kb():
    lang = get_lang()
    lang_text = "فارسی" if lang == "fa" else "English"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 زبان: {lang_text}", callback_data="toggle_lang", style="primary")],
        [InlineKeyboardButton(msg("btn_admins"), callback_data="manage_admins", style="primary")],
        [InlineKeyboardButton(msg("btn_backup"), callback_data="backup_db", style="primary")],
        [InlineKeyboardButton(f"🎯 حداقل Ping ایران: {get_iran_ping_min_ok()}/4", callback_data="set_iran_ping_threshold", style="primary")],
        [InlineKeyboardButton(
            f"⚡ حالت Ping: {'عادی / Check-Host' if get_ping_engine_mode()==0 else ('Core / Relay Delay' if get_ping_engine_mode()==1 else 'Core + Relay Delay + Check-Host')}",
            callback_data="toggle_ping_engine_mode", style="primary"
        )],
        [InlineKeyboardButton(msg("btn_replace_database"), callback_data="replace_db", style="danger")],
        [InlineKeyboardButton("🗄 مدیریت آرشیو و دیتای فشرده", callback_data="data_management", style="primary")],
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
