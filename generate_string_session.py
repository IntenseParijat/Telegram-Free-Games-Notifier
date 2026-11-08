from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 123456
API_HASH = "HASH"

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print(f"\\nYour TELEGRAM_SESSION value is:\\n")
    print(client.session.save())
    input("(Enter to exit)")
