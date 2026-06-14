from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Any, Protocol

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

_TIMEFRAME_UNIT_MS = {
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}

# Quotes tried, in order, when the requested quote has no market on the exchange.
_FALLBACK_QUOTES = ("USDT", "USD", "USDC", "BUSD")


@dataclass
class OHLCVPoint:
    captured_at: datetime
    price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


@dataclass
class ResolvedSpotMarket:
    """Resolved spot market metadata for an exchange.

    Used by the multi-exchange spot volume ingestion path to record the
    actual quote currency CCXT routed to (which may differ from the requested
    quote — e.g. requested USDT, exchange only quotes USD).
    """

    exchange_id: str
    symbol: str
    base: str
    quote: str


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
        self._exchange_id = resolve_exchange_id(exchange_id)
        exchange_class = getattr(ccxt, self._exchange_id, None)
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
        self._market_load_error: CCXTMarketNotFoundError | None = None
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

        symbol = self._resolve_symbol(
            coin_id=coin_id, vs_currency=vs_currency, base_symbol=base_symbol
        )
        from_ms = int(from_dt.timestamp() * 1000)
        to_ms = int(to_dt.timestamp() * 1000)
        frame_ms = _timeframe_to_milliseconds(self._ohlcv_timeframe)

        points_by_time: dict[datetime, OHLCVPoint] = {}
        since_ms = from_ms

        while since_ms <= to_ms:
            candles = self._with_retries(
                lambda since=since_ms: self._exchange.fetch_ohlcv(
                    symbol=symbol,
                    timeframe=self._ohlcv_timeframe,
                    since=since,
                    limit=self._ohlcv_limit,
                )
            )
            if not candles:
                break

            for row in candles:
                point = _parse_candle(row, from_ms=from_ms, to_ms=to_ms)
                if point is not None:
                    points_by_time[point.captured_at] = point

            last_ts = int(candles[-1][0])
            if last_ts >= to_ms:
                break

            next_since = last_ts + frame_ms
            if next_since <= since_ms:
                break
            since_ms = next_since

        self._successful_requests += 1
        return [points_by_time[ts] for ts in sorted(points_by_time)]

    def reset_exchange_usage(self) -> None:
        self._successful_requests = 0

    def snapshot_exchange_usage(self) -> dict[str, int]:
        return {self._exchange_id: self._successful_requests}

    def resolve_spot_market(
        self,
        coin_id: str,
        vs_currency: str,
        base_symbol: str | None = None,
    ) -> ResolvedSpotMarket:
        """Resolve the spot market for ``coin_id`` on this exchange.

        Returns the resolved CCXT symbol along with the actual base/quote
        codes from the exchange's market metadata. Raises
        ``CCXTMarketNotFoundError`` (or ``BadSymbol``) if no market exists.
        """
        self._ensure_markets_loaded()
        symbol = self._resolve_symbol(
            coin_id=coin_id, vs_currency=vs_currency, base_symbol=base_symbol
        )
        market = self._markets().get(symbol) or {}
        base_part, _, quote_part = symbol.partition("/")
        return ResolvedSpotMarket(
            exchange_id=self._exchange_id,
            symbol=symbol,
            base=str(market.get("base") or base_part),
            quote=str(market.get("quote") or quote_part),
        )

    def _markets(self) -> dict:
        return getattr(self._exchange, "markets", {}) or {}

    def _ensure_markets_loaded(self) -> None:
        if self._market_load_error is not None:
            raise self._market_load_error
        if self._markets_loaded:
            return

        try:
            self._with_retries(self._exchange.load_markets)
        except TypeError as exc:
            if not _is_ccxt_market_id_sort_error(exc):
                raise
            self._market_load_error = CCXTMarketNotFoundError(
                f"CCXT market metadata for exchange={self._exchange_id} could not be loaded"
            )
            raise self._market_load_error from exc
        currencies = getattr(self._exchange, "currencies", {}) or {}

        for code, data in currencies.items():
            self._coin_name_to_code[_normalize_key(code)] = code
            if not isinstance(data, dict):
                continue
            for alias in (data.get("name"), data.get("id")):
                if isinstance(alias, str) and alias.strip():
                    self._coin_name_to_code[_normalize_key(alias)] = code

        self._markets_loaded = True

    def _resolve_symbol(
        self, coin_id: str, vs_currency: str, base_symbol: str | None = None
    ) -> str:
        if not self._markets_loaded:
            raise RuntimeError("Markets are not loaded")

        markets = self._markets()

        override = self._symbol_overrides.get(_normalize_key(coin_id))
        if override:
            if override in markets:
                return override
            raise CCXTMarketNotFoundError(
                f"Configured CCXT symbol override not found in markets: {override}"
            )

        base_code = self._resolve_base_code(coin_id=coin_id, base_symbol=base_symbol)

        requested_quote = vs_currency.upper()
        if requested_quote in ("BTC", "ETH"):
            candidate_quotes = [requested_quote]
        else:
            candidate_quotes = list(dict.fromkeys((requested_quote, *_FALLBACK_QUOTES)))

        for quote in candidate_quotes:
            symbol = f"{base_code}/{quote}"
            if symbol in markets:
                return symbol

        quote_set = set(candidate_quotes)
        for symbol, market in markets.items():
            if (
                isinstance(market, dict)
                and market.get("base") == base_code
                and market.get("quote") in quote_set
            ):
                return symbol

        raise CCXTMarketNotFoundError(
            f"No CCXT market found for coin_id={coin_id}, "
            f"resolved base={base_code}, quote={requested_quote}"
        )

    def _resolve_base_code(self, coin_id: str, base_symbol: str | None) -> str:
        if base_symbol:
            normalized = _normalize_key(base_symbol)
            return self._coin_name_to_code.get(normalized, normalized.upper())

        base_code = self._coin_name_to_code.get(_normalize_key(coin_id))
        if base_code is not None:
            return base_code

        slug_token = coin_id.split("-")[0].strip()
        return self._coin_name_to_code.get(_normalize_key(slug_token), slug_token.upper())

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
        self._exchange_usage: dict[str, int] = {
            _client_exchange_id(client): 0 for client in self._clients
        }

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
            except (CCXTMarketNotFoundError, BadSymbol) as exc:
                last_error = exc
                continue
            exchange_id = _client_exchange_id(client)
            self._exchange_usage[exchange_id] = self._exchange_usage.get(exchange_id, 0) + 1
            return points

        assert last_error is not None  # __init__ guarantees at least one client
        raise last_error

    def reset_exchange_usage(self) -> None:
        self._exchange_usage = dict.fromkeys(self._exchange_usage, 0)
        for client in self._clients:
            client.reset_exchange_usage()

    def snapshot_exchange_usage(self) -> dict[str, int]:
        usage = dict(self._exchange_usage)
        for client in self._clients:
            for exchange_id in client.snapshot_exchange_usage():
                usage.setdefault(exchange_id, 0)
        return usage


def parse_symbol_overrides(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for token in (item.strip() for item in raw.split(",")):
        if not token:
            continue
        coin_id, sep, symbol = token.partition(":")
        coin_id = coin_id.strip()
        symbol = symbol.strip()
        if not sep or not coin_id or not symbol:
            raise ValueError(f"Invalid CCXT symbol override entry: {token}")
        pairs[coin_id] = symbol
    return pairs


def parse_exchange_ids(raw: str) -> list[str]:
    return [exchange_id.strip() for exchange_id in raw.split(",") if exchange_id.strip()]


def resolve_exchange_id(exchange_id: str) -> str:
    normalized = exchange_id.strip().lower()
    return _EXCHANGE_ID_ALIASES.get(normalized, normalized)


def exponential_backoff_seconds(*, attempt: int, base: float) -> float:
    return base * (2**attempt)


def _is_ccxt_market_id_sort_error(exc: TypeError) -> bool:
    message = str(exc)
    return "'<' not supported" in message and "str" in message and "NoneType" in message


def _client_exchange_id(client: object) -> str:
    return str(getattr(client, "exchange_id", client.__class__.__name__))


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _timeframe_to_milliseconds(timeframe: str) -> int:
    unit_ms = _TIMEFRAME_UNIT_MS.get(timeframe[-1:])
    if unit_ms is None:
        raise ValueError(f"Unsupported CCXT timeframe: {timeframe}")
    return int(timeframe[:-1]) * unit_ms


def _parse_candle(row: Any, *, from_ms: int, to_ms: int) -> OHLCVPoint | None:
    """Convert one CCXT candle row to an OHLCVPoint.

    Returns None for rows that are malformed, have a non-positive close, or
    fall outside the requested time range.
    """
    if len(row) < 5 or row[0] is None or row[4] is None:
        return None

    ts_ms = int(row[0])
    close_price = float(row[4])
    if close_price <= 0 or not from_ms <= ts_ms <= to_ms:
        return None

    return OHLCVPoint(
        captured_at=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        price=close_price,
        open=_optional_float(row[1]),
        high=_optional_float(row[2]),
        low=_optional_float(row[3]),
        close=close_price,
        volume=_optional_float(row[5]) if len(row) > 5 else None,
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
