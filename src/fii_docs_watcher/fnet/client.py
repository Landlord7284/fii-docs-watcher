"""HTTP transport for Fundos.NET: rate limited, retried, size capped.

Two measured properties of this host shape everything here.

**Latency is bimodal.** Successful responses come back in either ~0.3s or
~60.3s, with nothing in between, on every endpoint. A conventional 30s timeout
would turn roughly half of all successful requests into spurious failures, so
the read timeout defaults to 120s. Do not lower it without re-measuring.

**Failures are unremarkable.** Timeouts and HTTP 500s appear even on requests
that succeed moments later. They are retried with exponential backoff and
jitter, and only a persistent failure is allowed to fail a unit of work.
"""

from __future__ import annotations

import logging
import random
import time
from types import TracebackType
from typing import Any

import httpx

from ..config import SourceConfig
from ..errors import SourceContractError, TransientSourceError

log = logging.getLogger(__name__)

# 408 and 429 are retryable by definition; 5xx is retryable on this host because
# it is routinely transient rather than a real server-side defect.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class Response:
    """A completed HTTP response, already read into memory."""

    __slots__ = ("content", "headers", "status_code", "url")

    def __init__(self, status_code: int, content: bytes, headers: httpx.Headers, url: str) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers
        self.url = url

    def json(self) -> Any:
        """Parse the body as JSON.

        A body that is not JSON where JSON was expected usually means an HTML
        error page arrived with HTTP 200 -- a real failure mode on this host --
        so it is reported as a contract error rather than a parse error.
        """
        import json as _json

        try:
            return _json.loads(self.content)
        except ValueError as exc:
            preview = self.content[:200].decode("utf-8", "replace")
            raise SourceContractError(
                f"expected JSON from {self.url} but got {len(self.content)} bytes that do not "
                f"parse: {preview!r}",
                context={"url": self.url, "status": self.status_code},
            ) from exc


class FnetClient:
    """A thin, deliberately serial client for the Fundos.NET public endpoints.

    Requests are spaced by `min_request_interval_seconds` so total volume stays
    comparable to human use. The source is public and unauthenticated; this
    client holds no credential and must never acquire one.
    """

    def __init__(self, config: SourceConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._last_request = 0.0
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                config.read_timeout_seconds,
                connect=config.connect_timeout_seconds,
                read=config.read_timeout_seconds,
            ),
            headers={"User-Agent": config.user_agent, "Accept": "*/*"},
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FnetClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _throttle(self) -> None:
        wait = self.config.min_request_interval_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, so retries never synchronise."""
        ceiling = min(
            self.config.backoff_base_seconds * (2**attempt), self.config.backoff_max_seconds
        )
        return random.uniform(0, ceiling)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Response:
        """GET `path` relative to the configured base URL, with retries.

        Raises `TransientSourceError` once the retry budget is spent, so the
        caller can record the failure against one entity or document and carry
        on with the rest of the batch.
        """
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        attempts = self.config.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if attempt:
                delay = self._backoff(attempt - 1)
                log.debug(
                    "retrying after backoff",
                    extra={"url": url, "attempt": attempt + 1, "delay_s": round(delay, 2)},
                )
                time.sleep(delay)
            self._throttle()
            started = time.monotonic()
            try:
                with self._client.stream("GET", url, params=params) as response:
                    self._last_request = time.monotonic()
                    elapsed = self._last_request - started

                    if response.status_code in _RETRYABLE_STATUS:
                        last_error = TransientSourceError(
                            f"HTTP {response.status_code} from {url}",
                            context={"url": url, "status": response.status_code},
                        )
                        log.debug(
                            "retryable status",
                            extra={
                                "url": url,
                                "status": response.status_code,
                                "elapsed_s": round(elapsed, 1),
                            },
                        )
                        continue
                    if response.status_code >= 400:
                        # 4xx other than the retryable ones means we asked for
                        # something wrong; retrying would just repeat the mistake.
                        raise SourceContractError(
                            f"HTTP {response.status_code} from {url}",
                            context={"url": url, "status": response.status_code},
                        )

                    body = self._read_capped(response, url)
                    self._last_request = time.monotonic()
                    elapsed = self._last_request - started

                    log.debug(
                        "request ok",
                        extra={
                            "url": url,
                            "status": response.status_code,
                            "bytes": len(body),
                            "elapsed_s": round(elapsed, 1),
                        },
                    )
                    return Response(response.status_code, body, response.headers, url)
            except httpx.HTTPError as exc:
                self._last_request = time.monotonic()
                last_error = TransientSourceError(
                    f"{type(exc).__name__} from {url}: {exc}", context={"url": url}
                )
                log.debug(
                    "request failed",
                    extra={
                        "url": url,
                        "error": type(exc).__name__,
                        "elapsed_s": round(time.monotonic() - started, 1),
                    },
                )

        raise TransientSourceError(
            f"giving up on {url} after {attempts} attempt(s): {last_error}",
            context={"url": url, "attempts": attempts},
        )

    def _read_capped(self, response: httpx.Response, url: str) -> bytes:
        """Read a streamed body, refusing to buffer more than the configured cap.

        The cap exists because the source is untrusted: without it a runaway or
        malicious response would be bounded only by available memory.
        """
        limit = self.config.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise SourceContractError(
                    f"response from {url} exceeds max_response_bytes ({limit})",
                    context={"url": url, "limit": limit},
                )
            chunks.append(chunk)
        return b"".join(chunks)
