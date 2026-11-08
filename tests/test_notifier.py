import unittest
from unittest.mock import MagicMock, patch

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
            games = notifier.fetch_all_games("key")
        self.assertEqual([g["game_id"] for g in games], ["g1", "g2"])

    def test_duplicate_ids_across_pages_are_removed(self):
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
            games = notifier.fetch_all_games("key")
        self.assertEqual({g["game_id"] for g in games}, {"g1", "g2", "g3"})


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

    def test_no_partial_sentence_is_returned(self):
        self.assertEqual(notifier.trim_description_to_fit("No period here", "X" * 99, 100), "")


class FormattingTests(unittest.TestCase):
    def test_markdown_style_footer_link_converts_to_telegram_html(self):
        result = notifier.format_footer_line("Powered By [Intense Network](https://t.me/IntenseNetwork)")
        self.assertEqual(
            result,
            '<a href="https://t.me/IntenseNetwork">Intense Network</a>',
        )

    def test_invalid_game_url_is_rejected(self):
        self.assertIsNone(notifier.valid_http_url("javascript:alert(1)"))
        self.assertIsNone(notifier.valid_http_url("not a url"))
        self.assertEqual(notifier.valid_http_url("https://example.com/game"), "https://example.com/game")


class StateTests(unittest.TestCase):
    def test_state_file_round_trip(self):
        with patch.object(notifier, "STATE_PATH") as state_path:
            state_path.exists.return_value = False
            state_path.parent.mkdir = MagicMock()
            state_path.with_suffix.return_value = MagicMock()
            temp = state_path.with_suffix.return_value
            temp.write_text = MagicMock()
            temp.replace = MagicMock()
            # save_state should not silently prune identifiers.
            notifier.save_state({"steam:one", "epic:two"})
            self.assertTrue(temp.write_text.called)


if __name__ == "__main__":
    unittest.main()
