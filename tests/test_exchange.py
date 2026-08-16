from __future__ import annotations

import unittest
from unittest.mock import patch

from gridbot.exchange import BinanceSpotClient, free_balance


def _exchange_info() -> dict:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
                ],
            }
        ]
    }


class NormalizeMarketSellQuantityTests(unittest.TestCase):
    def _client(self) -> BinanceSpotClient:
        return BinanceSpotClient("key", "secret", "https://api.binance.com")

    @patch.object(BinanceSpotClient, "get_exchange_info")
    def test_rounds_down_to_lot_step(self, get_exchange_info) -> None:
        get_exchange_info.return_value = _exchange_info()
        client = self._client()

        result = client.normalize_market_sell_quantity("BTCUSDT", 60000.0, 0.123456789)

        self.assertEqual(result, 0.12345)

    @patch.object(BinanceSpotClient, "get_exchange_info")
    def test_below_min_notional_returns_none(self, get_exchange_info) -> None:
        get_exchange_info.return_value = _exchange_info()
        client = self._client()

        # 0.00001 BTC * 60000 = 0.6 USDT, below the 5.0 min notional.
        result = client.normalize_market_sell_quantity("BTCUSDT", 60000.0, 0.00001)

        self.assertIsNone(result)

    @patch.object(BinanceSpotClient, "get_exchange_info")
    def test_below_min_qty_returns_none(self, get_exchange_info) -> None:
        get_exchange_info.return_value = _exchange_info()
        client = self._client()

        result = client.normalize_market_sell_quantity("BTCUSDT", 60000.0, 0.000001)

        self.assertIsNone(result)


class FreeBalanceTests(unittest.TestCase):
    def test_returns_free_amount_for_asset(self) -> None:
        account = {"balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}]}
        self.assertEqual(free_balance(account, "BTC"), 0.5)

    def test_returns_zero_for_missing_asset(self) -> None:
        account = {"balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}]}
        self.assertEqual(free_balance(account, "ETH"), 0)


if __name__ == "__main__":
    unittest.main()
