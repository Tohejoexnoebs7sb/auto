# Telegram Bot v4.2 — Fully Modular

This project is a compatibility-first full modular split of the patched 4.1.19 bot.
The original function bodies are distributed by responsibility; no `legacy_engine.py` monolith is used.

## Layout
- `main.py` — process entrypoint only
- `app/core/runtime.py` — imports, environment, paths, logging
- `app/core/helpers.py` — general helpers/settings formatting
- `app/core/lifecycle.py` — workers, startup/shutdown, application lifecycle
- `app/storage/database.py` — SQLite schema, migrations, maintenance
- `app/storage/profiles.py` — profile/settings persistence
- `app/features/manual_queue.py` — manual queue
- `app/features/parsers.py` — protocol/config/proxy parsing and dedup
- `app/features/ping.py` — Check-Host, Iran ping rules, Full Config core integration
- `app/services/scraper_poster.py` — scraping and posting
- `app/services/pipeline.py` — automatic scan/test/post pipeline
- `app/ui/keyboards.py` — Telegram keyboards/UI helpers
- `app/handlers/telegram_handlers.py` — commands, callbacks, text/document handlers

## Railway / GitHub
Commit the project files, but NEVER commit `/app/data`, `bot.db`, logs, backups, or `.env`.
The included `.gitignore` excludes runtime state.
