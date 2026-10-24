"""
Settings for ntulearn-mcp.

Fixed values (URLs, paths, politeness limits) are module constants.
Things you may want to change without editing code come from environment
variables, read by load_settings().
"""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# Always the full base URL: relative fetches hit a CDN host and 404.
BASE_URL = "https://ntulearn.ntu.edu.sg"

# Project root = the folder with pyproject.toml.
# This file is src/uni_copilot/ntulearn_mcp/config.py, so the root is 3 levels up.
# UNI_COPILOT_HOME overrides it if the package is ever installed elsewhere.
PROJECT_ROOT = Path(
    os.environ.get("UNI_COPILOT_HOME") or Path(__file__).resolve().parents[3]
)

# Browser profile with your login cookies (git-ignored, never commit).
PROFILE_DIR = PROJECT_ROOT / ".auth" / "profile"
# Local data and caches (git-ignored).
DATA_DIR = PROJECT_ROOT / ".data"
CACHE_DIR = DATA_DIR / "cache"

# NTULearn returns UTC; we show Singapore time.
SGT = ZoneInfo("Asia/Singapore")

# Polite access (see CLAUDE.md).
REQUEST_INTERVAL_S = 1.0   # at least this long between two requests
MAX_CONCURRENT = 2         # at most this many requests in flight
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3            # for 429 and 5xx, with growing waits

# Calendar API rejects ranges longer than 16 weeks (17 weeks -> 400, verified).
CALENDAR_MAX_WEEKS = 16

# How long login() waits for you to finish signing in.
LOGIN_TIMEOUT_S = 300
# How long a silent (hidden-browser) sign-in may take before we give up on it.
SILENT_LOGIN_TIMEOUT_S = 30


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Force a term name like "26S1". None = pick the newest YYS1/YYS2 term.
    term_override: str | None
    # Grades are opt-in. When False the grades tool is not registered at all.
    enable_grades: bool


def load_settings() -> Settings:
    """Read settings from environment variables (set in .mcp.json or your shell)."""
    term = os.environ.get("NTULEARN_TERM", "").strip()
    return Settings(
        term_override=term or None,
        enable_grades=_env_flag("NTULEARN_ENABLE_GRADES"),
    )
