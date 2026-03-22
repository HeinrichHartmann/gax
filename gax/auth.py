"""Authentication management for gax"""

import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

# Default config directory
CONFIG_DIR = Path.home() / ".config" / "gax"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

# Scopes needed for Google Sheets, Docs, and Gmail
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Default OAuth client credentials (public client for CLI apps)
# Users can replace with their own in ~/.config/gax/credentials.json
DEFAULT_CLIENT_CONFIG = {
    "installed": {
        "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
        "client_secret": "YOUR_CLIENT_SECRET",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost", "urn:ietf:wg:oauth:2.0:oob"],
    }
}


def get_config_dir() -> Path:
    """Get or create config directory."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def get_credentials_path() -> Path:
    """Get path to OAuth client credentials."""
    return CREDENTIALS_FILE


def get_token_path() -> Path:
    """Get path to stored token."""
    return TOKEN_FILE


def credentials_exist() -> bool:
    """Check if OAuth client credentials are configured."""
    return CREDENTIALS_FILE.exists()


def token_exists() -> bool:
    """Check if we have a stored token."""
    return TOKEN_FILE.exists()


def get_status() -> dict:
    """Get current auth status."""
    config_dir = get_config_dir()
    return {
        "config_dir": str(config_dir),
        "credentials_path": str(CREDENTIALS_FILE),
        "credentials_exists": credentials_exist(),
        "token_path": str(TOKEN_FILE),
        "token_exists": token_exists(),
        "authenticated": is_authenticated(),
    }


def is_authenticated() -> bool:
    """Check if we have valid credentials."""
    if not token_exists():
        return False
    try:
        creds = load_credentials()
        return creds is not None and creds.valid
    except Exception:
        return False


def load_credentials() -> Credentials | None:
    """Load stored credentials."""
    if not TOKEN_FILE.exists():
        return None

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    return Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )


def save_credentials(creds: Credentials) -> None:
    """Save credentials to token file."""
    get_config_dir()
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


def login() -> Credentials:
    """Run OAuth flow and store credentials."""
    if not credentials_exist():
        raise FileNotFoundError(
            f"OAuth credentials not found at {CREDENTIALS_FILE}\n"
            "Please download OAuth client credentials from Google Cloud Console:\n"
            "  1. Go to https://console.cloud.google.com/apis/credentials\n"
            "  2. Create OAuth 2.0 Client ID (Desktop app)\n"
            "  3. Download JSON and save to: {CREDENTIALS_FILE}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
    )

    # Run local server for OAuth callback
    creds = flow.run_local_server(port=0)

    # Save for future use
    save_credentials(creds)

    return creds


def logout() -> bool:
    """Remove stored token."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        return True
    return False


def get_authenticated_credentials() -> Credentials:
    """Get credentials, running login flow if needed."""
    creds = load_credentials()

    if creds is None:
        creds = login()
    elif not creds.valid:
        if creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
            save_credentials(creds)
        else:
            creds = login()

    return creds
