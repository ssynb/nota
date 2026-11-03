"""All settings come from the environment (see .env.example); limits are product parameters, not code."""
import os
from pathlib import Path


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://nota:nota@localhost:55432/nota")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
LLM_TIMEOUT_SECONDS = _float("LLM_TIMEOUT_SECONDS", 120)

# Free plan
FREE_RECORDINGS_PER_DAY = _int("QUOTA_FREE_RECORDINGS_PER_DAY", 10)
FREE_MINUTES_PER_MONTH = _float("QUOTA_FREE_MINUTES_PER_MONTH", 300)
FREE_ASKS_PER_MONTH = _int("QUOTA_FREE_ASKS_PER_MONTH", 30)
# Pro fair use: rolling windows of speech minutes (Omi: 4h / 16h / 20h)
PRO_FAIR_USE_MINUTES = {
    24: _float("QUOTA_PRO_MINUTES_PER_24H", 240),
    72: _float("QUOTA_PRO_MINUTES_PER_3D", 960),
    168: _float("QUOTA_PRO_MINUTES_PER_7D", 1200),
}

JOB_LEASE_SECONDS = _float("JOB_LEASE_SECONDS", 30)
JOB_MAX_ATTEMPTS = _int("JOB_MAX_ATTEMPTS", 3)
JOB_RETRY_BASE_SECONDS = _float("JOB_RETRY_BASE_SECONDS", 10)
RESUME_INTERVAL_SECONDS = _float("RESUME_INTERVAL_SECONDS", 30)
ASK_LEASE_SECONDS = _float("ASK_LEASE_SECONDS", 30)

CONVERSATION_GAP_SECONDS = _float("CONVERSATION_GAP_SECONDS", 120)
OWNER_THRESHOLD = _float("VOICEPRINT_OWNER_THRESHOLD", 0.75)
SPEAKER_CLUSTER_THRESHOLD = _float("SPEAKER_CLUSTER_THRESHOLD", 0.72)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
