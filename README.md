# Telegram Free Games Notifier

Checks FreeGameRadar for newly listed free games across Epic Games, Steam, PlayStation, and Xbox, then posts them to a Telegram channel.

## Setup

1. Create a Telegram API application at [my.telegram.org](https://my.telegram.org).
2. Generate a Telethon **StringSession** using the instructions below.
3. Add these repository secrets under **Settings → Secrets and variables → Actions**:

   | Secret | Value |
   | --- | --- |
   | `TELEGRAM_API_ID` | Telegram API ID |
   | `TELEGRAM_API_HASH` | Telegram API hash |
   | `TELEGRAM_SESSION` | Telethon StringSession |
   | `TELEGRAM_CHANNEL` | Channel username or numeric ID, e.g. `@YourChannel` |
   | `FREEGAMERADAR_API_KEY` | Optional FreeGameRadar API key |

4. Add the Telegram account used for the session as an administrator of the destination channel with permission to post.
5. Enable GitHub Actions and run **Actions → Free Game Notifier → Run workflow** once to initialize duplicate tracking.

## Generate a Telethon StringSession

A StringSession lets the GitHub Actions runner sign in as your Telegram account without an interactive login on every scheduled run.

### 1. Get your Telegram API credentials

1. Sign in at [my.telegram.org](https://my.telegram.org).
2. Open **API development tools**.
3. Create an application if you have not created one before.
4. Copy the numeric **App api_id** and **App api_hash**. These are the values for `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

### 2. Generate the StringSession on your Windows PC

Install Python, then open Command Prompt or PowerShell and run:

```powershell
py -m pip install --U pip
py -m pip install --U Telethon
```

Download [generate_string_session.py](https://github.com/IntenseParijat/Telegram-Free-Games-Notifier/blob/main/generate_string_session.py)

Run it in any way

Example:
```powershell
python generate_string_session.py
```

Telethon will ask for your phone number (including country code), the login code sent by Telegram, and your two-step verification password if you have one enabled. Once login succeeds, the script prints a long session string.

Copy the **entire** printed string. Do not share it in chats, screenshots, source code, or commit it to GitHub.

### 3. Add it to GitHub Secrets

1. Open this repository on GitHub.
2. Go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Set the name to `TELEGRAM_SESSION`.
4. Paste the complete generated string as the value and save it.
5. Add `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_CHANNEL`, and optionally `FREEGAMERADAR_API_KEY` in the same way.

The Telegram account used to generate the session must be an administrator of the destination channel and have permission to post messages.

### Session security

A Telethon StringSession is effectively a login credential for your Telegram account. Anyone who obtains it may be able to access that account. Store it only as the private `TELEGRAM_SESSION` repository secret. Never put it in the repository, README, workflow YAML, logs, or a public session-generation website. If it is exposed, revoke the corresponding session from Telegram's **Settings → Devices / Active Sessions** and generate a new one.

## Schedule and duplicate protection

The workflow runs every six hours and can also be started manually. GitHub Actions schedules are best-effort and may be delayed. Concurrency prevents overlapping notifier runs.

The first successful scan creates `data/seen_games.json` as a baseline and does not post the existing catalogue. Later scans post only unseen listings. A listing is identified by platform plus `game_id` (falling back to API `id`), so the same game on separate platforms remains distinct. IDs are retained if a listing temporarily disappears from the API.

The state file is committed to this repository after the run. Keep it in the repository to retain duplicate protection between scheduled runs.

## Local run

```bash
python -m pip install --upgrade pip
python -m pip install --upgrade -r requirements.txt
python notifier.py
```

Set the same environment variables used by the workflow before running locally.

## Behaviour

- Fetches every API page using `page` and `limit`.
- Sends the image URL supplied by FreeGameRadar; falls back to a text post if the image cannot be sent.
- Includes title, platform, category, price, description, and claim link where provided, while respecting Telegram message limits.
- Marks a listing as seen only after Telegram confirms the send.
