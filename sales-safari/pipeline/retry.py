"""Shared per-stage retry with exponential backoff.

Used by both the live web worker (webapp/app.py) and the standalone resume
runner (pipeline/resume.py). A transient stage failure -- a flaky Chromium
launch (the ICU `Invalid file descriptor` class), a network blip, a rate-limit
burst -- is retried instead of killing the whole run.

Design rules:
  * Cancellation is never retried. `should_stop()` is called before each attempt
    and any `cancel_exc` it raises (or the stage raises) propagates immediately.
  * Only wrap the *work*, not validation guards. A logical guard like
    "0 pains produced" must live at the call site, OUTSIDE the retry, so a
    deterministic empty result is not re-run N times.
  * Stages must be idempotent to be safe here (dedup-on-insert or clear-first).
"""
import time
import traceback


def run_with_retry(fn, *, name, attempts=3, base_delay=5.0, log=None,
                   should_stop=None, cancel_exc=(), advisory=False, default=None):
    """Call ``fn()`` with retry + exponential backoff; return its result.

    Args:
        fn: zero-arg callable performing the stage work.
        name: label for logs.
        attempts: total tries before giving up (>=1).
        base_delay: backoff seconds; doubles each retry (5 -> 10 -> 20 ...).
        log: optional ``log(kind, msg)`` structured logger.
        should_stop: optional zero-arg callable checked before each attempt;
            may raise ``cancel_exc`` to abort without retrying.
        cancel_exc: exception type/tuple meaning "cancelled" -> re-raised, never retried.
        advisory: if True, on final failure return ``default`` instead of raising.
        default: value returned for an exhausted advisory stage.

    Returns:
        ``fn()``'s result, or ``default`` for an exhausted advisory stage.

    Raises:
        The last exception for an exhausted non-advisory stage, or ``cancel_exc``.
    """
    def _log(kind, msg):
        if log:
            log(kind, msg)

    for attempt in range(1, attempts + 1):
        try:
            if should_stop:
                should_stop()
            return fn()
        except cancel_exc:
            raise
        except Exception as e:
            _log("WARN", f"{name} attempt {attempt}/{attempts} failed: {e}")
            traceback.print_exc()
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                _log("RETRY", f"{name} retry in {delay:g}s")
                time.sleep(delay)
            elif advisory:
                _log("NOTIFY", f"advisory {name} gave up after {attempts} tries; continuing")
                return default
            else:
                _log("NOTIFY", f"{name} failed after {attempts} tries: {e}")
                raise
    return default
