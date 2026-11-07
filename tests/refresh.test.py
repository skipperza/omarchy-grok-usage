#!/usr/bin/python3
"""An expired Grok CLI login is renewed from the saved refresh token."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collect  # noqa: E402


class RefreshAuth(unittest.TestCase):
  def test_expired_login_is_renewed_and_written_back(self) -> None:
    with self._auth("2020-01-01T00:00:00Z") as path:
      seen: dict[str, object] = {}

      def fake_request(url, method="GET", headers=None, data=None, timeout=10):
        seen["url"] = url
        seen["method"] = method
        body = (data or b"").decode("utf-8")
        seen["grant"] = "grant_type=refresh_token" in body
        seen["has_refresh"] = "refresh_token=saved-refresh" in body
        payload = json.dumps({
          "access_token": "new-access-token",
          "expires_in": 21600,
          "refresh_token": "rotated-refresh",
        }).encode("utf-8")
        return 200, payload

      original = collect.usage_request
      collect.usage_request = fake_request
      try:
        token, expires = collect.ensure_fresh_auth(path)
      finally:
        collect.usage_request = original

      stored = json.loads(path.read_text())
      entry = next(iter(stored.values()))
      self.assertEqual(token, "new-access-token")
      self.assertFalse(collect.token_expired(expires))
      self.assertEqual(entry["key"], "new-access-token")
      self.assertEqual(entry["refresh_token"], "rotated-refresh")
      self.assertEqual(entry["email"], "kept@example.com")
      self.assertEqual(seen["url"], collect.XAI_TOKEN_URL)
      self.assertEqual(seen["method"], "POST")
      self.assertTrue(seen["grant"])
      self.assertTrue(seen["has_refresh"])

  def test_failed_refresh_leaves_the_saved_login_alone(self) -> None:
    with self._auth("2020-01-01T00:00:00Z") as path:
      before = path.read_text()

      def fake_request(*_args, **_kwargs):
        return 401, b"{}"

      original = collect.usage_request
      collect.usage_request = fake_request
      try:
        token, expires = collect.ensure_fresh_auth(path)
      finally:
        collect.usage_request = original

      self.assertEqual(token, "old-access")
      self.assertEqual(expires, "2020-01-01T00:00:00Z")
      self.assertEqual(path.read_text(), before)

  def _auth(self, expires_at: str):
    import tempfile

    class _Box:
      def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        path = root / "auth.json"
        path.write_text(json.dumps({
          "https://auth.x.ai::client": {
            "key": "old-access",
            "refresh_token": "saved-refresh",
            "expires_at": expires_at,
            "oidc_client_id": "client",
            "email": "kept@example.com",
          }
        }))
        path.chmod(0o600)
        self.path = path
        return path

      def __exit__(self, *_exc):
        self.tmp.cleanup()

    return _Box()


if __name__ == "__main__":
  unittest.main()
