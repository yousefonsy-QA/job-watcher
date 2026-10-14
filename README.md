# Job Watcher

Sends newly posted Software QA jobs (QA Engineer, Software Tester, SDET, Test Automation, …)
to Telegram with the job link. No AI, no paid services — runs on GitHub Actions
every 15 minutes (GitHub may stretch that to ~30 minutes at busy times).

## Sources
| Source | What is kept |
|:--|:--|
| LinkedIn (public job search, last 3 hours — already-seen jobs are never re-sent) | Egypt: any workplace. KSA, UAE, Qatar, Kuwait: **remote only** (read from the posting text — LinkedIn's public pages ignore the remote filter) |
| [Remote OK](https://remoteok.com), [We Work Remotely](https://weworkremotely.com), [Remotive](https://remotive.com) | Remote QA jobs open worldwide / EMEA / Middle East / Africa / Egypt, posted in the last 72 hours. Checked once an hour. Remotive's API delays jobs by 24 hours. |

Wuzzuf and Bayt block automated requests, so they are not included.

## Filters (`watcher_config.json`)
- Minimum required experience above `max_years` (4) → skipped
- Nationals-only roles (UAE/Saudi/… nationals, Emiratisation, Saudization) → skipped
- A foreign language other than Arabic/English stated as required → skipped ("nice to have" is fine)
- Non-software QA (construction QA/QC, pharma, hardware, …) and AI rater / annotation roles → skipped
- Remote-board jobs restricted to US / UK / EU / Americas residents → skipped

Skipped jobs are still recorded in `jobs.json` with the reason.

## Health alerts
Silence should mean "no new jobs", not "the watcher is broken". The watcher sends a Telegram alert when:
- **3 runs in a row** could not really check: LinkedIn answered fewer than half of the searches
  (rate limit / block), LinkedIn pages loaded but could not be read (layout changed), or a remote
  board feed could not be read;
- **a run fails** (script error or self-check tests failed) — alerted right away.

While a problem lasts it reminds every 12 hours (a new kind of problem alerts immediately), and it
sends **"back to normal"** once a run works again. Settings: `health` in `watcher_config.json`.

## Two runners: laptop + GitHub
LinkedIn often refuses requests from GitHub's servers but answers a home connection, so the watcher
also runs on the laptop (Windows Task Scheduler, every 15 minutes, `local_runner.py` in its own clone).

- Each laptop run pushes a heartbeat (`local_heartbeat_utc` in `watcher_state.json`).
- A GitHub run that sees a heartbeat younger than `local_active_minutes` (25) skips itself.
- When the laptop is off or asleep the heartbeat goes stale and GitHub takes over automatically.
- Both copies share `watcher_seen.json` / `jobs.json`; `push_state.py` merges them if both push at
  the same time, so a job is never sent twice.

## Files
- `watcher_config.json` — settings
- `.github/workflows/watch.yml` — schedule; **Run workflow** also offers a Telegram test message
- `watcher_seen.json`, `watcher_state.json`, `jobs.json` — state, committed by the workflow
- `test_job_watcher.py` — offline checks, run before every check
- `push_state.py` — commits and pushes state, merging with the other runner if needed
- `local_runner.py` — laptop runner (Task Scheduler)

## Setup
1. Repository **Settings → Secrets and variables → Actions** → add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. **Actions** tab → *Job Watcher* → **Run workflow** to test.

## Local use
```bash
python job_watcher.py --dry-run     # print what would be sent, save nothing
python job_watcher.py --test        # Telegram test message (needs .env)
python test_job_watcher.py          # offline filter tests
```

Limits: LinkedIn may change its pages or rate-limit requests; filters are regex-based, so unusual
wording can slip through either way.
