import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
CAPTURES_DIR = DATA_DIR / "captures"
KEY_FILE = DATA_DIR / "secret.key"
DB_PATH = DATA_DIR / "dashboard.db"

for d in (DATA_DIR, SESSIONS_DIR, CAPTURES_DIR):
    d.mkdir(parents=True, exist_ok=True)

PLATFORMS = {
    "ubereats": {
        "label": "Uber Eats",
        "login_url": "https://merchants.ubereats.com/manager/",
        "home_url": "https://merchants.ubereats.com/manager/",
    },
    "demaecan": {
        "label": "Demae-Can",
        "login_url": "https://partner.demae-can.com/merchant-admin/login",
        "home_url": "https://partner.demae-can.com/merchant-admin/",
    },
}

POLL_INTERVAL_SECONDS = 12

# Uber Eats automation is disabled for now (risk of account flagging).
# Re-enable with: set to True, re-capture the session, restart.
ENABLE_UBER_POLLING = False


def _load_or_create_key() -> bytes:
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    os.chmod(KEY_FILE, 0o600)
    return key


_cipher = Fernet(_load_or_create_key())


def encrypt_text(text: str) -> str:
    return _cipher.encrypt(text.encode()).decode()


def decrypt_text(token: str) -> str:
    return _cipher.decrypt(token.encode()).decode()


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(path, 0o600)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())
