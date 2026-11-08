"""User-editable settings for the FreeGameRadar Telegram notifier.

Edit this file to control first-run behaviour, schedule, message footer, and fields.
Keep all credentials in GitHub Actions Secrets, never in this file.
"""

# First run:
# True  = post all currently free listings on the first run.
# False = silently save the current catalogue as the baseline; post future additions only.
SEND_EXISTING_ON_FIRST_RUN = False

# How often to run the notifier (1 to 24 hours).
# GitHub Actions uses this value to sync its schedule on manual workflow dispatch.
RUN_INTERVAL_HOURS = 6

# Custom Telegram footer. Use "" to hide either line.
TELEGRAM_CHANNEL_TAG = "@IntenseGaming"
TELEGRAM_FOOTER = "Powered By Intense Network"

# API response fields. All are enabled by default.
# Set any option to False to hide that field.
DISPLAY_FIELDS = {
    "description": True,
    "image": True,
    "platform": True,
    "category": True,
    "discount_percent": True,
    "original_price": True,
    "final_price": True,
    "discount_price": True,
    "currency": True,
    "start_date": True,
    "end_date": True,
    "is_free": True,
    "game_id": True,
    "id": True,
    "created_at": True,
    "updated_at": True,
}
