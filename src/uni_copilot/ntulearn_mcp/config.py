"""
Settings for ntulearn-mcp.

Fixed values (URLs, paths, politeness limits) are module constants.
Things you may want to change without editing code come from environment
variables, read by load_settings().
"""

import os
from dataclasses import dataclass, field
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

# ---------- Phase 4: course content, files and search ----------

# One file for the content cache and the search index (git-ignored, inside
# .data/). "cache" because deleting it is always safe: everything in it can
# be fetched again.
CONTENT_DB = CACHE_DIR / "content.sqlite"
# Only one indexer run at a time (between programs, like .data/sync.lock).
INDEX_LOCK = DATA_DIR / "index.lock"

# Folder listings and page text count as fresh this long; tools take
# refresh=true to check live sooner. File text has no age limit: it's kept
# until the content item's `modified` time changes (the probe found no size
# or modified time on attachments themselves).
CONTENT_FRESH_S = 6 * 60 * 60

# Size limits, counted while downloading: no file download sends a size up
# front (probe, 29 Oct: no Content-Length), and only 7 of 283 embedded files
# say theirs.
READ_MAX_BYTES = 50 * 1_000_000        # read_file_text and the indexer
DOWNLOAD_MAX_BYTES = 200 * 1_000_000   # download_file
SEND_MAX_BYTES = 50 * 1_000_000        # send_course_file (Telegram's limit for bots)
# How much text one read_file_text call returns at most; the result says
# which pages came back and how to ask for the next ones.
READ_MAX_PAGES = 20
READ_MAX_CHARS = 30_000

# Downloads follow redirects by hand (probe: NTULearn -> NTULearn with a
# signed link -> Blackboard's file server -> the file). Only these hosts are
# allowed: NTULearn itself (with your cookies) and hosts ending in one of
# FILE_HOST_SUFFIXES (without cookies; seen: alt-<id>.blackboard.com).
MAX_DOWNLOAD_REDIRECTS = 5
FILE_HOST_SUFFIXES = (".blackboard.com",)

# The indexer walks every course, so it goes at half the tools' pace: a
# scheduled sync or a Claude session may be using NTULearn at the same time,
# each with its own 1-per-second pacer.
INDEXER_INTERVAL_S = 2.0

# Embeddings (search by meaning). Gemini's free tier, per Google Cloud project
# (your AI Studio screenshot, 29 Oct): 100 requests/min, 30,000 tokens/min,
# 1,000 requests/day. We stay under 80% of the per-minute limits.
GEMINI_API_KEY_NAME = "GEMINI_API_KEY"   # the NAME of the .env line; the key itself is never in code
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 768                  # shorter vectors than the model's maximum: smaller file, nearly as good
EMBED_MAX_REQUESTS_PER_MIN = 80
EMBED_MAX_TOKENS_PER_MIN = 24_000
EMBED_BATCH_SIZE = 50                   # chunks per request (not verified: how a batch counts toward the daily limit)
# Chunks: the pieces of text that get one vector each.
CHUNK_WORDS = 300
CHUNK_OVERLAP_WORDS = 50


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# The project's .env file (git-ignored): Telegram's settings, and the Phase 4
# ones below (UNI_COPILOT_*, GEMINI_API_KEY).
ENV_FILE = PROJECT_ROOT / ".env"


# Reads KEY=VALUE lines from .env, the same rules as notify/telegram.py's
# read_env_file (which can't be imported here: it imports this file):
# blank lines and # comments skipped, one pair of surrounding quotes removed,
# a byte-order mark accepted. A missing or unreadable file is just an empty
# dict, so a broken .env can never stop the sync or the servers.
def _env_file_values(path: Path = ENV_FILE) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


# One Phase 4 setting: a real environment variable wins, then .env, else "".
# (The Phase 1 settings NTULEARN_TERM and NTULEARN_ENABLE_GRADES stay
# environment-only, as before.)
def _setting(name: str, file_values: dict[str, str]) -> str:
    return (os.environ.get(name) or file_values.get(name) or "").strip()


# Where download_file saves: UNI_COPILOT_DOWNLOAD_DIR (environment or .env)
# if set, else your Downloads folder + "uni-copilot" (decision 7). Each
# course gets a subfolder. `~` is allowed.
def _download_dir(file_values: dict[str, str] | None = None) -> Path:
    custom = _setting("UNI_COPILOT_DOWNLOAD_DIR", _env_file_values() if file_values is None else file_values)
    return Path(custom).expanduser() if custom else Path.home() / "Downloads" / "uni-copilot"


@dataclass(frozen=True)
class Settings:
    # Force a term name like "26S1". None = pick the newest YYS1/YYS2 term.
    term_override: str | None
    # Grades are opt-in. When False the grades tool is not registered at all.
    enable_grades: bool
    # Phase 4. Defaults so code that builds Settings without them keeps working.
    # Which embedding service search uses: "gemini", or "off" for keyword-only
    # (from UNI_COPILOT_EMBEDDINGS; anything else also means keyword-only).
    embeddings: str = "gemini"
    # The folder download_file saves into.
    download_dir: Path = field(default_factory=_download_dir)


def load_settings() -> Settings:
    """Read settings from environment variables (set in .mcp.json or your shell);
    the Phase 4 ones also from the project's .env file."""
    term = os.environ.get("NTULEARN_TERM", "").strip()
    file_values = _env_file_values()
    # Not checked here on purpose: the sync and both servers call this at start,
    # and a typo in a Phase 4 setting must never stop them. Search (step 16)
    # treats an unknown value as "keyword-only" and says why.
    embeddings = _setting("UNI_COPILOT_EMBEDDINGS", file_values).lower() or "gemini"
    return Settings(
        term_override=term or None,
        enable_grades=_env_flag("NTULEARN_ENABLE_GRADES"),
        embeddings=embeddings,
        download_dir=_download_dir(file_values),
    )
