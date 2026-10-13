#!/usr/bin/env python3
"""
DeFi arbitrage paper scanner (read-only).

Compares the price you would actually get swapping on Uniswap v3 (on Base and
on Arbitrum) with Binance, for BTC and ETH. Looks for buy-here-sell-there
gaps that survive ALL costs, and simulates the trades with fake money.

Read-only: no wallet, no private key, no transactions. It only asks the
blockchain "how much would I get if I swapped this much?" through Uniswap's
official Quoter contract, the same thing the Uniswap app does before you swap.

Costs included:
  * Uniswap pool fee and price impact (both are inside the Quoter's answer)
  * Gas for each swap, priced live from each chain, plus a small L1 data fee
  * Binance taker fee (0.1%)
  * Delay: a gap spotted on one check is filled at the NEXT check's prices.

NOT included (so real results would be worse):
  * MEV bots that see your transaction and get in first
  * Moving funds between Binance, Base and Arbitrum (bridges, withdrawals)
  * Failed transactions that still cost gas

Usage:
  pip install -r requirements.txt
  python3 defi_scanner.py               # live, until Ctrl+C
  python3 defi_scanner.py --minutes 60
  python3 defi_scanner.py --size 500    # simulate $500 trades instead of $50
  python3 defi_scanner.py --demo        # simulated prices, no internet
"""

import argparse
import asyncio
import csv
import random
import time
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------- settings ---

BINANCE_FEE = 0.001
MIN_PROFIT_USD = 0.01       # ignore gaps that would make less than this
POLL_SECONDS = 2.0          # Base makes a block every 2 seconds; override with --interval
TICK_TIMEOUT = 15.0         # a price check slower than this counts as failed
FEE_TIERS = [100, 500, 3000, 10000]   # Uniswap pool fees: 0.01% .. 1%
SWAP_GAS_OVERHEAD = 60_000  # router + token transfer gas on top of the pool swap

# Uniswap v3 QuoterV2 and token addresses (from Uniswap's official deployment docs).
CHAINS = {
    "base": {
        "rpc": "https://mainnet.base.org",
        "quoter": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        "usdc": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
        "tokens": {
            "BTC": ("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8),   # cbBTC
            "ETH": ("0x4200000000000000000000000000000000000006", 18),  # WETH
        },
        "l1_fee_usd": 0.01,
    },
    "arbitrum": {
        "rpc": "https://arb1.arbitrum.io/rpc",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "usdc": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
        "tokens": {
            "BTC": ("0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", 8),   # WBTC
            "ETH": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),  # WETH
        },
        "l1_fee_usd": 0.01,
    },
}
ASSETS = ["BTC", "ETH"]
BINANCE_SYMBOL = {"BTC": "BTC/USDC", "ETH": "ETH/USDC"}

# keccak("quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
QUOTE_SELECTOR = "c6a5026a"

# ----------------------------------------------------------- abi helpers ---


def _word(n: int) -> str:
    return format(n, "064x")


def encode_quote(token_in: str, token_out: str, amount_in: int, fee: int) -> str:
    return ("0x" + QUOTE_SELECTOR
            + _word(int(token_in, 16)) + _word(int(token_out, 16))
            + _word(amount_in) + _word(fee) + _word(0))


def decode_quote(hexdata: str):
    """Returns (amount_out, gas_estimate) or None."""
    if not hexdata or len(hexdata) < 2 + 64 * 4:
        return None
    d = hexdata[2:]
    return int(d[0:64], 16), int(d[192:256], 16)

# ------------------------------------------------------------- live feed ---


class LiveFeed:
    """
    Produces, per asset and venue:
      buy_price  = USD paid per token, all fees in, at the chosen trade size
      sell_price = USD received per token, all fees in
      gas_usd    = cost of one swap transaction (0 on Binance)
    """

    def __init__(self, size_usd):
        import aiohttp
        import ccxt.async_support as ccxt
        self.size = size_usd
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        self.binance = ccxt.binance({"enableRateLimit": True})
        self.tier = {}      # (chain, asset) -> fee tier
        self.ref = {}       # asset -> Binance mid
        self.notes = []

    async def _rpc(self, url, calls):
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                for i, (m, p) in enumerate(calls)]
        async with self.http.post(url, json=body) as r:
            res = await r.json(content_type=None)
        if isinstance(res, dict):  # endpoint refused the batch
            raise RuntimeError(res.get("error", res))
        out = [None] * len(calls)
        for item in res:
            out[item["id"]] = item.get("result")
        return out

    def _quote_call(self, chain, token_in, token_out, amount_in, fee):
        c = CHAINS[chain]
        return ("eth_call", [{"to": c["quoter"],
                              "data": encode_quote(token_in, token_out, amount_in, fee)},
                             "latest"])

    async def _binance(self):
        out = {}
        for a in ASSETS:
            t = await self.binance.fetch_ticker(BINANCE_SYMBOL[a])
            out[a] = {"buy_price": t["ask"] / (1 - BINANCE_FEE),
                      "sell_price": t["bid"] * (1 - BINANCE_FEE),
                      "gas_usd": 0.0}
            self.ref[a] = (t["bid"] + t["ask"]) / 2
        return out

    async def setup(self):
        """Pick the fee tier with the best price for each pool."""
        await self._binance()
        for chain, c in CHAINS.items():
            usdc = c["usdc"][0]
            for a in ASSETS:
                tok, dec = c["tokens"][a]
                qty = int(self.size / self.ref[a] * 10 ** dec)
                calls = [self._quote_call(chain, tok, usdc, qty, f) for f in FEE_TIERS]
                try:
                    res = await self._rpc(c["rpc"], calls)
                except Exception as e:
                    self.notes.append(f"{chain}: cannot reach chain ({type(e).__name__})")
                    break
                best = None
                for f, r in zip(FEE_TIERS, res):
                    q = decode_quote(r)
                    if q and (best is None or q[0] > best[1]):
                        best = (f, q[0])
                if best:
                    self.tier[(chain, a)] = best[0]
                    self.notes.append(f"{chain} {a}/USDC: using {best[0]/10000:.2f}% pool")
                else:
                    self.notes.append(f"{chain} {a}/USDC: no pool found, skipped")

    async def _chain(self, chain):
        c = CHAINS[chain]
        usdc, udec = c["usdc"]
        calls, keys = [("eth_gasPrice", [])], []
        for a in ASSETS:
            if (chain, a) not in self.tier:
                continue
            tok, dec = c["tokens"][a]
            fee = self.tier[(chain, a)]
            qty = int(self.size / self.ref[a] * 10 ** dec)
            calls.append(self._quote_call(chain, tok, usdc, qty, fee))                      # sell
            calls.append(self._quote_call(chain, usdc, tok, int(self.size * 10 ** udec), fee))  # buy
            keys.append((a, qty, dec))
        res = await self._rpc(c["rpc"], calls)
        gas_price = int(res[0], 16)
        eth_usd = self.ref["ETH"]
        out = {}
        for i, (a, qty, dec) in enumerate(keys):
            sell, buy = decode_quote(res[1 + 2 * i]), decode_quote(res[2 + 2 * i])
            if not sell or not buy:
                continue
            gas_units = (sell[1] + buy[1]) / 2 + SWAP_GAS_OVERHEAD
            out[a] = {
                "sell_price": (sell[0] / 10 ** udec) / (qty / 10 ** dec),
                "buy_price": self.size / (buy[0] / 10 ** dec),
                "gas_usd": gas_units * gas_price / 1e18 * eth_usd + c["l1_fee_usd"],
            }
        return out

    async def tick(self):
        quotes = {a: {} for a in ASSETS}
        try:
            for a, q in (await self._binance()).items():
                quotes[a]["binance"] = q
        except Exception as e:
            quotes["_err_binance"] = type(e).__name__
        results = await asyncio.gather(*(self._chain(ch) for ch in self.tier_chains()),
                                       return_exceptions=True)
        for ch, r in zip(self.tier_chains(), results):
            if isinstance(r, Exception):
                quotes["_err_" + ch] = type(r).__name__
                continue
            for a, q in r.items():
                quotes[a][ch] = q
        return quotes

    def tier_chains(self):
        return sorted({ch for ch, _ in self.tier})

    async def close(self):
        await self.binance.close()
        await self.http.close()


class DemoFeed:
    """Made-up prices shaped like the real thing, for testing offline."""

    def __init__(self, size_usd):
        self.size = size_usd
        self.mid = {"BTC": 83000.0, "ETH": 3100.0}
        self.off = {(a, v): 0.0 for a in ASSETS for v in ("binance", "base", "arbitrum")}
        self.notes = ["DEMO: prices are simulated"]

    async def setup(self):
        pass

    async def tick(self):
        out = {}
        for a in ASSETS:
            self.mid[a] *= 1 + random.gauss(0, 0.0004)
            out[a] = {}
            for v in ("binance", "base", "arbitrum"):
                k = (a, v)
                self.off[k] = self.off[k] * 0.6 + random.gauss(0, 0.0002)
                if v != "binance" and random.random() < 0.01:
                    self.off[k] += random.choice([-1, 1]) * random.uniform(0.001, 0.004)
                m = self.mid[a] * (1 + self.off[k])
                if v == "binance":
                    out[a][v] = {"buy_price": m * 1.00002 / (1 - BINANCE_FEE),
                                 "sell_price": m * 0.99998 * (1 - BINANCE_FEE), "gas_usd": 0}
                else:
                    cost = 0.0005 + 0.0004 * self.size / 1000   # pool fee + price impact
                    out[a][v] = {"buy_price": m * (1 + cost), "sell_price": m * (1 - cost),
                                 "gas_usd": random.uniform(0.005, 0.06)}
        return out

    async def close(self):
        pass

# ------------------------------------------------------------ paper trader ---


def route_pnl(qb, qs, size):
    qty = size / qb["buy_price"]
    proceeds = qty * qs["sell_price"]
    gas = qb["gas_usd"] + qs["gas_usd"]
    return proceeds - size - gas, proceeds - size, gas


def token_name(asset, venue):
    """The actual token you'd hold. Different names = not the same thing."""
    if asset == "BTC":
        return {"binance": "BTC", "base": "cbBTC", "arbitrum": "WBTC"}[venue]
    return asset


class Trader:
    """
    Paper account with real balances. Money starts split evenly across
    venues (half dollars, half coins). Each trade uses dollars on the buy
    venue and coins on the sell venue, so funds pile up on one side until
    a gap opens the other way. Nothing is moved between venues.
    """

    def __init__(self, size, capital, include_wrapped):
        self.size = size
        self.capital = capital
        self.include_wrapped = include_wrapped
        self.usd = None          # venue -> dollars
        self.tok = None          # (asset, venue) -> coins
        self.seen = self.filled = self.missed = self.blocked = 0
        self.profit = 0.0
        self.gas_paid = 0.0
        self.pending = None
        self.recent = []
        self.checks_ok = 0      # Binance and at least one chain answered
        self.checks_bad = 0
        self.last_mid = {}      # (asset, venue) -> last price seen, for valuing coins

    def record_health(self, quotes):
        venues = {v for a in ASSETS for v in quotes.get(a, {})}
        if "binance" in venues and len(venues) >= 2:
            self.checks_ok += 1
        else:
            self.checks_bad += 1
        for a in ASSETS:
            for v, q in quotes.get(a, {}).items():
                self.last_mid[(a, v)] = (q["buy_price"] + q["sell_price"]) / 2

    def _init_balances(self, quotes):
        venues = sorted({v for a in ASSETS for v in quotes.get(a, {})})
        per = self.capital / len(venues)
        self.usd = {v: per / 2 for v in venues}
        self.tok = {}
        for a in ASSETS:
            for v in venues:
                q = quotes.get(a, {}).get(v)
                mid = (q["buy_price"] + q["sell_price"]) / 2 if q else 1
                self.tok[(a, v)] = (per / 2 / len(ASSETS)) / mid
        self.start_tok = dict(self.tok)

    def allowed(self, a, b, s):
        return self.include_wrapped or token_name(a, b) == token_name(a, s)

    def trade_size(self, a, b, s, qb):
        """Largest trade up to --size that our balances on both sides allow."""
        spend = min(self.size, self.usd.get(b, 0) - qb["gas_usd"],
                    self.tok.get((a, s), 0) * qb["buy_price"])
        return spend if spend >= self.size * 0.2 else 0.0

    def can_trade(self, a, b, s, qb):
        return self.trade_size(a, b, s, qb) > 0

    def best(self, quotes, mode="allowed"):
        """mode: 'allowed' (same token), 'fundable' (same token AND we hold the
        money on both sides), or 'wrapped' (different tokens, shown only)."""
        best = None
        for a in ASSETS:
            venues = quotes.get(a, {})
            for b in venues:
                for s in venues:
                    if b == s:
                        continue
                    same = self.allowed(a, b, s)
                    if mode == "wrapped" and token_name(a, b) == token_name(a, s):
                        continue
                    if mode != "wrapped" and not same:
                        continue
                    size = self.size
                    if mode == "fundable":
                        size = self.trade_size(a, b, s, venues[b])
                        if not size:
                            continue
                    pnl, before_gas, gas = route_pnl(venues[b], venues[s], size)
                    if best is None or pnl > best[3]:
                        best = (a, b, s, pnl, before_gas, gas)
        return best

    def value(self, quotes):
        """Everything marked at the latest price seen for each venue.
        Returns (now, if_held, stale) where stale means some prices are old."""
        if self.usd is None:
            return self.capital, self.capital, False
        now = sum(self.usd.values())
        held = self.capital / 2
        stale = False
        for (a, v), qty in self.tok.items():
            if v not in quotes.get(a, {}):
                stale = True
            mid = self.last_mid.get((a, v))
            if mid is None:
                continue
            now += qty * mid
            held += self.start_tok[(a, v)] * mid
        return now, held, stale

    def step(self, quotes, log):
        now = datetime.now().strftime("%H:%M:%S")
        self.record_health(quotes)
        if self.usd is None:
            if not any(quotes.get(a) for a in ASSETS):
                return None
            self._init_balances(quotes)
        if self.pending:
            a, b, s, seen_pnl = self.pending
            self.pending = None
            v = quotes.get(a, {})
            if b in v and s in v:
                spend = self.trade_size(a, b, s, v[b])
                pnl, _, gas = route_pnl(v[b], v[s], spend or self.size)
                if pnl <= 0:
                    self.missed += 1
                    result = "missed (gap closed)"
                elif not self.can_trade(a, b, s, v[b]):
                    self.blocked += 1
                    result = "blocked (no funds there)"
                    pnl = 0.0
                else:
                    qty = spend / v[b]["buy_price"]
                    self.usd[b] -= spend + v[b]["gas_usd"]
                    self.tok[(a, b)] += qty
                    self.tok[(a, s)] -= qty
                    self.usd[s] += qty * v[s]["sell_price"] - v[s]["gas_usd"]
                    self.filled += 1
                    self.profit += pnl
                    self.gas_paid += gas
                    result = "filled"
                row = [now, a, b, s, f"{seen_pnl:.4f}", f"{pnl:.4f}", f"{gas:.4f}", result]
                log.writerow(row)
                self.recent = ([row] + self.recent)[:8]

        best = self.best(quotes)
        go = self.best(quotes, "fundable")
        if go and go[3] >= MIN_PROFIT_USD:
            self.seen += 1
            self.pending = (go[0], go[1], go[2], go[3])
        elif best and best[3] >= MIN_PROFIT_USD:
            self.blocked += 1   # a real gap, but our money is on the wrong side
        return best

# ----------------------------------------------------------------- display ---


def render(quotes, best, tr, started, mode, notes):
    prices = Table(title=f"What a ${tr.size:,.0f} trade gets you (all fees included)", expand=True)
    for c in ("Asset", "Venue", "Buy at", "Sell at", "Gas per swap"):
        prices.add_column(c, justify="left" if c in ("Asset", "Venue") else "right")
    for a in ASSETS:
        for v, q in quotes.get(a, {}).items():
            gas = "none" if v == "binance" else f"${q['gas_usd']:.4f}"
            prices.add_row(a, f"{v} ({token_name(a, v)})", f"{q['buy_price']:,.2f}",
                           f"{q['sell_price']:,.2f}", gas)
    for k, e in quotes.items():
        if k.startswith("_err_"):
            prices.add_row("", k[5:], f"[red]{e}", "", "")

    best_txt = "--"
    if best:
        a, b, s, pnl, before_gas, gas = best
        colour = "green" if pnl >= MIN_PROFIT_USD else "dim"
        best_txt = (f"{a}: buy {b} -> sell {s}   before gas ${before_gas:+.4f}   "
                    f"gas ${gas:.4f}   [{colour}]net ${pnl:+.4f}[/{colour}]")
    wrap = tr.best(quotes, "wrapped") if not tr.include_wrapped else None
    wrap_txt = "--"
    if wrap:
        a, b, s, pnl = wrap[:4]
        wrap_txt = (f"{token_name(a, b)} on {b} vs {token_name(a, s)} on {s}: "
                    f"${pnl:+.4f}  [dim](different tokens, not tradeable as arbitrage)[/dim]")
    now_val, held_val, stale = tr.value(quotes)
    stale_txt = "  [yellow](using last known prices: a feed is down)[/yellow]" if stale else ""
    total = tr.checks_ok + tr.checks_bad
    if total:
        pct = 100 * tr.checks_ok / total
        colour = "green" if pct >= 95 else "yellow" if pct >= 80 else "red"
        health = f"[{colour}]{tr.checks_ok} ok, {tr.checks_bad} failed ({pct:.1f}% ok)[/{colour}]"
    else:
        health = "--"

    stats = Table.grid(padding=(0, 2))
    stats.add_column(style="bold")
    stats.add_column()
    stats.add_row("Mode", mode)
    stats.add_row("Running", f"{(time.time()-started)/60:.1f} min")
    stats.add_row("Price checks", health)
    stats.add_row("Best route now", best_txt)
    stats.add_row("Wrapper price gap", wrap_txt)
    stats.add_row("Gaps spotted", str(tr.seen))
    stats.add_row("Filled", f"[green]{tr.filled}")
    stats.add_row("Missed (closed in 1 block)", f"[yellow]{tr.missed}")
    stats.add_row("Blocked checks (money on wrong side)", f"[red]{tr.blocked}")
    stats.add_row("Gas spent on filled trades", f"${tr.gas_paid:,.4f}")
    stats.add_row("Paper profit", f"${tr.profit:,.4f}")
    stats.add_row("Account value now", f"${now_val:,.2f}  (started ${tr.capital:,.2f}){stale_txt}")
    stats.add_row("If you'd just held", f"${held_val:,.2f}")
    stats.add_row("Setup", "  |  ".join(notes) or "--")

    recent = Table(title="Recent attempts", expand=True)
    for c in ("Time", "Asset", "Buy", "Sell", "Seen $", "At fill $", "Gas $", "Result"):
        recent.add_column(c)
    for r in tr.recent:
        st = "green" if r[7] == "filled" else "yellow"
        recent.add_row(*r[:7], f"[{st}]{r[7]}")

    layout = Layout()
    layout.split_column(
        Layout(prices, size=4 + 2 * 3 + 2),
        Layout(Panel(stats, title="Paper account (fake money)"), size=18),
        Layout(recent),
    )
    return layout

# -------------------------------------------------------------------- main ---


async def main():
    p = argparse.ArgumentParser(description="DeFi arbitrage paper scanner")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--size", type=float, default=25.0, help="USD per simulated trade")
    p.add_argument("--capital", type=float, default=300.0,
                   help="fake starting USD, split across Binance, Base and Arbitrum")
    p.add_argument("--include-wrapped", action="store_true",
                   help="also trade cbBTC vs WBTC vs BTC as if they were the same (they are not)")
    p.add_argument("--minutes", type=float, default=0)
    p.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds between checks")
    p.add_argument("--no-ui", action="store_true")
    args = p.parse_args()

    console = Console()
    feed = DemoFeed(args.size) if args.demo else LiveFeed(args.size)
    mode = "DEMO (simulated prices)" if args.demo else "LIVE on-chain quotes, paper money"
    try:
        await feed.setup()
    except Exception as e:
        console.print(f"[red]Could not reach Binance: {e}")
        await feed.close()
        return
    if not args.demo and not feed.tier:
        console.print("[red]No Uniswap pools reachable.", feed.notes)
        await feed.close()
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"defi_log_{stamp}.csv"
    f = open(log_path, "w", newline="")
    log = csv.writer(f)
    log.writerow(["time", "asset", "buy_on", "sell_on", "profit_when_seen_usd",
                  "profit_at_fill_usd", "gas_usd", "result"])
    tr = Trader(args.size, args.capital, args.include_wrapped)
    started = time.time()

    async def loop(live=None):
        while True:
            t0 = time.time()
            try:
                quotes = await asyncio.wait_for(feed.tick(), TICK_TIMEOUT)
            except asyncio.TimeoutError:
                quotes = {a: {} for a in ASSETS}
                quotes["_err_all"] = "timeout"
            best = tr.step(quotes, log)
            f.flush()
            if live:
                live.update(render(quotes, best, tr, started, mode, feed.notes))
            if args.minutes and time.time() - started >= args.minutes * 60:
                return
            await asyncio.sleep(max(0, args.interval - (time.time() - t0)))

    try:
        if args.no_ui:
            await loop()
        else:
            with Live(console=console, refresh_per_second=4) as live:
                await loop(live)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await feed.close()
        f.close()
        console.print(Panel.fit(
            f"Ran {(time.time()-started)/60:.1f} min  |  trade size ${args.size:,.0f}\n"
            f"Gaps spotted {tr.seen}  |  filled {tr.filled}  |  missed {tr.missed}  |  blocked {tr.blocked}\n"
            f"Price checks: {tr.checks_ok} ok, {tr.checks_bad} failed\n"
            f"Paper profit ${tr.profit:,.4f}  (after ${tr.gas_paid:,.4f} gas)\n"
            f"Not counted: MEV bots, bridging funds, failed transactions\n"
            f"Log: {log_path}",
            title="Session summary (paper money)"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
