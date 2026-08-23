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


def process_alive(job_id):
    r"""True when a resume runner for this job is running. Command line, not a pidfile:
    a stale pidfile outlives a killed process and would make the guard sit idle forever.

    The match MUST happen inside PowerShell. Piping CommandLine strings back to stdout runs
    them through the console formatter, which TRUNCATES each line at the host buffer width
    -- 70 columns here. A resume command line is ~105 characters, so what reached Python was
    "...\.venv\Scripts\python.exe -", with `pipeline.resume` and the job id cut clean off.
    process_alive() therefore returned False for a perfectly healthy runner, every time.

    Run pbfamily0001 paid for that: the guard restarted it every 10 minutes for six hours on
    2026-08-21, piling concurrent runners onto one SQLite file. Stage 5 sat at 2/3 from
    10:51 to 13:41 and stage 7 restarted 10 times, each one resetting soft-filter progress
    to 0/8925. Returning a COUNT keeps the formatter out of it -- an integer cannot wrap.
    """
    if not job_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("job_id must be alphanumeric (it is interpolated into PowerShell)")
    script = (
        "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*pipeline.resume*' -and "
        "$_.CommandLine -like '*%s*' }).Count" % job_id
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True)
    try:
        return int((proc.stdout or "0").strip()) > 0
    except ValueError:
        # Never guess. A guard that reads "unknown" as "dead" is what caused the thrash.
        log("process check failed for %s (%r); assuming ALIVE and not restarting"
            % (job_id, (proc.stderr or proc.stdout or "")[:120].strip()))
        return True


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
    if process_alive(job_id):
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
