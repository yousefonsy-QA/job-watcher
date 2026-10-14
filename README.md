# Job Watcher

Sends newly posted Software QA jobs (QA Engineer, Software Tester, SDET, Test Automation, …)
to Telegram with the job link and two buttons. No AI, no paid services — runs on GitHub Actions
every 15 minutes (GitHub may stretch that to ~30 minutes at busy times).

## Sources
| Source | What is kept |
|:--|:--|
| LinkedIn (public job search, last hour) | Egypt: any workplace. KSA, UAE, Qatar, Kuwait: **remote only** (read from the posting text — LinkedIn's public pages ignore the remote filter) |
| [Remote OK](https://remoteok.com), [We Work Remotely](https://weworkremotely.com), [Remotive](https://remotive.com) | Remote QA jobs open worldwide / EMEA / Middle East / Africa / Egypt, posted in the last 72 hours. Checked once an hour. Remotive's API delays jobs by 24 hours. |

Wuzzuf and Bayt block automated requests, so they are not included.

## Filters (`watcher_config.json`)
- Minimum required experience above `max_years` (4) → skipped
- Nationals-only roles (UAE/Saudi/… nationals, Emiratisation, Saudization) → skipped
- A foreign language other than Arabic/English stated as required → skipped ("nice to have" is fine)
- Non-software QA (construction QA/QC, pharma, hardware, …) and AI rater / annotation roles → skipped
- Remote-board jobs restricted to US / UK / EU / Americas residents → skipped

Skipped jobs are still recorded in `jobs.json` with the reason.

## Telegram buttons
Each message has **✅ قدّمت** (applied) and **❌ مش مهتم** (not interested). Presses are processed on the
next run (within ~30 minutes): the job's status in `jobs.json` is updated and the button changes to
show the choice with an **Undo** (تراجع) option.

## Files
- `watcher_config.json` — settings
- `.github/workflows/watch.yml` — schedule; **Run workflow** also offers a Telegram test message
- `watcher_seen.json`, `watcher_state.json`, `jobs.json` — state, committed by the workflow
- `test_job_watcher.py` — offline checks, run before every check

## Setup
1. Repository **Settings → Secrets and variables → Actions** → add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. **Actions** tab → *Job Watcher* → **Run workflow** to test.

## Local use
```bash
python job_watcher.py --dry-run     # print what would be sent, save nothing
python job_watcher.py --test        # Telegram test message with buttons (needs .env)
python test_job_watcher.py          # offline filter tests
```

Limits: LinkedIn may change its pages or rate-limit requests; filters are regex-based, so unusual
wording can slip through either way.
