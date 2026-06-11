from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import time

import httpx
import pytest

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
    client = CoinGeckoClient(base_url="https://example.com")
    client._request_with_retry = lambda *, url, params: []  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        client.fetch_coin_detail("ethereum")


def test_request_markets_rejects_non_list_payload() -> None:
    client = CoinGeckoClient(base_url="https://example.com")
    client._request_with_retry = lambda *, url, params: {"not": "a-list"}  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        client.request_markets({})


def _patched_client(monkeypatch, responses, **kwargs):
    """CoinGeckoClient whose HTTP layer is replaced by a scripted response queue.

    ``responses`` items are either status codes or exceptions to raise; once the
    queue is exhausted further requests return 200.
    """
    requests_made: list[str] = []
    sleeps: list[float] = []
    queue = list(responses)

    class FakeHTTPClient:
        def __init__(self, timeout=None) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, url, params=None, headers=None):
            requests_made.append(url)
            item = queue.pop(0) if queue else 200
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item, json={"ok": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = CoinGeckoClient(base_url="https://example.com", **kwargs)
    return client, requests_made, sleeps


def test_request_with_retry_retries_retryable_statuses_until_success(monkeypatch) -> None:
    client, requests_made, sleeps = _patched_client(monkeypatch, [429, 503], max_retries=3)

    result = client._request_with_retry(url="https://example.com/ping", params={})

    assert result == {"ok": True}
    assert len(requests_made) == 3
    assert sleeps == [2.0, 4.0]


def test_request_with_retry_raises_status_error_after_exhausting_retries(monkeypatch) -> None:
    client, requests_made, sleeps = _patched_client(monkeypatch, [429, 429, 429], max_retries=2)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        client._request_with_retry(url="https://example.com/ping", params={})

    assert excinfo.value.response.status_code == 429
    assert len(requests_made) == 2
    assert sleeps == [2.0]


def test_request_with_retry_does_not_retry_non_retryable_statuses(monkeypatch) -> None:
    client, requests_made, sleeps = _patched_client(monkeypatch, [404], max_retries=3)

    with pytest.raises(httpx.HTTPStatusError):
        client._request_with_retry(url="https://example.com/ping", params={})

    assert len(requests_made) == 1
    assert sleeps == []


def test_request_with_retry_retries_transport_errors_until_success(monkeypatch) -> None:
    client, requests_made, sleeps = _patched_client(
        monkeypatch, [httpx.ConnectError("boom"), httpx.ConnectError("boom")], max_retries=3
    )

    result = client._request_with_retry(url="https://example.com/ping", params={})

    assert result == {"ok": True}
    assert len(requests_made) == 3
    assert sleeps == [2.0, 4.0]


def test_request_with_retry_reraises_transport_error_after_exhausting_retries(monkeypatch) -> None:
    errors = [httpx.ConnectError("boom"), httpx.ConnectError("boom")]
    client, requests_made, sleeps = _patched_client(monkeypatch, errors, max_retries=2)

    with pytest.raises(httpx.ConnectError):
        client._request_with_retry(url="https://example.com/ping", params={})

    assert len(requests_made) == 2
    assert sleeps == [2.0]
