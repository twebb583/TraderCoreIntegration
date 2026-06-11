# TraderCoreIntegration

Shared package for external integration infrastructure used by Trader services.

## Scope

- CoinGecko HTTP client with API-key headers + retry/backoff
- CCXT client with retry/backoff, symbol resolution, and multi-exchange fallback
- Utility parsers for exchange IDs and symbol override config

## Install

```bash
python -m pip install -e .
```

## Tests

```bash
python -m pytest
```
