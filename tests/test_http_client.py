"""Outbound HTTP layer tests: retries, backoff, caching and decompression.

The tests replace ``urllib.request.urlopen`` with a fake, so they never touch
the network and never sleep for real.
"""

from __future__ import annotations

import gzip
import io
import unittest
import urllib.error
from unittest import mock

from app.http_client import HttpClient, HttpError, _cache_key, _maybe_decompress

from tests.support import DatabaseTestCase


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"{}", headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/api", status, f"HTTP {status}", {}, io.BytesIO(b"")
    )


class RetryTests(DatabaseTestCase):
    def _client(self) -> HttpClient:
        return HttpClient(cache_enabled=False)

    def test_retryable_status_is_retried_then_succeeds(self) -> None:
        outcomes = [http_error(503), FakeResponse(200, b'{"ok": true}')]

        def fake_urlopen(request, timeout=None):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client = self._client()
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen), mock.patch(
            "app.http_client.time.sleep"
        ):
            response = client.get("https://example.test/api")
        self.assertEqual(response.status, 200)
        stats = client.stats()
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["failures"], 0)

    def test_retries_are_exhausted_and_raise(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise http_error(429)

        client = self._client()
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen), mock.patch(
            "app.http_client.time.sleep"
        ):
            with self.assertRaises(HttpError) as raised:
                client.get("https://example.test/api")
        self.assertEqual(raised.exception.status, 429)
        # max_retries=2 means three attempts in total
        self.assertEqual(len(calls), 3)
        self.assertEqual(client.stats()["failures"], 1)

    def test_transport_failure_is_retried_and_wrapped(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise TimeoutError("timed out")

        client = self._client()
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen), mock.patch(
            "app.http_client.time.sleep"
        ):
            with self.assertRaises(HttpError) as raised:
                client.get("https://example.test/api")
        self.assertIn("transport failure", str(raised.exception))
        self.assertEqual(len(calls), 3)

    def test_non_retryable_status_is_not_retried(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise http_error(404)

        client = self._client()
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen), mock.patch(
            "app.http_client.time.sleep"
        ):
            with self.assertRaises(HttpError):
                client.get("https://example.test/api")
        self.assertEqual(len(calls), 1)

    def test_raise_for_status_false_returns_the_error_response(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise http_error(404)

        client = self._client()
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen):
            response = client.get("https://example.test/api", raise_for_status=False)
        self.assertEqual(response.status, 404)

    def test_head_status_never_raises_for_an_error_status(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise http_error(403)

        client = self._client()
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(client.head_status("https://example.test/file"), 403)


class CacheTests(DatabaseTestCase):
    def test_second_get_is_served_from_the_sqlite_cache(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            return FakeResponse(200, b'{"ok": true}', {"Content-Type": "application/json"})

        client = HttpClient(cache_enabled=True)
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen), mock.patch(
            "app.http_client.time.sleep"
        ):
            first = client.get("https://example.test/cached/path")
            second = client.get("https://example.test/cached/path")

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(first.text, second.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.stats()["cache_hits"], 1)

    def test_expired_cache_entry_is_refetched(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            return FakeResponse(200, b'{"ok": true}')

        client = HttpClient(cache_enabled=True)
        with mock.patch("app.http_client.urllib.request.urlopen", fake_urlopen), mock.patch(
            "app.http_client.time.sleep"
        ):
            client.get("https://example.test/cached/ttl")
            client.get("https://example.test/cached/ttl", cache_ttl=0)
        self.assertEqual(len(calls), 2)

    def test_cache_key_is_stable_and_method_sensitive(self) -> None:
        self.assertEqual(_cache_key("GET", "https://x/y"), _cache_key("get", "https://x/y"))
        self.assertNotEqual(_cache_key("GET", "https://x/y"), _cache_key("HEAD", "https://x/y"))


class HelperTests(DatabaseTestCase):
    def _client(self) -> HttpClient:
        return HttpClient(backoff_base=0.5, cache_enabled=False)

    def test_backoff_prefers_retry_after_header(self) -> None:
        client = self._client()
        self.assertEqual(client._backoff_delay(0, "5"), 5.0)
        self.assertEqual(client._backoff_delay(3, "600"), 30.0)  # clamped

    def test_backoff_is_exponential_and_jittered(self) -> None:
        client = self._client()
        low = client._backoff_delay(0, "not-a-number")
        high = client._backoff_delay(3, None)
        self.assertGreaterEqual(low, 0.5)
        self.assertGreater(high, low)

    def test_gzip_body_is_decompressed(self) -> None:
        payload = b'{"hello": "world"}'
        decoded = _maybe_decompress(gzip.compress(payload), {"content-encoding": "gzip"})
        self.assertEqual(decoded, payload)

    def test_unknown_encoding_is_returned_unchanged(self) -> None:
        payload = b"plain"
        self.assertEqual(_maybe_decompress(payload, {}), payload)
        self.assertEqual(_maybe_decompress(payload, {"content-encoding": "br"}), payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
