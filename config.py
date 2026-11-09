"""Environment-driven configuration."""
import os

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

DATA_DIR = "/data" if os.path.isdir("/data") else os.path.join(os.getcwd(), "chrome-profile")

WA_URL = "https://web.whatsapp.com"

REPORTS_PER_RUN = int(os.environ.get("REPORTS_PER_RUN", "30"))
MAX_REPORTS = int(os.environ.get("MAX_REPORTS", "200"))
DELAY = float(os.environ.get("DELAY_BETWEEN_REPORTS", "4"))

PAGE_LOAD_TIMEOUT = int(os.environ.get("PAGE_LOAD_TIMEOUT", "90"))
ACTION_TIMEOUT = int(os.environ.get("ACTION_TIMEOUT", "30"))

PAIR_TIMEOUT = int(os.environ.get("PAIR_TIMEOUT", "120"))
QR_ATTEMPTS = int(os.environ.get("QR_ATTEMPTS", "6"))
QR_WAIT = int(os.environ.get("QR_WAIT", "25"))

DEBUG_SHOTS = os.environ.get("DEBUG_SHOTS", "1") == "1"
MAX_QUEUE = 3

CHROME_LOW_MEMORY = os.environ.get("CHROME_LOW_MEMORY", "1") == "1"
KEEPALIVE_SECONDS = int(os.environ.get("KEEPALIVE_SECONDS", "240"))

MYSQL_URL = os.environ.get("MYSQL_URL", "").strip()
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "2"))
DB_LOG_REPORTS = os.environ.get("DB_LOG_REPORTS", "1") == "1"
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "0"))
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)