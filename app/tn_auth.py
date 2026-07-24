"""tn_auth — one-time browser consent that mints the Google token the thailandnow
module uses to CREATE Docs in Drive folders.

Scopes: ``documents`` + ``drive`` (the newsroom token only has ``drive.readonly``,
which can't create files or set permissions). Writes to the path in
``options.google_token_path`` (default ``~/.config/railjack/google_token.json``,
mode 600) in the exact shape ``app.thailandnow._google_token`` reads.

Run once on a machine with a browser (google-auth-oauthlib is pulled ephemerally
so the app's deps stay clean):

    uv run --with google-auth-oauthlib python -m app.tn_auth \\
        --client-id <id> --client-secret <secret>
    # or: export TN_OAUTH_CLIENT_ID / TN_OAUTH_CLIENT_SECRET, then run without flags
    # or: --client-secrets /path/to/desktop_client_secret.json

After consent, ``/api/thailandnow/provision`` and ``/events/create`` work headless
— no browser again unless the refresh token is revoked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit(
        "google-auth-oauthlib not importable — run me with:\n"
        "  uv run --with google-auth-oauthlib python -m app.tn_auth ..."
    )

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
OUT = Path(os.path.expanduser(os.environ.get("TN_TOKEN_PATH", "~/.config/railjack/google_token.json")))


def _client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def main() -> None:
    ap = argparse.ArgumentParser(prog="tn_auth")
    ap.add_argument("--client-id", default=os.environ.get("TN_OAUTH_CLIENT_ID"))
    ap.add_argument("--client-secret", default=os.environ.get("TN_OAUTH_CLIENT_SECRET"))
    ap.add_argument("--client-secrets", help="Desktop OAuth client JSON (overrides id/secret)")
    ap.add_argument("--port", type=int, default=0, help="loopback port (0 = auto)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    if a.client_secrets:
        flow = InstalledAppFlow.from_client_secrets_file(a.client_secrets, SCOPES)
    elif a.client_id and a.client_secret:
        flow = InstalledAppFlow.from_client_config(_client_config(a.client_id, a.client_secret), SCOPES)
    else:
        sys.exit(
            "no OAuth client — pass --client-id/--client-secret (or set "
            "TN_OAUTH_CLIENT_ID/TN_OAUTH_CLIENT_SECRET), or --client-secrets FILE"
        )

    open_browser = not a.no_browser
    if open_browser:
        import webbrowser
        try:
            webbrowser.get()
        except webbrowser.Error:
            open_browser = False
    if not open_browser:
        print(
            "\n>>> No auto-openable browser here. Paste the URL that prints below into a\n"
            ">>> browser ON THIS MACHINE, approve, and this continues (redirect → localhost).\n"
        )

    creds = flow.run_local_server(port=a.port, prompt="consent", open_browser=open_browser)
    if not creds.refresh_token:
        sys.exit("no refresh_token returned — revoke any prior grant and retry with prompt=consent")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token": creds.token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or SCOPES),
    }
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {OUT} (mode 600) — /api/thailandnow/provision + /events/create are now live.")


if __name__ == "__main__":
    main()
