"""Tunables read from the environment.

Production gets them from the systemd unit's Environment= lines; local dev from
.env, which is loaded here so values are visible to modules that read their
configuration at import time (live_delays loads it too, idempotently, for the
case where it is imported first). Every knob has a safe default, so the app
boots with nothing set.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Local dev convenience; in production systemd passes the values in."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if key and not key.startswith("#") and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


_load_dotenv()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        if raw:
            log.warning("ignoring non-integer %s=%r; using default %d", name, raw, default)
        return default
