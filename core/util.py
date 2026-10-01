"""Small shared helpers: time, logging (with secret redaction), run locks."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import subprocess

ISO = "%Y-%m-%dT%H:%M:%SZ"
_SECRETS: list[str] = []


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return now().strftime(ISO)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def ago_iso(hours: float) -> str:
    return (now() - dt.timedelta(hours=hours)).strftime(ISO)


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, ISO).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def days_since(s: str | None) -> float | None:
    d = parse_iso(s)
    return None if d is None else (now() - d).total_seconds() / 86400.0


# --------------------------------------------------------------------------- #
# Logging that never leaks tokens
# --------------------------------------------------------------------------- #
def register_secret(value: str | None) -> None:
    if value and len(value) >= 8 and value not in _SECRETS:
        _SECRETS.append(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


def log(module: str, msg: str) -> None:
    print(f"[{now_iso()}] [{module}] {redact(str(msg))}", flush=True)


# --------------------------------------------------------------------------- #
# Shell helper for user-configured commands (tests, lint, setup)
# --------------------------------------------------------------------------- #
def run_cmd(cmd: str | list[str], cwd: str | None = None, timeout: int = 900,
            shell: bool = True, stdin: str | None = None) -> tuple[int, str]:
    """Run a command, return (exit_code, combined output). Never raises."""
    try:
        r = subprocess.run(cmd, cwd=cwd, shell=shell, capture_output=True,
                           text=True, timeout=timeout, input=stdin)
        return r.returncode, redact((r.stdout or "") + (r.stderr or ""))
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"command not found: {e}"


def tail(text: str, n: int = 25) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-n:])


# --------------------------------------------------------------------------- #
# Prevent two copies of the same module running at once (Linux)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def run_lock(name: str):
    try:
        import fcntl  # not available on Windows
    except ImportError:
        yield True
        return
    fh = open(f".{name}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        with contextlib.suppress(OSError):
            os.remove(f".{name}.lock")
