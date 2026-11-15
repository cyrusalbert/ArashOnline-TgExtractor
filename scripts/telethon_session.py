#!/usr/bin/env python3
"""One-off helper: create a Telethon StringSession for scraper.mode: telethon.

Usage
    pip install telethon
    python scripts/telethon_session.py

1. Get api_id / api_hash at https://my.telegram.org  ->  "API development tools".
2. Run this script, enter them, then your phone number and the login code.
3. Store the three values as GitHub secrets (Settings -> Secrets and variables -> Actions):
       TELEGRAM_API_ID   TELEGRAM_API_HASH   TELEGRAM_SESSION
   or, for local runs, put them in a git-ignored .env file in the repo root.

WARNING: the printed session string logs in as YOUR Telegram account.
Treat it like a password: never commit it, never paste it into a chat or issue.
Consider using a spare account dedicated to this job.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API ID: ").strip())
api_hash = input("API hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:  # prompts for phone + code
    print("\nTELEGRAM_SESSION (keep secret):\n")
    print(client.session.save())
