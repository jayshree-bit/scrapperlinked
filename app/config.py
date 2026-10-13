import os
from dotenv import load_dotenv

load_dotenv()


def get_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "prospect_verification")
MONGODB_INPUT_COLLECTION = os.getenv("MONGODB_INPUT_COLLECTION", "prospects")
MONGODB_OUTPUT_COLLECTION = os.getenv("MONGODB_OUTPUT_COLLECTION", "linkedin_prospect_profiles")

BATCH_SIZE = get_int("BATCH_SIZE", 50)
MAX_RETRIES = get_int("MAX_RETRIES", 3)
RETRY_BASE_SECONDS = get_int("RETRY_BASE_SECONDS", 2)
RETRY_MAX_SECONDS = get_int("RETRY_MAX_SECONDS", 60)
REQUEST_TIMEOUT_SECONDS = get_int("REQUEST_TIMEOUT_SECONDS", 25)
MAX_CONSECUTIVE_ERRORS = get_int("MAX_CONSECUTIVE_ERRORS", 5)

CHOCODATA_API_KEY = os.getenv("CHOCODATA_API_KEY", "")
CHOCODATA_BASE_URL = os.getenv("CHOCODATA_BASE_URL", "https://api.chocodata.com/api/v1")
CHOCODATA_PROFILE_PATH = os.getenv("CHOCODATA_PROFILE_PATH", "/linkedin/profile")
_base = CHOCODATA_BASE_URL.rstrip("/")
_path = "/" + CHOCODATA_PROFILE_PATH.strip("/")
CHOCODATA_PROFILE_URL = _base if _base.endswith(_path) else _base + _path
REQUEST_DELAY_SECONDS = get_int("REQUEST_DELAY_SECONDS", 2)

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip().strip("'\"")
SERPER_URL = os.getenv("SERPER_URL", "https://google.serper.dev/search")
# chocodata_then_serper | serper_only | chocodata_only
PROVIDER_MODE = os.getenv("PROVIDER_MODE", "chocodata_then_serper")

INPUT_FILE = os.getenv("INPUT_FILE", "sample_prospects.csv")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "verification_report.xlsx")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
BROWSER_PROFILE_DIR = os.getenv("BROWSER_PROFILE_DIR", ".browser_profile")