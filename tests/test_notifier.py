import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notifier



class PaginationTests(unittest.TestCase):
    def test_repeated_page_stops_and_returns_unique_records(self):
        first = [
            {"game_id": "g1", "platform": "Steam", "title": "One"},
            {"game_id": "g2", "platform": "Epic", "title": "Two"},
        ]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": first},
            {"success": True, "data": first},
        ]):
            games, complete = notifier.fetch_all_games("key")
        self.assertFalse(complete)
        self.assertEqual([game["game_id"] for game in games], ["g1", "g2"])

    def test_duplicate_game_keys_across_pages_are_removed(self):
        page1 = [
            {"game_id": "g1", "platform": "Steam", "title": "One"},
            {"game_id": "g2", "platform": "Epic", "title": "Two"},
        ]
        page2 = [
            {"game_id": "g2", "platform": "Epic", "title": "Two"},
            {"game_id": "g3", "platform": "Xbox", "title": "Three"},
        ]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": page1},
            {"success": True, "data": page2},
            {"success": True, "data": []},
        ]):
            games, complete = notifier.fetch_all_games("key")
        self.assertTrue(complete)
        self.assertEqual({game["game_id"] for game in games}, {"g1", "g2", "g3"})

    def test_reordered_repeated_page_is_detected(self):
        page1 = [
            {"game_id": "g1", "platform": "Steam"},
            {"game_id": "g2", "platform": "Epic"},
        ]
        page2 = list(reversed(page1))
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": page1},
            {"success": True, "data": page2},
        ]):
            games, complete = notifier.fetch_all_games("key")
        self.assertFalse(complete)
        self.assertEqual(len(games), 2)

    def test_repeated_short_page_is_not_misreported_as_complete(self):
        page = [{"game_id": "g1", "platform": "Steam"}]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": page},
            {"success": True, "data": list(reversed(page))},
        ]):
            games, complete = notifier.fetch_all_games("key")
        self.assertFalse(complete)
        self.assertEqual([game["game_id"] for game in games], ["g1"])

    def test_short_page_continues_until_empty_page(self):
        page = [{"game_id": "g1", "platform": "Steam"}]
        later_page = [{"game_id": "g2", "platform": "Epic"}]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": page},
            {"success": True, "data": later_page},
            {"success": True, "data": []},
        ]) as mocked:
            games, complete = notifier.fetch_all_games("key")
        self.assertTrue(complete)
        self.assertEqual({game["game_id"] for game in games}, {"g1", "g2"})
        self.assertEqual(mocked.call_count, 3)

    def test_different_pages_of_same_records_are_detected_after_deduplication(self):
        first = [{"game_id": "g1", "platform": "Steam"}]
        second = [{"game_id": "g2", "platform": "Epic"}]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": first},
            {"success": True, "data": second},
            {"success": True, "data": [{"game_id": "g1", "platform": "Steam"}, {"game_id": "g2", "platform": "Epic"}]},
        ]):
            games, complete = notifier.fetch_all_games("key")
        self.assertFalse(complete)
        self.assertEqual(len(games), 2)

    def test_ignores_malformed_records_and_continues_to_empty_page(self):
        valid_page = [{"game_id": "g1", "platform": "Steam"}]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": ["not-a-game", None, 42]},
            {"success": True, "data": valid_page},
            {"success": True, "data": []},
        ]) as mocked:
            games, complete = notifier.fetch_all_games("key")
        self.assertTrue(complete)
        self.assertEqual([game["game_id"] for game in games], ["g1"])
        self.assertEqual(mocked.call_count, 3)

    def test_repeated_malformed_pages_continue_until_empty_page(self):
        malformed = ["not-a-game", None, 42]
        valid_page = [{"game_id": "g1", "platform": "Steam"}]
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": malformed},
            {"success": True, "data": malformed},
            {"success": True, "data": valid_page},
            {"success": True, "data": []},
        ]) as mocked:
            games, complete = notifier.fetch_all_games("key")
        self.assertTrue(complete)
        self.assertEqual([game["game_id"] for game in games], ["g1"])
        self.assertEqual(mocked.call_count, 4)

class OfferEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.now = notifier._parse_offer_datetime("2026-11-09T12:00:00Z")

    def base_offer(self):
        return {
            "title": "Limited giveaway",
            "platform": "Epic Games",
            "is_free": True,
            "original_price": "19.99",
            "final_price": "0",
            "end_date": "2026-11-15T23:59:59Z",
            "description": "A normally paid title, temporarily free to claim.",
        }

    def test_accepts_active_paid_game_giveaway(self):
        self.assertTrue(notifier.is_eligible_temporary_offer(self.base_offer(), now=self.now))

    def test_rejects_permanently_free_game_without_expiry(self):
        game = self.base_offer()
        game["end_date"] = None
        self.assertFalse(notifier.is_eligible_temporary_offer(game, now=self.now))

    def test_rejects_expired_offer(self):
        game = self.base_offer()
        game["end_date"] = "2026-11-08T23:59:59Z"
        self.assertFalse(notifier.is_eligible_temporary_offer(game, now=self.now))

    def test_rejects_permanent_free_to_play_record(self):
        game = self.base_offer()
        game["description"] = "Free-to-play forever."
        self.assertFalse(notifier.is_eligible_temporary_offer(game, now=self.now))

    def test_rejects_game_pass_catalogue_record(self):
        game = self.base_offer()
        game["description"] = "Included with Xbox Game Pass."
        self.assertFalse(notifier.is_eligible_temporary_offer(game, now=self.now))

    def test_rejects_subscription_source(self):
        game = self.base_offer()
        game["source"] = "Xbox Game Pass subscription catalogue"
        self.assertFalse(notifier.is_eligible_temporary_offer(game, now=self.now))

    def test_rejects_zero_original_price(self):
        game = self.base_offer()
        game["original_price"] = "0"
        self.assertFalse(notifier.is_eligible_temporary_offer(game, now=self.now))


class CaptionOrderTests(unittest.TestCase):
    def test_caption_has_requested_order_and_blank_line_spacing(self):
        game = {
            "title": "Test Game",
            "description": "First sentence. Second sentence.",
            "platform": "Epic Games",
            "game_url": "https://example.com/claim",
            "end_date": "2026-11-15",
        }
        with patch.object(notifier, "enabled", side_effect=lambda field: field in {"description", "platform"}), patch.object(
            notifier, "footer_lines", return_value=["@Channel", "Powered By Intense Network"]
        ):
            message = notifier.compose_message(game, 1000)

        expected = (
            "<b>Test Game</b>\\n\\n"
            "First sentence. Second sentence.\\n\\n"
            "🎮 <b>Platform:</b> Epic Games\\n\\n"
            '🔗 <a href="https://example.com/claim">Get the game</a>\\n\\n'
            "⏳ <b>Free until:</b> 2026-11-15\\n\\n"
            "@Channel\\nPowered By Intense Network"
        )
        self.assertEqual(message, expected)

    def test_category_price_discount_and_ids_are_not_rendered(self):
        game = {
            "title": "Test Game",
            "platform": "Xbox",
            "category": "f2p",
            "discount_percent": 100,
            "original_price": "10",
            "final_price": "0",
            "discount_price": "free",
            "game_id": "abc",
            "id": "123",
            "end_date": "2026-11-15",
        }
        with patch.object(notifier, "enabled", side_effect=lambda field: field in {"platform"}):
            message = notifier.compose_message(game, 1000)
        for unwanted in ("Category", "Discount:", "Price:", "Discount price", "Game ID", "Listing ID"):
            self.assertNotIn(unwanted, message)


class CaptionTests(unittest.TestCase):
    def test_sentence_boundary_trimming_matches_requested_example(self):
        fixed = "X" * 98
        description = (
            "Description sentence 1. Description sentence 2. "
            "Loooooooooong Description sentence 3. Description sentence 4. "
            "Description sentence 5. Description sentence 6."
        )
        result = notifier.trim_description_to_fit(description, fixed, 200)
        self.assertEqual(
            result,
            "Description sentence 1. Description sentence 2. "
            "Loooooooooong Description sentence 3.",
        )
        self.assertLessEqual(len(fixed) + 2 + len(result), 200)

    def test_compose_message_trims_description_but_keeps_correct_order(self):
        game = {
            "title": "Long game",
            "description": "A" * 60 + ". " + "B" * 60 + ".",
            "platform": "Steam",
            "game_url": "https://example.com/claim",
            "end_date": "2026-11-15",
        }
        with patch.object(notifier, "enabled", side_effect=lambda field: field in {"description", "platform"}), patch.object(
            notifier, "footer_lines", return_value=["@Channel"]
        ):
            message = notifier.compose_message(game, 170)
        self.assertLessEqual(notifier.visible_text_length(message), 170)
        self.assertTrue(message.startswith("<b>Long game</b>\\n\\n"))
        self.assertIn("🎮 <b>Platform:</b>", message)
        self.assertLess(message.index("🎮 <b>Platform:</b>"), message.index("Get the game"))
        self.assertLess(message.index("Get the game"), message.index("Free until"))
        self.assertLess(message.index("Free until"), message.index("@Channel"))

    def test_html_entities_are_counted_as_visible_characters(self):
        fixed = "X" * 98
        description = "A &amp; B. A &amp; B. A &amp; B."
        result = notifier.trim_description_to_fit(description, fixed, 110)
        self.assertEqual(result, "A &amp; B.")

    def test_emoji_use_telegram_utf16_length(self):
        self.assertEqual(notifier.visible_text_length("🎮"), 2)
        self.assertEqual(notifier.visible_text_length("A🎮B"), 4)

    def test_no_partial_sentence_is_returned(self):
        self.assertEqual(notifier.trim_description_to_fit("No period here", "X" * 99, 100), "")


class FormattingTests(unittest.TestCase):
    def test_markdown_style_footer_link_converts_to_telegram_html(self):
        result = notifier.format_footer_line(
            "Powered By [Intense Network](https://t.me/IntenseNetwork)"
        )
        self.assertEqual(
            result,
            'Powered By <a href="https://t.me/IntenseNetwork">Intense Network</a>',
        )

    def test_configured_footer_link_converts_exactly(self):
        result = notifier.format_footer_line("Powered By [Intense Network](t.me/IntenseNetwork)")
        self.assertEqual(
            result,
            'Powered By <a href="https://t.me/IntenseNetwork">Intense Network</a>',
        )

    def test_footer_link_without_space_is_not_padded(self):
        result = notifier.format_footer_line("Join[Network](t.me/IntenseNetwork)")
        self.assertEqual(result, 'Join<a href="https://t.me/IntenseNetwork">Network</a>')


    def test_multiple_links_and_plain_text_are_preserved(self):
        result = notifier.format_footer_line(
            "Join [Network](t.me/IntenseNetwork) or [News](https://t.me/News) now"
        )
        self.assertEqual(
            result,
            'Join <a href="https://t.me/IntenseNetwork">Network</a> or '
            '<a href="https://t.me/News">News</a> now',
        )

    def test_invalid_game_urls_are_rejected(self):
        self.assertIsNone(notifier.valid_http_url("javascript:alert(1)"))
        self.assertIsNone(notifier.valid_http_url("not a url"))
        self.assertEqual(notifier.valid_http_url("https://example.com/game"), "https://example.com/game")



class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_send_network_error_does_not_fallback_to_text(self):
        class Client:
            def __init__(self):
                self.photo_calls = 0
                self.text_calls = 0
            async def send_file(self, *args, **kwargs):
                self.photo_calls += 1
                raise OSError("connection lost after send")
            async def send_message(self, *args, **kwargs):
                self.text_calls += 1

        client = Client()
        game = {"title": "Test game", "image_url": "https://example.com/image.jpg"}
        with patch.object(notifier, "enabled", side_effect=lambda field: field == "image"), patch.object(
            notifier, "compose_message", return_value="<b>Test game</b>"
        ), patch.object(notifier, "valid_http_url", return_value="https://example.com/image.jpg"):
            with self.assertRaises(OSError):
                await notifier.send_one(client, "@channel", game)
        self.assertEqual(client.photo_calls, 1)
        self.assertEqual(client.text_calls, 0)

    async def test_photo_is_forced_to_compressed_photo_mode(self):
        class Client:
            def __init__(self):
                self.kwargs = None
            async def send_file(self, *args, **kwargs):
                self.kwargs = kwargs
            async def send_message(self, *args, **kwargs):
                raise AssertionError("text fallback should not be used")

        client = Client()
        game = {"title": "Test game", "image_url": "https://example.com/image.jpg"}
        with patch.object(notifier, "enabled", side_effect=lambda field: field == "image"), patch.object(
            notifier, "compose_message", return_value="<b>Test game</b>"
        ), patch.object(notifier, "valid_http_url", return_value="https://example.com/image.jpg"):
            await notifier.send_one(client, "@channel", game)
        self.assertIsNotNone(client.kwargs)
        self.assertIs(client.kwargs.get("force_document"), False)

    async def test_photo_payload_rejection_falls_back_to_text(self):
        class Client:
            def __init__(self):
                self.photo_calls = 0
                self.text_calls = 0
            async def send_file(self, *args, **kwargs):
                self.photo_calls += 1
                raise ValueError("invalid media")
            async def send_message(self, *args, **kwargs):
                self.text_calls += 1

        client = Client()
        game = {"title": "Test game", "image_url": "https://example.com/image.jpg"}
        with patch.object(notifier, "enabled", side_effect=lambda field: field == "image"), patch.object(
            notifier, "compose_message", return_value="<b>Test game</b>"
        ), patch.object(notifier, "valid_http_url", return_value="https://example.com/image.jpg"):
            await notifier.send_one(client, "@channel", game)
        self.assertEqual(client.photo_calls, 1)
        self.assertEqual(client.text_calls, 1)


class StateTests(unittest.TestCase):
    def test_state_file_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "seen_games.json"
            with patch.object(notifier, "STATE_PATH", state_path):
                notifier.save_state({"steam:one", "epic:two"})
                self.assertEqual(notifier.load_state(), {"steam:one", "epic:two"})

    def test_state_limit_does_not_silently_discard_history(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "seen_games.json"
            with patch.object(notifier, "STATE_PATH", state_path), patch.object(notifier, "MAX_STATE_KEYS", 1):
                with self.assertRaises(RuntimeError):
                    notifier.save_state({"steam:one", "epic:two"})


if __name__ == "__main__":
    unittest.main()
