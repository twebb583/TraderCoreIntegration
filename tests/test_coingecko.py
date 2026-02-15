from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradercoreintegration.coingecko import CoinGeckoClient


def test_fetch_top_altcoins_raw_filters_bitcoin_and_rank() -> None:
    client = CoinGeckoClient(base_url="https://example.com")

    payload = [
        {"id": "bitcoin", "market_cap_rank": 1},
        {"id": "ethereum", "market_cap_rank": 2},
        {"id": "no-rank"},
        {"id": "solana", "market_cap_rank": 5},
    ]

    client.request_markets = lambda params: payload  # type: ignore[method-assign]

    coins = client.fetch_top_altcoins_raw(vs_currency="usd", limit=10)

    assert [coin["id"] for coin in coins] == ["ethereum", "solana"]
