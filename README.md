# Job Watcher

Checks LinkedIn's public job search every 15 minutes for newly posted Software QA jobs
(QA Engineer, Software Tester, SDET, Test Automation, …) in Egypt (any workplace) and
KSA, UAE, Qatar, Kuwait (remote only — detected from the posting text),
skips postings whose minimum required experience is above `max_years`, and sends the rest
to Telegram with the job link. No AI, no paid services — runs on GitHub Actions.

- Settings: [`watcher_config.json`](watcher_config.json)
- Schedule: [`.github/workflows/watch.yml`](.github/workflows/watch.yml)
- State: `watcher_seen.json` (job IDs already handled) and `jobs.json` (jobs found), committed by the workflow

## Setup
1. Repository **Settings → Secrets and variables → Actions** → add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. **Actions** tab → *Job Watcher* → **Run workflow** to test.

## Local use
```bash
python job_watcher.py --dry-run   # print what would be sent, save nothing
python job_watcher.py --test      # send a Telegram test message (needs .env)
```

Limits: GitHub can delay scheduled runs at busy times; LinkedIn may change its pages or rate-limit
requests; the experience filter is regex-based, so postings without a stated number of years are sent.
