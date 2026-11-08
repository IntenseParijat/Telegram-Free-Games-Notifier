"""FreeGameRadar Telegram notifier."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

import config

API_URL = "https://freegameradar.com/api/v1/developer/free"
STATE_PATH = Path(os.getenv("STATE_FILE", "data/seen_games.json"))
PAGE_SIZE = 100
MAX_PHOTO_CAPTION = 1_000
MAX_TEXT_MESSAGE = 4_096
REQUEST_TIMEOUT = (15, 45)
MAX_RETRIES = 5
MAX_PAGES = 10_000
MAX_STATE_KEYS = 100_000
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("free-game-notifier")

API_FIELDS = (
    "description", "platform", "category", "discount_percent", "original_price",
    "final_price", "discount_price", "currency", "start_date", "end_date",
    "is_free", "game_id", "id", "created_at", "updated_at",
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_page(session: requests.Session, page: int, api_key: str | None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "Telegram-Free-Games-Notifier/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                API_URL,
                headers=headers,
                params={"page": page, "limit": PAGE_SIZE},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(max(int(retry_after), 1), 120) if retry_after else min(2 ** attempt, 60)
                except ValueError:
                    wait = min(2 ** attempt, 60)
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                log.warning("API returned HTTP %s on page %s; retrying in %ss", response.status_code, page, wait)
                time.sleep(wait)
                continue

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected non-object API response on page {page}")
            if payload.get("success") is not True or not isinstance(payload.get("data"), list):
                raise RuntimeError(f"Unexpected API response on page {page}: success/data fields missing")
            return payload
        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Could not fetch API page {page} after {MAX_RETRIES} attempts") from exc
            wait = min(2 ** attempt, 60)
            log.warning("Page %s request failed (%s); retrying in %ss", page, exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch page {page}")


def game_key(game: dict[str, Any]) -> str:
    platform = str(game.get("platform") or "Unknown").strip().casefold()
    identifier = str(game.get("game_id") or game.get("id") or "").strip()
    if not identifier:
        identifier = re.sub(r"\\s+", " ", str(game.get("title") or "").strip()).casefold()
    if not identifier:
        raise ValueError("Game record has no usable game_id, id, or title")
    return f"{platform}:{identifier}"


def fetch_all_games(api_key: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Fetch unique listings until the API signals the end or repeats a page.

    Do not infer the end of pagination from a short page: FreeGameRadar has
    returned non-terminal short pages. A repeated page is a partial scan.
    """
    all_games: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_page_fingerprints: set[frozenset[str]] = set()
    scan_complete = False

    with requests.Session() as session:
        for page in range(1, MAX_PAGES + 1):
            payload = fetch_page(session, page, api_key)
            records = payload["data"]

            if not records:
                scan_complete = True
                log.info("API pagination ended normally at empty page %s", page)
                break

            valid_records = [record for record in records if isinstance(record, dict)]
            page_keys = [game_key(record) for record in valid_records]
            fingerprint = frozenset(page_keys)

            if fingerprint and fingerprint in seen_page_fingerprints:
                log.warning(
                    "FreeGameRadar repeated page %s; stopping with a partial scan after %s unique records.",
                    page,
                    len(all_games),
                )
                break
            if fingerprint:
                seen_page_fingerprints.add(fingerprint)

            added_this_page = 0
            for record, key in zip(valid_records, page_keys):
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_games.append(record)
                added_this_page += 1

            log.info(
                "Fetched page %s: %s records, %s new, %s unique total",
                page,
                len(records),
                added_this_page,
                len(all_games),
            )

            if added_this_page == 0:
                log.warning(
                    "Page %s contained no new game IDs; stopping with a partial scan.",
                    page,
                )
                break
        else:
            log.warning("Pagination safety limit reached after %s pages; scan is PARTIAL.", MAX_PAGES)

    return all_games, scan_complete


def load_state() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        keys = data.get("seen", []) if isinstance(data, dict) else []
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ValueError("State file must contain a 'seen' array of strings")
        return set(keys)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Cannot read duplicate state at {STATE_PATH}: {exc}") from exc


def save_state(seen: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if len(seen) > MAX_STATE_KEYS:
        raise RuntimeError(f"Duplicate state exceeds safety limit ({MAX_STATE_KEYS}); refusing to discard history")
    temp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temp.write_text(
        json.dumps({"version": 1, "seen": sorted(seen)}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(STATE_PATH)


def safe_text(value: Any) -> str:
    """Escape displayed data after trimming surrounding whitespace."""
    return html.escape(str(value or "").strip(), quote=False)


def escape_text(value: Any) -> str:
    """Escape text without altering intentional spaces in formatted templates."""
    return html.escape(str(value or ""), quote=False)


def enabled(field: str) -> bool:
    return bool(config.DISPLAY_FIELDS.get(field, False))


def _format_footer_html_links(value: str) -> str:
    """Convert supported user-friendly footer links into Telegram HTML."""
    # Markdown links, including a prefix such as "Powered By ".
    pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|t\.me/[^\s)]+)\)")
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(value):
        # Keep all configured text. Trim only the separator at the join between
        # plain text and the anchor, then add exactly one space when needed.
        prefix = value[cursor:match.start()]
        if prefix:
            parts.append(escape_text(prefix.rstrip()))
            if prefix[-1].isspace():
                parts.append(" ")
        target = match.group(2)
        if target.startswith("t.me/"):
            target = "https://" + target
        parts.append(telegram_link(target, match.group(1)))
        cursor = match.end()
    if not parts:
        return safe_text(value)
    parts.append(escape_text(value[cursor:]))
    return "".join(parts)


def telegram_link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{safe_text(label)}</a>'


def format_footer_line(value: str) -> str:
    """Support plain text, Markdown links, and legacy t.me shorthand."""
    value = str(value or "").strip()
    if not value:
        return ""
    return _format_footer_html_links(value)


def footer_lines() -> list[str]:
    lines = []
    if str(config.TELEGRAM_CHANNEL_TAG).strip():
        lines.append(format_footer_line(config.TELEGRAM_CHANNEL_TAG))
    if str(config.TELEGRAM_FOOTER).strip():
        lines.append(format_footer_line(config.TELEGRAM_FOOTER))
    return lines


def formatted_optional_fields(game: dict[str, Any]) -> list[str]:
    lines = []
    if enabled("platform") and game.get("platform"):
        lines.append(f"🎮 <b>Platform:</b> {safe_text(game['platform'])}")
    if enabled("category") and game.get("category"):
        lines.append(f"🏷 <b>Category:</b> {safe_text(game['category'])}")
    if enabled("discount_percent") and game.get("discount_percent") not in (None, ""):
        lines.append(f"🏷 <b>Discount:</b> {safe_text(game['discount_percent'])}%")

    original = str(game.get("original_price") or "").strip() if enabled("original_price") else ""
    final = str(game.get("final_price") or "").strip() if enabled("final_price") else ""
    currency = str(game.get("currency") or "").strip() if enabled("currency") else ""
    if original or final:
        price = f"{safe_text(original)} → {safe_text(final)}" if original and final and original != final else safe_text(final or original)
        if currency:
            price += f" {safe_text(currency)}"
        lines.append(f"💰 <b>Price:</b> {price}")

    if enabled("discount_price") and game.get("discount_price") not in (None, ""):
        lines.append(f"💸 <b>Discount price:</b> {safe_text(game['discount_price'])}")
    if enabled("start_date") and game.get("start_date"):
        lines.append(f"🗓 <b>Starts:</b> {safe_text(game['start_date'])}")
    if enabled("end_date") and game.get("end_date"):
        lines.append(f"⏳ <b>Ends:</b> {safe_text(game['end_date'])}")
    if enabled("is_free") and game.get("is_free") is not None:
        lines.append(f"🆓 <b>Free:</b> {safe_text(game['is_free'])}")
    if enabled("game_id") and game.get("game_id"):
        lines.append(f"<b>Game ID:</b> <code>{safe_text(game['game_id'])}</code>")
    if enabled("id") and game.get("id") is not None:
        lines.append(f"<b>Listing ID:</b> <code>{safe_text(game['id'])}</code>")
    if enabled("created_at") and game.get("created_at"):
        lines.append(f"<b>Added:</b> {safe_text(game['created_at'])}")
    if enabled("updated_at") and game.get("updated_at"):
        lines.append(f"<b>Updated:</b> {safe_text(game['updated_at'])}")
    return lines


def valid_http_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc or any(ch.isspace() for ch in url):
        return None
    return url


def fixed_caption_parts(game: dict[str, Any]) -> tuple[str, str]:
    lines = [f"<b>{safe_text(game.get('title') or 'Untitled game')}</b>"]
    lines.extend(formatted_optional_fields(game))
    game_url = valid_http_url(game.get("game_url"))
    if game_url:
        lines.append(f'🔗 <a href="{html.escape(game_url, quote=True)}">Get the game</a>')
    lines.extend(footer_lines())
    description = str(game.get("description") or "").strip() if enabled("description") else ""
    return "\n".join(lines), description


def visible_text_length(text: str) -> int:
    """Count visible text in the HTML formatted by this notifier, using UTF-16 units."""
    without_tags = re.sub(r"<[^>]*>", "", text)
    visible = html.unescape(without_tags)
    return len(visible.encode("utf-16-le")) // 2


def trim_description_to_fit(description: str, fixed_caption: str, limit: int) -> str:
    """Keep complete sentences within the available rendered character budget."""
    if not description:
        return ""

    separator = "\n\n" if fixed_caption else ""
    remaining = limit - visible_text_length(fixed_caption) - visible_text_length(separator)
    if remaining <= 0:
        return ""

    best_end = -1
    for match in re.finditer(r"\.", description):
        candidate = description[:match.end()]
        if visible_text_length(candidate) <= remaining:
            best_end = match.end()
        else:
            break

    if best_end >= 0:
        return description[:best_end].rstrip()
    if visible_text_length(description) <= remaining:
        return description
    return ""


def compose_message(game: dict[str, Any], limit: int) -> str:
    fixed, description = fixed_caption_parts(game)
    escaped_description = safe_text(description)
    shortened = trim_description_to_fit(escaped_description, fixed, limit)
    if shortened:
        return f"{fixed}\n\n{shortened}"
    return fixed


async def send_one(client: TelegramClient, channel: str, game: dict[str, Any]) -> None:
    title = str(game.get("title") or "Untitled game")
    image_url = valid_http_url(game.get("image_url")) if enabled("image") else None

    if image_url:
        caption = compose_message(game, MAX_PHOTO_CAPTION)
        if visible_text_length(caption) > MAX_PHOTO_CAPTION:
            raise ValueError(f"Photo caption for {title} exceeds {MAX_PHOTO_CAPTION} Telegram UTF-16 characters")
        try:
            await client.send_file(channel, image_url, caption=caption, parse_mode="html")
            return
        except Exception as exc:
            log.warning("Image send failed for %s (%s); falling back to text", title, exc)

    message = compose_message(game, MAX_TEXT_MESSAGE)
    if visible_text_length(message) > MAX_TEXT_MESSAGE:
        raise ValueError(f"Text message for {title} exceeds Telegram's message limit after trimming")
    await client.send_message(channel, message, parse_mode="html", link_preview=False)


async def run_notifier() -> None:
    api_key = os.getenv("FREEGAMERADAR_API_KEY", "").strip() or None
    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    channel = required_env("TELEGRAM_CHANNEL")
    session_string = required_env("TELEGRAM_SESSION")

    games, scan_complete = fetch_all_games(api_key)
    if not games:
        log.info("API returned an empty catalogue; leaving state untouched")
        if not scan_complete:
            raise RuntimeError("FreeGameRadar scan was partial and returned no records; retry next run.")
        return

    current_games: dict[str, dict[str, Any]] = {}
    for game in games:
        if str(game.get("is_free", "")).strip().lower() in ("1", "true", "yes"):
            current_games[game_key(game)] = game

    first_run = not STATE_PATH.exists()
    seen = load_state()

    if first_run and not config.SEND_EXISTING_ON_FIRST_RUN and scan_complete:
        save_state(set(current_games))
        log.info("Initialized duplicate state with %s listings; first-run posting disabled", len(current_games))
        return

    new_games = [(key, game) for key, game in current_games.items() if key not in seen]
    log.info(
        "Fetched %s API records; %s unique free listings; %s not yet posted; scan_complete=%s",
        len(games),
        len(current_games),
        len(new_games),
        scan_complete,
    )

    if not new_games:
        if first_run and scan_complete:
            save_state(set(current_games))
        if scan_complete:
            log.info("No new free games")
            return
        raise RuntimeError("FreeGameRadar pagination was partial. State was not changed; retry next scheduled run.")

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("TELEGRAM_SESSION is invalid or expired; generate a fresh Telethon StringSession")

        failed_titles: list[str] = []
        for index, (key, game) in enumerate(new_games, start=1):
            title = str(game.get("title") or "Untitled game")
            posted = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    await send_one(client, channel, game)
                    posted = True
                    break
                except FloodWaitError as exc:
                    wait = max(int(exc.seconds), 1) + 1
                    log.warning("Telegram flood wait for %s; sleeping %ss", title, wait)
                    await asyncio.sleep(wait)
                except Exception:
                    if attempt == MAX_RETRIES:
                        log.exception("Giving up on %s for this run; it remains eligible for the next run", title)
                        break
                    wait = min(2 ** attempt, 60)
                    log.exception("Failed posting %s (attempt %s/%s); retrying in %ss", title, attempt, MAX_RETRIES, wait)
                    await asyncio.sleep(wait)

            if posted:
                seen.add(key)
                save_state(seen)
                log.info("Posted %s (%s/%s)", title, index, len(new_games))
            else:
                failed_titles.append(title)
            await asyncio.sleep(1)

        if failed_titles:
            preview = ", ".join(failed_titles[:10])
            more = f" (and {len(failed_titles) - 10} more)" if len(failed_titles) > 10 else ""
            raise RuntimeError(f"{len(failed_titles)} game(s) could not be delivered and remain pending: {preview}{more}")

        if not scan_complete:
            raise RuntimeError(
                "FreeGameRadar returned a partial catalogue. New games in the returned pages were sent, "
                "but later pages were not available to scan."
            )
    finally:
        await client.disconnect()


def validate_config() -> None:
    if type(config.SEND_EXISTING_ON_FIRST_RUN) is not bool:
        raise ValueError("SEND_EXISTING_ON_FIRST_RUN must be True or False")
    if type(config.RUN_INTERVAL_HOURS) is not int or not 1 <= config.RUN_INTERVAL_HOURS <= 24:
        raise ValueError("RUN_INTERVAL_HOURS must be an integer from 1 to 24")
    if not isinstance(config.DISPLAY_FIELDS, dict):
        raise ValueError("DISPLAY_FIELDS must be a dictionary")
    for field in (*API_FIELDS, "image"):
        if field not in config.DISPLAY_FIELDS or type(config.DISPLAY_FIELDS[field]) is not bool:
            raise ValueError(f"DISPLAY_FIELDS['{field}'] must be True or False")


def main() -> int:
    try:
        validate_config()
        asyncio.run(run_notifier())
        return 0
    except Exception:
        log.exception("Notifier run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
