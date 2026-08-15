from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

import requests

from gridbot.exchange import is_unknown_order_error
from gridbot.main import _cancel_all_orders_ignoring_missing


def _http_error(status_code: int, code: int, msg: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.binance.com/api/v3/openOrders"
    response._content = json.dumps({"code": code, "msg": msg}).encode()
    error = requests.HTTPError(response=response)
    return error


class IsUnknownOrderErrorTests(unittest.TestCase):
    def test_code_minus_2011_is_unknown_order(self) -> None:
        error = _http_error(400, -2011, "Unknown order sent.")
        self.assertTrue(is_unknown_order_error(error))

    def test_insufficient_balance_is_not_unknown_order(self) -> None:
        error = _http_error(400, -2010, "Account has insufficient balance for requested action.")
        self.assertFalse(is_unknown_order_error(error))


class CancelAllOrdersIgnoringMissingTests(unittest.TestCase):
    def test_swallows_unknown_order_error(self) -> None:
        exchange = Mock()
        exchange.cancel_all_orders.side_effect = _http_error(400, -2011, "Unknown order sent.")

        _cancel_all_orders_ignoring_missing(exchange, "EDENUSDT")

        exchange.cancel_all_orders.assert_called_once_with("EDENUSDT")

    def test_reraises_other_http_errors(self) -> None:
        exchange = Mock()
        exchange.cancel_all_orders.side_effect = _http_error(418, -1003, "Too many requests.")

        with self.assertRaises(requests.HTTPError):
            _cancel_all_orders_ignoring_missing(exchange, "EDENUSDT")


if __name__ == "__main__":
    unittest.main()
