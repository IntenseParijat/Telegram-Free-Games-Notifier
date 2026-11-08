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

All non-secret preferences are in `config.py`. Edit this file on `main` to change the notifier.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SEND_EXISTING_ON_FIRST_RUN` | `True` | Post all existing listings on the first scan, or silently seed the baseline. |
| `RUN_INTERVAL_HOURS` | `6` | Scheduled interval, from 1 to 24 hours. |
| `TELEGRAM_CHANNEL_TAG` | `@IntenseGaming` | First footer line; set to `""` to disable. |
| `TELEGRAM_FOOTER` | `Powered By [Intense Network](t.me/IntenseNetwork)` | Second footer line; set to `""` to disable. |
| `DISPLAY_FIELDS` | All `True` | Select which API fields appear in messages. |

### First-run behaviour

- `False`: the first successful scan silently records the current free games as the baseline. Only games added later are posted.
- `True`: the first successful scan posts all currently free games and records the catalogue to prevent repeats.

The repository tracks `data/seen_games.json` from the start. The checked-in file currently contains an empty `seen` list, so with `SEND_EXISTING_ON_FIRST_RUN = True`, the next run will post the catalogue currently returned by FreeGameRadar. Set it to `False` before the first run if you prefer a silent baseline. After the database has been populated, changing this option does not replay existing games. To deliberately reset tracking, clear the `seen` list in the JSON file.

### Schedule

Set `RUN_INTERVAL_HOURS` to an integer from 1 to 24. The workflow updates its cron expression from this setting when you manually run **Actions → Free Game Notifier → Run workflow**. After changing the interval in `config.py`, manually dispatch the workflow once to apply the new schedule. GitHub scheduled runs are best-effort and may be delayed.

### Footer

Edit `TELEGRAM_CHANNEL_TAG` and `TELEGRAM_FOOTER` to customise the final lines. Set either value to `""` to omit that line. The current defaults render as:

```text
@IntenseGaming
Powered By Intense Network
```

The footer is rendered as Telegram HTML. For a linked footer, use Telegram HTML anchor syntax, for example `Powered By <a href="https://t.me/IntenseNetwork">Intense Network</a>`. Plain Markdown link syntax is not interpreted by Telegram.

### Available API fields

The `DISPLAY_FIELDS` dictionary exposes these FreeGameRadar response fields. Each option is `True` by default; set it to `False` to hide it.

| Config key | API field | Meaning |
| --- | --- | --- |
| `description` | `description` | Game description; trimmed at the last complete period when needed. |
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

The game `title` and `game_url` are always included as the identity and claim destination; they are not configurable because they are essential to a useful alert.

For example, to disable images and descriptions while retaining the rest:

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

For photo posts, the notifier reserves space for the title, selected metadata, claim link, and footer, then uses the remaining capacity for the description. It keeps only complete description sentences ending in a period when the full description cannot fit. It does not cut a sentence in the middle. Photo captions are capped at 1,000 visible characters to stay below Telegram's 1,024-character caption maximum. The notifier calculates the available space after all other enabled message fields and keeps the longest sequence of complete description sentences ending in periods that fits. It never truncates a sentence. If even the first sentence cannot fit, the description is omitted. HTML tag syntax is excluded when measuring visible text.

If images are disabled or the image upload fails, a text message is sent instead. Text messages use Telegram's 4,096-character limit and the same sentence-boundary trimming rule.

## Duplicate protection

The notifier stores platform plus `game_id` (falling back to the API listing `id`) in `data/seen_games.json`. The same game on different platforms is treated as a separate listing. IDs are retained even if a listing temporarily disappears from the API. The state file is committed back to the repository after each workflow run.

Do not delete or replace `data/seen_games.json` unless you intentionally want to reset duplicate tracking.



## API pagination and recovery

The notifier requests pages of 100 records. If FreeGameRadar returns the same page more than once (as happened during one observed run), it logs a warning and stops paging. It still processes unique records already returned, so new games on the returned page can be posted, but the run remains failed and clearly reports that the catalogue scan was partial. Games on later pages cannot be detected until FreeGameRadar fixes its pagination behaviour.

A partial scan never initializes a first-run baseline. That prevents the notifier from treating the first page as the complete catalogue and permanently suppressing games it could not see. If the database already exists, successfully posted titles are recorded normally; failed Telegram deliveries remain unseen and eligible for the next run.

If an API request fails or returns malformed JSON, the scan stops and duplicate state is left unchanged. The workflow commits successful state updates even when the notifier reports failure, while keeping the Actions job failed so the error remains visible.

The repository keeps an empty `data/seen_games.json` file under version control. Do not delete it unless intentionally resetting duplicate tracking. With `SEND_EXISTING_ON_FIRST_RUN = True`, an empty tracked database is treated as a first run and the current listings are posted. Set it to `False` to silently seed the current catalogue instead.

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
