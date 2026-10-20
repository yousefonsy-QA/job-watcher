"""Watch job boards for new Software QA jobs and send them to Telegram. No AI, no tokens.

Every run:
  1. LinkedIn: one OR search per country for jobs posted in the last `window_seconds` (3 hours),
     so runs that GitHub delayed or LinkedIn refused leave no gaps (seen jobs are never re-sent).
     Countries marked remote_only (Gulf) keep only postings that say they are remote.
  2. Remote boards (RemoteOK, We Work Remotely, Remotive), at most once per
     `boards_every_minutes`: keep QA jobs open to candidates in Egypt / worldwide.
  3. Filter: QA title, excluded titles/domains, nationals-only, mandatory foreign
     language, minimum required years <= max_years.
  4. Send eligible jobs to Telegram; record every new job in jobs.json.
  5. Health: alert on Telegram after 3 runs in a row that could not really check
     (LinkedIn refusing requests, pages no longer readable, feeds down), and when it recovers.

Usage:
  python job_watcher.py              one check (GitHub Actions runs this)
  python job_watcher.py --dry-run    one check, print only, save/send nothing
  python job_watcher.py --test       send a Telegram test message
  python job_watcher.py --chat-id    print your Telegram chat id (setup)
  python job_watcher.py --notify-failure   Telegram alert when a workflow step failed

Settings: watcher_config.json. Secrets: environment variables (GitHub Actions secrets)
or .env in this folder (git-ignored).
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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "watcher_config.json"
SEEN_FILE = HERE / "watcher_seen.json"
STATE_FILE = HERE / "watcher_state.json"
JOBS_FILE = HERE / "jobs.json"
LOG_FILE = HERE / "watcher.log"
ENV_FILE = HERE / ".env"  # git-ignored; the laptop runner reads its Telegram secrets from here

LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
REMOTEOK_API = "https://remoteok.com/api"
WWR_RSS = "https://weworkremotely.com/remote-jobs.rss"
REMOTIVE_API = "https://remotive.com/api/remote-jobs"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}

QA_TITLE = re.compile(r"\b(qa|q\.a|quality assurance|tester|testing|test engineer|test automation|"
                      r"sdet|quality engineer|automation engineer)\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?)?\s*(?:years?|yrs?)\b", re.I)
EXPERIENCE_HINT = re.compile(r"experience|exp\b|background|track record|hands-on|"
                             r"(years?|yrs?)\s+(in|as|of)\s+(software|qa|quality|test|automation|it\b)", re.I)
REMOTE_YES = re.compile(r"\bremote\b|work from home|work-from-home|\bwfh\b|work from anywhere|"
                        r"home[- ]based|telecommut|distributed team", re.I)
REMOTE_NO = re.compile(r"not (a )?remote|no remote|non-remote|remote (work )?(is )?not|"
                       r"on-?site (only|role|position|based|work)|must (be based|reside|relocate)|"
                       r"relocat|work from (the |our )?office|office[- ]based|"
                       r"\bhybrid\b", re.I)
REQUIRED = re.compile(r"\b(required|requirement|mandatory|must|essential|only|exclusively|fluen\w*|native|"
                      r"proficien\w*|business[- ]level|\bc1\b|\bc2\b)", re.I)
OPTIONAL = re.compile(r"\b(plus|nice[- ]to[- ]have|advantage\w*|preferred|preferably|bonus|desirable|"
                      r"beneficial|an asset|is a benefit)\b", re.I)
SENTENCES = re.compile(r"[\n.;•●▪]")


# ---------------------------------------------------------------- helpers
def now_cairo() -> datetime:
    """Current Africa/Cairo time (naive). Cloud runners are UTC, so convert explicitly."""
    return to_cairo(datetime.now(timezone.utc))


def to_cairo(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Africa/Cairo")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - Windows without tzdata: use the machine's local time
        return dt.astimezone().replace(tzinfo=None)


def ago_text(posted: datetime, now: datetime) -> str:
    minutes = max(0, int((now - posted).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} minutes ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // 1440} days ago"


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


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, data, pretty: bool = True):
    text = json.dumps(data, ensure_ascii=False, indent=2 if pretty else None)
    path.write_text(text + "\n", encoding="utf-8")


IN_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"


def laptop_active(state: dict, cfg: dict) -> bool:
    """True when the laptop copy ran recently; the GitHub copy then skips to avoid doubling requests."""
    beat = state.get("local_heartbeat_utc")
    if not beat:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(beat)
    return age < timedelta(minutes=cfg.get("local_active_minutes", 25))


RUN_DEADLINE = time.monotonic() + 9 * 60  # stop well before the 15-minute schedule / job timeout
RATE_LIMITED = {"streak": 0}              # consecutive 429s; LinkedIn is refusing us when this grows


def out_of_time() -> bool:
    return time.monotonic() > RUN_DEADLINE or RATE_LIMITED["streak"] >= 4


def http_get(url: str, retries: int = 2) -> str | None:
    for _ in range(retries):
        if out_of_time():
            return None
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                RATE_LIMITED["streak"] = 0
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited: short back-off; give up the run if it keeps happening
                RATE_LIMITED["streak"] += 1
                time.sleep(15)
                continue
            if e.code in (400, 403, 404, 410):
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


def new_job(**kw) -> dict:
    """Common shape for every source. `text` is None when it must be fetched later (LinkedIn)."""
    job = {"id": "", "source": "", "title": "", "company": "", "location": "", "url": "",
           "posted": None, "ago": "", "text": None, "applicants": "", "employment": "",
           "remote": False, "remote_only": False, "region_check": False}
    job.update(kw)
    return job


# ---------------------------------------------------------------- LinkedIn
FAILED_SEARCHES: list[str] = []
SEARCHES_MADE = [0]
PARSE_ISSUES: list[str] = []   # pages that loaded but could not be read (site layout changed?)
BOARD_ISSUES: list[str] = []   # remote boards whose feed could not be read


def linkedin_jobs(cfg: dict) -> list[dict]:
    jobs = []
    for target in cfg["searches"]:
        for j in linkedin_search(cfg["query"], target["location"], cfg["window_seconds"]):
            j["remote_only"] = target["remote_only"]
            jobs.append(j)
        time.sleep(cfg["delay_seconds"])
    return jobs


def linkedin_search(query: str, location: str, window_seconds: int, max_pages: int = 3) -> list[dict]:
    """All titles in one OR query; follows pages while LinkedIn returns full pages."""
    jobs = []
    for _ in range(max_pages):
        batch = linkedin_search_page(query, location, window_seconds, start=len(jobs))
        jobs += batch
        if len(batch) < 10:
            break
        time.sleep(2)
    return jobs


def linkedin_search_page(query: str, location: str, window_seconds: int, start: int) -> list[dict]:
    q = urllib.parse.urlencode({"keywords": query, "location": location,
                                "f_TPR": f"r{window_seconds}", "sortBy": "DD", "start": start})
    SEARCHES_MADE[0] += 1
    page = http_get(f"{LINKEDIN_SEARCH}?{q}")
    if not page:
        FAILED_SEARCHES.append(f"LinkedIn {location} (start {start})")
        return []
    jobs = []
    for card in page.split("<li")[1:]:
        m = re.search(r'href="([^"]*?/jobs/view/[^"?]*?-(\d{8,}))', card)
        if not m:
            continue
        jobs.append(new_job(
            id=m.group(2), source="LinkedIn",
            url=html.unescape(m.group(1)).replace("://eg.", "://www.").split("?")[0],
            title=first(r'base-search-card__title[^>]*>(.*?)</h3>', card),
            company=first(r'base-search-card__subtitle[^>]*>(.*?)</h4>', card),
            location=first(r'job-search-card__location[^>]*>(.*?)</span>', card),
            ago=first(r'<time[^>]*>(.*?)</time>', card),
        ))
    if not jobs and "<li" in page:  # an empty result is a tiny page with no <li>; cards we can't read are not
        PARSE_ISSUES.append(f"LinkedIn search results ({location})")
    return jobs


def linkedin_details(job: dict) -> bool:
    """Fill text/applicants/employment. False when the page could not be loaded (retry next run)."""
    page = http_get(LINKEDIN_JOB.format(job["id"])) or ""
    text = strip_tags(first(r'show-more-less-html__markup[^>]*>(.*?)</div>', page) or "")
    closed = "No longer accepting applications" in page
    if not text and not closed:
        if len(page) > 5000:  # full page arrived but the description block was not found
            PARSE_ISSUES.append("LinkedIn job page")
        return False
    job.update(text=text, closed=closed,
               applicants=first(r'num-applicants__caption[^>]*>(.*?)</', page),
               employment=first(r'Employment type\s*</h3>\s*<span[^>]*>(.*?)</span>', page))
    return True


def linkedin_posted(ago: str, now: datetime) -> datetime:
    """'12 minutes ago' -> absolute local time (approximate to the unit LinkedIn shows)."""
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week)", ago, re.I)
    if not m:
        return now
    n, unit = int(m.group(1)), m.group(2).lower()
    return now - timedelta(**{unit + "s": n})


# ---------------------------------------------------------------- remote boards
def remoteok_jobs() -> list[dict]:
    # API terms: link back to the Remote OK URL and mention Remote OK as the source.
    data = json.loads(http_get(REMOTEOK_API) or "[]")
    jobs = []
    for j in data[1:]:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        jobs.append(new_job(
            id=f"remoteok:{j['id']}", source="Remote OK", url=j.get("url") or "",
            title=html.unescape(j.get("position") or ""), company=html.unescape(j.get("company") or ""),
            location=j.get("location") or "", text=strip_tags(j.get("description") or ""),
            posted=to_cairo(datetime.fromtimestamp(int(j.get("epoch") or 0), timezone.utc)),
            remote=True, region_check=True))
    return jobs


def wwr_jobs() -> list[dict]:
    feed = http_get(WWR_RSS) or ""
    jobs = []
    for item in feed.split("<item>")[1:]:
        def tag(name):
            m = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
            return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
        company, _, title = tag("title").partition(": ")
        link = tag("link")
        try:
            posted = to_cairo(parsedate_to_datetime(tag("pubDate")))
        except (TypeError, ValueError):
            posted = None
        jobs.append(new_job(
            id=f"wwr:{link.rstrip('/').rsplit('/', 1)[-1]}", source="We Work Remotely", url=link,
            title=title or company, company=company if title else "",
            location=tag("region") or tag("country"), text=strip_tags(tag("description")),
            posted=posted, remote=True, region_check=True))
    return jobs


def remotive_jobs() -> list[dict]:
    # API terms: link back to the Remotive URL and mention Remotive; jobs are delayed 24 hours.
    data = json.loads(http_get(f"{REMOTIVE_API}?category=qa") or "{}")
    jobs = []
    for j in data.get("jobs", []):
        try:
            posted = to_cairo(datetime.fromisoformat(j["publication_date"]))
        except (KeyError, ValueError):
            posted = None
        jobs.append(new_job(
            id=f"remotive:{j['id']}", source="Remotive", url=j.get("url") or "",
            title=j.get("title") or "", company=j.get("company_name") or "",
            location=j.get("candidate_required_location") or "", text=strip_tags(j.get("description") or ""),
            posted=posted, remote=True, region_check=True))
    return jobs


def board_jobs(cfg: dict, state: dict, now: datetime) -> list[dict]:
    """Remote boards change slowly: fetch them at most every `boards_every_minutes`."""
    last = state.get("boards_last_fetch")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=cfg["boards_every_minutes"]):
        return []
    state["boards_last_fetch"] = now.isoformat(timespec="minutes")
    jobs = []
    for name, fetch in (("remoteok", remoteok_jobs), ("wwr", wwr_jobs), ("remotive", remotive_jobs)):
        if name not in cfg["boards"]:
            continue
        try:
            found = fetch()
        except (ValueError, KeyError) as e:
            log(f"{name}: could not read feed ({e})")
            found = []
        if not found:  # these feeds always list dozens of jobs; empty means unreachable or changed
            BOARD_ISSUES.append(name)
        jobs += found
        time.sleep(cfg["delay_seconds"])
    return jobs


# ---------------------------------------------------------------- health alerts
def run_problems(cfg: dict) -> list[str]:
    """Signs that this run did not really check for jobs (as opposed to there being no new jobs)."""
    problems = []
    made, failed = SEARCHES_MADE[0], len(FAILED_SEARCHES)
    if made and (made - failed) / made < cfg["health"]["min_answered_ratio"]:
        problems.append(f"LinkedIn answered only {made - failed}/{made} searches (rate limit or block)")
    if PARSE_ISSUES:
        problems.append("LinkedIn pages loaded but jobs could not be read — the page layout may have "
                        f"changed ({', '.join(sorted(set(PARSE_ISSUES)))[:120]})")
    if BOARD_ISSUES:
        problems.append(f"Could not read remote board feed: {', '.join(BOARD_ISSUES)}")
    return problems


def update_health(state: dict, problems: list[str], cfg: dict, now: datetime, notify) -> None:
    """Alert after N bad runs in a row, remind while it lasts, and say when it is back to normal.

    `notify(text)` sends a Telegram message. State lives in state["health"].
    """
    h = state.setdefault("health", {"bad_streak": 0, "alerted_at": None, "alerted_kinds": []})
    if not problems:
        if h.get("alerted_at"):
            notify("✅ <b>Job watcher is back to normal.</b>\nJobs are being checked again.")
        h.update(bad_streak=0, alerted_at=None, alerted_kinds=[])
        return
    h["bad_streak"] = h.get("bad_streak", 0) + 1
    if h["bad_streak"] < cfg["health"]["alert_after_bad_runs"]:
        return
    kinds = sorted({p[:24] for p in problems})
    new_kind = not set(kinds) <= set(h.get("alerted_kinds") or [])
    last = h.get("alerted_at")
    if (not new_kind and last
            and now - datetime.fromisoformat(last) < timedelta(hours=cfg["health"]["remind_every_hours"])):
        return
    h["alerted_kinds"] = sorted(set(h.get("alerted_kinds") or []) | set(kinds))
    notify(f"⚠️ <b>Job watcher problem</b> — {h['bad_streak']} runs in a row\n"
           + "\n".join(f"• {html.escape(p)}" for p in problems)
           + "\n\nNo message from the watcher right now does <b>not</b> mean there are no new jobs."
           + f"\n{actions_url()}")
    h["alerted_at"] = now.isoformat(timespec="minutes")


def actions_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "yousefonsy-QA/job-watcher")
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"https://github.com/{repo}/actions" + (f"/runs/{run_id}" if run_id else "")


def notify_failure():
    """Called by the workflow when a step failed (script crash or self-check tests failed)."""
    cfg, env, state = load_json(CONFIG_FILE, {}), load_env(), load_json(STATE_FILE, {})
    now = now_cairo()

    def notify(text):
        tg_call(env["TELEGRAM_BOT_TOKEN"], "sendMessage", {"chat_id": env["TELEGRAM_CHAT_ID"], "text": text,
                                                            "parse_mode": "HTML", "disable_web_page_preview": "true"})
    # a crashed run counts as a bad run; alert right away (it will not fix itself)
    cfg = {**cfg, "health": {**cfg["health"], "alert_after_bad_runs": 1}}
    update_health(state, ["The watcher run failed (script error or self-check tests failed)"], cfg, now, notify)
    save_json(STATE_FILE, state)


# ---------------------------------------------------------------- filters
def is_remote(job: dict, text: str) -> bool:
    """True when the posting itself says remote and does not require office presence."""
    head = f"{job['title']} {job['location']}"
    if re.search(r"\bremote\b", head, re.I):
        return not re.search(r"\bhybrid\b|on-?site", head, re.I)
    return bool(REMOTE_YES.search(text)) and not REMOTE_NO.search(text)


def min_required_years(text: str) -> int | None:
    """Largest lower bound among 'N years' phrases that talk about experience.

    Taking the largest lower bound means "5+ years overall, incl. 2 years X" -> 5.
    Returns None when no experience requirement is stated.
    """
    found = []
    for sentence in SENTENCES.split(text):
        if not EXPERIENCE_HINT.search(sentence):
            continue
        for m in YEARS.finditer(sentence):
            n = int(m.group(1))
            if 0 <= n <= 15:
                found.append(n)
    return max(found) if found else None


def required_sentence(text: str, subject: re.Pattern) -> str | None:
    """First sentence that mentions `subject` as a requirement (not as a nice-to-have)."""
    for sentence in SENTENCES.split(text):
        if subject.search(sentence) and REQUIRED.search(sentence) and not OPTIONAL.search(sentence):
            return sentence.strip()
    return None


def region_blocked(job: dict, cfg: dict) -> str | None:
    """Remote-board jobs: reason when the job is not open to someone living in Egypt."""
    loc = job["location"].strip()
    if loc and not re.search(cfg["remote_regions_ok"], loc, re.I):
        return f"remote only for {loc[:40]}"
    lock = re.search(cfg["remote_region_lock"], job["text"] or "", re.I)
    if lock:
        return f"region lock: '{lock.group(0)[:40]}'"
    return None


def skip_reason(job: dict, cfg: dict, filters: dict) -> str | None:
    """None when the job should be sent; otherwise a short reason recorded in jobs.json."""
    text = job["text"] or ""
    if filters["desc_domain"].search(text[:600]):
        return "non-software domain"
    if filters["nationals_title"].search(job["title"]) or required_sentence(text, filters["nationals"]):
        return "nationals only"
    lang = required_sentence(text, filters["languages"])
    if lang:
        return f"foreign language required ({filters['languages'].search(lang).group(0)})"
    if job["region_check"]:
        blocked = region_blocked(job, cfg)
        if blocked:
            return blocked
    elif job["remote_only"] and not job["remote"]:
        return "not remote (remote-only country)"
    years = job["years"]
    if years is not None and years > cfg["max_years"]:
        return f"JD min {years} years"
    return None


def build_filters(cfg: dict) -> dict:
    words = cfg["filters"]
    return {
        "title_exclude": re.compile("|".join(words["exclude_title"]), re.I),
        "nationals_title": re.compile("|".join(words["nationals_title"]), re.I),
        "nationals": re.compile(words["nationals_requirement"], re.I),
        "languages": re.compile(r"\b(" + "|".join(words["foreign_languages"]) + r")\b", re.I),
        "desc_domain": re.compile("|".join(words["exclude_description_domain"]), re.I),
    }


# ---------------------------------------------------------------- Telegram
def tg_call(token: str, method: str, params: dict) -> dict:
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}") | {"ok": False}
    except Exception as e:  # noqa: BLE001 - report and continue
        log(f"Telegram {method} failed: {e}")
        return {"ok": False}


def format_message(job: dict) -> str:
    years = job["years"]
    yrs = f"{years}+ yrs" if years is not None else "years not stated"
    where = "🏠 Remote" if job["remote"] else "On-site / Hybrid"
    posted = f" ({job['posted']:%H:%M})" if job["posted"] else ""
    extra = f" · {html.escape(job['applicants'])}" if job["applicants"] else ""
    return (f"🆕 <b>{html.escape(job['title'])}</b>\n"
            f"🏢 {html.escape(job['company'])}\n"
            f"📍 {html.escape(job['location'] or 'Anywhere')} · {where}\n"
            f"🕒 {html.escape(job['ago'] or 'just now')}{posted}\n"
            f"🎯 {yrs}{extra}\n"
            f"📰 {job['source']}\n"
            f"🔗 {job['url']}")


def print_chat_id(token: str):
    updates = tg_call(token, "getUpdates", {}).get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates if "message" in u}
    if not chats:
        print("No messages yet. Open your bot in Telegram, send it /start, then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


# ---------------------------------------------------------------- main run
def run(dry_run: bool):
    cfg = load_json(CONFIG_FILE, {})
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not dry_run and not (token and chat_id):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (GitHub secrets or .env)")

    seen = set(load_json(SEEN_FILE, []))
    state = load_json(STATE_FILE, {})
    if IN_GITHUB and not dry_run and laptop_active(state, cfg):
        log(f"laptop runner is active (last run {state['local_heartbeat_utc']} UTC) — GitHub run skipped")
        return
    jobs = load_json(JOBS_FILE, [])
    seen |= {j["watcher_id"] for j in jobs if j.get("watcher_id")}
    seen |= {m.group(1) for j in jobs
             if "linkedin" in j.get("job_url", "") and (m := re.search(r"(\d{9,})", j["job_url"]))}
    filters = build_filters(cfg)
    now = now_cairo()

    candidates = {}
    for job in linkedin_jobs(cfg) + board_jobs(cfg, {} if dry_run else state, now):
        if job["id"] in seen or job["id"] in candidates:
            continue
        if job["posted"] and now - job["posted"] > timedelta(hours=cfg["boards_max_age_hours"]):
            continue  # remote boards list jobs for weeks; only recent ones are worth a message
        if not QA_TITLE.search(job["title"]) or filters["title_exclude"].search(job["title"]):
            continue
        candidates[job["id"]] = job

    sent = skipped = 0
    for job_id, job in candidates.items():
        if job["source"] == "LinkedIn":
            time.sleep(cfg["delay_seconds"])
            if not linkedin_details(job):
                continue  # not loaded (rate limit / time limit): not marked seen, retried next run
            if job.get("closed"):
                seen.add(job_id)
                continue
            job["remote"] = is_remote(job, job["text"])
            job["posted"] = linkedin_posted(job["ago"], now)
        else:
            job["ago"] = ago_text(job["posted"], now) if job["posted"] else ""
        job["years"] = min_required_years(job["text"] or "")
        reason = skip_reason(job, cfg, filters)

        if dry_run:
            print(("SEND " if not reason else "SKIP ") + f"[{job['source']}] {job['title']} | {job['company']} | "
                  f"{job['location']} | {'remote' if job['remote'] else 'on-site'} | "
                  f"{reason or ('JD min %s years' % job['years'] if job['years'] is not None else 'years n/a')}"
                  f" | {job['ago']}")
            continue

        if not reason:
            resp = tg_call(token, "sendMessage", {"chat_id": chat_id, "text": format_message(job),
                                                  "parse_mode": "HTML", "disable_web_page_preview": "true"})
            if not resp.get("ok"):
                log(f"Telegram send failed for {job_id}: {resp.get('description', '')}")
                continue  # not marked seen: retried next run
            sent += 1
        else:
            skipped += 1
        yrs_note = f"JD min {job['years']} years" if job["years"] is not None else "Years not stated"
        jobs.append({
            "watcher_id": job_id,
            "job_id": f"{re.sub(r'[^a-z0-9]+', '', job['company'].lower())[:14]}_{re.sub(r'[^0-9a-z]+', '', job_id)[-12:]}",
            "job_title": job["title"], "company": job["company"], "location": job["location"],
            "posted_at": job["posted"].isoformat(timespec="minutes") if job["posted"] else "",
            "job_url": job["url"], "alt_url": "", "source": f"{job['source']} (watcher)",
            "employment_type": job["employment"] or "UNKNOWN",
            "status": "SKIPPED" if reason else "NEW (watcher)",
            "match_score": "N/A" if reason else "UNKNOWN", "opportunity_score": "N/A" if reason else "UNKNOWN",
            "wishlist": "No", "salary": "UNKNOWN", "easy_apply": "UNKNOWN",
            "scouted_date": f"{now:%Y-%m-%d}", "record_file": "",
            "notes": (f"SKIP (watcher): {reason}. " if reason else f"{yrs_note}. ")
                     + ("Remote. " if job["remote"] else "") + (job["applicants"] or ""),
        })
        seen.add(job_id)

    if dry_run:
        print(f"{len(candidates)} new QA listings (dry run, nothing saved)")
        return
    problems = run_problems(cfg)
    update_health(state, problems, cfg, now, lambda text: tg_call(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}))
    if not IN_GITHUB:  # tells the GitHub copy that the laptop is covering the checks
        state["local_heartbeat_utc"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    save_json(SEEN_FILE, sorted(seen), pretty=False)
    save_json(STATE_FILE, state)
    if sent or skipped:
        save_json(JOBS_FILE, jobs)
    total = SEARCHES_MADE[0]
    note = " — stopped early: LinkedIn rate limit / time limit" if out_of_time() else ""
    log(f"checked {len(candidates)} new listings: {sent} sent to Telegram, {skipped} skipped "
        f"({total - len(FAILED_SEARCHES)}/{total} LinkedIn searches answered){note}")
    if problems:
        log(f"health: bad run {state['health']['bad_streak']} in a row — " + " | ".join(problems))


def send_test(env: dict):
    job = new_job(id="test", source="Test", title="Job watcher is connected ✅", company="—",
                  location="Anywhere", url="https://github.com/yousefonsy-QA/job-watcher",
                  remote=True, ago="just now")
    job["years"] = None
    resp = tg_call(env["TELEGRAM_BOT_TOKEN"], "sendMessage", {
        "chat_id": env["TELEGRAM_CHAT_ID"], "text": format_message(job), "parse_mode": "HTML",
        "disable_web_page_preview": "true"})
    print("sent" if resp.get("ok") else f"failed: {resp.get('description', '')}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--chat-id":
        print_chat_id(load_env()["TELEGRAM_BOT_TOKEN"])
    elif arg == "--test":
        send_test(load_env())
    elif arg == "--notify-failure":
        notify_failure()
    else:
        run(dry_run=arg == "--dry-run")
