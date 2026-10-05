"""Environment-driven configuration. Nothing here should need editing to deploy."""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
# Optional. Every question in the preclinical bank ships with a written
# explanation, so the bot runs perfectly with the LLM switched off — no key, no
# spend. Set both to enable generation for questions that have none.
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_ENABLED = bool(LLM_API_KEY and LLM_MODEL)

# Display / scheduling timezone. The DB stores timestamptz, so this only affects
# how times are shown to users and when the cron jobs fire.
TZ = os.getenv("BOT_TZ", "Asia/Singapore")

# Weakness detection: below this many answered questions the picker stays uniform.
MIN_ATTEMPTS_FOR_ADAPTIVE = int(os.getenv("MIN_ATTEMPTS_FOR_ADAPTIVE", "10"))
# Error-rate floor so a topic the user has mastered still resurfaces occasionally
# ("while still being holistic and covering different topics").
WEIGHT_FLOOR = float(os.getenv("WEIGHT_FLOOR", "0.15"))

# The audience tier a student picks with /level. Must stay in step with the
# CHECK constraints in schema.sql.
LEVELS: dict[str, str] = {
    "preclin": "Pre-Clinical",
    "clin": "Clinical",
    "postmbbs": "Post-MBBS",
}
DEFAULT_LEVEL = "preclin"
LEVEL_EMOJI: dict[str, str] = {
    "preclin": "📖",
    "clin": "🩺",
    "postmbbs": "🎓",
}

# Shown in italics at the foot of /start and /help.
CREDIT = (
    "Made by Zhong Han (Vice-Chairperson, LKC OphSoc 26/27) and the Acuity Team: "
    "Zhong Han, Rahul, Hongpeng and Jeromy."
)

DISCLAIMER = "For revision only, not clinical advice."
