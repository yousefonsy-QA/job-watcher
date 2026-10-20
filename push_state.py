"""Commit the watcher's state files and push them, merging with the other copy if needed.

Two copies share this repo's state: the laptop runner and GitHub Actions. When both push at the
same time the second push is rejected; instead of failing (or overwriting), this merges:
seen IDs and jobs are combined, and timestamps keep the newest value.

Usage: python push_state.py "commit message"
Used by the GitHub Actions checkout and by local_runner.py — it refuses to run when files other
than the state files are modified, so it never touches development work.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILES = ["watcher_seen.json", "watcher_state.json", "jobs.json"]
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # no console pop-ups from the scheduled task


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, check=check,
                          creationflags=NO_WINDOW)


def read(name: str):
    path = HERE / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write(name: str, data) -> None:
    if data is None:
        return
    pretty = name != "watcher_seen.json"
    text = json.dumps(data, ensure_ascii=False, indent=2 if pretty else None)
    (HERE / name).write_text(text + "\n", encoding="utf-8")


def job_key(job: dict) -> str:
    return job.get("watcher_id") or job.get("job_url") or json.dumps(job, sort_keys=True)


def merge(ours: dict, theirs: dict) -> dict:
    """Combine our state with the state the other copy already pushed."""
    merged = {}
    merged["watcher_seen.json"] = sorted(set(ours.get("watcher_seen.json") or [])
                                         | set(theirs.get("watcher_seen.json") or []))

    jobs = list(theirs.get("jobs.json") or [])
    known = {job_key(j) for j in jobs}
    jobs += [j for j in ours.get("jobs.json") or [] if job_key(j) not in known]
    merged["jobs.json"] = jobs if (ours.get("jobs.json") or theirs.get("jobs.json")) else None

    state = dict(theirs.get("watcher_state.json") or {})
    our_state = ours.get("watcher_state.json") or {}
    for key in ("local_heartbeat_utc", "boards_last_fetch"):
        values = [v for v in (state.get(key), our_state.get(key)) if v]
        if values:
            state[key] = max(values)  # same ISO format on both sides, so string order is time order
    if "health" in our_state:
        state["health"] = our_state["health"]  # this run is the newest one
    merged["watcher_state.json"] = state
    return merged


def main(message: str) -> int:
    other_changes = [line[3:] for line in git("status", "--porcelain").stdout.splitlines()
                     if line[3:].strip('"') not in STATE_FILES and not line[3:].endswith(".log")]
    if other_changes:
        print(f"refusing to push: non-state files changed: {other_changes}")
        return 2
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for attempt in range(4):
        git("add", "--", *[f for f in STATE_FILES if (HERE / f).exists()])
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("no state changes")
            return 0
        git("commit", "-q", "-m", message)
        if git("push", "-q", "origin", branch, check=False).returncode == 0:
            print("state pushed" + (f" (merged after {attempt} retries)" if attempt else ""))
            return 0
        # the other copy pushed first: take its version and add ours on top
        ours = {f: read(f) for f in STATE_FILES}
        git("fetch", "-q", "origin", branch)
        git("reset", "-q", "--hard", f"origin/{branch}")
        for name, data in merge(ours, {f: read(f) for f in STATE_FILES}).items():
            write(name, data)
        time.sleep(3)
    print("could not push state after 4 attempts")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "state: watcher run"))
