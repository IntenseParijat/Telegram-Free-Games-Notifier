"""User-editable settings for the FreeGameRadar Telegram notifier.

Edit this file to control notifications, message content, and schedule.
Secrets must remain in GitHub Actions Secrets, never in this file.
"""

# First-run behaviour:
# True  = post all currently free listings the first time the notifier runs.
# False = silently save them as the baseline; only future additions are posted.
SEND_EXISTING_ON_FIRST_RUN = False

# How often GitHub Actions should run the notifier.
# The workflow reads this value from this file. Allowed range: 1 to 24 hours.
RUN_INTERVAL_HOURS = 6

# Telegram footer. Set either value to an empty string to omit that line.
TELEGRAM_CHANNEL_TAG = "@IntenseGaming"
TELEGRAM_FOOTER = "Powered By Intense Network"

# API fields available in the FreeGameRadar /api/v1/developer/free response.
# Set any value to False to hide that field from the Telegram post.
# The game title and game URL are always retained as the basic identity/claim link.
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
