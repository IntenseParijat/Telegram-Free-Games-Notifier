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


GAMES_API_URL = "https://freegameradar.com/api/v1/developer/games"


def _api_response_data(payload: Any, context: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError(f"Unexpected FreeGameRadar response for {context}")
    raw = payload.get("data")
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = raw.get("games") or raw.get("results") or raw.get("items") or []
    else:
        records = []
    if not isinstance(records, list):
        raise RuntimeError(f"Unexpected data array from FreeGameRadar for {context}")
    meta = payload.get("pagination") or payload.get("meta") or payload.get("pages") or {}
    if not isinstance(meta, dict):
        meta = {}
    return [x for x in records if isinstance(x, dict)], meta


def _api_limit_for_key(api_key: str | None) -> int:
    # FreeGameRadar's documented caps: 25 anonymous, 100 standard key,
    # 200 Pro. This repository uses an optional standard developer key.
    # 100 is the largest documented request supported by that tier.
    return 100 if api_key else 25


def fetch_page(
    session: requests.Session,
    page: int,
    api_key: str | None,
    platform: str | None = None,
    limit: int | None = None,
    endpoint: str = API_URL,
    search: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "Telegram-Free-Games-Notifier/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key

    params: dict[str, Any] = {
        "page": page,
        "limit": min(max(int(limit or _api_limit_for_key(api_key)), 1), 200),
    }
    if platform:
        params["platform"] = platform
    if search:
        params["search"] = search

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(endpoint, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(max(int(retry_after), 1), 120) if retry_after else min(2 ** attempt, 60)
                except ValueError:
                    wait = min(2 ** attempt, 60)
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                log.warning(
                    "FreeGameRadar HTTP %s (%s page %s, limit %s); retrying in %ss",
                    response.status_code, platform or "all-platform", page, params["limit"], wait,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), (list, dict)):
                raise RuntimeError(
                    f"Unexpected FreeGameRadar response for {platform or 'all-platform'} page {page}"
                )
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = min(2 ** attempt, 60)
            log.warning(
                "FreeGameRadar %s page %s request failed (%s); retrying in %ss",
                platform or "all-platform", page, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"Could not fetch FreeGameRadar {platform or 'all-platform'} page {page} after {MAX_RETRIES} attempts"
    ) from last_error


def _record_identity(record: dict[str, Any]) -> str:
    for field in ("game_id", "id", "offer_id", "offerId", "slug", "url", "game_url", "title"):
        value = record.get(field)
        if value not in (None, ""):
            return _normalised_text(value)
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def _fetch_platform_games(
    session: requests.Session,
    api_key: str | None,
    platform: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch one store's /free feed; verify actual page progression."""
    label = platform or "all-platform"
    limit = _api_limit_for_key(api_key)
    records_all: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_fingerprints: set[tuple[str, ...]] = set()
    expected_total_pages: int | None = None
    expected_total: int | None = None
    scan_complete = False

    for page in range(1, MAX_PAGES + 1):
        payload = fetch_page(session, page, api_key, platform, limit=limit)
        records, meta = _api_response_data(payload, f"{label} page {page}")
        if page == 1:
            for total_key in ("totalPages", "total_pages", "pages", "lastPage"):
                if meta.get(total_key) is not None:
                    try:
                        expected_total_pages = int(meta[total_key])
                    except (TypeError, ValueError):
                        pass
                    break
            for total_key in ("total", "totalItems", "total_items", "count"):
                if meta.get(total_key) is not None:
                    try:
                        expected_total = int(meta[total_key])
                    except (TypeError, ValueError):
                        pass
                    break

        if not records:
            scan_complete = True
            log.info("%s page %s returned 0 records; pagination ended", label, page)
            break

        ids = tuple(_record_identity(x) for x in records)
        if ids in page_fingerprints:
            log.error(
                "%s page %s exactly repeated an earlier page (%s records). "
                "The service appears to ignore the page parameter or cache page 1; scan is incomplete.",
                label, page, len(records),
            )
            break
        page_fingerprints.add(ids)

        added = 0
        for record in records:
            identity = _record_identity(record)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            records_all.append(record)
            added += 1

        log.info(
            "%s page %s/%s: received %s records, %s new, %s unique total (limit=%s, reported_total=%s)",
            label,
            page,
            expected_total_pages if expected_total_pages is not None else "?",
            len(records),
            added,
            len(records_all),
            limit,
            expected_total if expected_total is not None else "?",
        )

        if expected_total_pages is not None and page >= expected_total_pages:
            scan_complete = True
            break
        if expected_total is not None and len(records_all) >= expected_total:
            scan_complete = True
            break
        if len(records) < limit:
            # A short page is terminal only when there is no contradictory
            # pagination metadata and no known server-side page duplication.
            scan_complete = True
            break

    else:
        log.error("%s reached the %s-page safety limit; scan is incomplete", label, MAX_PAGES)

    if not scan_complete:
        log.warning("%s scan incomplete: collected %s unique records", label, len(records_all))
    return records_all, scan_complete


def fetch_game_search(
    session: requests.Session,
    api_key: str | None,
    search: str,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """Use the documented /games?search= filter for targeted metadata recovery."""
    label = f"search={search!r}" + (f", platform={platform}" if platform else "")
    payload = fetch_page(
        session, 1, api_key, platform, limit=min(_api_limit_for_key(api_key), 100),
        endpoint=GAMES_API_URL, search=search,
    )
    records, _ = _api_response_data(payload, label)
    log.info("FreeGameRadar /games %s returned %s records", label, len(records))
    return records


def merge_game_metadata(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    """Fill absent/empty fields from a matching /games lookup without overwriting offer state."""
    merged = dict(primary)
    for key, value in secondary.items():
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged

MOBILE_EPIC_API_URL = "https://egs-platform-service.store.epicgames.com/api/v2/public/discover/home"


def _mobile_price_amount(value: Any) -> float:
    """Normalize Epic mobile price values (usually integer minor units)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    # Epic storefront payloads normally express prices as cents.
    return amount / 100 if amount > 100 else amount


def fetch_epic_mobile_platform(
    session: requests.Session,
    platform: str,
) -> list[dict[str, Any]]:
    """Fetch mobile products and normalize active free-to-keep promotions.

    The mobile storefront endpoint is separate from Epic's regular PC free-games
    promotions feed. It returns products with platform-specific pricing and
    action data. Only products with an explicit active free claim action and
    a positive normal price are converted into notifier records.
    """
    params = {
        "count": 10,
        "country": os.getenv("EPIC_COUNTRY", "IN"),
        "locale": os.getenv("EPIC_LOCALE", "en-IN"),
        "platform": platform,
        "start": 0,
        "store": "EGS",
        # Epic's CDN caches this endpoint by the complete query string.
        "cb": int(time.time() * 1000),
    }
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "EpicGamesStore/1.0 (Android)",
    }

    payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                MOBILE_EPIC_API_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            candidate = response.json()
            if not isinstance(candidate, dict) or not isinstance(candidate.get("data"), list):
                raise RuntimeError(f"Epic {platform} mobile endpoint returned an unexpected response schema")
            payload = candidate
            break
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = min(2 ** attempt, 30)
            log.warning(
                "Epic %s mobile request failed (attempt %s/%s): %s; retrying in %ss",
                platform, attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)

    if payload is None:
        raise RuntimeError(f"Could not fetch Epic {platform} mobile offers after {MAX_RETRIES} attempts: {last_error}")

    if not isinstance(payload, dict):
        raise RuntimeError(f"Epic {platform} mobile endpoint returned an unexpected response")
    sections = payload.get("data") or []
    if not isinstance(sections, list):
        raise RuntimeError(f"Epic {platform} mobile endpoint returned an unexpected sections list")

    normalized: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict) or section.get("type") != "freeGame":
            continue

        offers = section.get("offers") or []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            content = offer.get("content") or {}
            if not isinstance(content, dict):
                continue

            slug = (content.get("mapping") or {}).get("slug")
            if not slug:
                continue

            # A mobile product can contain multiple Claim states: an active
            # one and a future scheduled one. Select only a currently active
            # zero-price Claim and use its effective date as the offer start.
            purchases = content.get("purchase") or []
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)

            def purchase_time(value: Any):
                return _parse_offer_datetime(value)

            current_claims: list[tuple[int, dict[str, Any]]] = []
            for index, purchase in enumerate(purchases):
                if not isinstance(purchase, dict):
                    continue
                purchase_price = purchase.get("price") or {}
                try:
                    decimal_price = float(purchase_price.get("decimalPrice"))
                except (TypeError, ValueError):
                    continue
                effective_at = purchase_time(purchase.get("purchaseStateEffectiveDate"))
                if (
                    purchase.get("purchaseType") == "Claim"
                    and decimal_price == 0
                    and effective_at is not None
                    and effective_at <= now
                ):
                    current_claims.append((index, purchase))
            if not current_claims:
                continue

            claim_index, claim_entry = current_claims[-1]
            claim_discount = claim_entry.get("discount") or {}
            end_value = claim_discount.get("discountEndDate")
            if not end_value:
                for next_purchase in purchases[claim_index + 1:]:
                    if not isinstance(next_purchase, dict):
                        continue
                    next_effective = next_purchase.get("purchaseStateEffectiveDate")
                    next_at = purchase_time(next_effective)
                    if next_at is not None and next_at > now:
                        end_value = next_effective
                        break
            if not end_value:
                log.info("Epic %s mobile: %s has no claim end time; skipping", platform, content.get("title"))
                continue

            original_price = 0.0
            original_display = claim_discount.get("originalPriceDisplay")
            if original_display:
                try:
                    original_price = float(re.sub(r"[^0-9.]", "", str(original_display)))
                except ValueError:
                    original_price = 0.0
            if original_price <= 0:
                for purchase in purchases:
                    if not isinstance(purchase, dict) or purchase is claim_entry:
                        continue
                    purchase_price = purchase.get("price") or {}
                    try:
                        candidate = float(purchase_price.get("decimalPrice"))
                    except (TypeError, ValueError):
                        continue
                    if candidate > 0:
                        original_price = candidate
                        break
            if original_price <= 0:
                log.info("Epic %s mobile: %s has no positive regular price; skipping", platform, content.get("title"))
                continue

            start_value = claim_entry.get("purchaseStateEffectiveDate")
            media = content.get("media") or {}
            if not isinstance(media, dict):
                media = {}
            media_item = media.get("card16x9") or media.get("coverImage") or {}
            image_url = media_item.get("imageSrc") if isinstance(media_item, dict) else None

            title = content.get("title") or "Unknown"
            page_url = f"https://store.epicgames.com/en-US/p/{slug}"
            description = content.get("description") or content.get("shortDescription") or ""
            identifier = (
                content.get("catalogItemId") or offer.get("offerId")
                or content.get("id") or content.get("productId")
                or content.get("namespace") or slug
            )

            normalized.append({
                "title": str(title),
                "description": str(description),
                "platform": f"Epic ({platform.title()})",
                "game_id": str(identifier),
                "id": str(identifier),
                "is_free": True,
                "original_price": original_price,
                "final_price": 0,
                "discount_price": 0,
                "start_date": start_value,
                "end_date": end_value,
                "game_url": page_url,
                "image_url": valid_http_url(image_url),
                "type": "BASE_GAME",
                "offer_type": "BASE_GAME",
                "source": "Epic mobile storefront",
            })

    log.info(
        "Epic %s mobile storefront returned %s sections; %s active free offers qualified",
        platform, len(sections), len(normalized),
    )
    return normalized

def fetch_epic_mobile_games(session: requests.Session) -> tuple[list[dict[str, Any]], bool]:
    """Fetch Android and iOS independently; report partial mobile coverage."""
    games: list[dict[str, Any]] = []
    seen: set[str] = set()
    complete = True
    for platform in ("android", "ios"):
        try:
            platform_games = fetch_epic_mobile_platform(session, platform)
        except Exception:
            complete = False
            log.exception("Epic %s mobile feed could not be fetched; mobile coverage is PARTIAL", platform)
            continue

        log.info("Epic %s mobile feed found %s qualifying giveaways", platform, len(platform_games))
        for game in platform_games:
            key = game_key(game)
            if key in seen:
                continue
            seen.add(key)
            games.append(game)
    return games, complete


def fetch_all_games(api_key: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Fetch independently paginated store feeds and merge them before filtering."""
    platforms: tuple[str, ...] = ("Epic", "Steam", "PlayStation", "Xbox")
    all_games: list[dict[str, Any]] = []
    seen_global: set[str] = set()
    all_complete = True

    with requests.Session() as session:
        for platform in platforms:
            games, complete = _fetch_platform_games(session, api_key, platform)
            all_complete = all_complete and complete

            added = 0
            for game in games:
                key = game_key(game)
                if key in seen_global:
                    continue
                seen_global.add(key)
                all_games.append(game)
                added += 1
            log.info(
                "Merged %s feed: %s records received, %s unique records added",
                platform, len(games), added,
            )

        mobile_games, mobile_complete = fetch_epic_mobile_games(session)
        all_complete = all_complete and mobile_complete
        mobile_added = 0
        for game in mobile_games:
            key = game_key(game)
            if key in seen_global:
                continue
            seen_global.add(key)
            all_games.append(game)
            mobile_added += 1
        log.info(
            "Merged Epic mobile feeds: %s qualified records, %s unique records added",
            len(mobile_games), mobile_added,
        )

    log.info(
        "Combined catalogue: %s unique API records; aggregate scan_complete=%s",
        len(all_games), all_complete,
    )
    return all_games, all_complete


def _offer_start_value(game: dict[str, Any]) -> Any:
    """Return the best available timestamp identifying this specific giveaway."""
    return (
        game.get("start_date") or game.get("startDate")
        or game.get("offer_start_date") or game.get("offerStartDate")
        or game.get("promotion_start_date") or game.get("promotionStartDate")
        or game.get("free_from") or game.get("freeFrom")
        or game.get("offer_started_at") or game.get("starts_at")
        or game.get("created_at") or game.get("createdAt")
    )


def game_key(game: dict[str, Any]) -> str:
    """Identify a promotion by platform, game and offer start time.

    Records without a parseable offer-start timestamp fall back to the
    platform-plus-game key, preserving duplicate protection for incomplete API
    records.
    """
    platform = str(game.get("platform") or "Unknown").strip().casefold()
    identifier = str(game.get("game_id") or game.get("id") or "").strip()
    if not identifier:
        identifier = re.sub(r"\s+", " ", str(game.get("title") or "").strip()).casefold()
    if not identifier:
        raise ValueError("Game record has no usable game_id, id, or title")

    start_value = _offer_start_value(game)
    start_at = _parse_offer_datetime(start_value)
    if start_at is not None:
        offer_start = start_at.isoformat(timespec="seconds").replace("+00:00", "Z")
        return f"{platform}:{identifier}:offer:{offer_start}"

    return f"{platform}:{identifier}"


def legacy_game_key(game: dict[str, Any]) -> str:
    """Return the pre-offer-tracking key for existing state migration."""
    platform = str(game.get("platform") or "Unknown").strip().casefold()
    identifier = str(game.get("game_id") or game.get("id") or "").strip()
    if not identifier:
        identifier = re.sub(r"\s+", " ", str(game.get("title") or "").strip()).casefold()
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

            if records and not valid_records:
                # Ignore malformed entries and continue. Do not let an invalid
                # record prevent discovery of later valid records or the empty
                # end marker. The overall page limit is the loop safety guard.
                log.warning("Ignoring %s malformed records on page %s.", len(records), page)
                continue

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
    """Only show the platform as metadata; suppress pricing/category noise."""
    if enabled("platform") and game.get("platform"):
        return [f"🎮 <b>Platform:</b> {safe_text(game['platform'])}"]
    return []


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


def _caption_blocks(game: dict[str, Any]) -> tuple[str, str, str]:
    """Return title, description and trailing fixed blocks in display order."""
    title = f"<b>{safe_text(game.get('title') or 'Untitled game')}</b>"
    description = safe_text(str(game.get("description") or "").strip()) if enabled("description") else ""

    blocks: list[str] = []
    platform = formatted_optional_fields(game)
    if platform:
        blocks.append("\n".join(platform))

    game_url = valid_http_url(game.get("game_url"))
    if game_url:
        blocks.append(f'🔗 <a href="{html.escape(game_url, quote=True)}">Get the game</a>')

    end_date = (
        game.get("end_date") or game.get("endDate")
        or game.get("offer_end_date") or game.get("offerEndDate")
        or game.get("free_until") or game.get("freeUntil")
        or game.get("claim_end_date") or game.get("claimEndDate")
        or game.get("promotion_end_date") or game.get("promotionEndDate")
        or game.get("offer_expires_at") or game.get("expires_at")
        or game.get("ends_at")
    )
    if end_date:
        blocks.append(f"⏳ <b>Free until:</b> {safe_text(end_date)}")

    footer = footer_lines()
    if footer:
        blocks.append("\n".join(footer))
    return title, description, "\n\n".join(blocks)


def fixed_caption_parts(game: dict[str, Any]) -> tuple[str, str]:
    title, description, trailing = _caption_blocks(game)
    fixed = title + ("\n\n" + trailing if trailing else "")
    return fixed, description


def visible_text_length(text: str) -> int:
    """Count visible text in HTML-formatted content using Telegram UTF-16 units."""
    without_tags = re.sub(r"<[^>]*>", "", text)
    visible = html.unescape(without_tags)
    return len(visible.encode("utf-16-le")) // 2


def trim_description_to_fit(description: str, fixed_caption: str, limit: int) -> str:
    """Keep complete sentences within the rendered character budget."""
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
    title, description, trailing = _caption_blocks(game)
    fixed = title + ("\n\n" + trailing if trailing else "")
    shortened = trim_description_to_fit(description, fixed, limit)
    if shortened:
        result = title + "\n\n" + shortened
        if trailing:
            result += "\n\n" + trailing
        return result
    return fixed


async def send_one(client: TelegramClient, channel: str, game: dict[str, Any]) -> None:
    title = str(game.get("title") or "Untitled game")
    image_url = valid_http_url(game.get("image_url")) if enabled("image") else None

    if image_url:
        caption = compose_message(game, MAX_PHOTO_CAPTION)
        if visible_text_length(caption) > MAX_PHOTO_CAPTION:
            raise ValueError(f"Photo caption for {title} exceeds {MAX_PHOTO_CAPTION} Telegram UTF-16 characters")
        try:
            # Only fall back when the photo operation clearly failed before
            # delivery. Retrying as text after an ambiguous network error can
            # create a duplicate post if Telegram accepted the original upload.
            await client.send_file(channel, image_url, caption=caption, parse_mode="html", force_document=False)
            return
        except FloodWaitError:
            raise
        except (ValueError, TypeError) as exc:
            log.warning("Photo payload rejected for %s (%s); falling back to text", title, exc)

    message = compose_message(game, MAX_TEXT_MESSAGE)
    if visible_text_length(message) > MAX_TEXT_MESSAGE:
        raise ValueError(f"Text message for {title} exceeds Telegram's message limit after trimming")
    await client.send_message(channel, message, parse_mode="html", link_preview=False)


def _normalised_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _parse_offer_datetime(value: Any):
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
            return parsed
        except (OverflowError, OSError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def eligibility_rejection_reason(game: dict[str, Any]) -> str:
    """Explain the first reason an API record is excluded."""
    if not isinstance(game, dict):
        return "malformed_record"

    is_free_value = game.get("is_free")
    is_free = (
        is_free_value
        if isinstance(is_free_value, bool)
        else str(is_free_value or "").strip().casefold() in {"1", "true", "yes", "y"}
    )
    if not is_free:
        return "is_free_false_or_missing"

    end_value = (
        game.get("end_date") or game.get("endDate")
        or game.get("offer_end_date") or game.get("offerEndDate")
        or game.get("free_until") or game.get("freeUntil")
        or game.get("claim_end_date") or game.get("claimEndDate")
        or game.get("promotion_end_date") or game.get("promotionEndDate")
        or game.get("offer_expires_at") or game.get("expires_at")
        or game.get("ends_at")
    )
    end_at = _parse_offer_datetime(end_value)
    from datetime import datetime, timezone
    if end_at is None:
        return "missing_or_unparseable_expiry"
    now_at = datetime.now(timezone.utc)
    if end_at <= now_at:
        return "offer_expired"

    original = (
        game.get("original_price") or game.get("originalPrice")
        or game.get("regular_price") or game.get("regularPrice")
        or game.get("retail_price") or game.get("retailPrice")
    )
    try:
        original_amount = float(re.sub(r"[^0-9.]", "", str(original or "")))
    except ValueError:
        original_amount = 0.0
    if original_amount <= 0:
        return "missing_positive_original_price"

    def flatten(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(flatten(v) for v in value.values())
        if isinstance(value, (list, tuple, set)):
            return " ".join(flatten(v) for v in value)
        return _normalised_text(value)

    searchable = " ".join(
        flatten(game.get(field))
        for field in (
            "type", "offer_type", "offerType", "access_type", "entitlement_type",
            "subscription", "subscription_name", "source", "availability_type",
            "included_with", "included_in", "is_subscription", "description",
        )
    )
    excluded_phrases = (
        "game pass", "xbox game pass", "pc game pass", "playstation plus",
        "ps plus", "subscription", "included with", "included in pass",
        "game catalog", "game catalogue", "permanently free",
    )
    if any(_normalised_text(term) in searchable for term in excluded_phrases):
        return "subscription_or_permanent_free_marker"
    if game.get("is_subscription") is True or game.get("subscription_included") is True:
        return "subscription_marker"
    has_subscription_marker = any(
        field in game
        for field in ("subscription", "subscription_name", "is_subscription", "subscription_included")
    )
    if has_subscription_marker and any(
        game.get(field) not in (None, "", False, 0)
        for field in ("subscription", "subscription_name")
    ):
        return "subscription_metadata"

    classification = " ".join(
        flatten(game.get(field))
        for field in ("category", "type", "offer_type", "offerType", "pricing_type", "availability_type", "description")
    )
    permanent_free_markers = ("free to play", "free to start", "free2play", "f2p", "permanently free")
    if any(_normalised_text(term) in classification for term in permanent_free_markers):
        return "free_to_play_marker"

    offer_type = _normalised_text(game.get("type") or game.get("offer_type") or game.get("offerType"))
    if any(term in offer_type for term in ("dlc", "loot", "beta", "in game")):
        return "non_standalone_game_offer"

    return "eligible"


def is_eligible_temporary_offer(game: dict[str, Any], now=None) -> bool:
    """Only admit active, free-to-keep offers, never permanent F2P or subscriptions.

    FreeGameRadar documents /developer/free as the active 100%-free giveaways
    feed. The docs list no offer-type query parameter, so page/limit remain the
    only pagination parameters. This gate uses the returned record fields.
    """
    from datetime import datetime, timezone

    if not isinstance(game, dict):
        return False
    is_free_value = game.get("is_free")
    if isinstance(is_free_value, bool):
        is_free = is_free_value
    else:
        is_free = str(is_free_value or "").strip().casefold() in {"1", "true", "yes", "y"}
    if not is_free:
        return False

    end_value = (
        game.get("end_date") or game.get("endDate")
        or game.get("offer_end_date") or game.get("offerEndDate")
        or game.get("free_until") or game.get("freeUntil")
        or game.get("claim_end_date") or game.get("claimEndDate")
        or game.get("promotion_end_date") or game.get("promotionEndDate")
        or game.get("offer_expires_at") or game.get("expires_at")
        or game.get("ends_at")
    )
    end_at = _parse_offer_datetime(end_value)
    now_at = now or datetime.now(timezone.utc)
    if now_at.tzinfo is None:
        now_at = now_at.replace(tzinfo=timezone.utc)
    if end_at is None or end_at <= now_at.astimezone(timezone.utc):
        return False

    # A normally paid game must have a positive regular/retail value.
    original = (
        game.get("original_price") or game.get("originalPrice")
        or game.get("regular_price") or game.get("regularPrice")
        or game.get("retail_price") or game.get("retailPrice")
    )
    try:
        original_amount = float(re.sub(r"[^0-9.]", "", str(original or "")))
    except ValueError:
        original_amount = 0.0
    if original_amount <= 0:
        return False

    def flatten(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(flatten(v) for v in value.values())
        if isinstance(value, (list, tuple, set)):
            return " ".join(flatten(v) for v in value)
        return _normalised_text(value)

    searchable = " ".join(
        flatten(game.get(field))
        for field in (
            "type", "offer_type", "offerType", "access_type", "entitlement_type",
            "subscription", "subscription_name", "source", "availability_type",
            "included_with", "included_in", "is_subscription", "description",
        )
    )
    excluded_phrases = (
        "game pass", "xbox game pass", "pc game pass", "playstation plus",
        "ps plus", "subscription", "included with", "included in pass",
        "game catalog", "game catalogue", "permanently free",
    )
    has_subscription_marker = any(
        field in game
        for field in ("subscription", "subscription_name", "is_subscription", "subscription_included")
    )
    # Epic mobile storefront records are normalized from a distinct mobile
    # endpoint. Do not mistake the descriptive source label for subscription
    # access; all other subscription markers and phrases remain excluded.
    is_epic_mobile_source = _normalised_text(game.get("source")) == "epic mobile storefront"
    if any(
        _normalised_text(term) in searchable
        for term in excluded_phrases
        if not (is_epic_mobile_source and _normalised_text(term) == "subscription")
    ):
        return False
    if game.get("is_subscription") is True or game.get("subscription_included") is True:
        return False
    if has_subscription_marker and any(
        game.get(field) not in (None, "", False, 0)
        for field in ("subscription", "subscription_name")
    ):
        return False

    # A record explicitly marked as a permanently/free-to-play title is not a
    # giveaway, even if the source happens to expose an unrelated end date.
    classification = " ".join(
        flatten(game.get(field))
        for field in ("category", "type", "offer_type", "offerType", "pricing_type", "availability_type", "description")
    )
    permanent_free_markers = ("free to play", "free to start", "free2play", "f2p", "permanently free")
    if any(_normalised_text(term) in classification for term in permanent_free_markers):
        return False

    offer_type = _normalised_text(game.get("type") or game.get("offer_type") or game.get("offerType"))
    if any(term in offer_type for term in ("dlc", "loot", "beta", "in game")):
        return False
    return True


async def run_notifier() -> None:
    api_key = os.getenv("FREEGAMERADAR_API_KEY", "").strip() or None
    api_id_text = required_env("TELEGRAM_API_ID")
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be a numeric Telegram API ID") from exc
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
    rejection_counts: dict[str, int] = {}
    for game in games:
        if is_eligible_temporary_offer(game):
            current_games[game_key(game)] = game
        else:
            reason = eligibility_rejection_reason(game)
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    log.info("Eligibility rejection breakdown: %s", json.dumps(rejection_counts, sort_keys=True))
    log.info(
        "Eligibility filter retained %s of %s API records; only active temporary paid-game offers qualify",
        len(current_games),
        len(games),
    )

    first_run = not STATE_PATH.exists()
    seen = load_state()

    if first_run and not config.SEND_EXISTING_ON_FIRST_RUN:
        if not scan_complete:
            raise RuntimeError(
                "First-run catalogue scan was partial; no baseline was written. Retry on the next run."
            )
        save_state(set(current_games))
        log.info("Initialized duplicate state with %s listings; first-run posting disabled", len(current_games))
        return

    # Offer-specific keys are authoritative. The old game-only key is kept
    # in state for history, but must not suppress a new offer with a new start
    # timestamp. The previously posted Flotsam offer is already represented by
    # its exact offer key in seen_games.json.
    new_games = [
        (key, game)
        for key, game in current_games.items()
        if key not in seen
    ]
    log.info(
        "Fetched %s API records; %s unique free offers; %s not yet posted; scan_complete=%s",
        len(games),
        len(current_games),
        len(new_games),
        scan_complete,
    )

    if not new_games:
        if first_run and scan_complete:
            save_state(set(current_games))
        if not scan_complete:
            # The source returned a repeated page, but every eligible offer we
            # actually received is already in persistent state. There is
            # nothing safe to deliver or mark seen, so this is a successful
            # no-op with an explicit warning, not a notifier failure.
            log.warning(
                "FreeGameRadar pagination was partial, but no unseen eligible offers were found "
                "in the records returned. State was left unchanged; the next scheduled run will retry."
            )
            return
        log.info("No new free games")
        return

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
            # A repeated page means discovery was incomplete, but does not undo
            # successful Telegram deliveries. Keep the run successful and make
            # the limitation visible as a warning so Actions reflects delivery.
            log.warning(
                "FreeGameRadar pagination was partial. Successfully delivered %s new listing(s); "
                "remaining pages will be retried on the next scheduled run.",
                len(new_games) - len(failed_titles),
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
