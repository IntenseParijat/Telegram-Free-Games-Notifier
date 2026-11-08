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

    def test_rejects_page_records_that_are_not_objects(self):
        with patch.object(notifier, "fetch_page", side_effect=[
            {"success": True, "data": ["not-a-game"]},
            {"success": True, "data": []},
        ]):
            games, complete = notifier.fetch_all_games("key")
        self.assertTrue(complete)
        self.assertEqual(games, [])

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
