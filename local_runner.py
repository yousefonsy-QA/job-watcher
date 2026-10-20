"""Laptop copy of the watcher, started by Windows Task Scheduler every 15 minutes.

Each run: bring this folder up to date with GitHub, check for jobs, push the state back.
The pushed heartbeat tells the GitHub copy to skip while the laptop is on; when the laptop is off
or asleep the heartbeat goes stale and GitHub takes over by itself.

This is also the folder where the code is edited, so a run never touches work in progress:
if code is modified or committed but not pushed yet, the run is skipped (GitHub covers it) and
the reason is written to runner.log. Secrets come from .env in this folder (git-ignored).
"""
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "runner.log"
STATE_FILES = {"watcher_seen.json", "watcher_state.json", "jobs.json"}
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def write_log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {text}\n")


def step(*cmd: str) -> subprocess.CompletedProcess:
    r = subprocess.run(list(cmd), cwd=HERE, capture_output=True, text=True, creationflags=NO_WINDOW)
    out = (r.stdout + r.stderr).strip()
    write_log(f"$ {' '.join(cmd[-2:])} -> {r.returncode}" + (f"\n{out}" if out else ""))
    return r


def git_out(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                          creationflags=NO_WINDOW).stdout


def work_in_progress() -> str | None:
    """Reason to skip when someone is editing code here; None when the folder is safe to update."""
    changed = [line[3:].strip('"') for line in git_out("status", "--porcelain").splitlines()]
    edited = [p for p in changed if p not in STATE_FILES and not p.endswith(".log")]
    if edited:
        return f"code changes not committed yet: {', '.join(edited[:5])}"
    ahead = git_out("rev-list", "--count", "origin/main..HEAD").strip()
    if ahead and ahead != "0":
        return f"{ahead} local commit(s) not pushed yet"
    return None


def main() -> int:
    python = sys.executable.replace("pythonw.exe", "python.exe")
    if step("git", "fetch", "-q", "origin").returncode != 0:
        return 1  # offline: GitHub takes over once the heartbeat is stale
    reason = work_in_progress()
    if reason:
        write_log(f"skipped — {reason} (GitHub covers this run)")
        return 0
    if set(p[3:].strip('"') for p in git_out("status", "--porcelain").splitlines()) & STATE_FILES:
        # state left uncommitted by an interrupted run: save it before updating
        step(python, "push_state.py", "state: leftover from interrupted laptop run")
    if step("git", "merge", "-q", "--ff-only", "origin/main").returncode != 0:
        return 1
    step(python, "job_watcher.py")
    return step(python, "push_state.py",
                f"state: laptop run {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}").returncode


if __name__ == "__main__":
    sys.exit(main())
