"""Read-only scan regressions; fixtures contain no real account data."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from humble_bundle_keys.auth import _interactive_login
from humble_bundle_keys.web import (
    Inventory,
    describe_membership,
    make_handler,
    membership_url,
    reconcile_rows,
    safe_error,
)


class WebInventoryTests(unittest.TestCase):
    def test_errors_preserve_launch_reason_and_mask_keys_and_urls(self):
        result = safe_error(RuntimeError(
            "spawn UNKNOWN https://example.com/?key=secret ABCDE-FGHIJ-KLMNO"))
        self.assertIn("spawn UNKNOWN", result)
        self.assertNotIn("secret", result)
        self.assertNotIn("ABCDE", result)

    def test_interactive_login_uses_selected_browser(self):
        playwright = MagicMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch("humble_bundle_keys.auth._is_authenticated", return_value=True):
            _interactive_login(playwright, Path(directory) / "state.json", "msedge")
        playwright.chromium.launch.assert_called_once_with(headless=False, channel="msedge")
        playwright.chromium.launch.return_value.close.assert_called_once()

    def test_pending_and_exhausted_are_distinct(self):
        cards = [{"title": "Game A", "claimed": False}]
        month = describe_membership({"choices_remaining": "2"}, cards, "url")
        self.assertEqual(month["state"], "pending")
        self.assertEqual(month["choices_remaining"], 2)
        exhausted = describe_membership({"choices_remaining": 0}, cards, "url")
        self.assertEqual(exhausted["state"], "exhausted")
        unknown = describe_membership({"choices_remaining": 2}, [], "url")
        self.assertEqual(unknown["state"], "unknown")

    def test_key_text_wins_over_pending_card(self):
        rows = [{"game_title": "Game A", "bundle_name": "Month", "key": "TEST-KEY"},
                {"game_title": "Game B", "bundle_name": "Month", "key": ""},
                {"game_title": "Game C", "bundle_name": "Month", "key": ""}]
        months = [{"state": "pending", "bundle_name": "Month",
                   "unclaimed_titles": ["Game A", "Game B"]}]
        self.assertEqual([r["status"] for r in reconcile_rows(rows, months)],
                         ["revealed", "pending_choice", "unrevealed"])

    def test_membership_url_validates_origin_and_subscription(self):
        self.assertIsNone(membership_url({"machine_name": "january_2020_choice"}))
        product = {"category": "subscriptioncontent", "machine_name": "january_2020_choice"}
        self.assertEqual(membership_url(product),
                         "https://www.humblebundle.com/membership/january-2020")
        product["choice_url"] = "https://evil.example/foo"
        self.assertIsNone(membership_url(product))

    def test_published_records_have_deadline_filters_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Inventory(Path(directory))
            inventory.publish({"rows": [
                {"game_title": "Shown", "bundle_name": "Test", "key": "TEST-ONLY-KEY",
                 "redemption_deadline": "Expired"}], "memberships": [], "warnings": [],
                "scanned_at": None})
            row = inventory.view()["rows"][0]
            self.assertEqual(row["status"], "revealed")
            self.assertEqual(row["deadline_state"], "expired")
            self.assertTrue(row["has_deadline"])
            self.assertEqual(Inventory(Path(directory)).view()["rows"], inventory.view()["rows"])

    def test_local_api_rejects_missing_token_and_external_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Inventory(Path(directory))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(inventory, "test-token"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(HTTPError):
                    urlopen(base + "/api/state")
                request = Request(base + "/api/state", headers={"X-Local-Token": "test-token"})
                with urlopen(request) as response:
                    self.assertEqual(json.load(response)["rows"], [])
                request = Request(base + "/api/scan", method="POST", headers={
                    "X-Local-Token": "test-token", "Origin": "https://evil.example"})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request)
                self.assertEqual(caught.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
