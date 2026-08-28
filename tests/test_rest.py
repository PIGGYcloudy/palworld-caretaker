"""v1 player-management REST contract tests."""
from __future__ import annotations

import json
import io
from pathlib import Path
import sys
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.errors import ApiError
from palworld_caretaker.rest import PalworldRESTClient


class _Response:
    status = 200

    def __init__(self, payload: bytes = b"{}"):
        self.payload = payload

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, amount: int | None = None): return self.payload if amount is None else self.payload[:amount]


class RESTPlayerManagementTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def opener(request, **_kwargs):
            self.requests.append(request)
            return _Response()

        self.client = PalworldRESTClient({"ADMIN_PASSWORD": "secret"}, opener=opener)

    def test_moderation_endpoints_have_typed_payloads(self):
        self.assertEqual(self.client.kick("steam-1", "AFK").endpoint, "/kick")
        self.assertEqual(self.client.ban("steam-2").endpoint, "/ban")
        self.assertEqual(self.client.unban("steam-3").endpoint, "/unban")
        self.assertEqual(self.client.announce("Hello").endpoint, "/announce")
        bodies = [json.loads(request.data) for request in self.requests]
        self.assertEqual(bodies, [
            {"userid": "steam-1", "message": "AFK"},
            {"userid": "steam-2", "message": ""},
            {"userid": "steam-3"},
            {"message": "Hello"},
        ])

    def test_moderation_rejects_empty_or_unsafe_text(self):
        for target in ("", "  ", "a\x00b", "two words", "../save", 'quote"', "bad\x7f", "user\u202eid"):
            with self.assertRaises(ApiError):
                self.client.kick(target)
        for message in ("bad\x00message", "bad\x01message"):
            with self.assertRaises(ApiError):
                self.client.ban("steam-1", message)

    def test_optional_player_network_fields_remain_compatible(self):
        payload = json.dumps({"players": [{
            "name": "Alice", "userId": "steam-1", "accountName": "alice",
            "ip": "192.0.2.1", "ping": 42, "location": "Desert", "location_x": 12.5,
        }]}).encode()
        client = PalworldRESTClient({"ADMIN_PASSWORD": "secret"}, opener=lambda *_args, **_kwargs: _Response(payload))
        player = client.player_records()[0]
        self.assertEqual((player.name, player.user_id, player.ip, player.ping, player.location),
                         ("Alice", "steam-1", "192.0.2.1", 42, "Desert"))
        self.assertEqual(player.location_x, 12.5)

    def test_http_statuses_and_response_size_are_bounded(self):
        missing = PalworldRESTClient(
            {"ADMIN_PASSWORD": "secret"},
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError("http://127.0.0.1/players", 404, "Not Found", {}, io.BytesIO(b"missing"))
            ),
        )
        with self.assertRaises(ApiError) as raised:
            missing.kick("steam-1")
        self.assertEqual(raised.exception.status, 404)

        oversized = PalworldRESTClient(
            {"ADMIN_PASSWORD": "secret"},
            opener=lambda *_args, **_kwargs: _Response(b"x" * (1024 * 1024 + 1)),
        )
        with self.assertRaisesRegex(ApiError, "too large"):
            oversized.players()


if __name__ == "__main__":
    unittest.main()
