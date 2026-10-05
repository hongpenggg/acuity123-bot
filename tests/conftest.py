"""Test bootstrap: put the repo on sys.path and give config.py the env it needs."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# bot/config.py reads these at import time, so they must exist before any import.
os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost:5432/studybot_test")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("ADMIN_IDS", "42")


@pytest.fixture(autouse=True)
def _quiet_logs(caplog):
    caplog.set_level("CRITICAL")
