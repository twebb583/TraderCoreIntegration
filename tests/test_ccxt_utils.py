# pyright: reportMissingImports=false
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradercoreintegration.ccxt import (
    CCXTMarketNotFoundError,
    MultiExchangeCCXTClient,
    OHLCVPoint,
    exponential_backoff_seconds,
    resolve_exchange_id,
)  # pyright: ignore[reportMissingImports]
from datetime import datetime, timedelta, timezone


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
