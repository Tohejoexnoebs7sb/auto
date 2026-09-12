from __future__ import annotations

# This module contains functions moved from the original bot engine.
# Function bodies are preserved; shared names are injected by app.loader after
# all feature modules are imported, so cross-module dependencies remain compatible.
from app.core.runtime import *  # noqa: F401,F403

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

        if d.startswith("cfg_settings_"):
            try: profile_id=int(d.rsplit("_",1)[1])
            except Exception: await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            await q.edit_message_text(f"🧩 <b>تنظیمات کانفیگ پروفایل {profile_id}</b>\n\nتعداد، زمان، Ping، تست و نحوه انتشار کانفیگ را جداگانه تنظیم کن.", parse_mode="HTML", reply_markup=profile_config_settings_kb(profile_id))
            return

        if d.startswith("prx_settings_"):
            try: profile_id=int(d.rsplit("_",1)[1])
            except Exception: await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            await q.edit_message_text(f"🌐 <b>تنظیمات پروکسی پروفایل {profile_id}</b>\n\nتعداد، زمان، Ping و نحوه انتشار پروکسی را جداگانه تنظیم کن.", parse_mode="HTML", reply_markup=profile_proxy_settings_kb(profile_id))
            return

        if d == "set_iran_ping_threshold":
            current=get_iran_ping_min_ok()
            ctx.user_data["action"]="set_iran_ping_threshold"
            await q.edit_message_text(
                f"🎯 <b>حداقل Ping ایران</b>\n\nمقدار فعلی: <b>{current}/4</b>\n\nیک عدد صحیح از <b>۰ تا ۴</b> بفرست.\nاین مقدار برای <b>تمامی پروفایل‌ها و هر دو نوع کانفیگ/پروکسی</b> اعمال می‌شود.\nمثلاً ۲ یعنی هر محل ایران باید حداقل ۲ پاسخ موفق از ۴ پاسخ داشته باشد؛ اگر حتی یک محل ایران ناقص/غایب باشد، رد می‌شود.",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ برگشت", callback_data="general_settings", style="primary")]])
            )
            return

        if d == "general_settings":
            lang = get_lang()
            lang_text = "فارسی" if lang == "fa" else "English"
            admins = list_admins()
            txt = msg("general_settings", lang=lang_text, admins_count=len(admins)+1, iran_ping_min_ok=get_iran_ping_min_ok())
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
            txt = msg("general_settings", lang=lang_text, admins_count=len(admins)+1, iran_ping_min_ok=get_iran_ping_min_ok())
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

        if d.startswith("toggle_ping_config_") or d.startswith("toggle_ping_proxy_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            if d.startswith("toggle_ping_config_"):
                current = get_profile_config_ping_mode(profile_id)
                new_mode = "iran" if current == "global" else "global"
                set_profile_config_ping_mode(profile_id, new_mode)
                label = "کانفیگ"
            else:
                current = get_profile_proxy_ping_mode(profile_id)
                new_mode = "iran" if current == "global" else "global"
                set_profile_proxy_ping_mode(profile_id, new_mode)
                label = "پروکسی"
            await q.answer(f"✅ Ping {label}: {'🇮🇷 ایران' if new_mode == 'iran' else '🌍 جهانی'}")
            await q.edit_message_text(
                "📡 <b>حالت Ping</b>\n\nفقط دو دکمه فعال است: کانفیگ و پروکسی.",
                parse_mode="HTML", reply_markup=ping_regions_kb(profile_id)
            )
            return

        if d.startswith("ping_regions_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            await q.edit_message_text(
                "📡 <b>حالت Ping</b>\n\nفقط دو حالت مستقل برای کانفیگ و پروکسی وجود دارد. با زدن هر دکمه، بین 🌍 جهانی و 🇮🇷 ایران جابه‌جا می‌شود.",
                parse_mode="HTML", reply_markup=ping_regions_kb(profile_id)
            )
            return

        if d.startswith("ping_region_config_") or d.startswith("ping_region_proxy_") or d.startswith("ping_region_all_global_") or d.startswith("set_ping_region_"):
            # Legacy callbacks: never expose the old multi-button selector.
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر"); return
            await q.answer("📡 انتخاب قدیمی است؛ از دو دکمه مستقیم کانفیگ/پروکسی استفاده کن.")
            await q.edit_message_text(
                "📡 <b>حالت Ping</b>\n\nفقط دو دکمه فعال است: کانفیگ و پروکسی.",
                parse_mode="HTML", reply_markup=ping_regions_kb(profile_id)
            )
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

        if d.startswith("cfg_test_mode_") or d.startswith("prx_test_mode_"):
            try:
                profile_id=int(d.rsplit("_",1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر", show_alert=True); return
            if d.startswith("cfg_test_mode_"):
                current=get_profile_config_test_mode(profile_id); new_mode=(current+1)%3
                set_profile_config_test_mode(profile_id,new_mode); label="کانفیگ"
            else:
                current=get_profile_proxy_test_mode(profile_id); new_mode=(current+1)%3
                set_profile_proxy_test_mode(profile_id,new_mode); label="پروکسی"
            await q.answer(f"🧪 {label}: {_test_mode_label(new_mode)}")
            # Re-open the relevant settings screen so the persisted mode is visible immediately.
            if d.startswith("cfg_test_mode_"):
                await q.edit_message_text("⚙️ <b>تنظیمات تست کانفیگ</b>", parse_mode="HTML", reply_markup=profile_config_settings_kb(profile_id))
            else:
                await q.edit_message_text("⚙️ <b>تنظیمات تست پروکسی</b>", parse_mode="HTML", reply_markup=profile_proxy_settings_kb(profile_id))
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
    ping_display = f"کانفیگ:{get_profile_config_ping_mode(profile_id)} / پروکسی:{get_profile_proxy_ping_mode(profile_id)}"
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

    if a == "set_iran_ping_threshold":
        try:
            value = int(t)
            if not 0 <= value <= 4:
                raise ValueError
            set_iran_ping_min_ok(value, apply_all_profiles=True)
            ctx.user_data.pop("action", None)
            await u.message.reply_text(f"✅ حداقل Ping ایران برای همه پروفایل‌ها روی {value}/4 تنظیم شد.")
            await u.message.reply_text(msg("general_settings", lang=("فارسی" if get_lang()=="fa" else "English"), admins_count=len(list_admins())+1, iran_ping_min_ok=get_iran_ping_min_ok()), parse_mode="HTML", reply_markup=general_settings_kb())
        except ValueError:
            await u.message.reply_text("❌ فقط عدد صحیح بین ۰ تا ۴ وارد کن.")
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
