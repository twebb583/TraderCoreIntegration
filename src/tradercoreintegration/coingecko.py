from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class CoinGeckoClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 30,
        api_key: str = "",
        max_retries: int = 3,
        backoff_base: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._headers: dict[str, str] = {}
        if api_key:
            # CoinGecko Pro/Demo uses x-cg-demo-api-key or x-cg-pro-api-key.
            # The demo key header works for both demo and free-tier API keys.
            self._headers["x-cg-demo-api-key"] = api_key
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def request_markets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self._base_url}/coins/markets"
        return self._request_with_retry(url=url, params=params)

    def fetch_top_altcoins_raw(self, vs_currency: str, limit: int = 100) -> list[dict[str, Any]]:
        params = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": max(limit + 1, 250),
            "page": 1,
            "sparkline": "false",
        }
        payload = self.request_markets(params)

        markets: list[dict[str, Any]] = []
        for item in payload:
            coin_id = str(item.get("id", ""))
            if coin_id == "bitcoin":
                continue

            rank = item.get("market_cap_rank")
            if rank is None:
                continue

            markets.append(item)

        markets.sort(key=lambda coin: int(coin.get("market_cap_rank") or 10**9))
        return markets[:limit]

    def _request_with_retry(self, *, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        last_response: httpx.Response | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.get(url, params=params, headers=self._headers)
                last_response = response

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt >= self._max_retries:
                        break
                    wait = self._backoff_base**attempt
                    logger.warning(
                        "CoinGecko returned %d (attempt %d/%d), retrying in %.1fs",
                        response.status_code,
                        attempt,
                        self._max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    raise ValueError("CoinGecko markets response must be a list")
                return data

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                wait = self._backoff_base**attempt
                logger.warning(
                    "CoinGecko transport error (attempt %d/%d): %s, retrying in %.1fs",
                    attempt,
                    self._max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)

        if last_exc is not None:
            raise last_exc
        if last_response is not None:
            raise httpx.HTTPStatusError(
                f"CoinGecko returned retryable status after {self._max_retries} attempts",
                request=last_response.request,
                response=last_response,
            )
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            f"CoinGecko returned retryable status after {self._max_retries} attempts",
            request=request,
            response=httpx.Response(status_code=500, request=request),
        )
