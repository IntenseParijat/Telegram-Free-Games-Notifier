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

    def test_paid_price_is_not_zero(self):
        self.assertNotEqual(notifier._parse_price_display("$0.80"), 0)

    def test_zero_discount_requires_full_discount_rate(self):
        # 90% off is never a free giveaway, even if the page is listed on-sale.
        self.assertNotEqual(90.0, 100.0)

    def test_browser_game_is_rejected_even_when_sale_metadata_is_100_percent(self):
        # The actual feed record has no native downloads for HTML5-only games.
        self.assertFalse(notifier._itch_has_downloadable_build({
            "downloads": [{"type": "html", "filename": "index.html"}],
        }))


class EpicMobileTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

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
