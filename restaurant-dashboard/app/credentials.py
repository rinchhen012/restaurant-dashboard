"""Encrypted storage of platform login credentials (local only) + CLI.

Credentials are stored as a single JSON blob encrypted with the local
Fernet key in data/secret.key. Used by the capture tool's --auto mode.

CLI usage:
    python -m app.credentials ubereats --user <email> --password <pwd> [--pin <manager-pin>]
    python -m app.credentials demaecan --user <email> --password <pwd>
    python -m app.credentials <platform> --show
    python -m app.credentials <platform> --delete
"""
import argparse
import json

from app import config

CRED_PATH = config.DATA_DIR / "credentials.json"


def get_credentials(platform: str) -> dict | None:
    if not CRED_PATH.exists():
        return None
    try:
        blob = json.loads(config.decrypt_text(CRED_PATH.read_text()))
    except Exception:
        return None
    return blob.get(platform)


def set_credentials(platform: str, creds: dict):
    blob = {}
    if CRED_PATH.exists():
        try:
            blob = json.loads(config.decrypt_text(CRED_PATH.read_text()))
        except Exception:
            blob = {}
    blob[platform] = creds
    config.save_json(CRED_PATH, config.encrypt_text(json.dumps(blob)))
    print(f"Stored encrypted credentials for {platform}")


def delete_credentials(platform: str):
    if not CRED_PATH.exists():
        print(f"No credentials file; nothing to delete for {platform}")
        return
    blob = json.loads(config.decrypt_text(CRED_PATH.read_text()))
    blob.pop(platform, None)
    config.save_json(CRED_PATH, config.encrypt_text(json.dumps(blob)))
    print(f"Deleted credentials for {platform}")


def main():
    parser = argparse.ArgumentParser(description="Encrypted platform credentials")
    parser.add_argument("platform", help="platform or account key (e.g. demaecan, demaecan-nerima, ubereats)")
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--pin", help="Uber Eats manager PIN")
    parser.add_argument("--show", action="store_true", help="show stored username (masked)")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    if args.delete:
        delete_credentials(args.platform)
        return
    if args.show:
        c = get_credentials(args.platform)
        if not c:
            print("No stored credentials.")
        else:
            user = c.get("username", "")
            masked = user[:2] + "*" * max(0, len(user) - 2)
            print(f"{args.platform}: user={masked} pin={'*' * len(str(c.get('pin', '')))}")
        return
    if not args.user or not args.password:
        parser.error("--user and --password are required (or --show / --delete)")
    creds = {"username": args.user, "password": args.password}
    if args.pin:
        creds["pin"] = args.pin
    set_credentials(args.platform, creds)


if __name__ == "__main__":
    main()
