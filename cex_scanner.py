#!/usr/bin/env python3
"""
BTC arbitrage paper-trading scanner.

Watches the BTC/USDT price on several exchanges every second, finds moments
where you could buy on one exchange and sell on another for a profit AFTER
fees, and simulates the trade with fake money.

Nothing here touches real money. No API keys are needed or used.

Realism built in:
  * Taker fees on both sides of every trade.
  * Latency: a gap spotted on one tick is only "filled" at the NEXT tick's
    prices. If the gap has closed by then, the trade is counted as missed.
  * Inventory: arbitrage needs money already sitting on both exchanges.
    Capital is split across exchanges, and each trade moves BTC from one
    exchange to another. When an exchange runs out of BTC or USDT, the bot
    can't trade that direction any more.

Usage:
  pip install -r requirements.txt
  python3 cex_scanner.py                 # live public prices, runs until Ctrl+C
  python3 cex_scanner.py --minutes 60    # stop after an hour
  python3 cex_scanner.py --demo          # simulated prices, no internet needed
"""

import argparse
import asyncio
import csv
import random
import statistics
import time
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout

# ---------------------------------------------------------------- settings ---

SYMBOL = "BTC/USDT"

# Spot taker fees (standard tier, no discounts). Edit if yours differ.
FEES = {
    "binance": 0.0010,
    "okx": 0.0010,
    "bybit": 0.0010,
    "kucoin": 0.0010,
    "gateio": 0.0020,
}

MIN_NET_EDGE = 0.0002   # only act on gaps >= 0.02% after fees
MAX_TRADE_BTC = 0.01    # cap per simulated trade
POLL_SECONDS = 1.0       # override with --interval
TICK_TIMEOUT = 15.0      # a price check slower than this counts as failed

# ----------------------------------------------------------- price sources ---


class LiveFeed:
    """Public ticker data via ccxt. No keys, read-only."""

    def __init__(self, names):
        import ccxt.async_support as ccxt
        # Some exchanges were renamed between ccxt versions.
        aliases = {"gateio": ["gate", "gateio"]}
        self.exchanges = {}
        for n in names:
            cls = None
            for attr in aliases.get(n, [n]):
                cls = getattr(ccxt, attr, None)
                if cls:
                    break
            if cls is None:
                print(f"Skipping {n}: not supported by your ccxt version")
                continue
            self.exchanges[n] = cls({"enableRateLimit": True})

    async def _one(self, name, ex):
        try:
            t = await ex.fetch_ticker(SYMBOL)
            if t.get("bid") and t.get("ask"):
                return name, {
                    "bid": float(t["bid"]),
                    "ask": float(t["ask"]),
                    "bid_size": float(t.get("bidVolume") or MAX_TRADE_BTC),
                    "ask_size": float(t.get("askVolume") or MAX_TRADE_BTC),
                }
        except Exception as e:  # network hiccups, region blocks, etc.
            return name, {"error": type(e).__name__}
        return name, {"error": "no quote"}

    async def tick(self):
        results = await asyncio.gather(
            *(self._one(n, ex) for n, ex in self.exchanges.items())
        )
        return dict(results)

    async def close(self):
        await asyncio.gather(*(ex.close() for ex in self.exchanges.values()))


class DemoFeed:
    """Random-walk prices with occasional short dislocations, for testing."""

    def __init__(self, names):
        self.names = names
        self.mid = 62000.0
        self.offset = {n: random.uniform(-8, 8) for n in names}

    async def tick(self):
        self.mid *= 1 + random.gauss(0, 0.0003)
        out = {}
        for n in self.names:
            self.offset[n] = self.offset[n] * 0.7 + random.gauss(0, 6)
            if random.random() < 0.03:  # rare spike on one venue
                self.offset[n] += random.choice([-1, 1]) * random.uniform(60, 250)
            m = self.mid + self.offset[n]
            half = random.uniform(0.5, 3)
            out[n] = {"bid": m - half, "ask": m + half,
                      "bid_size": random.uniform(0.001, 0.5),
                      "ask_size": random.uniform(0.001, 0.5)}
        return out

    async def close(self):
        pass

# ------------------------------------------------------------ paper trader ---


class PaperTrader:
    def __init__(self, names, capital, start_price):
        self.names = names
        per = capital / len(names)
        # Half of each exchange's share in USDT, half in BTC.
        self.usdt = {n: per / 2 for n in names}
        self.btc = {n: (per / 2) / start_price for n in names}
        self.start_capital = capital
        self.start_btc_total = sum(self.btc.values())
        self.start_usdt_total = sum(self.usdt.values())
        self.seen = 0
        self.filled = 0
        self.missed = 0
        self.blocked = 0
        self.realized = 0.0
        self.pending = None
        self.recent = []
        self.checks_ok = 0    # ticks where at least two exchanges answered
        self.checks_bad = 0

    @staticmethod
    def edge(q_buy, q_sell, f_buy, f_sell):
        cost = q_buy["ask"] * (1 + f_buy)
        proceeds = q_sell["bid"] * (1 - f_sell)
        return proceeds / cost - 1

    def best_gap(self, quotes):
        best = None
        ok = [n for n in self.names if "bid" in quotes.get(n, {})]
        for a in ok:
            for b in ok:
                if a == b:
                    continue
                gross = quotes[b]["bid"] / quotes[a]["ask"] - 1
                net = self.edge(quotes[a], quotes[b], FEES[a], FEES[b])
                if best is None or net > best[3]:
                    best = (a, b, gross, net)
        return best

    def step(self, quotes, log):
        now = datetime.now().strftime("%H:%M:%S")

        # 1. Try to fill whatever we decided to do on the previous tick,
        #    at THIS tick's prices (simulated reaction delay).
        if self.pending:
            a, b, seen_net = self.pending
            self.pending = None
            qa, qb = quotes.get(a, {}), quotes.get(b, {})
            if "ask" in qa and "bid" in qb:
                net = self.edge(qa, qb, FEES[a], FEES[b])
                size = min(
                    MAX_TRADE_BTC,
                    self.usdt[a] / (qa["ask"] * (1 + FEES[a])),
                    self.btc[b],
                    qa["ask_size"], qb["bid_size"],
                )
                if net <= 0:
                    self.missed += 1
                    result, pnl = "missed (gap closed)", 0.0
                elif size <= 1e-7:
                    self.blocked += 1
                    result, pnl = "blocked (no inventory)", 0.0
                else:
                    cost = size * qa["ask"] * (1 + FEES[a])
                    proceeds = size * qb["bid"] * (1 - FEES[b])
                    self.usdt[a] -= cost
                    self.btc[a] += size
                    self.btc[b] -= size
                    self.usdt[b] += proceeds
                    pnl = proceeds - cost
                    self.realized += pnl
                    self.filled += 1
                    result = "filled"
                row = [now, a, b, f"{seen_net*100:.4f}", f"{net*100:.4f}",
                       f"{size:.6f}" if result == "filled" else "0",
                       f"{pnl:.6f}", result]
                log.writerow(row)
                self.recent = ([row] + self.recent)[:8]

        # 2. Look for a new opportunity.
        best = self.best_gap(quotes)
        if best and best[3] >= MIN_NET_EDGE:
            self.seen += 1
            self.pending = (best[0], best[1], best[3])
        return best

    def equity(self, mid):
        return sum(self.usdt.values()) + sum(self.btc.values()) * mid

    def hold_value(self, mid):
        return self.start_usdt_total + self.start_btc_total * mid

# ----------------------------------------------------------------- display ---


def health_text(t):
    total = t.checks_ok + t.checks_bad
    if not total:
        return "--"
    pct = 100 * t.checks_ok / total
    colour = "green" if pct >= 95 else "yellow" if pct >= 80 else "red"
    return f"[{colour}]{t.checks_ok} ok, {t.checks_bad} failed ({pct:.1f}% ok)[/{colour}]"


def render(quotes, best, trader, mid, started, mode):
    prices = Table(title="Live quotes  BTC/USDT", expand=True)
    for c in ("Exchange", "Bid", "Ask", "Fee", "BTC held", "USDT held"):
        prices.add_column(c, justify="right" if c != "Exchange" else "left")
    for n in trader.names:
        q = quotes.get(n, {})
        if "bid" in q:
            prices.add_row(n, f"{q['bid']:,.2f}", f"{q['ask']:,.2f}",
                           f"{FEES[n]*100:.2f}%", f"{trader.btc[n]:.6f}",
                           f"{trader.usdt[n]:.2f}")
        else:
            prices.add_row(n, "[red]" + q.get("error", "--"), "", "", "", "")

    eq = trader.equity(mid)
    hold = trader.hold_value(mid)
    mins = (time.time() - started) / 60
    gap_txt = "--"
    if best:
        colour = "green" if best[3] >= MIN_NET_EDGE else "dim"
        gap_txt = (f"buy {best[0]} -> sell {best[1]}   "
                   f"raw {best[2]*100:+.4f}%   "
                   f"[{colour}]after fees {best[3]*100:+.4f}%[/{colour}]")

    stats = Table.grid(padding=(0, 2))
    stats.add_column(style="bold")
    stats.add_column()
    stats.add_row("Mode", mode)
    stats.add_row("Running", f"{mins:.1f} min")
    stats.add_row("Price checks", health_text(trader))
    stats.add_row("Best gap now", gap_txt)
    stats.add_row("Gaps spotted", str(trader.seen))
    stats.add_row("Filled", f"[green]{trader.filled}")
    stats.add_row("Missed (closed before fill)", f"[yellow]{trader.missed}")
    stats.add_row("Blocked (out of inventory)", f"[red]{trader.blocked}")
    stats.add_row("Arbitrage profit", f"${trader.realized:,.4f}")
    stats.add_row("Account value now", f"${eq:,.2f}  (started ${trader.start_capital:,.2f})")
    stats.add_row("If you'd just held", f"${hold:,.2f}")

    recent = Table(title="Recent attempts", expand=True)
    for c in ("Time", "Buy", "Sell", "Seen %", "At fill %", "Profit $", "Result"):
        recent.add_column(c)
    for r in trader.recent:
        style = "green" if r[7] == "filled" else "yellow"
        recent.add_row(r[0], r[1], r[2], r[3], r[4], r[6], f"[{style}]{r[7]}")

    layout = Layout()
    layout.split_column(
        Layout(prices, size=len(trader.names) + 5),
        Layout(Panel(stats, title="Paper account (fake money)"), size=15),
        Layout(recent),
    )
    return layout

# -------------------------------------------------------------------- main ---


async def main():
    p = argparse.ArgumentParser(description="BTC arbitrage paper-trading scanner")
    p.add_argument("--demo", action="store_true", help="simulated prices, offline")
    p.add_argument("--capital", type=float, default=68.0, help="fake starting USD")
    p.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = until Ctrl+C)")
    p.add_argument("--exchanges", default=",".join(FEES), help="comma-separated list")
    p.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds between checks")
    p.add_argument("--no-ui", action="store_true", help="print a summary only")
    args = p.parse_args()

    names = [n.strip() for n in args.exchanges.split(",") if n.strip() in FEES]
    feed = DemoFeed(names) if args.demo else LiveFeed(names)
    mode = "DEMO (simulated prices)" if args.demo else "LIVE public prices, paper money"
    console = Console()

    quotes = await feed.tick()
    good = [n for n in names if "bid" in quotes.get(n, {})]
    if len(good) < 2:
        console.print("[red]Need at least two exchanges responding. Got:", quotes)
        console.print("Some exchanges block certain countries. Try --exchanges with others.")
        await feed.close()
        return
    trader = PaperTrader(good, args.capital,
                         statistics.median((quotes[n]["bid"] + quotes[n]["ask"]) / 2 for n in good))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"arb_log_{stamp}.csv"
    f = open(log_path, "w", newline="")
    log = csv.writer(f)
    log.writerow(["time", "buy_on", "sell_on", "net_edge_when_seen_pct",
                  "net_edge_at_fill_pct", "size_btc", "profit_usd", "result"])

    started = time.time()
    mid = 0.0

    async def loop(live=None):
        nonlocal quotes, mid
        while True:
            t0 = time.time()
            good_now = [n for n in trader.names if "bid" in quotes.get(n, {})]
            if len(good_now) >= 2:
                trader.checks_ok += 1
            else:
                trader.checks_bad += 1
            if good_now:
                mid = statistics.median((quotes[n]["bid"] + quotes[n]["ask"]) / 2 for n in good_now)
            best = trader.step(quotes, log)
            f.flush()
            if live:
                live.update(render(quotes, best, trader, mid, started, mode))
            if args.minutes and time.time() - started >= args.minutes * 60:
                break
            await asyncio.sleep(max(0, args.interval - (time.time() - t0)))
            try:
                quotes = await asyncio.wait_for(feed.tick(), TICK_TIMEOUT)
            except asyncio.TimeoutError:
                quotes = {n: {"error": "timeout"} for n in trader.names}

    try:
        if args.no_ui:
            await loop()
        else:
            with Live(console=console, refresh_per_second=4, screen=False) as live:
                await loop(live)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await feed.close()
        f.close()
        eq = trader.equity(mid) if mid else trader.start_capital
        console.print()
        console.print(Panel.fit(
            f"Ran {(time.time()-started)/60:.1f} min  |  gaps spotted {trader.seen}  |  "
            f"filled {trader.filled}  |  missed {trader.missed}  |  blocked {trader.blocked}\n"
            f"Price checks: {trader.checks_ok} ok, {trader.checks_bad} failed\n"
            f"Arbitrage profit: ${trader.realized:,.4f}\n"
            f"Account value: ${eq:,.2f}  vs  just holding: ${trader.hold_value(mid):,.2f}\n"
            f"Every attempt is logged in {log_path}",
            title="Session summary (paper money)"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
