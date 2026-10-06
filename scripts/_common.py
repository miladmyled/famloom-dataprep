"""Shared helpers for the dev scripts: repo path, .env loading, target printing, safety checks."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

# Windows consoles default to cp1252; event titles contain emoji
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PRODUCTION_DB_NAMES = {"x3db"}
# Kafka bootstrap values a dev script may publish to (local Docker Kafka by default).
ALLOWED_DEV_KAFKA = {s.strip() for s in os.getenv("DEV_KAFKA_ALLOWED", "localhost:9092,127.0.0.1:9092").split(",") if s.strip()}


def db_target() -> str:
    return f"{os.getenv('DB_HOST')} / {os.getenv('DB_NAME')} (user {os.getenv('DB_USER')})"


def require_dev_db() -> None:
    """Print the DB target and exit if it is the production database."""
    print(f"[TARGET] database: {db_target()}")
    if os.getenv("DB_NAME") in PRODUCTION_DB_NAMES:
        sys.exit("[REFUSED] This script only runs against the dev database.")


def require_dev_kafka() -> None:
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
    print(f"[TARGET] kafka: {bootstrap}")
    if bootstrap not in ALLOWED_DEV_KAFKA:
        sys.exit(f"[REFUSED] KAFKA_BOOTSTRAP_SERVERS must be one of {sorted(ALLOWED_DEV_KAFKA)} for dev publishing.")


def reports_dir() -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    return REPORTS_DIR
