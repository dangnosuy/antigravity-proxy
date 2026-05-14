from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
DEFAULT_TOKEN_DIR = ROOT_DIR

ANTIGRAVITY_BASE_URL = os.environ.get(
    "AG_BASE_URL",
    "https://daily-cloudcode-pa.googleapis.com",
).rstrip("/")

OAUTH_TOKEN_URL = os.environ.get(
    "AG_OAUTH_TOKEN_URL",
    "https://oauth2.googleapis.com/token",
)

OAUTH_CLIENT_ID = os.environ.get("AG_OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("AG_OAUTH_CLIENT_SECRET", "")

TOKEN_FILE = Path(os.environ.get("AG_TOKEN_FILE", DEFAULT_TOKEN_DIR / "token.txt"))
REFRESH_TOKEN_FILE = Path(os.environ.get("AG_REFRESH_TOKEN_FILE", DEFAULT_TOKEN_DIR / "refresh_token.txt"))

DEFAULT_MODEL = os.environ.get("AG_DEFAULT_MODEL", "gemini-3-flash")
DEFAULT_MAX_OUTPUT_TOKENS = int(os.environ.get("AG_MAX_OUTPUT_TOKENS", "8192"))
DEFAULT_TEMPERATURE = float(os.environ.get("AG_TEMPERATURE", "1.0"))
ANTIGRAVITY_VERSION = os.environ.get("AG_ANTIGRAVITY_VERSION", "1.107.0")

PORT = int(os.environ.get("PORT", "5005"))
LOG_PAYLOADS = os.environ.get("AG_PROXY_LOG_PAYLOADS", "0").lower() in {"1", "true", "yes"}

HEADERS_BASE = {
    "accept": "*/*",
    "content-type": "application/json",
    "user-agent": f"antigravity/{ANTIGRAVITY_VERSION} linux/amd64 google-api-nodejs-client/10.3.0",
    "x-goog-api-client": "gl-node/22.21.1",
}
