"""File-backed logging with per-subject subfolders and 7-day auto-cleanup.

The job runner writes its diagnostics to `logs/<subject>/<date>.log` rather than
journald, so a contract failure leaves a durable, greppable record on disk
independent of journald retention. Old files are pruned: anything older than
`RETENTION_DAYS` (7) is removed each time a line is appended.

Layout:
    logs/
      jobs/          2026-08-31.log      job lifecycle + failure diagnostics
      server/        <date>.log          (reserved) startup / general events

No secrets are ever written. The diagnostics helper (server/jobs.py) keeps
service-account credentials and API keys out of what it logs.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

RETENTION_DAYS = 7
LOG_ROOT = Path(__file__).resolve().parent.parent / "logs"

_configured = set()
_handlers: dict[str, logging.Handler] = {}
_stdout_configured = False


def _stale_before() -> float:
    return time.time() - RETENTION_DAYS * 86400


def cleanup(subject: Optional[str] = None) -> None:
    """Remove log files older than RETENTION_DAYS under LOG_ROOT (optionally
    scoped to one subject's subfolder)."""
    base = LOG_ROOT / subject if subject else LOG_ROOT
    if not base.exists():
        return
    cutoff = _stale_before()
    for path in base.rglob("*"):
        if path.is_file():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def get_logger(name: str, subject: str) -> logging.Logger:
    """Return a logger that writes to `logs/<subject>/<date>.log` with a stable
    name. The handler is created once per (name, subject) and reused."""
    global _stdout_configured
    if subject not in _configured:
        subdir = LOG_ROOT / subject
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{time.strftime('%Y-%m-%d')}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        _configured.add(subject)
        _handlers[subject] = handler
        # Prune old files for this subject whenever we open a fresh stream.
        cleanup(subject)

    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    # Re-attach the handler if this logger lost it (reimports / reconfig).
    handler = _handlers.get(subject)
    if handler is not None and handler not in log.handlers:
        log.addHandler(handler)
    if not _stdout_configured:
        # Also mirror to stderr so `journalctl`, `docker logs`, or a foreground
        # run still sees it — but the authoritative copy is the file.
        out = logging.StreamHandler()
        out.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(out)
        logging.getLogger().setLevel(logging.INFO)
        _stdout_configured = True
    return log
