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
    # Some wrappers put page fields in meta.pagination rather than the
    # top-level metadata object.
    nested = meta.get("pagination") or meta.get("page") or {}
    if isinstance(nested, dict):
        meta = {**meta, **nested}
    return [x for x in records if isinstance(x, dict)], meta


def _api_limit_for_key(api_key: str | None) -> int:
    """Use the largest documented page size for the configured FGR tier.

    Anonymous: 25. Standard key: 100. Pro key: 200. Pro users can set
    FREEGAMERADAR_API_LIMIT=200; the default remains the Standard tier.
    """
    default_limit = 100 if api_key else 25
    override = os.getenv("FREEGAMERADAR_API_LIMIT", "").strip()
    if not override:
        return default_limit
    try:
        requested = int(override)
    except ValueError:
        log.warning("Ignoring invalid FREEGAMERADAR_API_LIMIT=%r; using %s", override, default_limit)
        return default_limit
    allowed = (25, 100, 200)
    if requested not in allowed:
        log.warning("Ignoring unsupported FREEGAMERADAR_API_LIMIT=%s; use 25, 100 or 200", requested)
        return default_limit
    if not api_key and requested > 25:
        log.warning("No FreeGameRadar key is configured; limiting page size to the anonymous maximum of 25")
        return 25
    return requested


def fetch_page(
    session: requests.Session,
    page: int,
    api_key: str | None,
    platform: str | None = None,
    limit: int | None = None,
    endpoint: str = API_URL,
    search: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "Telegram-Free-Games-Notifier/1.0",
    }
    if api_key:
        headers["X-API-Key"] = api_key

    effective_limit = min(max(int(limit or _api_limit_for_key(api_key)), 1), 200)
    params: dict[str, Any] = {
        "page": page,
        "limit": effective_limit,
        # FGR's documented page parameter should select a different result
        # window. The cache buster prevents a CDN from replaying page 1 for
        # later page URLs when the cache key is configured incorrectly.
        "_cb": int(time.time() * 1000),
    }
    if platform:
        params["platform"] = platform
    if search:
        params["search"] = search

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(endpoint, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            if attempt == 1:
                log.info(
                    "FreeGameRadar request: %s [page=%s, limit=%s, platform=%s, search=%s]",
                    response.url, page, effective_limit, platform or "all", search or "-",
                )
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
            for total_key in (
                "totalPages", "total_pages", "pages", "lastPage", "last_page",
                "last_page_number", "page_count",
            ):
                if meta.get(total_key) is not None:
                    try:
                        expected_total_pages = int(meta[total_key])
                    except (TypeError, ValueError):
                        pass
                    break
            for total_key in (
                "total", "totalItems", "total_items", "total_records",
                "totalResults", "total_results", "totalCount", "total_count",
            ):
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

        if page == 1:
            sample = records[0]
            log.info(
                "%s first-page schema: %s",
                label, ", ".join(sorted(str(key) for key in sample.keys())),
            )
            log.info(
                "%s first-page example: title=%r, is_free=%r, expiry_fields=%s, price_fields=%s",
                label,
                sample.get("title"),
                sample.get("is_free"),
                {
                    key: sample.get(key)
                    for key in (
                        "end_date", "endDate", "offer_end_date", "offerEndDate",
                        "free_until", "expires_at", "expiration_date",
                    )
                    if sample.get(key) not in (None, "")
                },
                {
                    key: sample.get(key)
                    for key in (
                        "original_price", "originalPrice", "regular_price",
                        "retail_price", "worth", "price",
                    )
                    if sample.get(key) not in (None, "", 0)
                },
            )

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

        if added == 0:
            log.error(
                "%s page %s returned %s rows but none were new; stopping incomplete instead of looping",
                label, page, len(records),
            )
            break

        if expected_total_pages is not None and page >= expected_total_pages:
            scan_complete = True
            break
        if expected_total is not None and len(records_all) >= expected_total:
            scan_complete = True
            break
        if len(records) < limit:
            # FreeGameRadar can return non-terminal short pages. Continue to
            # the next numbered page; only an empty page or authoritative
            # pagination metadata confirms the end of the feed.
            log.info(
                "%s page %s was short (%s/%s); checking page %s for continuation",
                label, page, len(records), limit, page + 1,
            )

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
        session, 1, api_key, platform, limit=_api_limit_for_key(api_key),
        endpoint=GAMES_API_URL, search=search,
    )
    records, _ = _api_response_data(payload, label)
    log.info("FreeGameRadar /games %s returned %s records", label, len(records))
    return records


def merge_game_metadata(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    """Fill display metadata only; catalogue results never create offer state."""
    merged = dict(primary)
    for field in ("description", "image_url", "game_url"):
        if merged.get(field) in (None, "") and secondary.get(field) not in (None, ""):
            merged[field] = secondary[field]
    return merged


def _canonical_game_title(value: Any) -> str:
    """Normalize provider suffixes and platform labels to one base game title."""
    title = _normalised_text(value)
    # Repeatedly remove provider/platform descriptions, e.g.
    # "Knightica (Epic Games) Giveaway" -> "knightica".
    suffix_patterns = (
        r"\\s+mobile\\s+giveaway$",
        r"\\s+epic\\s+games\\s+giveaway$",
        r"\\s+epic\\s+giveaway$",
        r"\\s+giveaway$",
        r"\\s+mobile$",
        r"\\s+android$",
        r"\\s+ios$",
        r"\\s+epic\\s+games$",
        r"\\s+epic$",
    )
    changed = True
    while changed:
        changed = False
        for pattern in suffix_patterns:
            normalized = re.sub(pattern, "", title).strip()
            if normalized != title:
                title = normalized
                changed = True
    return title



def _platform_family(value: Any) -> str:
    text = _normalised_text(value)
    if "epic" in text or "egs" in text:
        return "epic"
    if "steam" in text:
        return "steam"
    if "playstation" in text or "ps4" in text or "ps5" in text:
        return "playstation"
    if "xbox" in text:
        return "xbox"
    if any(term in text.split() for term in ("android", "ios")) or "mobile" in text:
        return "mobile"
    return text


def _same_game_listing(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Require exact normalized title and compatible store families."""
    left_title = _canonical_game_title(left.get("title"))
    right_title = _canonical_game_title(right.get("title"))
    if not left_title or left_title != right_title:
        return False
    left_platform = _platform_family(left.get("platform"))
    right_platform = _platform_family(right.get("platform"))
    return not left_platform or not right_platform or left_platform == right_platform



GAMERPOWER_API_URL = "https://www.gamerpower.com/api/giveaways"


def fetch_gamerpower_mobile_games(
    session: requests.Session,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch active Android/iOS full-game giveaways and report feed health."""
    discovered: list[dict[str, Any]] = []
    all_complete = True
    by_giveaway: dict[tuple[str, str], dict[str, Any]] = {}

    for requested_platform in ("android", "ios"):
        params = {
            "platform": requested_platform,
            "type": "game",
            "sort-by": "date",
        }
        response = None
        empty_result = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = session.get(
                    GAMERPOWER_API_URL,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Telegram-Free-Games-Notifier/1.0",
                        "Cache-Control": "no-cache",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 201:
                    # GamerPower explicitly uses 201 when no active giveaways match.
                    empty_result = True
                    response = None
                    break
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        wait = min(max(int(retry_after), 1), 60) if retry_after else min(2 ** attempt, 30)
                    except ValueError:
                        wait = min(2 ** attempt, 30)
                    if attempt == MAX_RETRIES:
                        response.raise_for_status()
                    log.warning(
                        "GamerPower %s returned HTTP %s; retrying in %ss",
                        requested_platform, response.status_code, wait,
                    )
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    response = None
                    all_complete = False
                    log.exception(
                        "GamerPower %s request failed after %s attempts",
                        requested_platform, MAX_RETRIES,
                    )
                    break
                wait = min(2 ** attempt, 30)
                log.warning(
                    "GamerPower %s request failed (%s); retrying in %ss",
                    requested_platform, exc, wait,
                )
                time.sleep(wait)

        if empty_result:
            log.info("GamerPower %s: no active full-game giveaways (HTTP 201)", requested_platform)
            continue
        if response is None:
            all_complete = False
            log.warning("GamerPower %s feed unavailable; mobile source coverage is partial", requested_platform)
            continue

        try:
            payload = response.json()
        except ValueError:
            all_complete = False
            log.exception("GamerPower %s returned invalid JSON", requested_platform)
            continue
        if not isinstance(payload, list):
            all_complete = False
            log.error(
                "GamerPower %s returned unexpected payload type %s",
                requested_platform, type(payload).__name__,
            )
            continue

        matched = 0
        for item in payload:
            if not isinstance(item, dict) or _normalised_text(item.get("type")) != "game":
                continue

            title = str(item.get("title") or "").strip()
            if not title:
                continue

            platform_labels = _normalised_text(item.get("platforms"))
            available_platforms = [
                label for label in ("android", "ios")
                if label in platform_labels.split()
            ]
            if requested_platform not in available_platforms:
                continue

            end_value = item.get("end_date")
            end_at = _parse_offer_datetime(end_value, date_only_end=True)
            from datetime import datetime, timezone
            if end_at is None or end_at <= datetime.now(timezone.utc):
                log.info(
                    "GamerPower skipped %s: missing or expired end_date=%r",
                    title, end_value,
                )
                continue

            worth = item.get("worth")
            normal_price = _parse_price_display(worth)
            if normal_price <= 0:
                log.info("GamerPower skipped %s: no positive worth=%r", title, worth)
                continue

            claim_url = valid_http_url(
                item.get("open_giveaway_url")
                or item.get("open_giveaway")
                or item.get("gamerpower_url")
            )
            if not claim_url:
                log.info("GamerPower skipped %s: no valid claim URL", title)
                continue

            giveaway_id = str(item.get("id") or _normalised_text(title))
            published = str(item.get("published_date") or "").strip()
            stable_time = published or end_at.isoformat()
            stable_id = (giveaway_id, stable_time)

            platform_label = ", ".join(label.title() for label in available_platforms)
            details = " ".join((
                title,
                str(item.get("description") or ""),
                str(item.get("instructions") or ""),
                str(item.get("gamerpower_url") or ""),
            ))
            if "epic" in _normalised_text(details):
                display_platform = f"Epic ({platform_label})"
            else:
                display_platform = platform_label

            existing = by_giveaway.get(stable_id)
            if existing:
                # The same GamerPower record can be returned for both Android
                # and iOS filters. Preserve the union instead of posting it twice.
                labels = [x.strip() for x in str(existing.get("platform") or "").split(",")]
                labels.extend(x.strip() for x in display_platform.split(","))
                existing["platform"] = ", ".join(dict.fromkeys(x for x in labels if x))
                continue

            game = {
                "title": title,
                "description": str(item.get("description") or ""),
                "platform": display_platform,
                "game_id": giveaway_id,
                "id": giveaway_id,
                "is_free": True,
                "original_price": normal_price,
                "worth": worth,
                "final_price": 0,
                "discount_price": 0,
                "start_date": published or None,
                "end_date": end_value,
                "game_url": claim_url,
                "image_url": valid_http_url(item.get("image") or item.get("thumbnail")),
                "type": "BASE_GAME",
                "offer_type": "BASE_GAME",
                "source": "GamerPower mobile full-game giveaway",
                "source_url": "https://www.gamerpower.com/",
            }
            by_giveaway[stable_id] = game
            matched += 1

        log.info(
            "GamerPower %s: HTTP %s, %s records received, %s active full-game giveaways qualified",
            requested_platform, response.status_code, len(payload), matched,
        )

    discovered.extend(by_giveaway.values())
    return discovered, all_complete


MOBILE_EPIC_API_URL = "https://egs-platform-service.store.epicgames.com/api/v2/public/discover/home"


def _mobile_price_amount(value: Any) -> float:
    """Normalize Epic mobile price values (usually integer minor units)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    # Epic storefront payloads normally express prices as cents.
    return amount / 100 if amount > 100 else amount


def _parse_price_display(value: Any) -> float:
    """Parse currency display values without truncating thousands separators."""
    if value is None:
        return 0.0
    raw = re.sub(r"[^0-9,.]", "", str(value).strip())
    if not raw:
        return 0.0

    if "," in raw and "." in raw:
        # The last separator is the decimal mark; the other is a group mark.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[-1]
        if len(tail) in (1, 2):
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")

    try:
        return float(raw)
    except ValueError:
        return 0.0



def fetch_epic_mobile_platform(
    session: requests.Session,
    platform: str,
) -> list[dict[str, Any]]:
    """Fetch current Epic mobile freeGame module (Android or iOS)."""
    params = {
        "count": 10,
        "country": os.getenv("EPIC_COUNTRY", "IN"),
        "locale": os.getenv("EPIC_LOCALE", "en-IN"),
        "platform": platform,
        "start": 0,
        "store": "EGS",
        "cb": int(time.time() * 1000),
    }
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "UEGameRequest/1.0 epicgames",
    }

    payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(MOBILE_EPIC_API_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            candidate = response.json()
            if not isinstance(candidate, dict) or not isinstance(candidate.get("data"), list):
                raise RuntimeError(f"Epic {platform} mobile response has no data array")
            payload = candidate
            break
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = min(2 ** attempt, 30)
            log.warning("Epic %s mobile request failed (%s); retrying in %ss", platform, exc, wait)
            time.sleep(wait)

    if payload is None:
        raise RuntimeError(f"Could not fetch Epic {platform} mobile offers after {MAX_RETRIES} attempts: {last_error}")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    sections = payload["data"]
    log.info(
        "Epic %s mobile API response: %s sections (%s bytes decoded)",
        platform, len(sections), len(response.content),
    )

    normalized: list[dict[str, Any]] = []
    free_sections = [section for section in sections if isinstance(section, dict) and section.get("type") == "freeGame"]
    log.info("Epic %s mobile: found %s freeGame sections", platform, len(free_sections))

    for section in free_sections:
        for offer in section.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            content = offer.get("content") or {}
            if not isinstance(content, dict):
                continue

            slug = ((content.get("mapping") or {}).get("slug") or "").strip()
            title = str(content.get("title") or "Unknown")
            if not slug:
                log.info("Epic %s mobile skipped %s: missing product slug", platform, title)
                continue

            purchases = content.get("purchase") or []
            if not isinstance(purchases, list):
                log.info("Epic %s mobile skipped %s: purchase field is not a list", platform, title)
                continue

            candidates: list[tuple[int, dict[str, Any]]] = []
            for index, purchase in enumerate(purchases):
                if not isinstance(purchase, dict) or purchase.get("purchaseType") != "Claim":
                    continue
                price_data = purchase.get("price") or {}
                try:
                    claim_price = float(price_data.get("decimalPrice"))
                except (TypeError, ValueError):
                    continue
                effective_at = _parse_offer_datetime(purchase.get("purchaseStateEffectiveDate"))
                if claim_price == 0 and effective_at is not None and effective_at <= now:
                    candidates.append((index, purchase))

            if not candidates:
                upcoming = sum(
                    1 for purchase in purchases
                    if isinstance(purchase, dict)
                    and purchase.get("purchaseType") == "Claim"
                    and _parse_offer_datetime(purchase.get("purchaseStateEffectiveDate")) is not None
                    and _parse_offer_datetime(purchase.get("purchaseStateEffectiveDate")) > now
                )
                log.info(
                    "Epic %s mobile skipped %s: no currently active zero-price Claim (purchase_entries=%s, future_claims=%s)",
                    platform, title, len(purchases), upcoming,
                )
                continue

            claim_index, claim = candidates[-1]
            discount = claim.get("discount") or {}
            start_value = claim.get("purchaseStateEffectiveDate")
            end_value = discount.get("discountEndDate")
            if not end_value:
                next_state = [
                    _parse_offer_datetime(p.get("purchaseStateEffectiveDate"))
                    for p in purchases[claim_index + 1:]
                    if isinstance(p, dict)
                ]
                next_dates = [d for d in next_state if d is not None and d > now]
                if next_dates:
                    end_value = min(next_dates).isoformat().replace("+00:00", "Z")
            end_at = _parse_offer_datetime(end_value, date_only_end=True)
            if end_at is None or end_at <= now:
                log.info("Epic %s mobile skipped %s: missing or expired Claim end time", platform, title)
                continue

            # Epic returns the normal retail value in the active Claim's
            # discount.originalPriceDisplay. This is the canonical field;
            # do not infer a retail price from another future/expired state.
            original_display = discount.get("originalPriceDisplay")
            original_price = _parse_price_display(original_display)
            if original_price <= 0:
                # Some Epic responses place the normal price on priceDisplay.
                original_price = _parse_price_display(claim.get("priceDisplay"))
            if original_price <= 0:
                log.info(
                    "Epic %s mobile skipped %s: no positive normal price in active Claim (originalPriceDisplay=%r, priceDisplay=%r)",
                    platform, title, original_display, claim.get("priceDisplay"),
                )
                continue

            media = content.get("media") or {}
            if not isinstance(media, dict):
                media = {}
            image_data = media.get("card16x9") or media.get("coverImage") or {}
            image_url = image_data.get("imageSrc") if isinstance(image_data, dict) else None
            identifier = (
                content.get("catalogItemId") or offer.get("offerId")
                or content.get("id") or content.get("productId") or slug
            )

            normalized.append({
                "title": title,
                "description": str(content.get("description") or content.get("shortDescription") or ""),
                "platform": f"Epic ({platform.title()})",
                "game_id": str(identifier),
                "id": str(identifier),
                "is_free": True,
                "original_price": original_price,
                "final_price": 0,
                "discount_price": 0,
                "start_date": start_value,
                "end_date": end_value,
                "game_url": f"https://store.epicgames.com/en-US/p/{slug}",
                "image_url": valid_http_url(image_url),
                "type": "BASE_GAME",
                "offer_type": "BASE_GAME",
                "source": "Epic mobile storefront",
            })
            log.info(
                "Epic %s mobile qualified %s: original=%s, starts=%s, ends=%s",
                platform, title, original_price, start_value, end_value,
            )

    log.info("Epic %s mobile: %s currently active paid-game offer(s) qualified", platform, len(normalized))
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


def merge_platform_labels(existing: Any, incoming: Any) -> str:
    """Combine duplicated mobile platform labels without repeating store names."""
    combined = " ".join((str(existing or ""), str(incoming or "")))
    normalized = _normalised_text(combined)
    mobile_platforms = [
        label for label in ("Android", "iOS")
        if label.casefold() in normalized.split()
    ]
    if mobile_platforms:
        if "epic" in normalized:
            return f"Epic ({', '.join(mobile_platforms)})"
        return ", ".join(mobile_platforms)

    values = []
    for value in (str(existing or "").strip(), str(incoming or "").strip()):
        if value and value not in values:
            values.append(value)
    return ", ".join(values)



def fetch_all_games(api_key: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Fetch every store independently, then mobile sources, preserving partial results."""
    platforms: tuple[str, ...] = ("Epic", "Steam", "PlayStation", "Xbox")
    all_games: list[dict[str, Any]] = []
    seen_global: dict[str, dict[str, Any]] = {}
    all_complete = True

    def merge_records(records: list[dict[str, Any]], label: str) -> int:
        added = 0
        for game in records:
            key = game_key(game)
            existing = seen_global.get(key)
            if existing is not None:
                existing["platform"] = merge_platform_labels(
                    existing.get("platform"), game.get("platform")
                )
                # Prefer the official Epic storefront title/description/image/link
                # over aggregator paraphrases for the one combined mobile post.
                existing_is_epic = "epic mobile storefront" in _normalised_text(existing.get("source"))
                incoming_is_epic = "epic mobile storefront" in _normalised_text(game.get("source"))
                if incoming_is_epic and not existing_is_epic:
                    for field in ("title", "description", "image_url", "game_url", "original_price", "source"):
                        if game.get(field) not in (None, ""):
                            existing[field] = game[field]
                else:
                    for field in ("description", "image_url", "game_url"):
                        if existing.get(field) in (None, "") and game.get(field):
                            existing[field] = game[field]
                log.info(
                    "Collapsed duplicate giveaway source into %s (incoming=%s)",
                    key, game.get("source", "unknown"),
                )
                continue
            seen_global[key] = game
            all_games.append(game)
            added += 1
        log.info("Merged %s: %s received, %s unique new records", label, len(records), added)
        return added

    with requests.Session() as session:
        # FreeGameRadar offers separate platform filtering. If one store's
        # page parameter is broken, continue fetching the remaining stores.
        for platform in platforms:
            try:
                records, complete = _fetch_platform_games(session, api_key, platform)
            except Exception:
                all_complete = False
                log.exception("%s FreeGameRadar feed failed; continuing with the other stores", platform)
                continue
            all_complete = all_complete and complete
            merge_records(records, f"{platform} FreeGameRadar feed")

        # Epic mobile is a separate API; PC freeGames/promotions does not include
        # all Android/iOS giveaway listings.
        try:
            mobile_games, mobile_complete = fetch_epic_mobile_games(session)
        except Exception:
            log.exception("Epic mobile feeds failed")
            mobile_games, mobile_complete = [], False
        all_complete = all_complete and mobile_complete
        merge_records(mobile_games, "Epic mobile feeds")

        # Independent active-giveaway aggregator. Query type=game to exclude
        # loot/DLC/beta, and query platforms separately as documented.
        try:
            gamerpower_games, gamerpower_complete = fetch_gamerpower_mobile_games(session)
        except Exception:
            log.exception("GamerPower mobile fallback failed")
            gamerpower_games, gamerpower_complete = [], False
        all_complete = all_complete and gamerpower_complete
        merge_records(gamerpower_games, "GamerPower mobile feed")

    log.info(
        "Combined catalogue: %s unique records; all queried sources complete=%s",
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
    """Use one title+expiry key for mobile giveaway campaigns across providers."""
    platform = str(game.get("platform") or "Unknown").strip().casefold()
    source = _normalised_text(game.get("source"))
    title_value = game.get("title")
    normalized_platform = _normalised_text(platform)
    is_mobile = (
        "mobile" in source
        or any(token in normalized_platform.split() for token in ("android", "ios", "mobile"))
        or any(token in _normalised_text(title_value).split() for token in ("android", "ios", "mobile"))
    )

    if is_mobile:
        identifier = _canonical_game_title(title_value)
        end_value = (
            game.get("end_date") or game.get("endDate")
            or game.get("offer_end_date") or game.get("offerEndDate")
            or game.get("free_until") or game.get("freeUntil")
            or game.get("expires_at") or game.get("ends_at")
        )
        end_at = _parse_offer_datetime(end_value, date_only_end=True)
        if end_at is not None:
            return f"mobile:{identifier}:until:{end_at.date().isoformat()}"
        start_at = _parse_offer_datetime(_offer_start_value(game))
        if start_at is not None:
            return f"mobile:{identifier}:offer:{start_at.isoformat(timespec='seconds').replace('+00:00', 'Z')}"
        return f"mobile:{identifier}"

    identifier = str(game.get("game_id") or game.get("id") or "").strip()
    if not identifier:
        identifier = _normalised_text(title_value)
    if not identifier:
        raise ValueError("Game record has no usable game_id, id, or title")
    start_at = _parse_offer_datetime(_offer_start_value(game))
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


def load_state() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        keys = data.get("seen", []) if isinstance(data, dict) else []
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ValueError("State file must contain a 'seen' array of strings")

        seen = set(keys)
        historical_mobile_keys = (
            "mobile:chants of sennaar mobile giveaway:until:2026-11-12",
            "mobile:chants of sennaar mobile:until:2026-11-12",
            "mobile:chants of sennaar:until:2026-11-12",
        )
        if any(key in seen for key in historical_mobile_keys):
            canonical_key = "mobile:chants of sennaar:until:2026-11-12"
            if canonical_key not in seen:
                seen.add(canonical_key)
                log.info(
                    "Migrated historical Chants of Sennaar mobile giveaway markers to the canonical campaign key"
                )
        return seen
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
        or game.get("expiration_date") or game.get("expiry_date")
        or game.get("expire_date") or game.get("offer_expires")
        or game.get("valid_until") or game.get("end_at")
        or game.get("ends_at")
    )
    if end_date:
        blocks.append(f"⏳ <b>Free until:</b> {format_offer_expiry(end_date)}")

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


def format_offer_expiry(value: Any) -> str:
    """Render an offer expiry as YYYY-MM-DD HH:MM UTC without ISO noise."""
    from datetime import datetime, timezone

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse_offer_datetime(value, date_only_end=True)

    if parsed is None:
        return safe_text(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _parse_offer_datetime(value: Any, date_only_end: bool = False):
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            # Some APIs return Unix milliseconds instead of seconds.
            timestamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None

    # A date-only end date represents the full calendar day, not midnight at
    # its beginning. Start dates keep the normal midnight interpretation.
    if re.fullmatch(r"\d{4}-\\d{2}-\\d{2}", raw):
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
            if date_only_end:
                parsed = parsed.replace(hour=23, minute=59, second=59)
        except ValueError:
            return None
    else:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y"):
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


def eligibility_rejection_reason(game: dict[str, Any], now=None) -> str:
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
        or game.get("expiration_date") or game.get("expiry_date")
        or game.get("expire_date") or game.get("offer_expires")
        or game.get("valid_until") or game.get("end_at")
        or game.get("ends_at")
    )
    end_at = _parse_offer_datetime(end_value, date_only_end=True)
    from datetime import datetime, timezone
    now_at = now or datetime.now(timezone.utc)
    if now_at.tzinfo is None:
        now_at = now_at.replace(tzinfo=timezone.utc)
    if end_at is None:
        return "missing_or_unparseable_expiry"
    if end_at <= now_at.astimezone(timezone.utc):
        return "offer_expired"

    original = (
        game.get("original_price") or game.get("originalPrice")
        or game.get("regular_price") or game.get("regularPrice")
        or game.get("retail_price") or game.get("retailPrice")
        or game.get("worth")
    )
    original_amount = _parse_price_display(original)
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
            "subscription", "subscription_name", "availability_type",
            "included_with", "included_in", "is_subscription", "description",
        )
    )
    excluded_phrases = (
        "game pass", "xbox game pass", "pc game pass", "playstation plus",
        "ps plus", "included with subscription", "included in subscription",
        "subscription required", "requires subscription", "subscription only",
        "subscription catalogue", "subscription catalog",
        "included with", "included in pass",
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

    offer_classification = " ".join(
        flatten(game.get(field))
        for field in ("category", "type", "offer_type", "offerType", "pricing_type", "availability_type")
    )
    if any(term in offer_classification for term in ("dlc", "loot", "beta", "in game")):
        return "non_standalone_game_offer"
    return "eligible"


def is_eligible_temporary_offer(game: dict[str, Any], now=None) -> bool:
    """Accept only active, time-limited, normally paid full games."""
    return eligibility_rejection_reason(game, now=now) == "eligible"


def _targeted_metadata_enrichment(
    session: requests.Session,
    api_key: str | None,
    games: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich only verified eligible candidates, with a small lookup budget."""
    enriched = list(games)
    targets = [
        game for game in enriched
        if is_eligible_temporary_offer(game)
        and str(game.get("title") or "").strip()
        and (
            not game.get("description")
            or not game.get("image_url")
            or not game.get("game_url")
        )
    ]
    if not targets:
        log.info("Metadata enrichment: no eligible listings need descriptive fields")
        return enriched

    max_lookups = 10
    if len(targets) > max_lookups:
        log.warning(
            "Metadata enrichment capped at %s lookups; %s additional eligible rows keep their original metadata",
            max_lookups, len(targets) - max_lookups,
        )

    searched: set[tuple[str, str]] = set()
    for game in targets[:max_lookups]:
        title = str(game.get("title") or "").strip()
        display_platform = str(game.get("platform") or "").strip()
        family = _platform_family(display_platform)
        api_platforms = {
            "epic": "Epic",
            "steam": "Steam",
            "playstation": "PlayStation",
            "xbox": "Xbox",
        }
        platform = api_platforms.get(family)
        if platform is None:
            # The FGR catalogue is a PC/console catalogue; don't issue
            # unsupported Android/iOS searches for mobile-only listings.
            continue

        lookup_key = (_canonical_game_title(title), family)
        if lookup_key in searched:
            continue
        searched.add(lookup_key)
        try:
            records = fetch_game_search(session, api_key, title, platform)
        except Exception:
            log.exception("Metadata lookup failed for %s; leaving source row unchanged", title)
            continue

        match = next((record for record in records if _same_game_listing(game, record)), None)
        if not match:
            log.info(
                "Metadata lookup for %s returned %s results but no exact title/store match",
                title, len(records),
            )
            continue

        before = dict(game)
        game.update(merge_game_metadata(game, match))
        filled = [
            field for field in ("description", "image_url", "game_url")
            if before.get(field) in (None, "") and game.get(field) not in (None, "")
        ]
        log.info(
            "Metadata lookup enriched %s for %s: %s",
            len(filled), title, ", ".join(filled) or "no missing fields matched",
        )

    return enriched



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

    # Enrich source rows through the documented /games?search= endpoint
    # before eligibility filtering. Catalogue fields cannot create or alter
    # promotion dates, price or is_free state.
    with requests.Session() as metadata_session:
        games = _targeted_metadata_enrichment(metadata_session, api_key, games)

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
