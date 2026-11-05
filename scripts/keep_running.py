"""Keep FresherPilot running on this PC: start it (hidden), check every minute that it answers, and
start it again if it stopped (crash, closed window). If the app is already running (e.g. you started
`python main.py` yourself), this only watches.

Started at Windows sign-in by a shortcut in the Startup folder (scripts/install_autostart.ps1).
Logs: logs/server.log (the app) and logs/keep_running.log (this watchdog).
Stop for now: run scripts/stop.ps1. Remove autostart: scripts/install_autostart.ps1 -Remove.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
PORT = int(os.getenv("FP_PORT", "8000"))
HEALTH = f"http://127.0.0.1:{PORT}/api/health"
CHECK_EVERY = 60          # seconds between health checks
MAX_LOG_BYTES = 5_000_000  # server.log is rotated to server.log.1 past this
PID_FILE = LOGS / "keep_running.pid"
APP_PID_FILE = LOGS / "app.pid"  # the app this watchdog started (scripts/stop.ps1 stops it)
STOPPED_FLAG = LOGS / "stopped"  # written by scripts/stop.ps1: stay off until install_autostart.ps1 runs again
STATUS = f"http://127.0.0.1:{PORT}/api/status"
STALE_AFTER = 20 * 60  # app answers but no job reader finished a read for this long = hung: restart
# Measured Nov 4: after waking from sleep at 09:51 the app answered but read nothing until 10:40.


def log(msg: str) -> None:
    LOGS.mkdir(exist_ok=True)
    with (LOGS / "keep_running.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def readers_stale() -> bool:
    """True when the app is up but none of its job readers finished a read for STALE_AFTER seconds."""
    try:
        with urllib.request.urlopen(STATUS, timeout=15) as r:
            live = (json.load(r).get("live") or {})
    except Exception:  # noqa: BLE001
        return False  # can't tell: the health check decides
    if not live.get("running"):
        return False  # readers not started (e.g. not signed in yet)
    oks = [b.get("ok_at") or 0 for b in (live.get("boards") or {}).values()]
    return bool(oks) and time.time() - max(oks) > STALE_AFTER


def start_app() -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    out = LOGS / "server.log"
    if out.exists() and out.stat().st_size > MAX_LOG_BYTES:
        out.replace(LOGS / "server.log.1")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":  # the app itself runs with python.exe (output goes to the log)
        python = python.with_name("python.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_f = out.open("a", encoding="utf-8")
    return subprocess.Popen([str(python), "main.py"], cwd=ROOT, env=env, stdout=log_f, stderr=subprocess.STDOUT,
                            creationflags=flags)


def other_watchdog_running() -> bool:
    try:
        pid = int(PID_FILE.read_text())
    except Exception:  # noqa: BLE001
        return False
    if pid == os.getpid():
        return False
    # The PID alone isn't enough: after a reboot Windows reuses it (Nov 5: the saved 5068 was svchost.exe,
    # so every start after boot thought a watchdog was running and quit). Check it's really this script.
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                             capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "keep_running.py" in out
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    LOGS.mkdir(exist_ok=True)
    if STOPPED_FLAG.exists():
        return  # stopped on purpose (scripts/stop.ps1)
    if other_watchdog_running():
        return  # already watching (started by the scheduled task every 10 min / on wake)
    PID_FILE.write_text(str(os.getpid()))
    log("watchdog started")
    child: subprocess.Popen | None = None
    failures = 0
    started_at = time.time()
    last_loop = time.time()
    while True:
        if STOPPED_FLAG.exists():
            log("stop requested: watchdog exits")
            return
        gap = time.time() - last_loop
        last_loop = time.time()
        if gap > CHECK_EVERY * 3:
            log(f"PC was asleep or paused for {gap / 60:.0f} min")
            started_at = max(started_at, time.time() - STALE_AFTER + 5 * 60)  # give the readers 5 min after waking
        if healthy():
            failures = 0
            if time.time() - started_at > STALE_AFTER and readers_stale():
                log("app answers but no job reader finished a read for 20 min: restarting")
                restart_pid(child)
                child = None
                started_at = time.time()
        else:
            running = child is not None and child.poll() is None
            if running:
                failures += 1  # still starting up, or hung
                if failures >= 5:  # no answer for ~5 minutes: restart it
                    log("app not answering for 5 min: restarting")
                    child.kill()
                    child = None
            if child is None or child.poll() is not None:
                if child is not None:
                    log(f"app stopped (exit code {child.returncode}): starting again")
                time.sleep(5)
                if not healthy():  # nobody else started it meanwhile
                    child = start_app()
                    failures = 0
                    started_at = time.time()
                    APP_PID_FILE.write_text(str(child.pid))
                    log(f"app started (pid {child.pid})")
        time.sleep(CHECK_EVERY)


def restart_pid(child: subprocess.Popen | None) -> None:
    """Stop the app (ours, or the one in app.pid) so the loop starts a fresh one."""
    if child is not None and child.poll() is None:
        child.kill()
        return
    try:
        pid = int(APP_PID_FILE.read_text())
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                             capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        if "main.py" in out:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
