"""Shared outbound HTTP layer.

Provides retries with exponential backoff + jitter, per-host rate limiting,
timeouts, HTTP-status caching backed by SQLite, structured logging and clear
error types. Only the standard library is used.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import get_settings

USER_AGENT_FALLBACK = "ResearchForge/1.0 (local research client)"

# Minimum seconds between two requests to the same host. Public research APIs
# publish different limits; these are conservative anonymous values.
DEFAULT_HOST_INTERVALS: dict[str, float] = {
    "api.openalex.org": 0.12,
    "api.crossref.org": 0.25,
    "api.semanticscholar.org": 1.2,
    "huggingface.co": 0.35,
    "datasets-server.huggingface.co": 0.35,
    "zenodo.org": 0.6,
    "archive.ics.uci.edu": 0.6,
    "www.ebi.ac.uk": 0.4,
    "api.datacite.org": 0.4,
    "catalog.data.gov": 0.4,
    "api.fda.gov": 0.5,
    "api.worldbank.org": 0.4,
    "api.usgs.gov": 0.4,
    "www.who.int": 0.6,
}


class HttpError(RuntimeError):
    """Raised for transport failures or unexpected HTTP status codes."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status

    def to_dict(self) -> dict:
        return {"message": str(self), "url": self.url, "status": self.status}


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        text = self.text
        if not text:
            return None
        return json.loads(text)

    @property
    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([\w\-]+)", self.content_type, re.I)
        if match:
            charset = match.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


@dataclass
class RequestStats:
    requests: int = 0
    retries: int = 0
    failures: int = 0
    cache_hits: int = 0
    bytes_downloaded: int = 0
    hosts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "bytes_downloaded": self.bytes_downloaded,
            "hosts": dict(self.hosts),
        }


class _RateLimiter:
    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            if host not in self._locks:
                self._locks[host] = threading.Lock()
            return self._locks[host]

    def wait(self, host: str, interval: float) -> float:
        """Block until the host may be called again. Returns seconds slept."""
        with self._lock_for(host):
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delta = now - last
            sleep_for = max(0.0, interval - delta)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last[host] = time.monotonic()
            return sleep_for


_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    """Blocking HTTP client with caching, retries and polite rate limiting."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
        cache_enabled: bool = True,
        cache_getter=None,
        cache_setter=None,
        logger=None,
    ) -> None:
        settings = get_settings()
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.max_retries = max_retries if max_retries is not None else settings.http_max_retries
        self.backoff_base = (
            backoff_base if backoff_base is not None else settings.http_backoff_base
        )
        self.user_agent = settings.user_agent or USER_AGENT_FALLBACK
        self.cache_enabled = cache_enabled
        self.default_ttl = settings.cache_ttl_seconds
        self._cache_getter = cache_getter
        self._cache_setter = cache_setter
        self._limiter = _RateLimiter()
        self._stats = RequestStats()
        self._stats_lock = threading.Lock()
        self._logger = logger

        if self._cache_getter is None or self._cache_setter is None:
            try:  # lazily bind to the app database when available
                from .db import get_db

                db = get_db()
                self._cache_getter = db.cache_get
                self._cache_setter = db.cache_put
            except Exception:
                self.cache_enabled = False

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict:
        with self._stats_lock:
            return self._stats.as_dict()

    def _bump(self, field_name: str, host: str, amount: int = 1) -> None:
        with self._stats_lock:
            setattr(self._stats, field_name, getattr(self._stats, field_name) + amount)
            if host:
                self._stats.hosts[host] = self._stats.hosts.get(host, 0) + 1

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        getattr(self._logger, level)(event, **fields)

    # -- core request ------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        accept: str = "application/json",
        body: bytes | None = None,
        use_cache: bool | None = None,
        cache_ttl: int | None = None,
        cache_key: str | None = None,
        raise_for_status: bool = True,
        allowed_status: tuple[int, ...] = (200, 201, 202, 203, 206),
    ) -> HttpResponse:
        if params:
            filtered = {k: v for k, v in params.items() if v is not None and v != ""}
            if filtered:
                query = urllib.parse.urlencode(filtered, doseq=True)
                url = f"{url}{'&' if '?' in url else '?'}{query}"

        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        interval = DEFAULT_HOST_INTERVALS.get(host, 0.2)
        if host in {"api.semanticscholar.org"} and get_settings().semantic_scholar_api_key:
            interval = 0.35  # authenticated access is not as tightly limited

        want_cache = self.cache_enabled if use_cache is None else (use_cache and self.cache_enabled)
        ttl = self.default_ttl if cache_ttl is None else cache_ttl
        key = cache_key or _cache_key(method, url)

        if want_cache and method.upper() == "GET" and self._cache_getter:
            cached = self._cache_getter(key, ttl)
            if cached:
                status, body_bytes = cached
                self._bump("cache_hits", host)
                self._log("debug", "http_cache_hit", url=url, host=host)
                response = HttpResponse(
                    url=url,
                    status=status,
                    headers={"content-type": "application/json", "x-researchforge-cache": "hit"},
                    body=body_bytes,
                    from_cache=True,
                )
                return response

        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            req_headers.update({k: str(v) for k, v in headers.items()})

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._limiter.wait(host, interval)
            started = time.monotonic()
            try:
                request = urllib.request.Request(
                    url, data=body, headers=req_headers, method=method.upper()
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as raw:
                    status = raw.status
                    raw_body = raw.read()
                    resp_headers = {k.lower(): v for k, v in raw.headers.items()}
                elapsed_ms = int((time.monotonic() - started) * 1000)
                decoded = _maybe_decompress(raw_body, resp_headers)
                self._bump("requests", host)
                with self._stats_lock:
                    self._stats.bytes_downloaded += len(decoded)
                self._log(
                    "debug",
                    "http_request",
                    url=url,
                    host=host,
                    status=status,
                    elapsed_ms=elapsed_ms,
                    bytes=len(decoded),
                )
                response = HttpResponse(
                    url=url, status=status, headers=resp_headers, body=decoded,
                    elapsed_ms=elapsed_ms,
                )
                if want_cache and method.upper() == "GET" and status in allowed_status and self._cache_setter:
                    try:
                        self._cache_setter(key, url, status, decoded)
                    except Exception as exc:  # cache must never break a request
                        self._log("warning", "http_cache_write_failed", url=url, error=str(exc))
                return response
            except urllib.error.HTTPError as exc:
                status = exc.code
                payload = b""
                try:
                    payload = exc.read()
                except Exception:
                    payload = b""
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self._bump("requests", host)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if status in _RETRYABLE_STATUS and attempt < self.max_retries:
                    self._bump("retries", host)
                    delay = self._backoff_delay(attempt, retry_after)
                    self._log(
                        "warning",
                        "http_retry",
                        url=url,
                        status=status,
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                    )
                    time.sleep(delay)
                    last_error = HttpError(
                        f"HTTP {status}", url=url, status=status
                    )
                    continue
                self._bump("failures", host)
                self._log(
                    "warning",
                    "http_error",
                    url=url,
                    status=status,
                    elapsed_ms=elapsed_ms,
                )
                error = HttpError(f"HTTP {status} for {url}", url=url, status=status)
                if raise_for_status and status not in allowed_status:
                    raise error from exc
                return HttpResponse(
                    url=url,
                    status=status,
                    headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                    body=payload,
                    elapsed_ms=elapsed_ms,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._bump("requests", host)
                last_error = exc
                if attempt < self.max_retries:
                    self._bump("retries", host)
                    delay = self._backoff_delay(attempt, None)
                    self._log(
                        "warning",
                        "http_transport_retry",
                        url=url,
                        error=str(exc),
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                    )
                    time.sleep(delay)
                    continue
                self._bump("failures", host)
                self._log("error", "http_transport_failure", url=url, error=str(exc))
                raise HttpError(f"transport failure for {url}: {exc}", url=url) from exc

        raise HttpError(f"request failed for {url}: {last_error}", url=url)

    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(30.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        base = self.backoff_base * (2 ** attempt)
        return min(30.0, base + random.uniform(0, base * 0.25))

    # -- convenience -------------------------------------------------------
    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("accept", "application/json")
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise HttpError(
                f"invalid JSON from {url}: {exc}", url=url, status=response.status
            ) from exc

    def get_text(self, url: str, **kwargs: Any) -> str:
        kwargs.setdefault("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        return self.get(url, **kwargs).text

    def head_status(self, url: str, **kwargs: Any) -> int:
        """Return the HTTP status of a link without downloading the payload."""
        kwargs.setdefault("raise_for_status", False)
        kwargs.setdefault("use_cache", False)
        kwargs.setdefault("accept", "*/*")
        try:
            response = self.request("HEAD", url, **kwargs)
            return response.status
        except HttpError as exc:
            if exc.status:
                return exc.status
            raise


def _maybe_decompress(body: bytes, headers: dict[str, str]) -> bytes:
    encoding = (headers.get("content-encoding") or "").lower()
    if not encoding or encoding == "identity":
        return body
    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        return body
    return body


def _cache_key(method: str, url: str) -> str:
    return hashlib.sha256(f"{method.upper()} {url}".encode("utf-8")).hexdigest()


_shared_clients: dict[str, HttpClient] = {}
_shared_lock = threading.Lock()


def get_http_client(name: str = "default", **kwargs: Any) -> HttpClient:
    with _shared_lock:
        if name not in _shared_clients:
            _shared_clients[name] = HttpClient(**kwargs)
        return _shared_clients[name]


def reset_http_clients() -> None:
    """Used by tests / after a settings change."""
    with _shared_lock:
        _shared_clients.clear()
