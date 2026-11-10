# Telegram Free Games Notifier

Checks FreeGameRadar's documented store-filtered free-giveaway feeds independently (Epic, Steam, GOG and Other), merges the results, and posts eligible listings to a Telegram channel using your Telegram account through Telethon.

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


| Setting | Default | Purpose |
| --- | --- | --- |
| `SEND_EXISTING_ON_FIRST_RUN` | `True` | Post all existing listings on the first scan, or silently seed the baseline. |
| `RUN_INTERVAL_HOURS` | `6` | Informational only. Edit the workflow cron expression to change the actual schedule. |
| `TELEGRAM_CHANNEL_TAG` | `@IntenseGaming` | First footer line; set to `""` to disable. |
| `TELEGRAM_FOOTER` | `Powered By [Intense Network](t.me/IntenseNetwork)` | Second footer line; set to `""` to disable. |
| `DISPLAY_FIELDS` | See `config.py` | Controls the description, image and platform display. Category, discount, price, IDs and timestamps are intentionally omitted from captions. |

### First-run behaviour

- `False`: the first successful scan silently records the current free games as the baseline. Only games added later are posted.
- `True`: the first successful scan posts all currently free games and records the catalogue to prevent repeats.

The repository tracks `data/seen_games.json` from the start. It contains the previously recorded game IDs so those titles are not reposted. If you deliberately remove or clear the database, the next complete scan follows `SEND_EXISTING_ON_FIRST_RUN`: `True` posts the currently returned catalogue, while `False` silently seeds it as the baseline. After the database has been populated, changing this option does not replay existing games.

### Schedule

The actual schedule is defined by the `cron` expression in `.github/workflows/notifier.yml` (currently every six hours at minute 17 UTC). Edit that expression to change the scheduled interval. `RUN_INTERVAL_HOURS` in `config.py` is retained as an informational compatibility setting and does not change GitHub's schedule. GitHub scheduled runs are best-effort and may be delayed.

### Footer

Edit `TELEGRAM_CHANNEL_TAG` and `TELEGRAM_FOOTER` to customise the final lines. Set either value to `""` to omit that line. The current defaults render as:

```text
@IntenseGaming
Powered By Intense Network
```

The footer setting accepts plain text and Markdown-style links, including the configured shorthand `Powered By [Intense Network](t.me/IntenseNetwork)`. The notifier converts these to Telegram-supported HTML before sending, producing `Powered By Intense Network` with `Intense Network` clickable. Full `https://` links are also supported.

### Caption contents

The Telegram caption follows this exact order, with one blank line between each block:

1. **Game title**
2. **Description**
3. **Platform**
4. **Get the game** claim link
5. **Free until** offer expiry
6. Configured footer lines

Category, discount percentage, original/final/discount price, currency, `is_free`, game IDs, listing IDs, and created/updated timestamps are not shown. The footer remains controlled by `TELEGRAM_CHANNEL_TAG` and `TELEGRAM_FOOTER`.

### Description and Telegram limits

For photo posts, the notifier reserves space for the title, selected metadata, claim link, and footer, then uses the remaining capacity for the description. It keeps only complete description sentences ending in a period when the full description cannot fit. It does not cut a sentence in the middle. Photo captions are capped at 1,000 visible characters to stay below Telegram's 1,024-character caption maximum. The notifier calculates the available space after all other enabled message fields and keeps the longest sequence of complete description sentences ending in periods that fits. It never truncates a sentence. If even the first sentence cannot fit, the description is omitted. HTML tag syntax is excluded when measuring visible text.

If images are disabled or the image upload fails, a text message is sent instead. Text messages use Telegram's 4,096-character limit and the same sentence-boundary trimming rule.

## Duplicate protection

The notifier stores each giveaway separately using platform, `game_id` (falling back to the API listing `id`), and the offer start timestamp (`start_date` or another supported start-time field) in `data/seen_games.json`. A later promotion of the same game with a different start time is treated as a new offer and will be posted again, even when months or years have passed. The same offer remains suppressed on subsequent runs. If the API omits an offer-start timestamp, the notifier falls back to the previous platform-plus-game key so duplicate protection still works, but it cannot distinguish a later promotion for that record. A record is eligible only when it is currently marked free, has a future offer-expiry field, has a positive original/retail price, and does not indicate permanent F2P or subscription/pass catalogue access. The same game on different platforms is treated as a separate listing. IDs are retained even if a listing temporarily disappears from the API. The state file is committed back to the repository after each workflow run. Existing legacy game-only keys are migrated without immediately reposting the already-recorded game.

Do not delete or replace `data/seen_games.json` unless you intentionally want to reset duplicate tracking.



## API pagination and recovery

The notifier queries Epic, Steam, GOG and Other separately using FreeGameRadar's documented platform filter, with pages of up to 100 records for each store. It deduplicates within and across feeds. Each feed is paginated independently until an empty page, a repeated page, or the safety page limit is reached. A repeated page on one feed is reported as partial and does not discard records collected from other feeds.

A partial scan never initializes a first-run baseline. That prevents the notifier from treating the first page as the complete catalogue and permanently suppressing games it could not see. If the database already exists, successfully posted titles are recorded normally; failed Telegram deliveries remain unseen and eligible for the next run.

If an API request fails or returns malformed JSON, the scan stops and duplicate state is left unchanged. The workflow commits successful state updates even when the notifier reports failure, while keeping the Actions job failed so the error remains visible.

When `data/seen_games.json` is absent, the notifier treats the next run as a first run. `SEND_EXISTING_ON_FIRST_RUN = True` posts the currently available free listings; `False` seeds the database without posting, but only after a complete scan. The workflow does not create an empty database before the notifier runs.

## Local run

```powershell
py -m pip install --upgrade pip
py -m pip install --upgrade -r requirements.txt
py notifier.py
```

Set the same environment variables used by the workflow before running locally.

## Runtime behaviour

- Fetches the paginated FreeGameRadar active-free-giveaways endpoint (`/developer/free`, with documented `page` and `limit` parameters).
- Retries transient API failures and handles Telegram flood waits.
- Posts each newly discovered eligible time-limited giveaway once, using the API image as a compressed Telegram photo (never as a document).
- Marks a listing as seen only after Telegram confirms its message was sent.
- Supports manual workflow dispatch as a recovery option.
