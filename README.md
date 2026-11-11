# Telegram Free Games Notifier

Checks FreeGameRadar's documented Epic, Steam, PlayStation and Xbox feeds independently and also checks Epic's separate Android/iOS mobile giveaway API. It merges the results and posts eligible offers to a Telegram channel using your Telegram account through Telethon.

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

Optional GitHub Actions repository variable: `FREEGAMERADAR_API_LIMIT`. Leave it unset for the default tier-based size (100 with a key, 25 without one). Set it to `200` only when the FreeGameRadar key is on the Pro tier.

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

For FreeGameRadar store listings, the notifier stores each giveaway using its platform, stable game/listing ID, and offer start timestamp. A later promotion with a different start timestamp is treated as a new offer. If an offer-start field is missing, it falls back to a platform-plus-ID key.

Mobile giveaways are deduplicated across Epic's mobile feed and GamerPower using the normalized title plus the offer's expiry date. This prevents the same Android/iOS campaign from being posted twice when both sources report it. When both mobile platforms are available, their platform labels are combined in one notification. A later campaign with a different expiry date is a new offer.

The notifier writes a key only after Telegram confirms that the message was sent. The workflow commits changes to `data/seen_games.json` after each run. The previously delivered Flotsam offer remains recorded under its exact November 5 offer key; the original game-only key is retained as historical state.

Do not delete or replace `data/seen_games.json` unless you intentionally want to reset duplicate tracking.

## Epic Games Store mobile giveaways

Epic's mobile giveaways are not part of the regular PC free-games promotions feed. The notifier checks Epic's separate Android and iOS discovery API and also checks GamerPower's `type=game` giveaways filtered independently for Android and iOS.

Epic mobile records must contain a currently active, zero-price `Claim` purchase entry, a positive original price, and a valid end time. The expiry is read from `discountEndDate` or the next purchase-state transition. Price strings such as `$7.99` are parsed without requiring a separate paid purchase entry. The notifier logs the number of sections, offers, and the exact reason an offer is skipped.

GamerPower is used as an independent fallback. It is queried with `platform=android` / `platform=ios`, `type=game`, and `sort-by=date`; DLC, loot, and beta offers are not accepted. It requires a future `end_date`, a positive `worth`, and a valid claim URL. The claim URL intentionally uses GamerPower's `open_giveaway_url` redirect, providing the active hyperlink back to GamerPower required by its API terms. The primary FreeGameRadar API remains the source for Epic PC, Steam, PlayStation, and Xbox offers.

## API pagination and recovery

FreeGameRadar documents `page` and `limit` for `/developer/free`, with a maximum of 25 records per anonymous request, 100 with a Standard key, and 200 on Pro. This project requests 100 when `FREEGAMERADAR_API_KEY` is configured and 25 when it is not. Pro users can set the GitHub Actions repository variable `FREEGAMERADAR_API_LIMIT=200`; it is not assumed automatically because the API key does not reveal its tier to the notifier.

The notifier requests `page=1`, then `page=2`, `page=3`, and so on, using the maximum configured `limit` on every request. It does not use an undocumented offset or cursor. Short pages are not assumed to be terminal; the scan continues until the API returns an empty page or provides authoritative pagination metadata. Requests use no-cache headers and a cache-busting query value because prior runs received the first page again when requesting page 2.

If a later page exactly repeats an earlier page, or returns no new IDs, the notifier reports an incomplete scan rather than claiming completeness or looping forever. It still checks the other store feeds and both mobile sources and can post valid unseen listings found by those sources. A partial scan never initializes a first-run baseline.

The documented `/developer/games` endpoint supports `platform`, `category`, `search`, `letter`, `page`, and `limit`. The notifier uses `search` only to find descriptive metadata for candidate listings. Catalogue lookups cannot overwrite `is_free`, price, start, or expiry fields and cannot make a listing eligible by themselves.

A record is posted only when it has an active free flag, a future end date, a positive regular price, and no indication that it is permanent F2P, DLC/loot/beta, or subscription-only access. Each source logs an eligibility breakdown so exclusions are diagnosable. The bot saves a listing key only after Telegram confirms delivery. A partial discovery run can still deliver valid records it did find, while incomplete feeds are retried on the next scheduled run.

If an API request fails or returns malformed JSON, that feed is marked incomplete. Duplicate state is still committed in the workflow after a run, and the Actions job reports notifier failures rather than masking them.

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
