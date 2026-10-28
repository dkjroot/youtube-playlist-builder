"""Desktop Google OAuth shared by setup and unattended updates.

The CLI requests read or playlist-management scopes, refreshes existing credentials
where possible, and opens browser consent only for interactive commands. Token
replacement is atomic and private; no model service receives these credentials.
"""

import os
from pathlib import Path
import tempfile

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from oauthlib.oauth2 import OAuth2Error

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
WRITE_SCOPES = SCOPES + ["https://www.googleapis.com/auth/youtube"]


def save_token(path: Path, credentials: Credentials) -> None:
    """Replace the token atomically; its temporary file is owner-only too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".oauth-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authenticate(client_secrets: Path, token: Path, *, write_access: bool = False,
                 interactive: bool = True) -> Credentials:
    """Load or refresh OAuth credentials, requesting browser consent only when allowed.

    Background updates pass interactive=False so a missing token produces an
    actionable error instead of hanging a scheduled job in a browser. Existing
    credentials stay on disk until replacement consent succeeds.
    """
    scopes = WRITE_SCOPES if write_access else SCOPES
    credentials = None
    if token.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token))
        except (ValueError, KeyError) as error:
            raise ValueError(
                f"Cannot read the saved sign-in at {token}. Remove it and sign in again."
            ) from error
        if not credentials.has_scopes(scopes):
            # Keep the existing token on disk until the new consent succeeds.
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as error:
            raise ValueError(
                f"Google could not renew your sign-in. Remove {token} and sign in again."
            ) from error

    if not credentials or not credentials.valid:
        if not interactive:
            command = 'yt-playlists auth --write-access' if write_access else 'yt-playlists auth'
            raise ValueError(f'Sign-in is required. Run {command} in a terminal first; scheduled updates never open a browser.')
        if not client_secrets.is_file():
            raise ValueError(
                f"Missing OAuth client file: {client_secrets}. Follow the Google setup in README.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets), scopes, autogenerate_code_verifier=True
        )
        try:
            credentials = flow.run_local_server(
                host="localhost", port=0, timeout_seconds=180,
                authorization_prompt_message="Opening Google sign-in in your browser…",
                success_message="Return to the terminal to check whether sign-in succeeded.",
                prompt="consent",
            )
        except WSGITimeoutError as error:
            raise ValueError("Sign-in timed out. Run the command again to retry.") from error
        except OAuth2Error as error:
            raise ValueError("Google sign-in was declined or failed. Run the command again to retry.") from error
    if not credentials.has_scopes(scopes):
        raise ValueError("Google did not grant the requested YouTube permissions. Please sign in again.")
    save_token(token, credentials)
    return credentials

