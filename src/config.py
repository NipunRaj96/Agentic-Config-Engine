"""
JPERL Configuration Generator
Central configuration and constants.
"""

import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Internal Infrastructure ────────────────────────────────────────────────────
ROBOT_CHECKER_URL: str = os.environ.get("ROBOT_CHECKER_URL", "")

# ── LLM Provider Selector ──────────────────────────────────────────────────────
# Options: "astra" | "naukri" | "gemini" | "groq"
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "astra").lower().strip()

# ── Astra Gen-AI Real-Time Execution Service ────────────────────────────────────
ASTRA_BASE_URL: str      = os.environ.get("ASTRA_BASE_URL", os.environ.get("GENAI_BASE_URL", "")).rstrip("/")
ASTRA_SECRET_KEY: str    = os.environ.get("ASTRA_SECRET_KEY", os.environ.get("SECRET_KEY", "")).strip()
ASTRA_TEMPLATE_CODE: str = os.environ.get("ASTRA_TEMPLATE_CODE", os.environ.get("TEMPLATE_CODE", "")).strip()
ASTRA_APP_ID: str        = os.environ.get("ASTRA_APP_ID", os.environ.get("APP_ID", "")).strip()
ASTRA_SYSTEM_ID: str     = os.environ.get("ASTRA_SYSTEM_ID", os.environ.get("SYSTEM_ID", "")).strip()

# ── Gemini API ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL: str   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# ── Groq API (fallback when Gemini is rate-limited) ────────────────────────────
GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL: str   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL: str = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# ── Naukri Gen-AI API Gateway ──────────────────────────────────────────────────
NAUKRI_TOKEN: str = os.environ.get("NAUKRI_TOKEN", "")
NAUKRI_EMAIL: str = os.environ.get("NAUKRI_EMAIL", "")
NAUKRI_APP_ID: str = os.environ.get("NAUKRI_APP_ID", "")
NAUKRI_SYSTEM_ID: str = os.environ.get("NAUKRI_SYSTEM_ID", "")
NAUKRI_API_URL: str = os.environ.get("NAUKRI_API_URL", "")
NAUKRI_MODEL: str = os.environ.get("NAUKRI_MODEL", "")

# ── Playwright ─────────────────────────────────────────────────────────────────
PLAYWRIGHT_TIMEOUT_MS: int = 30_000
PLAYWRIGHT_WAIT_MS: int = 5_000        # time to let XHR/fetch requests fire

# ── Heuristics ─────────────────────────────────────────────────────────────────
# Maximum number of API candidates to send to the LLM
MAX_LLM_CANDIDATES: int = 3
HEURISTIC_TOP_N: int    = MAX_LLM_CANDIDATES   # alias used by HeuristicRanker

# ── Knowledge Base ─────────────────────────────────────────────────────────────
# KB entries older than this many days get a staleness warning in output
KB_STALENESS_DAYS: int = 90

# ── JPERL Defaults ─────────────────────────────────────────────────────────────
JPERL_DEFAULTS: dict = {
    "CanProcceed": 1,
    "Cookie": "YES",
    "CurrentJobNum": 1,
    "CurrentPageNum": 0,
    "DELETE_UNLISTED": "y",
    "ISCRAWL_FULL": 0,
    "IS_SITE_ID_DERIVED": 0,
    "JOBSCOUNT": 0,
    "MAXJBSPPAGE": 10,
    "MAXPAGESPARSE": "1",
    "MOVE_TO_JD": 0,
    "SAVE_JD_COOKIE": "Yes",
    "SQLDATE": "%Y-%m-%d",
    "StartPageNum": 0,
    "TERMRGX": "",
    "USER_AGENT": "Firefox/5.0",
    "JDVARS": {
        "FAREA":    {"type": "SUB", "callable": "Classifier_FAREA"},
        "ROLE":     {"type": "SUB", "callable": "Classifier_ROLE"},
        "INDUSTRY": {"type": "SUB", "callable": "Classifier_INDUSTRY"},
    },
}

# ── Static asset MIME types to discard during interception ─────────────────────
IGNORED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media", "websocket", "manifest"}

# ── URL fragments that almost certainly are NOT job-listing APIs ───────────────
IGNORED_URL_PATTERNS = [
    "google-analytics", "analytics", "hotjar", "segment", "mixpanel",
    "facebook", "twitter", "linkedin", "bing", "criteo", "doubleclick",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
    ".css", ".js.map", "mapbox", "openstreetmap", "leaflet", "maps.googleapis",
]

# ── Keywords that boost an endpoint's heuristic score ─────────────────────────
JOB_URL_KEYWORDS = ["job", "career", "opening", "position", "positions", "requisition", "vacancy"]
JOB_FIELD_KEYWORDS = ["title", "location", "jobid", "id", "description", "posting"]

# ── SQLite Config Cache ────────────────────────────────────────────────────────
import os
CACHE_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "knowledge_base",
    "config_cache.db"
)
CACHE_TTL_ATS_DAYS = 30
CACHE_TTL_CUSTOM_DAYS = 7

# ── Telemetry & Confidence Gates ──────────────────────────────────────────────
MIN_CONFIDENCE = 0.65
TELEMETRY_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "knowledge_base",
    "pipeline.db"
)
