from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw_value = response.headers.get("retry-after")
    if not raw_value:
        return None

    value = raw_value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()

    if seconds <= 0:
        return None
    return seconds


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
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base

    def request_markets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self._base_url}/coins/markets"
        data = self._request_with_retry(url=url, params=params)
        if not isinstance(data, list):
            raise ValueError("CoinGecko markets response must be a list")
        return data

    def fetch_coin_detail(self, coin_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        }
        data = self._request_with_retry(url=url, params=params)
        if not isinstance(data, dict):
            raise ValueError(f"CoinGecko detail response for {coin_id} must be an object")
        return data

    def fetch_categories_list(self) -> list[dict[str, Any]]:
        url = f"{self._base_url}/coins/categories/list"
        data = self._request_with_retry(url=url, params={})
        if not isinstance(data, list):
            raise ValueError("CoinGecko categories list response must be a list")
        return data

    def fetch_top_altcoins_raw(self, vs_currency: str, limit: int = 100) -> list[dict[str, Any]]:
        params = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": max(limit + 1, 250),
            "page": 1,
            "sparkline": "false",
        }
        markets = [
            item
            for item in self.request_markets(params)
            if str(item.get("id", "")) != "bitcoin" and item.get("market_cap_rank") is not None
        ]
        markets.sort(key=lambda coin: int(coin.get("market_cap_rank") or 10**9))
        return markets[:limit]

    def _request_with_retry(self, *, url: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.get(url, params=params, headers=self._headers)
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
                    return response.json()
                last_error = httpx.HTTPStatusError(
                    f"CoinGecko returned retryable status after {self._max_retries} attempts",
                    request=response.request,
                    response=response,
                )
                failure = f"returned {response.status_code}"
            except httpx.TransportError as exc:
                last_error = exc
                failure = f"transport error: {exc}"

            if attempt >= self._max_retries:
                break

            wait = (
                _retry_after_seconds(last_error.response)
                if isinstance(last_error, httpx.HTTPStatusError)
                else None
            ) or self._backoff_base**attempt
            logger.warning(
                "CoinGecko %s (attempt %d/%d), retrying in %.1fs",
                failure,
                attempt,
                self._max_retries,
                wait,
            )
            time.sleep(wait)

        assert last_error is not None  # loop always runs: max_retries is clamped to >= 1
        raise last_error
