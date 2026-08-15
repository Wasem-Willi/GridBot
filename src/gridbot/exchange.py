from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any
from urllib.parse import urlencode

import requests


def extract_binance_error_detail(error: requests.HTTPError) -> str:
    response = error.response
    if response is None:
        return str(error)
    try:
        body = response.json()
    except ValueError:
        return response.text or str(error)
    code = body.get("code")
    msg = body.get("msg") or body.get("description") or str(error)
    return f"code={code}, msg={msg}"


def is_insufficient_balance_error(error: requests.HTTPError) -> bool:
    response = error.response
    if response is None:
        return False
    try:
        body = response.json()
    except ValueError:
        return "insufficient" in (response.text or "").lower()
    code = body.get("code")
    msg = str(body.get("msg") or body.get("description") or "").lower()
    return code in {-2010, -2019} or ("insufficient" in msg and "balance" in msg)


def is_unknown_order_error(error: requests.HTTPError) -> bool:
    """True when Binance rejects a cancel because the order is already gone.

    Binance's bulk cancel-all endpoint (and single-order cancel) returns
    code -2011 "Unknown order sent." when there are no matching open orders
    left to cancel, e.g. they already filled or were cancelled by a prior
    cycle. That is not a real failure and should not block trading.
    """
    response = error.response
    if response is None:
        return False
    try:
        body = response.json()
    except ValueError:
        return "unknown order" in (response.text or "").lower()
    code = body.get("code")
    msg = str(body.get("msg") or body.get("description") or "").lower()
    return code == -2011 or "unknown order" in msg


@dataclass(frozen=True)
class FilterValues:
    min_value: Decimal
    max_value: Decimal
    step: Decimal


@dataclass(frozen=True)
class SymbolTradingRules:
    base_asset: str
    quote_asset: str
    price_filter: FilterValues
    lot_size_filter: FilterValues
    min_notional: Decimal | None


def _quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _quantize_up(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_UP)
    return units * step


def _parse_decimal(value: str | float | int | None, fallback: str = "0") -> Decimal:
    if value is None:
        return Decimal(fallback)
    return Decimal(str(value))


class BinanceSpotClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self._symbol_rules_cache: dict[str, SymbolTradingRules] | None = None

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.request(method, url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def _signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = dict(params or {})
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = 10_000
        query = urlencode(payload, doseq=True)
        signature = hmac.new(self.api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        payload["signature"] = signature
        return self._request(method, path, payload)

    def _load_symbol_rules(self) -> dict[str, SymbolTradingRules]:
        if self._symbol_rules_cache is not None:
            return self._symbol_rules_cache

        info = self.get_exchange_info()
        rules: dict[str, SymbolTradingRules] = {}
        for symbol_info in info.get("symbols", []):
            symbol = str(symbol_info.get("symbol", ""))
            filters = {f.get("filterType"): f for f in symbol_info.get("filters", [])}
            price_raw = filters.get("PRICE_FILTER")
            lot_raw = filters.get("LOT_SIZE")
            if price_raw is None or lot_raw is None:
                continue

            price_filter = FilterValues(
                min_value=_parse_decimal(price_raw.get("minPrice")),
                max_value=_parse_decimal(price_raw.get("maxPrice"), "1000000000000000"),
                step=_parse_decimal(price_raw.get("tickSize"), "0"),
            )
            lot_filter = FilterValues(
                min_value=_parse_decimal(lot_raw.get("minQty")),
                max_value=_parse_decimal(lot_raw.get("maxQty"), "1000000000000000"),
                step=_parse_decimal(lot_raw.get("stepSize"), "0"),
            )

            min_notional: Decimal | None = None
            min_notional_raw = filters.get("MIN_NOTIONAL")
            if min_notional_raw is not None:
                min_notional = _parse_decimal(min_notional_raw.get("minNotional"))
            notional_raw = filters.get("NOTIONAL")
            if min_notional is None and notional_raw is not None:
                min_notional = _parse_decimal(notional_raw.get("minNotional"))

            rules[symbol] = SymbolTradingRules(
                base_asset=str(symbol_info.get("baseAsset", "")),
                quote_asset=str(symbol_info.get("quoteAsset", "")),
                price_filter=price_filter,
                lot_size_filter=lot_filter,
                min_notional=min_notional,
            )

        self._symbol_rules_cache = rules
        return rules

    def get_symbol_assets(self, symbol: str) -> tuple[str, str]:
        rules = self._load_symbol_rules().get(symbol)
        if rules is None or not rules.base_asset or not rules.quote_asset:
            raise ValueError(f"Trading assets are unavailable for symbol: {symbol}")
        return rules.base_asset, rules.quote_asset

    def normalize_limit_order(self, symbol: str, price: float, quantity: float) -> tuple[float, float] | None:
        rules = self._load_symbol_rules().get(symbol)
        if rules is None:
            return price, quantity

        price_dec = _quantize_down(_parse_decimal(price), rules.price_filter.step)
        qty_dec = _quantize_down(_parse_decimal(quantity), rules.lot_size_filter.step)

        if price_dec <= 0 or qty_dec <= 0:
            return None
        if price_dec < rules.price_filter.min_value or price_dec > rules.price_filter.max_value:
            return None
        if qty_dec < rules.lot_size_filter.min_value:
            qty_dec = rules.lot_size_filter.min_value
            qty_dec = _quantize_up(qty_dec, rules.lot_size_filter.step)
        if qty_dec > rules.lot_size_filter.max_value:
            return None

        if rules.min_notional is not None and (price_dec * qty_dec) < rules.min_notional:
            required_qty = rules.min_notional / price_dec
            qty_dec = _quantize_up(required_qty, rules.lot_size_filter.step)
            if qty_dec < rules.lot_size_filter.min_value:
                qty_dec = rules.lot_size_filter.min_value
                qty_dec = _quantize_up(qty_dec, rules.lot_size_filter.step)
            if qty_dec > rules.lot_size_filter.max_value:
                return None
            if (price_dec * qty_dec) < rules.min_notional:
                return None
        return float(price_dec), float(qty_dec)

    def ping(self) -> None:
        self._request("GET", "/api/v3/ping")

    def get_exchange_info(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/exchangeInfo")

    def get_ticker_price(self, symbol: str) -> float:
        result = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(result["price"])

    def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, float]]:
        result = self._request(
            "GET",
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(result, list):
            raise ValueError("Expected list from klines endpoint")
        candles: list[dict[str, float]] = []
        for row in result:
            candles.append(
                {
                    "open_time": float(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        return candles

    def get_book_ticker(self, symbol: str) -> dict[str, float]:
        result = self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return {"bid": float(result["bidPrice"]), "ask": float(result["askPrice"])}

    def get_all_book_tickers(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v3/ticker/bookTicker")
        if not isinstance(result, list):
            raise ValueError("Expected list from bookTicker endpoint")
        return result

    def get_24h_tickers(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v3/ticker/24hr")
        if not isinstance(result, list):
            raise ValueError("Expected list from 24h ticker endpoint")
        return result

    def get_all_ticker_prices(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v3/ticker/price")
        if not isinstance(result, list):
            raise ValueError("Expected list from ticker price endpoint")
        return result

    def get_account(self) -> dict[str, Any]:
        result = self._signed_request("GET", "/api/v3/account")
        if not isinstance(result, dict):
            raise ValueError("Expected object from account endpoint")
        return result

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        result = self._signed_request("GET", "/api/v3/openOrders", {"symbol": symbol})
        if not isinstance(result, list):
            raise ValueError("Expected list from openOrders endpoint")
        return result

    def get_all_open_orders(self) -> list[dict[str, Any]]:
        result = self._signed_request("GET", "/api/v3/openOrders")
        if not isinstance(result, list):
            raise ValueError("Expected list from all openOrders endpoint")
        return result

    def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        return self._signed_request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id})

    def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        result = self._signed_request("DELETE", "/api/v3/openOrders", {"symbol": symbol})
        if not isinstance(result, list):
            raise ValueError("Expected list from cancel-all response")
        return result

    def place_limit_order(
        self, symbol: str, side: str, quantity: float, price: float, post_only: bool = True
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT_MAKER" if post_only else "LIMIT",
            "quantity": f"{quantity:.8f}",
            "price": f"{price:.8f}",
            "timeInForce": "GTC",
        }
        if post_only:
            params.pop("timeInForce")
        return self._signed_request("POST", "/api/v3/order", params)

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{quantity:.8f}",
        }
        return self._signed_request("POST", "/api/v3/order", params)
