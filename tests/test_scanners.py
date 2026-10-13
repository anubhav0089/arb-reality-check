"""Offline tests: no network, no exchange, no blockchain."""

import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cex_scanner as cex  # noqa: E402
import defi_scanner as defi  # noqa: E402


def null_log():
    return csv.writer(io.StringIO())


# ------------------------------------------------------------ DeFi helpers ---

def test_quote_selector_matches_uniswap_abi():
    # keccak256("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(b"quoteExactInputSingle((address,address,uint256,uint24,uint160))")
    assert k.hexdigest()[:8] == defi.QUOTE_SELECTOR


def test_encode_and_decode_quote():
    data = defi.encode_quote("0x" + "11" * 20, "0x" + "22" * 20, 12345, 500)
    assert data.startswith("0x" + defi.QUOTE_SELECTOR)
    assert len(data) == 2 + 8 + 64 * 5
    w = defi._word
    assert defi.decode_quote("0x" + w(999) + w(0) + w(1) + w(110000)) == (999, 110000)
    assert defi.decode_quote("0x") is None


def q(mid, spread=0.0003, gas=0.01):
    return {"buy_price": mid * (1 + spread), "sell_price": mid * (1 - spread), "gas_usd": gas}


def test_wrapped_btc_gap_is_not_arbitrage():
    """WBTC on Arbitrum and cbBTC on Base are different tokens: a price gap
    between them must never be traded by default."""
    quotes = {"BTC": {"binance": q(83700), "arbitrum": q(83300), "base": q(83800)},
              "ETH": {"binance": q(2510), "arbitrum": q(2510), "base": q(2510)}}
    t = defi.Trader(25, 300, include_wrapped=False)
    for _ in range(100):
        t.step(quotes, null_log())
    assert t.filled == 0
    assert t.best(quotes, "wrapped")[3] > 0      # the gap is visible...
    assert t.best(quotes)[3] < 0                 # ...but not counted


def test_balances_stop_a_persistent_gap():
    """A gap that never closes can only be traded until money runs out."""
    quotes = {"ETH": {"binance": q(2510), "arbitrum": q(2490), "base": q(2530)},
              "BTC": {"binance": q(83700), "arbitrum": q(83700), "base": q(83700)}}
    t = defi.Trader(25, 300, include_wrapped=False)
    for _ in range(1000):
        t.step(quotes, null_log())
    assert 0 < t.filled < 10
    assert t.usd["arbitrum"] < 25


def test_missing_prices_do_not_look_like_a_loss():
    quotes = {"ETH": {"binance": q(2510), "arbitrum": q(2510), "base": q(2510)},
              "BTC": {"binance": q(83700), "arbitrum": q(83700), "base": q(83700)}}
    t = defi.Trader(25, 300, include_wrapped=False)
    t.step(quotes, null_log())
    empty = {"ETH": {}, "BTC": {}}
    t.step(empty, null_log())
    now, held, stale = t.value(empty)
    assert stale
    assert abs(now - 300) < 1
    assert t.checks_bad == 1


# ------------------------------------------------------------- CEX trader ---

def test_cex_fees_kill_small_gaps():
    a = {"bid": 100.00, "ask": 100.01, "bid_size": 1, "ask_size": 1}
    b = {"bid": 100.10, "ask": 100.11, "bid_size": 1, "ask_size": 1}
    # 0.09% raw gap, 0.2% fees in total
    assert cex.PaperTrader.edge(a, b, 0.001, 0.001) < 0


def test_cex_gap_that_closes_is_missed():
    names = ["binance", "okx"]
    t = cex.PaperTrader(names, 68, 100.0)
    wide = {"binance": {"bid": 99, "ask": 99.01, "bid_size": 1, "ask_size": 1},
            "okx": {"bid": 101, "ask": 101.01, "bid_size": 1, "ask_size": 1}}
    flat = {"binance": {"bid": 100, "ask": 100.01, "bid_size": 1, "ask_size": 1},
            "okx": {"bid": 100, "ask": 100.01, "bid_size": 1, "ask_size": 1}}
    t.step(wide, null_log())   # spotted
    t.step(flat, null_log())   # gone by the next check
    assert t.seen == 1 and t.missed == 1 and t.filled == 0
