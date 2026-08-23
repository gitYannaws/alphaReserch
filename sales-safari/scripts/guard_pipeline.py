"""Restart the pooled-corpus run if it has died. Safe to call every few minutes.

Why this exists: on 2026-08-18 the extraction task was registered /IT (interactive token,
the only way to register as this user without a password). The session ended, Windows tore
the process down with 0xC000013A, and nothing noticed for 7.3 hours. Extraction itself is
checkpointed, so a restart costs minutes -- the loss was purely the time nobody spent
watching.

Exits quietly when the run is finished, already alive, or was stopped on purpose.

    python scripts/guard_pipeline.py pbfamily0001 AlphaPassportFamilyPipeline
"""
import subprocess
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "safari.sqlite"
GUARD_LOG = ROOT / "logs" / "guard.log"

# A run that keeps dying instantly is a real fault, not a session blip. Backing off stops
# the guard hammering a broken job every few minutes for days.
MAX_RESTARTS_PER_HOUR = 4
# Terminal states. 'cancelled' is deliberate (a human stopped it), so never revive it.
TERMINAL = {"done", "reported", "cancelled"}


def log(msg):
    GUARD_LOG.parent.mkdir(exist_ok=True)
    line = "%s  %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _ps(script):
    """Run one PowerShell expression and return its stdout, or None if it failed."""
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _task_is_running(task_name):
    """Ask the Scheduled Task itself. This is the ONLY authoritative source here."""
    if not task_name.replace("-", "").replace("_", "").isalnum():
        return None
    out = _ps("(Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue).State"
              % task_name)
    if out is None:
        return None
    state = out.strip().lower()
    return state == "running" if state else None


def _pidfile_alive(job_id):
    """The runner writes its own PID. Readable even when its command line is not."""
    pidfile = ROOT / "db" / ("resume-%s.pid" % job_id)
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    out = _ps("@(Get-Process -Id %d -ErrorAction SilentlyContinue).Count" % pid)
    if out is None:
        return None
    try:
        return int(out.strip()) > 0
    except ValueError:
        return None


def _any_unreadable_python():
    """True when a python process exists whose command line this session cannot read.

    Then we cannot prove the runner is dead, and must not act as if we can.
    """
    out = _ps("@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { -not $_.CommandLine }).Count")
    if out is None:
        return True
    try:
        return int(out.strip()) > 0
    except ValueError:
        return True


def process_alive(job_id, task_name=None):
    r"""True when a resume runner for this job is running -- or might be.

    This check has now been wrong twice, in two different ways, and both cost real hours.

    First it piped Win32_Process CommandLine strings back to Python. PowerShell runs those
    through the console formatter, which TRUNCATES at the host buffer width (70 columns
    here), so a ~105-character resume command line arrived as
    "...\.venv\Scripts\python.exe -" with `pipeline.resume` and the job id cut clean off.
    Every healthy runner read as dead, and pbfamily0001 was restarted every 10 minutes for
    six hours on 2026-08-21, stacking runners onto one SQLite file.

    Matching inside PowerShell fixed the truncation and did NOT fix the check. On
    2026-08-23 the Scheduled Task's own processes came back with a NULL CommandLine -- not
    readable from this session at all -- so `-like` matched nothing, the count was 0, and the
    guard restarted a live runner again. Two runners, "database is locked", a 199-second
    Claude call lost.

    The lesson both times: this function must never infer "dead" from "I could not see it".
    So it asks, in order, and takes the first source that answers:

      1. the Scheduled Task's own State  -- authoritative, and readable
      2. the runner's pidfile            -- written by the runner, readable
      3. a command-line match            -- the original method, kept as a backstop
      4. any python process this session cannot read -- cannot rule it out, so ALIVE
    """
    if not job_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("job_id must be alphanumeric (it is interpolated into PowerShell)")

    if task_name:
        running = _task_is_running(task_name)
        if running is not None:
            return running

    by_pid = _pidfile_alive(job_id)
    if by_pid:
        return True

    out = _ps("@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*pipeline.resume*' -and "
              "$_.CommandLine -like '*%s*' }).Count" % job_id)
    if out is not None:
        try:
            if int(out.strip()) > 0:
                return True
        except ValueError:
            pass

    if _any_unreadable_python():
        log("cannot read every python command line for %s; assuming ALIVE" % job_id)
        return True
    return False


def recent_restarts():
    if not GUARD_LOG.exists():
        return 0
    cutoff = time.time() - 3600
    n = 0
    for ln in GUARD_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "RESTARTED" not in ln:
            continue
        try:
            ts = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts.timestamp() >= cutoff:
            n += 1
    return n


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    job_id, task_name = sys.argv[1], sys.argv[2]

    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    row = c.execute("select stage, status from runs where job_id=?", (job_id,)).fetchone()
    if not row:
        log("no run row for %s, nothing to guard" % job_id)
        return
    stage, status = row
    if status in TERMINAL:
        return
    if process_alive(job_id, task_name):
        return

    n = recent_restarts()
    if n >= MAX_RESTARTS_PER_HOUR:
        log("NOT restarting %s: %d restarts in the last hour, backing off" % (job_id, n))
        return

    prog = c.execute("select done, total from run_progress where run_id=? "
                     "order by updated_at desc limit 1", (job_id,)).fetchone()
    where = ("%d/%d docs" % prog) if prog else "no progress row"
    subprocess.run(["schtasks", "/run", "/tn", task_name],
                   capture_output=True, text=True)
    log("RESTARTED %s via task %s (was stage=%s status=%s, %s)"
        % (job_id, task_name, stage, status, where))


if __name__ == "__main__":
    main()
