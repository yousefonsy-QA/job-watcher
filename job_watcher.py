"""Watch LinkedIn for new Software QA jobs and send them to Telegram. No AI, no tokens.

Every run: search LinkedIn's public job pages for jobs posted in the last hour,
keep QA titles, read each new job description, apply the Eligibility Filter
(minimum required years <= max_years), send eligible jobs to Telegram, and
append everything new to jobs.json + Jobs_Tracker.xlsx.

Usage:
  python 06_Tracking/job_watcher.py              one check (Task Scheduler runs this)
  python 06_Tracking/job_watcher.py --chat-id    print your Telegram chat id (setup)
  python 06_Tracking/job_watcher.py --test       send a test Telegram message
  python 06_Tracking/job_watcher.py --dry-run    one check, print only, save/send nothing

Settings: watcher_config.json next to this script. Secrets: environment variables
(GitHub Actions secrets) or .env in the parent folder. jobs.json is optional: when
build_jobs_excel.py is next to the script (local project) the Excel tracker is refreshed too.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

TRACKING = Path(__file__).resolve().parent
CONFIG_FILE = TRACKING / "watcher_config.json"
SEEN_FILE = TRACKING / "watcher_seen.json"
JOBS_FILE = TRACKING / "jobs.json"
LOG_FILE = TRACKING / "watcher.log"
ENV_FILE = TRACKING.parent / ".env"

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}

QA_TITLE = re.compile(r"\b(qa|q\.a|quality assurance|tester|testing|test engineer|test automation|"
                      r"sdet|quality engineer|automation engineer)\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?)?\s*(?:years?|yrs?)\b", re.I)
EXPERIENCE_HINT = re.compile(r"experience|exp\b|background|track record|hands-on|"
                             r"(years?|yrs?)\s+(in|as|of)\s+(software|qa|quality|test|automation|it\b)", re.I)


# ---------------------------------------------------------------- helpers
def now_cairo() -> datetime:
    """Current Africa/Cairo time (naive). Cloud runners are UTC, so convert explicitly."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Cairo")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - Windows without tzdata: local clock is Cairo already
        return datetime.now()


def log(msg: str):
    line = f"[{now_cairo():%Y-%m-%d %H:%M}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def http_get(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited: back off and retry
                time.sleep(30 * (attempt + 1))
                continue
            if e.code in (400, 404, 410):
                return None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5)
    return None


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</(p|li|div|h\d)>", "\n", s, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return re.sub(r"\s+", " ", strip_tags(m.group(1))).strip() if m else ""


# ---------------------------------------------------------------- LinkedIn
def search(keyword: str, location: str, window_seconds: int) -> list[dict]:
    q = urllib.parse.urlencode({"keywords": keyword, "location": location,
                                "f_TPR": f"r{window_seconds}", "sortBy": "DD", "start": 0})
    page = http_get(f"{SEARCH_URL}?{q}")
    if not page:
        return []
    jobs = []
    for card in page.split("<li")[1:]:
        m = re.search(r'href="([^"]*?/jobs/view/[^"?]*?-(\d{8,}))', card)
        if not m:
            continue
        jobs.append({
            "id": m.group(2),
            "url": html.unescape(m.group(1)).replace("://eg.", "://www.").split("?")[0],
            "title": first(r'base-search-card__title[^>]*>(.*?)</h3>', card),
            "company": first(r'base-search-card__subtitle[^>]*>(.*?)</h4>', card),
            "location": first(r'job-search-card__location[^>]*>(.*?)</span>', card),
            "ago": first(r'<time[^>]*>(.*?)</time>', card),
        })
    return jobs


def job_details(job_id: str) -> dict:
    page = http_get(JOB_URL.format(job_id)) or ""
    return {
        "text": strip_tags(first(r'show-more-less-html__markup[^>]*>(.*?)</div>', page) or ""),
        "applicants": first(r'num-applicants__caption[^>]*>(.*?)</', page),
        "employment": first(r'Employment type\s*</h3>\s*<span[^>]*>(.*?)</span>', page),
        "closed": "No longer accepting applications" in page,
    }


def posted_time(ago: str, now: datetime) -> datetime:
    """'12 minutes ago' -> absolute local time (approximate to the unit LinkedIn shows)."""
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week)", ago, re.I)
    if not m:
        return now
    n, unit = int(m.group(1)), m.group(2).lower()
    return now - timedelta(**{unit + "s": n})


def min_required_years(text: str) -> int | None:
    """Largest lower bound among 'N years' phrases that talk about experience.

    Taking the largest lower bound means "5+ years overall, incl. 2 years X" -> 5.
    Returns None when no experience requirement is stated.
    """
    found = []
    for sentence in re.split(r"[\n.;•●▪]", text):
        if not EXPERIENCE_HINT.search(sentence):
            continue
        for m in YEARS.finditer(sentence):
            n = int(m.group(1))
            if 0 <= n <= 15:
                found.append(n)
    return max(found) if found else None


# ---------------------------------------------------------------- Telegram
def telegram(token: str, chat_id: str, text: str) -> bool:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20) as r:
            return json.load(r).get("ok", False)
    except Exception as e:  # noqa: BLE001 - report and continue
        log(f"Telegram send failed: {e}")
        return False


def print_chat_id(token: str):
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
        updates = json.load(r).get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates if "message" in u}
    if not chats:
        print("No messages yet. Open your bot in Telegram, send it /start, then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def format_message(job: dict, years: int | None, posted: datetime) -> str:
    yrs = f"{years}+ yrs" if years is not None else "years not stated"
    return (f"🆕 <b>{html.escape(job['title'])}</b>\n"
            f"🏢 {html.escape(job['company'])}\n"
            f"📍 {html.escape(job['location'])}\n"
            f"🕒 {html.escape(job['ago'] or 'just now')} ({posted:%H:%M})\n"
            f"🎯 {yrs} · {html.escape(job['applicants'] or '')}\n"
            f"🔗 {job['url']}")


# ---------------------------------------------------------------- main run
def run(dry_run: bool):
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not dry_run and not (token and chat_id):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env (see watcher setup in Runbook.md)")

    seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else []
    seen |= {m.group(1) for j in jobs if (m := re.search(r"(\d{9,})", j["job_url"]))}
    exclude = re.compile("|".join(cfg["exclude_title_words"]), re.I)
    now = now_cairo()

    found = {}
    for location in cfg["locations"]:
        for keyword in cfg["keywords"]:
            for j in search(keyword, location, cfg["window_seconds"]):
                if j["id"] not in seen and QA_TITLE.search(j["title"]) and not exclude.search(j["title"]):
                    found.setdefault(j["id"], j)
            time.sleep(cfg["delay_seconds"])

    sent = skipped = 0
    for job_id, job in found.items():
        time.sleep(cfg["delay_seconds"])
        d = job_details(job_id)
        job["applicants"] = d["applicants"]
        if d["closed"]:
            seen.add(job_id)
            continue
        if re.search("|".join(cfg["exclude_description_words"]), d["text"][:600], re.I):
            seen.add(job_id)
            continue
        years = min_required_years(d["text"])
        posted = posted_time(job["ago"], now)
        eligible = years is None or years <= cfg["max_years"]
        yrs_note = f"JD min {years} years" if years is not None else "Years not stated"

        if dry_run:
            print(("SEND " if eligible else "SKIP ") + f"{job['title']} | {job['company']} | {yrs_note} | {job['ago']}")
            continue

        if eligible and telegram(token, chat_id, format_message(job, years, posted)):
            sent += 1
        elif not eligible:
            skipped += 1
        jobs.append({
            "job_id": f"{re.sub(r'[^a-z0-9]+', '', job['company'].lower())[:14]}_{job_id}",
            "job_title": job["title"], "company": job["company"], "location": job["location"],
            "posted_at": posted.isoformat(timespec="minutes"), "job_url": job["url"], "alt_url": "",
            "source": "LinkedIn (watcher)", "employment_type": d["employment"] or "UNKNOWN",
            "status": "NEW (watcher)" if eligible else "SKIPPED",
            "match_score": "UNKNOWN" if eligible else "N/A",
            "opportunity_score": "UNKNOWN" if eligible else "N/A",
            "wishlist": "No", "salary": "UNKNOWN", "easy_apply": "UNKNOWN",
            "scouted_date": f"{now:%Y-%m-%d}", "record_file": "",
            "notes": (f"{yrs_note}. " if eligible else f"SKIP (Eligibility rule, watcher): {yrs_note}. ")
                     + (d["applicants"] or ""),
        })
        seen.add(job_id)

    if dry_run:
        print(f"{len(found)} new QA listings (dry run, nothing saved)")
        return
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    if sent or skipped:
        JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            sys.path.insert(0, str(TRACKING))
            import build_jobs_excel
            build_jobs_excel.main()
        except ImportError:
            pass  # cloud repo: no Excel tracker, jobs.json is the record
        except PermissionError:
            log("Excel is open — tracker not refreshed this run (jobs.json is saved)")
    log(f"checked {len(found)} new listings: {sent} sent to Telegram, {skipped} skipped")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    env = load_env()
    if arg == "--chat-id":
        print_chat_id(env["TELEGRAM_BOT_TOKEN"])
    elif arg == "--test":
        ok = telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], "✅ Job watcher is connected.")
        print("sent" if ok else "failed")
    else:
        run(dry_run=arg == "--dry-run")
