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


def test_fetch_coin_detail_returns_object() -> None:
    client = CoinGeckoClient(base_url="https://example.com")
    detail = {"id": "ethereum", "categories": ["Smart Contract Platform", "Layer 1 (L1)"]}

    captured: dict = {}

    def fake_retry(*, url: str, params):
        captured["url"] = url
        captured["params"] = params
        return detail

    client._request_with_retry = fake_retry  # type: ignore[method-assign]

    result = client.fetch_coin_detail("ethereum")

    assert result == detail
    assert captured["url"].endswith("/coins/ethereum")
    assert captured["params"]["market_data"] == "false"
    assert captured["params"]["tickers"] == "false"


def test_fetch_categories_list_returns_list() -> None:
    client = CoinGeckoClient(base_url="https://example.com")
    payload = [
        {"category_id": "smart-contract-platform", "name": "Smart Contract Platform"},
        {"category_id": "decentralized-finance-defi", "name": "Decentralized Finance (DeFi)"},
    ]

    captured: dict = {}

    def fake_retry(*, url: str, params):
        captured["url"] = url
        return payload

    client._request_with_retry = fake_retry  # type: ignore[method-assign]

    result = client.fetch_categories_list()

    assert result == payload
    assert captured["url"].endswith("/coins/categories/list")


def test_fetch_coin_detail_rejects_non_object() -> None:
    import pytest

    client = CoinGeckoClient(base_url="https://example.com")
    client._request_with_retry = lambda *, url, params: []  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        client.fetch_coin_detail("ethereum")
