"""User-editable settings for the FreeGameRadar Telegram notifier.

Edit this file to control first-run behaviour, schedule, message footer, and fields.
Keep all credentials in GitHub Actions Secrets, never in this file.
"""

# First run:
# True  = post all currently free listings on the first run.
# False = silently save the current catalogue as the baseline; post future additions only.
SEND_EXISTING_ON_FIRST_RUN = True

# How often to run the notifier (1 to 24 hours).
# GitHub Actions uses this value to sync its schedule on manual workflow dispatch.
RUN_INTERVAL_HOURS = 6

# Custom Telegram footer. Use "" to hide either line.
TELEGRAM_CHANNEL_TAG = "@IntenseGaming"
TELEGRAM_FOOTER = "Powered By [Intense Network](t.me/IntenseNetwork)"

# Caption fields. Only description, image and platform are rendered.
# The caption always follows: title, description, platform, claim link,
# offer expiry, then footer. Category, discount and price metadata stay hidden.
DISPLAY_FIELDS = {
    "description": True,
    "image": True,
    "platform": True,
    "category": False,
    "discount_percent": False,
    "original_price": False,
    "final_price": False,
    "discount_price": False,
    "currency": False,
    "start_date": False,
    "end_date": False,
    "is_free": False,
    "game_id": False,
    "id": False,
    "created_at": False,
    "updated_at": False,
}
