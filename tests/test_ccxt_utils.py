# pyright: reportMissingImports=false
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import datetime, timedelta, timezone
import time

import pytest
from ccxt.base.errors import BadSymbol, RateLimitExceeded

from tradercoreintegration.ccxt import (
    CCXTClient,
    CCXTMarketNotFoundError,
    MultiExchangeCCXTClient,
    OHLCVPoint,
    ResolvedSpotMarket,
    _timeframe_to_milliseconds,
    exponential_backoff_seconds,
    parse_exchange_ids,
    parse_symbol_overrides,
    resolve_exchange_id,
)


def test_exponential_backoff_seconds_doubles() -> None:
    assert exponential_backoff_seconds(attempt=0, base=2.0) == 2.0
    assert exponential_backoff_seconds(attempt=2, base=1.5) == 6.0


def test_exchange_alias_gate_resolves_to_gateio() -> None:
    assert resolve_exchange_id("gate") == "gateio"


def test_multi_exchange_client_falls_back_when_market_missing() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)

    class MissingMarketClient:
        exchange_id = "missing"

        def fetch_ohlcv_range(self, coin_id, vs_currency, from_dt, to_dt, base_symbol=None):
            raise CCXTMarketNotFoundError("No market")

        def reset_exchange_usage(self) -> None:
            pass

        def snapshot_exchange_usage(self) -> dict[str, int]:
            return {"missing": 0}

    class CoinbaseClient:
        exchange_id = "coinbase"

        def fetch_ohlcv_range(self, coin_id, vs_currency, from_dt, to_dt, base_symbol=None):
            return [OHLCVPoint(captured_at=now, price=456.0)]

        def reset_exchange_usage(self) -> None:
            pass

        def snapshot_exchange_usage(self) -> dict[str, int]:
            return {"coinbase": 0}

    client = MultiExchangeCCXTClient([MissingMarketClient(), CoinbaseClient()])
    points = client.fetch_ohlcv_range(
        coin_id="some-coin",
        vs_currency="usdt",
        from_dt=now - timedelta(days=1),
        to_dt=now,
    )

    assert len(points) == 1
    assert points[0].price == 456.0
    assert client.snapshot_exchange_usage().get("missing") == 0
    assert client.snapshot_exchange_usage().get("coinbase") == 1


def test_multi_exchange_client_raises_last_error_when_all_exchanges_fail() -> None:
    now = datetime(2026, 2, 14, tzinfo=timezone.utc)

    class BadSymbolClient:
        exchange_id = "badsym"

        def fetch_ohlcv_range(self, coin_id, vs_currency, from_dt, to_dt, base_symbol=None):
            raise BadSymbol("bad symbol")

        def reset_exchange_usage(self) -> None:
            pass

        def snapshot_exchange_usage(self) -> dict[str, int]:
            return {}

    class MissingMarketClient:
        exchange_id = "missing"

        def fetch_ohlcv_range(self, coin_id, vs_currency, from_dt, to_dt, base_symbol=None):
            raise CCXTMarketNotFoundError("no market")

        def reset_exchange_usage(self) -> None:
            pass

        def snapshot_exchange_usage(self) -> dict[str, int]:
            return {}

    client = MultiExchangeCCXTClient([BadSymbolClient(), MissingMarketClient()])

    with pytest.raises(CCXTMarketNotFoundError, match="no market"):
        client.fetch_ohlcv_range(
            coin_id="some-coin",
            vs_currency="usdt",
            from_dt=now - timedelta(days=1),
            to_dt=now,
        )


def test_multi_exchange_client_reset_zeroes_usage_and_propagates() -> None:
    now = datetime(2026, 2, 14, tzinfo=timezone.utc)

    class OkClient:
        exchange_id = "ok"

        def __init__(self) -> None:
            self.reset_calls = 0

        def fetch_ohlcv_range(self, coin_id, vs_currency, from_dt, to_dt, base_symbol=None):
            return [OHLCVPoint(captured_at=now, price=1.0)]

        def reset_exchange_usage(self) -> None:
            self.reset_calls += 1

        def snapshot_exchange_usage(self) -> dict[str, int]:
            return {}

    inner = OkClient()
    client = MultiExchangeCCXTClient([inner])
    client.fetch_ohlcv_range(
        coin_id="c", vs_currency="usdt", from_dt=now - timedelta(days=1), to_dt=now
    )
    assert client.snapshot_exchange_usage() == {"ok": 1}

    client.reset_exchange_usage()
    assert client.snapshot_exchange_usage() == {"ok": 0}
    assert inner.reset_calls == 1


def test_resolved_spot_market_dataclass_roundtrip() -> None:
    market = ResolvedSpotMarket(exchange_id="binance", symbol="ETH/USDT", base="ETH", quote="USDT")
    assert market.exchange_id == "binance"
    assert market.symbol == "ETH/USDT"
    assert market.base == "ETH"
    assert market.quote == "USDT"


def _stub_market_client(
    markets: dict,
    *,
    overrides: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> CCXTClient:
    """CCXTClient with injected markets and name lookup so no network is touched."""
    client = CCXTClient(exchange_id="binance", symbol_overrides=overrides)
    client._exchange.markets = markets  # type: ignore[attr-defined]
    client._coin_name_to_code.update(names or {})
    client._markets_loaded = True
    return client


def test_ccxt_client_resolve_spot_market_uses_market_metadata() -> None:
    client = _stub_market_client(
        {"ETH/USDT": {"base": "ETH", "quote": "USDT"}},
        names={"ethereum": "ETH"},
    )

    market = client.resolve_spot_market(coin_id="ethereum", vs_currency="usdt")
    assert market.symbol == "ETH/USDT"
    assert market.base == "ETH"
    assert market.quote == "USDT"
    assert market.exchange_id == "binance"


def test_resolve_spot_market_prefers_symbol_override() -> None:
    client = _stub_market_client(
        {"FOO/USDT": {"base": "FOO", "quote": "USDT"}},
        overrides={"Some-Coin": "FOO/USDT"},
    )

    market = client.resolve_spot_market(coin_id="some-coin", vs_currency="usdt")
    assert market.symbol == "FOO/USDT"


def test_resolve_spot_market_rejects_override_missing_from_markets() -> None:
    client = _stub_market_client({}, overrides={"some-coin": "FOO/USDT"})

    with pytest.raises(CCXTMarketNotFoundError):
        client.resolve_spot_market(coin_id="some-coin", vs_currency="usdt")


def test_resolve_spot_market_falls_back_through_stable_quotes() -> None:
    client = _stub_market_client(
        {"ETH/USD": {"base": "ETH", "quote": "USD"}},
        names={"ethereum": "ETH"},
    )

    market = client.resolve_spot_market(coin_id="ethereum", vs_currency="usdt")
    assert market.symbol == "ETH/USD"
    assert market.quote == "USD"


def test_resolve_spot_market_scans_market_metadata_for_nonstandard_keys() -> None:
    client = _stub_market_client(
        {"ETH/USDT:USDT": {"base": "ETH", "quote": "USDT"}},
        names={"ethereum": "ETH"},
    )

    market = client.resolve_spot_market(coin_id="ethereum", vs_currency="usdt")
    assert market.symbol == "ETH/USDT:USDT"


def test_resolve_spot_market_btc_quote_does_not_fall_back_to_stables() -> None:
    client = _stub_market_client(
        {"ETH/USDT": {"base": "ETH", "quote": "USDT"}},
        names={"ethereum": "ETH"},
    )

    with pytest.raises(CCXTMarketNotFoundError):
        client.resolve_spot_market(coin_id="ethereum", vs_currency="btc")


def test_resolve_spot_market_falls_back_to_coin_id_slug() -> None:
    client = _stub_market_client({"RENDER/USDT": {"base": "RENDER", "quote": "USDT"}})

    market = client.resolve_spot_market(coin_id="render-token", vs_currency="usdt")
    assert market.symbol == "RENDER/USDT"


def test_fetch_ohlcv_range_pages_dedupes_and_filters_rows() -> None:
    day_ms = 86_400_000
    from_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    to_dt = datetime(2026, 1, 4, tzinfo=timezone.utc)
    t0 = int(from_dt.timestamp() * 1000)
    t1, t2, t3 = t0 + day_ms, t0 + 2 * day_ms, t0 + 3 * day_ms

    pages = [
        [
            [t0 - day_ms, 1.0, 1.0, 1.0, 1.0, 1.0],  # before from_dt -> dropped
            [t0, 10.0, 12.0, 9.0, 11.0, 100.0],  # kept
            [t1],  # malformed row -> dropped
            [t1, None, None, None, 0, 5.0],  # non-positive close -> dropped
            [t2, 1.0, 2.0, 0.5, 1.5],  # no volume column -> kept with volume None
        ],
        [
            [t1, 9.0, 9.0, 9.0, 9.9, 9.0],  # re-fetched timestamp: newest data wins
            [t3, 20.0, 21.0, 19.0, 20.5, 50.0],  # kept; reaches to_dt -> stop paging
        ],
    ]

    class FakeExchange:
        markets = {"ETH/USDT": {"base": "ETH", "quote": "USDT"}}

        def __init__(self) -> None:
            self.calls: list[int] = []

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            assert symbol == "ETH/USDT"
            assert timeframe == "1d"
            self.calls.append(since)
            return pages[len(self.calls) - 1]

    client = CCXTClient(exchange_id="binance")
    fake = FakeExchange()
    client._exchange = fake  # type: ignore[assignment]
    client._coin_name_to_code["ethereum"] = "ETH"
    client._markets_loaded = True

    points = client.fetch_ohlcv_range(
        coin_id="ethereum", vs_currency="usdt", from_dt=from_dt, to_dt=to_dt
    )

    assert fake.calls == [t0, t3]
    assert [int(p.captured_at.timestamp() * 1000) for p in points] == [t0, t1, t2, t3]
    assert [p.close for p in points] == [11.0, 9.9, 1.5, 20.5]
    assert [p.price for p in points] == [11.0, 9.9, 1.5, 20.5]
    assert (points[0].open, points[0].high, points[0].low) == (10.0, 12.0, 9.0)
    assert [p.volume for p in points] == [100.0, 9.0, None, 50.0]
    assert client.snapshot_exchange_usage() == {"binance": 1}


def test_market_load_sort_type_error_is_treated_as_missing_market() -> None:
    client = CCXTClient(exchange_id="binance")

    class BrokenExchange:
        currencies = {}

        def __init__(self) -> None:
            self.load_calls = 0

        def load_markets(self):
            self.load_calls += 1
            raise TypeError("'<' not supported between instances of 'str' and 'NoneType'")

    exchange = BrokenExchange()
    client._exchange = exchange  # type: ignore[assignment]

    with pytest.raises(CCXTMarketNotFoundError):
        client.resolve_spot_market(coin_id="ethereum", vs_currency="usdt")
    with pytest.raises(CCXTMarketNotFoundError):
        client.resolve_spot_market(coin_id="solana", vs_currency="usdt")

    assert exchange.load_calls == 1


def test_with_retries_retries_retryable_errors_until_success(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = CCXTClient(exchange_id="binance", max_retries=2, backoff_seconds=1.0)

    outcomes = [RateLimitExceeded("slow down"), RateLimitExceeded("slow down"), "ok"]

    def flaky():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert client._with_retries(flaky) == "ok"
    assert sleeps == [1.0, 2.0]


def test_with_retries_raises_after_max_retries(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    client = CCXTClient(exchange_id="binance", max_retries=1, backoff_seconds=1.0)

    calls = {"count": 0}

    def always_rate_limited():
        calls["count"] += 1
        raise RateLimitExceeded("nope")

    with pytest.raises(RateLimitExceeded):
        client._with_retries(always_rate_limited)
    assert calls["count"] == 2  # initial attempt + 1 retry


def test_parse_symbol_overrides_parses_entries_and_strips_whitespace() -> None:
    assert parse_symbol_overrides("") == {}
    assert parse_symbol_overrides(" eth : ETH/USDT ,sol:SOL/USDT,") == {
        "eth": "ETH/USDT",
        "sol": "SOL/USDT",
    }


def test_parse_symbol_overrides_rejects_malformed_entries() -> None:
    with pytest.raises(ValueError):
        parse_symbol_overrides("missing-separator")
    with pytest.raises(ValueError):
        parse_symbol_overrides("eth:")


def test_parse_exchange_ids_splits_and_skips_empty_tokens() -> None:
    assert parse_exchange_ids("") == []
    assert parse_exchange_ids(" binance, gate ,,okx ") == ["binance", "gate", "okx"]


def test_timeframe_to_milliseconds_supports_known_units() -> None:
    assert _timeframe_to_milliseconds("15m") == 15 * 60_000
    assert _timeframe_to_milliseconds("4h") == 4 * 3_600_000
    assert _timeframe_to_milliseconds("1d") == 86_400_000
    assert _timeframe_to_milliseconds("1w") == 604_800_000
    with pytest.raises(ValueError):
        _timeframe_to_milliseconds("10s")
