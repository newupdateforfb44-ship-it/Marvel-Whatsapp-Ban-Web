# WhatsApp Report Bot

Telegram-controlled WhatsApp reporting bot. Headless Chromium drives WhatsApp Web;
MySQL records users, jobs, and every report attempt.

## Files

| File | Purpose |
|---|---|
| `config.py` | Environment configuration |
| `wa.py` | WhatsApp Web automation (Selenium) |
| `db.py` | MySQL layer (aiomysql) |
| `bot.py` | Telegram frontend + job queue |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Image with Chromium + chromedriver |
| `render.yaml` | Render blueprint (free plan, no disk) |
| `.dockerignore` | Build exclusions |
| `schema.sql` | Optional manual schema |

## Commands

User: `/start` `/menu` `/cancel` `/status` `/history` `/relink` `/browser`
Admin: `/stats` `/ban <id>` `/unban <id>`

## Free-plan limits

- No persistent disk — the WhatsApp link is lost on every restart. Use `/relink`.
- 512 MB RAM / 0.1 CPU. Low-memory Chromium is on by default.
- Sleeps after 15 idle minutes; the keep-alive ping prevents this while running.
- 750 instance-hours per month. One free service uses nearly all of it.

## Setup

1. Get a MySQL URL (Aiven, filess.io, Clever Cloud, Railway).
   Allow `0.0.0.0/0` if the host has an IP allowlist.
2. Push this repo to GitHub.
3. Render → New + → Blueprint → connect the repo.
4. Set `TELEGRAM_BOT_TOKEN`, `MYSQL_URL`, `ADMIN_IDS`.
5. Deploy. Then `/start` → `/relink` → link → 5-report test.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Exit code 137 in logs | OOM — set `REPORTS_PER_RUN=15`, `DB_POOL_SIZE=1` |
| `database unreachable` | Check the URL, the allowlist, and that the host is awake |
| Reports fail with "chat menu not found" | WhatsApp changed its DOM — update the selector lists at the top of `wa.py` |
| "session expired" mid-run | Restart wiped the profile — `/relink` and rerun |
| `Too many linked devices` | Remove stale entries in WhatsApp → Linked devices |