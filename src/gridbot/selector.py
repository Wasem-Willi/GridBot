from __future__ import annotations

from dataclasses import dataclass

from gridbot.exchange import BinanceSpotClient


@dataclass(frozen=True)
class RankedSymbol:
    symbol: str
    score: float
    quote_volume: float
    spread_pct: float
    volatility_proxy: float


def _normalize(value: float, floor: float, ceiling: float) -> float:
    if ceiling <= floor:
        return 0.0
    clipped = max(min(value, ceiling), floor)
    return (clipped - floor) / (ceiling - floor)


def select_symbols(
    exchange: BinanceSpotClient,
    blacklist: set[str],
    max_symbols: int,
) -> list[RankedSymbol]:
    info = exchange.get_exchange_info()
    active_spot_symbols = {
        s["symbol"]
        for s in info["symbols"]
        if s.get("status") == "TRADING"
        and s.get("isSpotTradingAllowed") is True
        and s.get("quoteAsset") == "USDT"
        and s["symbol"] not in blacklist
    }

    tickers = exchange.get_24h_tickers()
    book_tickers = {
        row.get("symbol", ""): (
            float(row.get("bidPrice", 0.0)),
            float(row.get("askPrice", 0.0)),
        )
        for row in exchange.get_all_book_tickers()
    }
    candidates: list[RankedSymbol] = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if symbol not in active_spot_symbols:
            continue
        quote_volume = float(t.get("quoteVolume", 0.0))
        high = float(t.get("highPrice", 0.0))
        low = float(t.get("lowPrice", 0.0))
        last = float(t.get("lastPrice", 0.0))
        if quote_volume <= 0 or last <= 0:
            continue
        bid_ask = book_tickers.get(symbol)
        if bid_ask is None:
            continue
        bid, ask = bid_ask
        if bid <= 0 or ask <= 0:
            continue
        spread_pct = max((ask - bid) / last, 0.0)
        volatility_proxy = max((high - low) / last, 0.0)
        candidates.append(
            RankedSymbol(
                symbol=symbol,
                score=0.0,
                quote_volume=quote_volume,
                spread_pct=spread_pct,
                volatility_proxy=volatility_proxy,
            )
        )

    if not candidates:
        return []

    min_vol = min(c.quote_volume for c in candidates)
    max_vol = max(c.quote_volume for c in candidates)
    min_spread = min(c.spread_pct for c in candidates)
    max_spread = max(c.spread_pct for c in candidates)
    min_volatility = min(c.volatility_proxy for c in candidates)
    max_volatility = max(c.volatility_proxy for c in candidates)

    ranked: list[RankedSymbol] = []
    for c in candidates:
        liquidity_score = _normalize(c.quote_volume, min_vol, max_vol)
        spread_score = 1.0 - _normalize(c.spread_pct, min_spread, max_spread)
        # Target mid-range volatility for grid stability.
        volatility_norm = _normalize(c.volatility_proxy, min_volatility, max_volatility)
        volatility_score = 1.0 - abs(0.5 - volatility_norm) * 2.0
        composite = liquidity_score * 0.5 + spread_score * 0.3 + volatility_score * 0.2
        ranked.append(
            RankedSymbol(
                symbol=c.symbol,
                score=composite,
                quote_volume=c.quote_volume,
                spread_pct=c.spread_pct,
                volatility_proxy=c.volatility_proxy,
            )
        )

    ranked.sort(key=lambda s: s.score, reverse=True)
    return ranked[:max_symbols]
