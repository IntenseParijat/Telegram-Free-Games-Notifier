"""Post newly listed free games from FreeGameRadar to a Telegram channel."""

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

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

API_URL = "https://freegameradar.com/api/v1/developer/free"
STATE_PATH = Path(os.getenv("STATE_FILE", "data/seen_games.json"))
PAGE_SIZE = 100
MAX_DESCRIPTION = 2_500
MAX_CAPTION = 1_000
REQUEST_TIMEOUT = (15, 45)
MAX_RETRIES = 5
MAX_PAGES = 10_000
MAX_STATE_KEYS = 100_000

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("free-game-notifier")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret/environment variable: {name}")
    return value


def fetch_page(session: requests.Session, page: int, api_key: str | None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "Telegram-Free-Games-Notifier/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(API_URL, headers=headers, params={"page": page, "limit": PAGE_SIZE}, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                wait = min(int(retry_after), 120) if retry_after and retry_after.isdigit() else min(2 ** attempt, 60)
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                log.warning("API returned %s on page %s; retrying in %ss", response.status_code, page, wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), list):
                raise RuntimeError(f"Unexpected API response on page {page}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Could not fetch API page {page} after {MAX_RETRIES} attempts") from exc
            wait = min(2 ** attempt, 60)
            log.warning("Page %s request failed (%s); retrying in %ss", page, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch page {page}")


def fetch_all_games(api_key: str | None) -> list[dict[str, Any]]:
    all_games: list[dict[str, Any]] = []
    seen_page_fingerprints: set[tuple[str, ...]] = set()
    with requests.Session() as session:
        for page in range(1, MAX_PAGES + 1):
            games = fetch_page(session, page, api_key)["data"]
            if not games:
                break
            fingerprint = tuple(str(g.get("id") or g.get("game_id") or "") for g in games if isinstance(g, dict))
            if fingerprint in seen_page_fingerprints:
                log.warning("API repeated page %s; stopping pagination", page)
                break
            seen_page_fingerprints.add(fingerprint)
            all_games.extend(g for g in games if isinstance(g, dict))
            log.info("Fetched page %s: %s records (total %s)", page, len(games), len(all_games))
            if len(games) < PAGE_SIZE:
                break
        else:
            raise RuntimeError(f"Pagination safety limit reached ({MAX_PAGES} pages)")
    return all_games


def game_key(game: dict[str, Any]) -> str:
    platform = str(game.get("platform") or "Unknown").strip().casefold()
    identifier = str(game.get("game_id") or game.get("id") or "").strip()
    if not identifier:
        identifier = re.sub(r"\s+", " ", str(game.get("title") or "").strip()).casefold()
    if not identifier:
        raise ValueError("Game record has no usable game_id, id, or title")
    return f"{platform}:{identifier}"


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
        log.warning("State has %s keys; retaining %s", len(seen), MAX_STATE_KEYS)
        seen = set(sorted(seen)[-MAX_STATE_KEYS:])
    temp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temp.write_text(json.dumps({"version": 1, "seen": sorted(seen)}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def safe_text(value: Any) -> str:
    return html.escape(str(value or "").strip(), quote=False)


def build_caption(game: dict[str, Any]) -> str:
    title = safe_text(game.get("title") or "Untitled game")
    platform = safe_text(game.get("platform") or "Unknown platform")
    category = safe_text(game.get("category") or "")
    description = re.sub(r"\n{3,}", "\n\n", str(game.get("description") or "").strip())
    description = html.escape(description, quote=False)
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION - 1].rstrip() + "…"
    lines = [f"<b>{title}</b>", f"🎮 <b>Platform:</b> {platform}"]
    if category:
        lines.append(f"🏷 <b>Category:</b> {category}")
    original = str(game.get("original_price") or "").strip()
    final = str(game.get("final_price") or "").strip()
    if original or final:
        price = f"{safe_text(original)} → {safe_text(final)}" if original and final and original != final else safe_text(final or original)
        lines.append(f"💰 <b>Price:</b> {price}")
    if description:
        lines.extend(["", description])
    lines.append("")
    return "\n".join(lines)


def valid_http_url(value: Any) -> str | None:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else None


async def send_one(client: TelegramClient, channel: str, game: dict[str, Any]) -> None:
    title = str(game.get("title") or "Untitled game")
    game_url = valid_http_url(game.get("game_url"))
    image_url = valid_http_url(game.get("image_url"))
    caption = build_caption(game)
    if game_url:
        caption += f'\n🔗 <a href="{html.escape(game_url, quote=True)}">Get the game</a>'
    else:
        log.warning("No valid game_url for %s", title)

    if image_url:
        photo_caption = caption
        if len(photo_caption) > MAX_CAPTION:
            photo_caption = f"<b>{safe_text(title)}</b>\n🎮 <b>Platform:</b> {safe_text(game.get('platform') or 'Unknown platform')}"
            if game_url:
                photo_caption += f'\n🔗 <a href="{html.escape(game_url, quote=True)}">Get the game</a>'
        try:
            await client.send_file(channel, image_url, caption=photo_caption, parse_mode="html")
        except Exception as exc:
            log.warning("Image send failed for %s (%s); using text fallback", title, exc)
            text = caption if len(caption) <= 4096 else caption[:4095].rstrip() + "…"
            await client.send_message(channel, text, parse_mode="html", link_preview=False)
            return
        if len(caption) > MAX_CAPTION:
            full_text = caption if len(caption) <= 4096 else caption[:4095].rstrip() + "…"
            await client.send_message(channel, full_text, parse_mode="html", link_preview=False)
        return

    text = caption if len(caption) <= 4096 else caption[:4095].rstrip() + "…"
    await client.send_message(channel, text, parse_mode="html", link_preview=False)


async def run_notifier() -> None:
    api_key = os.getenv("FREEGAMERADAR_API_KEY", "").strip() or None
    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    channel = required_env("TELEGRAM_CHANNEL")
    session_string = required_env("TELEGRAM_SESSION")

    games = fetch_all_games(api_key)
    if not games:
        log.info("API returned an empty catalogue; leaving state untouched")
        return

    candidates: list[tuple[str, dict[str, Any]]] = []
    current_keys: set[str] = set()
    for game in games:
        if str(game.get("is_free", "")).strip().lower() not in ("1", "true", "yes"):
            continue
        key = game_key(game)
        current_keys.add(key)
        candidates.append((key, game))

    seen = load_state()
    if not STATE_PATH.exists():
        save_state(current_keys)
        log.info("Initialized duplicate state with %s listings; no first-run posts", len(current_keys))
        return

    new_games = [(key, game) for key, game in candidates if key not in seen]
    log.info("API records: %s | free listings: %s | new: %s", len(games), len(candidates), len(new_games))
    if not new_games:
        log.info("No new free games")
        return

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("TELEGRAM_SESSION is invalid or expired; generate a fresh Telethon StringSession")

    try:
        for index, (key, game) in enumerate(new_games, start=1):
            title = str(game.get("title") or "Untitled game")
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    await send_one(client, channel, game)
                    seen.add(key)
                    save_state(seen)
                    log.info("Posted %s (%s/%s)", title, index, len(new_games))
                    break
                except FloodWaitError as exc:
                    wait = int(exc.seconds) + 1
                    log.warning("Telegram flood wait: sleeping %ss", wait)
                    await asyncio.sleep(wait)
                except Exception:
                    if attempt == MAX_RETRIES:
                        raise
                    wait = min(2 ** attempt, 60)
                    log.exception("Failed posting %s (attempt %s/%s); retry in %ss", title, attempt, MAX_RETRIES, wait)
                    await asyncio.sleep(wait)
            await asyncio.sleep(1)
    finally:
        await client.disconnect()


def main() -> int:
    try:
        asyncio.run(run_notifier())
        return 0
    except Exception:
        log.exception("Notifier run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
