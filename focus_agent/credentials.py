"""
credentials.py — Fetches Focus ERP login from Firestore (cloud-only).

The dashboard passes the logged-in user's Firebase ID token via the
AGENT_AUTH_TOKEN environment variable. This module uses that token to
read the credentials document from Firestore. No local fallback:
if the user is disabled or the token is invalid, the agent cannot run.

Firestore structure:
  Collection: app_config
  Document:   erp_credentials
  Fields:     username (string), password (string)
"""
import json
import os
import sys
import urllib.request
import urllib.error

FIREBASE_PROJECT_ID = "sriambikasagents"

FIRESTORE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents/app_config/erp_credentials"
)


def get_credentials(filepath: str = None) -> tuple:
    """Fetch ERP credentials from Firestore using the user's auth token.
    The filepath argument is kept for call-compatibility but ignored."""
    token = os.environ.get("AGENT_AUTH_TOKEN", "").strip()
    if not token:
        print("❌ No auth token provided (AGENT_AUTH_TOKEN).")
        print("   The agent must be run from the dashboard by a signed-in user.")
        sys.exit(1)

    req = urllib.request.Request(
        FIRESTORE_URL,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("❌ Access denied fetching credentials.")
            print("   Your account may be disabled or the session expired.")
        elif e.code == 404:
            print("❌ Credentials document not found in Firestore.")
            print("   Expected: app_config/erp_credentials with username+password fields.")
        else:
            print(f"❌ Firestore error {e.code}: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Could not reach Firestore: {e}")
        print("   Check the internet connection.")
        sys.exit(1)

    fields = data.get("fields", {})
    username = fields.get("username", {}).get("stringValue", "").strip()
    password = fields.get("password", {}).get("stringValue", "").strip()

    if not username or not password:
        print("❌ Credentials document is missing username or password fields.")
        sys.exit(1)

    print(f"✅ Credentials loaded from cloud for: {username}")
    return username, password
