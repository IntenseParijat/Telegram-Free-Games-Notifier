"""Generate a Telethon StringSession for GitHub Actions.

Run locally only. Never commit or share the generated session string.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 123456  # Replace with your Telegram API ID
API_HASH = "YOUR_API_HASH"  # Replace with your Telegram API hash

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nYour TELEGRAM_SESSION value is:\n")
    print(client.session.save())
    input("\nPress Enter to exit...")
