# Telegram Free Games Notifier

Checks FreeGameRadar for free games across its supported platforms and posts new listings to a Telegram channel using your Telegram account through Telethon.

## Setup

1. Create a Telegram API application at [my.telegram.org](https://my.telegram.org).
2. Generate a Telethon StringSession using the instructions below.
3. Add these repository secrets under **Settings → Secrets and variables → Actions**:

   | Secret | Value |
   | --- | --- |
   | `TELEGRAM_API_ID` | Telegram API ID |
   | `TELEGRAM_API_HASH` | Telegram API hash |
   | `TELEGRAM_SESSION` | Telethon StringSession |
   | `TELEGRAM_CHANNEL` | Channel username or numeric ID, e.g. `@IntenseGaming` |
   | `FREEGAMERADAR_API_KEY` | Optional FreeGameRadar API key |

4. Make the Telegram account used for the session an administrator of the destination channel with permission to post.
5. Review and edit `config.py`.
6. Enable GitHub Actions and run **Actions → Free Game Notifier → Run workflow**.

## Generate a Telethon StringSession

A StringSession lets GitHub Actions authenticate as your Telegram account without requiring an interactive login on every scheduled run.

### Get Telegram API credentials

1. Sign in at [my.telegram.org](https://my.telegram.org).
2. Open **API development tools**.
3. Create an application if you have not already done so.
4. Copy the numeric **App api_id** and **App api_hash**.

### Generate the session on Windows

Install Telethon:

```powershell
py -m pip install --upgrade pip
py -m pip install --upgrade Telethon
```

Download [generate_string_session.py](https://github.com/IntenseParijat/Telegram-Free-Games-Notifier/blob/main/generate_string_session.py), enter your API ID and API hash in the file, and run:

```powershell
py generate_string_session.py
```

Enter your phone number including country code, the login code sent by Telegram, and your two-step verification password if enabled. The script prints the StringSession.

Copy the entire string and add it as the `TELEGRAM_SESSION` repository secret. Do not commit it or share it.

### Add repository secrets

Open **Settings → Secrets and variables → Actions → New repository secret** and add the values listed in the Setup table.

A StringSession is effectively a login credential for your Telegram account. Anyone with it may be able to access that account. Keep it only in GitHub Secrets. If exposed, revoke the session from Telegram **Settings → Devices / Active Sessions** and generate a new one.

## Configuration

All non-secret preferences are in `config.py`. Edit that file on `main` to change how the notifier behaves.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SEND_EXISTING_ON_FIRST_RUN` | `False` | Whether to post all currently free listings on the first run. |
| `RUN_INTERVAL_HOURS` | `6` | Scheduled run interval, from 1 to 24 hours. |
| `TELEGRAM_CHANNEL_TAG` | `@IntenseGaming` | Optional channel tag shown in the footer. Empty string disables it. |
| `TELEGRAM_FOOTER` | `Powered By Intense Network` | Optional footer text. Empty string disables it. |
| `DISPLAY_FIELDS` | See below | Choose which API fields appear in messages. |

### First-run behaviour

- `False`: the first successful scan silently records the current free games as the baseline. Only games added later are posted.
- `True`: the first successful scan posts all currently free games, then records them so later runs do not repost them.

Changing this setting after a baseline has already been created does not reset the database. To deliberately start over, remove `data/seen_games.json` from the repository before the next run. Be aware that this may cause existing games to be posted again when first-run sending is enabled.

### Schedule

Change `RUN_INTERVAL_HOURS` to an integer from 1 to 24. The workflow applies the configured interval to its GitHub Actions schedule when it runs. GitHub scheduled workflows are best-effort and may start later than the exact cron minute. You can always start a run manually.

### Footer

Edit `TELEGRAM_CHANNEL_TAG` and `TELEGRAM_FOOTER` to customise the final lines of each message. Set either value to `""` to omit that line.

### Available API fields

The `DISPLAY_FIELDS` dictionary includes the fields returned by the FreeGameRadar `GET /api/v1/developer/free` response:

| Config key | API field | Meaning |
| --- | --- | --- |
| `description` | `description` | Game description; shortened at the last complete period when needed. |
| `image` | `image_url` | Attach the API-provided image. |
| `platform` | `platform` | Store/platform name. |
| `category` | `category` | API category. |
| `discount_percent` | `discount_percent` | Discount percentage. |
| `original_price` | `original_price` | Original price. |
| `final_price` | `final_price` | Current/final price. |
| `discount_price` | `discount_price` | Discount amount/value. |
| `currency` | `currency` | Currency code. |
| `start_date` | `start_date` | Offer start date, when supplied. |
| `end_date` | `end_date` | Offer end date, when supplied. |
| `is_free` | `is_free` | Whether the listing is free. |
| `game_id` | `game_id` | Store-specific game identifier. |
| `id` | `id` | FreeGameRadar listing identifier. |
| `created_at` | `created_at` | Listing creation timestamp. |
| `updated_at` | `updated_at` | Last update timestamp. |

The game title and claim URL are always included as the basic identity and destination link. These are intentionally not configurable because they are essential to a useful game alert.

All `DISPLAY_FIELDS` options are `True` by default. Set any field to `False` to hide it. For example, to disable images and the long description:

```python
DISPLAY_FIELDS = {
    "description": False,
    "image": False,
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
```

### Description and Telegram limits

Photo captions are kept within 1,000 characters, leaving a margin below Telegram's 1,024-character caption limit for standard accounts. The notifier calculates the remaining space after the title, selected metadata, claim link, and footer. If the description does not fit, it keeps only complete sentences up to the last period that fits. It never cuts the description in the middle of a sentence. If no complete sentence fits, the description is omitted.

If an image is disabled or cannot be sent, the notifier sends a text message instead, using Telegram's 4,096-character message limit and the same sentence-boundary trimming rule.

## Duplicate protection

The notifier stores platform plus `game_id` (falling back to the API listing `id`) in `data/seen_games.json`. The same game on different platforms is treated as a separate listing. IDs are retained even if a listing temporarily disappears from the API. The state file is committed back to the repository after each workflow run.

Do not delete or replace `data/seen_games.json` unless you intentionally want to reset duplicate tracking.

## Local run

```powershell
py -m pip install --upgrade pip
py -m pip install --upgrade -r requirements.txt
py notifier.py
```

Set the same environment variables used by the workflow before running locally.

## Runtime behaviour

- Fetches the complete paginated FreeGameRadar catalogue.
- Retries transient API failures and handles Telegram flood waits.
- Posts each newly discovered listing once, using the API image when enabled.
- Marks a listing as seen only after Telegram confirms its message was sent.
- Supports manual workflow dispatch as a recovery option.
