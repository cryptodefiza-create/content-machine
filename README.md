# Creator Content Machine v2

A safe, human-in-the-loop content generator for multiple X/Twitter personas. Telegram is the control surface for approvals, and the web dashboard is for review. No auto-posting.

## Quickstart (Local)

1. Install dependencies

```bash
pip install -r requirements.txt
```

2. Configure environment

```bash
cp config/.env.example .env
# Fill in GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

3. Run locally

```bash
python3 scripts/run_local.py
```

Optional: run a single scan without the bot

```bash
python3 scripts/cron_runner.py --once
```

## Railway Deploy

1. Create a new Railway project.
2. Add a PostgreSQL plugin (Railway sets `DATABASE_URL`).
3. Add environment variables:
   - `GEMINI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `DASHBOARD_SECRET` (optional, for web auth)
4. Deploy using the provided `railway.toml` and `Procfile`.

## Persona Config (YAML/JSON)

Personas are defined in `config/personas_v2.json` (default) and validated on startup. YAML is also supported via `config/personas_v2.yaml`.

```yaml
version: 2
personas:
  pro:
    key: pro
    name: "Head of BD"
    handle: "personal"
    bio: "Privacy-focused builder and operator."
    role: "Commentator + Educator"
    tone:
      meme: 0.2
      serious: 0.7
      educational: 0.6
    forbidden_phrases:
      - "delve"
      - "excited to announce"
    stance:
      - "Privacy is infrastructure."
    hot_takes:
      - "Builders > hype."
    examples:
      - "privacy infra is finally shipping."
```

## Telegram Commands

- `/generate <persona> <topic or link>`
- `/batch <persona> <N> <topic>`
- `/style <persona> <example> | <topic>`
- `/trends [N|today|week]`
- `/queue`
- `/approve <id>`
- `/reject <id>`
- `/dryrun on|off`
- `/export <run_id>`

## Safety Guarantees

- Human approval required before anything is marked “ready to publish”.
- No auto-posting, DMs, emails, wallets, or shell execution.
- External inputs are treated as untrusted and validated.
- Secrets are read only from environment variables.

## Config Overview

Single source of truth: `config/settings.json`

- Pipeline stages: `SCOUT -> IDEATE -> DRAFT -> QUALITY_CHECK -> QUEUE`
- Cache settings (TTL + max entries)
- Dedupe threshold and window
- Cost rates for token estimation
- Rate limits and retry/backoff
- Runtime: `dry_run`
- Exports: CSV files written to `data/exports/run_<run_id>.csv`
- Master CSV: `data/exports/all_runs.csv` appends every draft
- Google Sheets export (optional): set `GOOGLE_SHEETS_SPREADSHEET_ID` and service account creds to append rows
- Daily combo message (summary + trends) at 14:30 (server time)
- Health check endpoint: `/health`
- Health endpoint auth: set `HEALTH_SECRET` and pass `X-Health-Token` header
- Optional digest override: set `TREND_DIGEST_CHAT_ID` to send to a specific chat

## Tests

```bash
python3 -m pytest
```

## What Changed in v2

- Structured pipeline with explicit stages and typed outputs.
- Persona voice profiles with examples, banned phrases, and stance bullets.
- Internal critique pass for hooks, repetition, vague claims, and CTAs.
- Dedupe across drafts per persona per day.
- LLM caching + token/cost estimation.
- Dry-run mode for safe local/CI runs.
- Improved logs with run IDs for traceability.
