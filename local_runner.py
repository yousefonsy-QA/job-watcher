"""Laptop copy of the watcher, started by Windows Task Scheduler every 15 minutes.

Each run: take the latest code and shared state from GitHub, check for jobs, push the state back.
The pushed heartbeat tells the GitHub copy to skip while the laptop is on; when the laptop is off
or asleep the heartbeat goes stale and GitHub takes over by itself.

Runs in its own clone (never in a development checkout): it resets the clone to origin/main.
Secrets come from .env in the folder above this clone. Output: runner.log next to this file.
"""
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "runner.log"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def step(*cmd: str) -> int:
    r = subprocess.run(list(cmd), cwd=HERE, capture_output=True, text=True, creationflags=NO_WINDOW)
    out = (r.stdout + r.stderr).strip()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] $ {' '.join(cmd[-2:])} -> {r.returncode}"
                + (f"\n{out}" if out else "") + "\n")
    return r.returncode


def main() -> int:
    if step("git", "fetch", "-q", "origin") != 0:
        return 1  # offline: nothing useful to do; GitHub takes over once the heartbeat is stale
    step("git", "reset", "-q", "--hard", "origin/main")
    python = sys.executable.replace("pythonw.exe", "python.exe")
    step(python, "job_watcher.py")
    return step(python, "push_state.py", f"state: laptop run {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}")


if __name__ == "__main__":
    sys.exit(main())
