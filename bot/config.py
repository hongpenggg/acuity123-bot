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

# Where the repo is, for the GitHub links we hand out when a note PDF cannot be
# sent as a file (bot/resources.py). Point these at your fork.
REPO_SLUG = os.getenv("REPO_SLUG", "hongpenggg/acuity123-bot")
REPO_REF = os.getenv("REPO_REF", "main")

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

# Split so the society can edit either line without touching the other. The
# welcome renders CREDIT and DISCLAIMER as one italic block, with the society
# line and the disclaimer joined on the last line.
ACUITY_CREDIT = ("By the Acuity Team: Zhong Han (Vice-Pres, LKC OphSoc 26/27), "
                 "Hongpeng, Rahul, Jeromy")
SOCIETY_CREDIT = "For LKC OphSoc (@lkceye)."

CREDIT = (
    "👁 LKC OphSoc Bot\n"
    f"{ACUITY_CREDIT}\n"
    f"{SOCIETY_CREDIT}"
)

DISCLAIMER = "Revision only, not clinical advice."
