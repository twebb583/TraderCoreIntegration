from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Protocol

import ccxt
from ccxt.base.errors import (
    BadSymbol,
    DDoSProtection,
    ExchangeError,
    ExchangeNotAvailable,
    NetworkError,
    RequestTimeout,
    RateLimitExceeded,
)

_RETRYABLE_CCXT_ERRORS = (
    NetworkError,
    RequestTimeout,
    RateLimitExceeded,
    DDoSProtection,
    ExchangeNotAvailable,
)

_EXCHANGE_ID_ALIASES = {
    "gate": "gateio",
}


@dataclass
class OHLCVPoint:
    captured_at: datetime
    price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


class CCXTMarketNotFoundError(ExchangeError):
    pass


class CCXTClient:
    def __init__(
        self,
        exchange_id: str,
        *,
        timeout_seconds: int = 30,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        ohlcv_timeframe: str = "1d",
        ohlcv_limit: int = 1000,
        enable_rate_limit: bool = True,
        symbol_overrides: dict[str, str] | None = None,
    ) -> None:
        resolved_exchange_id = resolve_exchange_id(exchange_id)
        self._exchange_id = resolved_exchange_id
        exchange_class = getattr(ccxt, resolved_exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unsupported CCXT exchange: {exchange_id}")

        self._exchange = exchange_class(
            {
                "enableRateLimit": enable_rate_limit,
                "timeout": timeout_seconds * 1000,
            }
        )
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._ohlcv_timeframe = ohlcv_timeframe
        self._ohlcv_limit = max(1, ohlcv_limit)
        self._symbol_overrides = {
            _normalize_key(coin_id): symbol for coin_id, symbol in (symbol_overrides or {}).items()
        }

        self._markets_loaded = False
        self._coin_name_to_code: dict[str, str] = {}
        self._successful_requests = 0

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    def fetch_ohlcv_range(
        self,
        coin_id: str,
        vs_currency: str,
        from_dt: datetime,
        to_dt: datetime,
        base_symbol: str | None = None,
    ) -> list[OHLCVPoint]:
        self._ensure_markets_loaded()

        symbol = self._resolve_symbol(coin_id=coin_id, vs_currency=vs_currency, base_symbol=base_symbol)
        since_ms = int(from_dt.timestamp() * 1000)
        to_ms = int(to_dt.timestamp() * 1000)
        frame_ms = _timeframe_to_milliseconds(self._ohlcv_timeframe)

        points_by_ts: dict[int, OHLCVPoint] = {}

        while since_ms <= to_ms:
            _since = since_ms
            candles = self._with_retries(
                lambda _s=_since: self._exchange.fetch_ohlcv(
                    symbol=symbol,
                    timeframe=self._ohlcv_timeframe,
                    since=_s,
                    limit=self._ohlcv_limit,
                )
            )

            if not candles:
                break

            from_ms = int(from_dt.timestamp() * 1000)
            for row in candles:
                if len(row) < 5:
                    continue

                ts_ms = int(row[0])
                open_price = float(row[1]) if len(row) > 1 and row[1] is not None else None
                high_price = float(row[2]) if len(row) > 2 and row[2] is not None else None
                low_price = float(row[3]) if len(row) > 3 and row[3] is not None else None
                close_price = float(row[4]) if len(row) > 4 and row[4] is not None else 0.0
                volume = float(row[5]) if len(row) > 5 and row[5] is not None else None

                if close_price <= 0:
                    continue

                if ts_ms < from_ms or ts_ms > to_ms:
                    continue

                points_by_ts[ts_ms] = OHLCVPoint(
                    captured_at=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    price=close_price,
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                )

            last_ts = int(candles[-1][0])
            if last_ts >= to_ms:
                break

            next_since = last_ts + frame_ms
            if next_since <= since_ms:
                break
            since_ms = next_since

        self._successful_requests += 1
        return [points_by_ts[ts] for ts in sorted(points_by_ts)]

    def reset_exchange_usage(self) -> None:
        self._successful_requests = 0

    def snapshot_exchange_usage(self) -> dict[str, int]:
        return {self._exchange_id: self._successful_requests}

    def _ensure_markets_loaded(self) -> None:
        if self._markets_loaded:
            return

        self._with_retries(self._exchange.load_markets)
        currencies = getattr(self._exchange, "currencies", {}) or {}

        for code, data in currencies.items():
            self._coin_name_to_code[_normalize_key(code)] = code

            if isinstance(data, dict):
                name = data.get("name")
                cur_id = data.get("id")
                if isinstance(name, str) and name.strip():
                    self._coin_name_to_code[_normalize_key(name)] = code
                if isinstance(cur_id, str) and cur_id.strip():
                    self._coin_name_to_code[_normalize_key(cur_id)] = code

        self._markets_loaded = True

    def _resolve_symbol(self, coin_id: str, vs_currency: str, base_symbol: str | None = None) -> str:
        if not self._markets_loaded:
            raise RuntimeError("Markets are not loaded")

        normalized_coin_id = _normalize_key(coin_id)
        override = self._symbol_overrides.get(normalized_coin_id)
        markets = getattr(self._exchange, "markets", {}) or {}

        if override:
            if override in markets:
                return override
            raise CCXTMarketNotFoundError(f"Configured CCXT symbol override not found in markets: {override}")

        base_code: str | None = None
        if base_symbol:
            normalized_symbol = _normalize_key(base_symbol)
            base_code = self._coin_name_to_code.get(normalized_symbol, normalized_symbol.upper())

        if base_code is None:
            base_code = self._coin_name_to_code.get(normalized_coin_id)

        if base_code is None:
            slug_token = coin_id.split("-")[0].strip()
            base_code = self._coin_name_to_code.get(_normalize_key(slug_token), slug_token.upper())

        requested_quote = vs_currency.upper()
        if requested_quote in ("BTC", "ETH"):
            candidate_quotes = [requested_quote]
        else:
            candidate_quotes = [requested_quote, "USDT", "USD", "USDC", "BUSD"]

        seen_quotes: set[str] = set()
        for quote in candidate_quotes:
            if quote in seen_quotes:
                continue
            seen_quotes.add(quote)

            symbol = f"{base_code}/{quote}"
            if symbol in markets:
                return symbol

        for symbol, market in markets.items():
            if not isinstance(market, dict):
                continue
            if market.get("base") == base_code and market.get("quote") in seen_quotes:
                return symbol

        raise CCXTMarketNotFoundError(
            f"No CCXT market found for coin_id={coin_id}, resolved base={base_code}, quote={requested_quote}"
        )

    def _with_retries(self, operation):
        attempts = self._max_retries + 1

        for attempt in range(attempts):
            try:
                return operation()
            except _RETRYABLE_CCXT_ERRORS:
                if attempt >= self._max_retries:
                    raise
                time.sleep(exponential_backoff_seconds(attempt=attempt, base=self._backoff_seconds))


class ExchangePriceClient(Protocol):
    @property
    def exchange_id(self) -> str: ...

    def fetch_ohlcv_range(
        self,
        coin_id: str,
        vs_currency: str,
        from_dt: datetime,
        to_dt: datetime,
        base_symbol: str | None = None,
    ) -> list[OHLCVPoint]: ...

    def reset_exchange_usage(self) -> None: ...

    def snapshot_exchange_usage(self) -> dict[str, int]: ...


class MultiExchangeCCXTClient:
    def __init__(self, clients: list[ExchangePriceClient]) -> None:
        if not clients:
            raise ValueError("At least one CCXT client must be provided")
        self._clients = list(clients)
        self._exchange_usage: dict[str, int] = {}
        for client in self._clients:
            exchange_id = getattr(client, "exchange_id", client.__class__.__name__)
            self._exchange_usage[str(exchange_id)] = 0

    def fetch_ohlcv_range(
        self,
        coin_id: str,
        vs_currency: str,
        from_dt: datetime,
        to_dt: datetime,
        base_symbol: str | None = None,
    ) -> list[OHLCVPoint]:
        last_error: Exception | None = None

        for client in self._clients:
            try:
                points = client.fetch_ohlcv_range(
                    coin_id=coin_id,
                    vs_currency=vs_currency,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    base_symbol=base_symbol,
                )
                exchange_id = str(getattr(client, "exchange_id", client.__class__.__name__))
                self._exchange_usage[exchange_id] = self._exchange_usage.get(exchange_id, 0) + 1
                return points
            except (CCXTMarketNotFoundError, BadSymbol) as exc:
                last_error = exc
                continue

        if last_error is not None:
            raise last_error
        return []

    def reset_exchange_usage(self) -> None:
        for exchange_id in list(self._exchange_usage):
            self._exchange_usage[exchange_id] = 0
        for client in self._clients:
            client.reset_exchange_usage()

    def snapshot_exchange_usage(self) -> dict[str, int]:
        usage = dict(self._exchange_usage)
        for client in self._clients:
            for exchange_id, count in client.snapshot_exchange_usage().items():
                if exchange_id not in usage:
                    usage[exchange_id] = 0
                _ = count
        return usage


def parse_symbol_overrides(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}

    pairs: dict[str, str] = {}
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(f"Invalid CCXT symbol override entry: {token}")
        coin_id, symbol = token.split(":", 1)
        coin_id = coin_id.strip()
        symbol = symbol.strip()
        if not coin_id or not symbol:
            raise ValueError(f"Invalid CCXT symbol override entry: {token}")
        pairs[coin_id] = symbol

    return pairs


def parse_exchange_ids(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [exchange_id.strip() for exchange_id in raw.split(",") if exchange_id.strip()]


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def resolve_exchange_id(exchange_id: str) -> str:
    normalized = exchange_id.strip().lower()
    return _EXCHANGE_ID_ALIASES.get(normalized, normalized)


def _timeframe_to_milliseconds(timeframe: str) -> int:
    if timeframe.endswith("m"):
        return int(timeframe[:-1]) * 60_000
    if timeframe.endswith("h"):
        return int(timeframe[:-1]) * 3_600_000
    if timeframe.endswith("d"):
        return int(timeframe[:-1]) * 86_400_000
    if timeframe.endswith("w"):
        return int(timeframe[:-1]) * 604_800_000
    raise ValueError(f"Unsupported CCXT timeframe: {timeframe}")


def exponential_backoff_seconds(*, attempt: int, base: float) -> float:
    return base * (2**attempt)
