#!/usr/bin/env python3
"""One-time OAuth setup script for Google Calendar integration.

Run this script locally to authorize Dharvis to access your Google Calendar.
It will open a browser window for authentication and save the token.

Usage:
    python scripts/setup_gcal_auth.py

Prerequisites:
    1. Create a project in Google Cloud Console
    2. Enable the Google Calendar API
    3. Create OAuth 2.0 credentials (Desktop application)
    4. Download credentials.json and place in project root

After running:
    - token.json will be created with your refresh token
    - Copy this file to your deployment server if needed
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.calendar_service import run_oauth_flow
from src.config import config


def main():
    print("=" * 50)
    print("Dharvis - Google Calendar OAuth Setup")
    print("=" * 50)
    print()

    credentials_path = config.GOOGLE_CALENDAR_CREDENTIALS_PATH
    token_path = config.GOOGLE_CALENDAR_TOKEN_PATH

    print(f"Looking for credentials at: {credentials_path}")
    print(f"Token will be saved to: {token_path}")
    print()

    if token_path.exists():
        response = input("Token already exists. Overwrite? (y/N): ")
        if response.lower() != "y":
            print("Aborted.")
            return

    print("Starting OAuth flow...")
    print("A browser window will open for authentication.")
    print()

    success = run_oauth_flow(credentials_path, token_path)

    if success:
        print()
        print("Setup complete!")
        print("You can now run Dharvis with Google Calendar integration.")
    else:
        print()
        print("Setup failed. Please check the error messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
