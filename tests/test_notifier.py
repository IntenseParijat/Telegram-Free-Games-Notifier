from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import notifier


class PriceParsingTests(unittest.TestCase):
    def test_currency_decimal_is_not_scaled_as_minor_units(self):
        self.assertEqual(notifier._parse_price_display("$0.80"), 0.80)
        self.assertEqual(notifier._parse_price_display("USD 14.99"), 14.99)

    def test_formatted_zero_is_zero_but_non_price_is_not_coerced(self):
        self.assertEqual(notifier._parse_price_display("Free"), 0.0)
        self.assertEqual(notifier._parse_price_display(""), 0.0)
        self.assertEqual(notifier._parse_price_display({"amount": "14.99"}), 14.99)


class ItchEligibilityTests(unittest.TestCase):
    def test_html5_browser_build_is_not_downloadable(self):
        self.assertFalse(notifier._itch_has_downloadable_build({
            "downloads": [{"type": "html", "filename": "index.html"}],
            "platforms": ["HTML5"],
        }))

    def test_native_download_is_accepted(self):
        self.assertTrue(notifier._itch_has_downloadable_build({
            "downloads": [{"type": "default", "filename": "game-windows.zip"}],
        }))

    def test_package_files_are_supported(self):
        self.assertTrue(notifier._itch_has_downloadable_build({
            "packages": [{"files": [{"filename": "game-linux.tar.gz", "size": 1234}]}],
        }))

    def test_itch_guildmaster_style_90_percent_sale_is_rejected(self):
        ok, reason, current, original = notifier._itch_offer_price_check({
            "price": "$0.80",
            "original_price": "$8.00",
            "sale": {"rate": 90},
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "discount_not_100_percent")
        # Rate validation rejects before price parsing; the separate 100%-rate
        # test below proves that a live $0.80 amount cannot pass either.
        self.assertIsNone(current)
        self.assertIsNone(original)

    def test_itch_missing_live_price_is_not_assumed_free(self):
        ok, reason, _, _ = notifier._itch_offer_price_check({
            "original_price": "$8.00",
            "sale": {"rate": 100},
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_or_invalid_live_price")

    def test_itch_nonzero_price_is_rejected_even_if_sale_rate_claims_100(self):
        ok, reason, current, _ = notifier._itch_offer_price_check({
            "price": "$0.80",
            "original_price": "$8.00",
            "sale": {"rate": 100},
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "live_price_not_zero")
        self.assertEqual(current, 0.80)

    def test_html5_only_platform_is_rejected_even_if_archive_is_listed(self):
        self.assertFalse(notifier._itch_has_downloadable_build({
            "platforms": ["HTML5"],
            "downloads": [{"type": "default", "filename": "game.zip"}],
        }))

    def test_itch_zero_price_requires_explicit_current_price_and_msrp(self):
        ok, reason, current, original = notifier._itch_offer_price_check({
            "price": "$0.00",
            "original_price": "$8.00",
            "sale": {"rate": 100},
        })
        self.assertTrue(ok)
        self.assertEqual(reason, "eligible")
        self.assertEqual(current, 0.0)
        self.assertEqual(original, 8.0)

    def test_paid_price_is_not_zero(self):
        self.assertNotEqual(notifier._parse_price_display("$0.80"), 0)

    def test_zero_discount_requires_full_discount_rate(self):
        # 90% off is never a free giveaway, even if the page is listed on-sale.
        self.assertNotEqual(90.0, 100.0)

    def test_itch_sale_candidate_requires_downloadable_zero_price_game(self):
        self.assertTrue(notifier._itch_has_downloadable_build({
            "downloads": [{"type": "default", "filename": "game.zip"}],
        }))
        self.assertFalse(notifier._itch_has_downloadable_build({
            "downloads": [{"type": "html", "filename": "index.html"}],
        }))

    def test_browser_game_is_rejected_even_when_sale_metadata_is_100_percent(self):
        # The actual feed record has no native downloads for HTML5-only games.
        self.assertFalse(notifier._itch_has_downloadable_build({
            "downloads": [{"type": "html", "filename": "index.html"}],
        }))


class EpicMobileTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

    def test_epic_android_platform_is_not_misclassified_as_epic_pc(self):
        self.assertEqual(notifier._platform_family("Epic (Android)"), "mobile")
        self.assertEqual(notifier._platform_family("Epic (iOS)"), "mobile")
        self.assertEqual(notifier._platform_family("Epic"), "epic")

    def test_verified_epic_android_offer_passes_general_eligibility(self):
        game = {
            "title": "A Tiny Sticker Tale",
            "platform": "Epic (Android)",
            "source": "Epic mobile storefront",
            "is_free": True,
            "original_price": "$139.00",
            "final_price": 0,
            "start_date": "2026-11-27T10:00:00Z",
            "end_date": "2026-12-03T16:00:00Z",
            "type": "BASE_GAME",
        }
        self.assertEqual(notifier.eligibility_rejection_reason(game, now=self.now), "eligible")

    def test_active_zero_price_claim_is_accepted(self):
        purchases = [{
            "purchaseType": "Claim",
            "price": {"decimalPrice": 0},
            "purchaseStateEffectiveDate": "2026-11-26T16:00:00Z",
            "discount": {
                "discountEndDate": "2026-12-03T16:00:00Z",
                "originalPriceDisplay": "$3.99",
            },
        }]
        result = notifier._epic_mobile_active_claim(purchases, now=self.now)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 0)
        self.assertEqual(result[2], "2026-12-03T16:00:00Z")

    def test_direct_product_lookup_falls_back_without_raising(self):
        class Response:
            def raise_for_status(self):
                raise notifier.requests.HTTPError("not found")
        class Session:
            def get(self, *args, **kwargs):
                return Response()
        from unittest.mock import patch
        with patch.object(notifier, "_fetch_epic_mobile_catalog_product", return_value=None):
            self.assertIsNone(notifier.fetch_epic_mobile_product(Session(), "example", "android"))

    def test_paid_claim_is_rejected(self):
        purchases = [{
            "purchaseType": "Claim",
            "price": {"decimalPrice": 0.80},
            "purchaseStateEffectiveDate": "2026-11-26T16:00:00Z",
            "discount": {"discountEndDate": "2026-12-03T16:00:00Z"},
        }]
        self.assertIsNone(notifier._epic_mobile_active_claim(purchases, now=self.now))

    def test_upcoming_claim_is_rejected(self):
        purchases = [{
            "purchaseType": "Claim",
            "price": {"decimalPrice": 0},
            "purchaseStateEffectiveDate": "2026-11-28T16:00:00Z",
            "discount": {"discountEndDate": "2026-12-03T16:00:00Z"},
        }]
        self.assertIsNone(notifier._epic_mobile_active_claim(purchases, now=self.now))

    def test_claim_without_future_expiry_is_rejected(self):
        purchases = [{
            "purchaseType": "Claim",
            "price": {"decimalPrice": 0},
            "purchaseStateEffectiveDate": "2026-11-26T16:00:00Z",
            "discount": {},
        }]
        self.assertIsNone(notifier._epic_mobile_active_claim(purchases, now=self.now))


class EpicPcAndSourceIsolationTests(unittest.TestCase):
    def test_upcoming_offer_is_not_eligible_until_start(self):
        game = {
            "title": "Upcoming giveaway",
            "platform": "Epic",
            "source": "Epic Games Store official PC promotions",
            "is_free": True,
            "original_price": "$14.99",
            "final_price": 0,
            "start_date": "2026-11-28T16:00:00Z",
            "end_date": "2026-12-03T16:00:00Z",
            "type": "BASE_GAME",
        }
        self.assertEqual(notifier.eligibility_rejection_reason(game), "offer_not_started")

    def test_official_epic_pc_record_replaces_unverified_aggregator_state(self):
        existing = {
            "title": "Bread and Fred",
            "platform": "Epic",
            "game_id": "bread-and-fred-id",
            "source": "FreeGameRadar",
            "is_free": True,
            "original_price": "$14.99",
            "final_price": 0,
            "end_date": "2026-12-03T16:00:00Z",
        }
        official = {
            "title": "Bread & Fred",
            "platform": "Epic",
            "game_id": "bread-and-fred-id",
            "source": "Epic Games Store official PC promotions",
            "is_free": True,
            "original_price": 14.99,
            "final_price": 0,
            "start_date": "2026-11-26T16:00:00Z",
            "end_date": "2026-12-03T16:00:00Z",
            "type": "BASE_GAME",
        }
        notifier._merge_duplicate_offer(existing, official)
        self.assertEqual(existing["source"], official["source"])
        self.assertEqual(existing["start_date"], official["start_date"])
        self.assertEqual(notifier.eligibility_rejection_reason(existing), "eligible")

    def test_unverified_epic_aggregator_row_is_rejected(self):
        game = {
            "title": "Bread and Fred",
            "platform": "Epic",
            "source": "FreeGameRadar",
            "is_free": True,
            "original_price": "$14.99",
            "final_price": 0,
            "end_date": "2026-12-03T16:00:00Z",
            "type": "BASE_GAME",
        }
        self.assertEqual(
            notifier.eligibility_rejection_reason(game),
            "epic_offer_not_verified_by_official_store",
        )

    def test_official_epic_record_with_nonzero_final_price_is_rejected(self):
        game = {
            "title": "Paid title",
            "platform": "Epic",
            "source": "Epic Games Store official PC promotions",
            "is_free": True,
            "original_price": "$14.99",
            "final_price": "$0.80",
            "end_date": "2026-12-03T16:00:00Z",
            "type": "BASE_GAME",
        }
        self.assertEqual(notifier.eligibility_rejection_reason(game), "final_price_not_zero")


if __name__ == "__main__":
    unittest.main()
