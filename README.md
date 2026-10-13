# Arbitrage Reality Check

A viral post claimed a 19-year-old built a crypto arbitrage bot in 48 hours
and turned **$68 into $750,000** by "spotting price errors before humans
notice." I built two paper-trading scanners to test that claim against live
market data.

**Result: once fees, gas, delay and real balances are counted, I found no
arbitrage at all on major exchanges or on-chain. The only "profit" I ever saw
came from a bug in my own simulator, and finding it was the most useful
lesson of the project.**

No API keys, no wallet, no real orders. Everything here is read-only.

---

## What's in the repo

| File | What it does |
|---|---|
| `cex_scanner.py` | Streams BTC/USDT bid and ask prices from Binance, OKX, Bybit, KuCoin and Gate.io every second, looks for buy-low/sell-high gaps after taker fees, and paper-trades them. |
| `defi_scanner.py` | Asks Uniswap v3's on-chain Quoter contract on **Base** and **Arbitrum** what a real swap would return, compares that with Binance for BTC and ETH, and paper-trades any gap that survives pool fees, price impact and live gas costs. |
| `tests/` | Offline tests for the contract-call encoding, the fee math, and the balance rules. |

### What makes the simulation honest

- **All costs included.** Exchange taker fees, Uniswap pool fees, price impact at the chosen trade size, and live gas prices from each chain.
- **Reaction delay.** A gap spotted on one check is filled at the *next* check's prices. If it has closed by then, it counts as **missed**.
- **Real balances.** Money starts split across venues. Each trade spends dollars on one side and coins on the other, so a gap can only be traded until one side runs dry (**blocked**).
- **Token identity.** WBTC (Arbitrum), cbBTC (Base) and BTC (Binance) are *different assets*, so a price gap between them is shown separately and never counted as arbitrage.
- **Feed health.** Every run reports how many price checks succeeded, so "no opportunities" can't be confused with "no data."
- **Benchmark.** Account value is compared with simply holding the starting coins.

---

## Findings (live runs, 13 October 2026)

### 1. Centralized exchanges: prices match within 0.015%, fees are 0.2%

Within the first minutes, the five exchanges quoted BTC within about **$12 of
each other on an $83,700 asset (≈0.015%)**. Binance's bid/ask spread was
**one cent**. A buy on one exchange and a sell on another costs at least
**0.2%** in taker fees, more than ten times the observed gap.

**Gaps spotted after fees: 0.** Professional market makers keep these
books aligned within milliseconds; a laptop polling once a second never sees
the gap open.

### 2. DeFi, first version: a fake $114 profit (my bug)

The first DeFi scanner reported **1,624 trades and $114.79 profit in 54
minutes**, all on the same route: buy "BTC" on Arbitrum, sell "BTC" on Base,
every two seconds, never missing.

That pattern was the giveaway. A real arbitrage gap is gone within a block,
not open for an hour while thousands of bots watch it. Two bugs:

1. **Different tokens.** The "BTC" on Arbitrum is WBTC (BitGo); on Base it is
   cbBTC (Coinbase). They can't be swapped one-for-one across chains; you have
   to redeem through each issuer, which costs fees and takes days. The steady
   ~0.2% price difference is the market pricing that in.
2. **Unlimited money.** The simulator never tracked balances, so it "bought"
   on Arbitrum 1,624 times without running out of dollars.

Replaying the same prices with balances tracked: **2 trades, $0.06 total**,
then stuck with no dollars left on Arbitrum.

### 3. DeFi, fixed version: nothing

Two runs, both with zero opportunities:

| Run | Length | Price checks | Gaps spotted | Blocked |
|---|---|---|---|---|
| 13 Oct, afternoon | 74.6 min | not tracked yet | 0 | 0 |
| 13 Oct, night | 62.1 min | **434 ok, 3 failed (99.3%)** | **0** | **0** |

"Blocked" counts every profitable same-token gap *even if the account
couldn't fund it*. So across 434 successful checks of Binance, Base and
Arbitrum, there was not a single moment when ETH-for-ETH or BTC-for-BTC
would have paid even one cent after fees and gas.

One honest limit: over public RPC endpoints a full check took about
8.6 seconds, so the scanner saw roughly one Base block in four. A gap that
opened and closed between checks would be missed, but a bot that slow
couldn't have traded it anyway; MEV searchers close those within the block.

### Why the viral claim doesn't hold up

- The edge it describes (speed) belongs to firms with servers next to the
  exchanges and to MEV searchers who pay block builders for ordering. A bot
  that checks once a second, let alone one waiting on an AI model, loses that
  race every time.
- Arbitrage profits are a fraction of a percent per trade. Turning $68 into
  $6,732 in one night is a ~100× return, which arbitrage can't produce
  without leverage that would wipe the account on one bad fill.
- The post ends with "follow me for an auto DM," the standard funnel for
  paid courses, signal groups and malicious "deploy this bot contract" scams.

### What the paper results still leave out

So real-money results would be *worse*: MEV bots front-running on-chain
transactions, failed transactions that still pay gas, withdrawal and bridge
fees for rebalancing between venues, and order-book depth beyond the top
level on exchanges.

---

## Run it yourself

Needs Python 3.9 or newer. On a Mac, use `python3`.

```bash
git clone https://github.com/anubhav0089/arb-reality-check.git
cd arb-reality-check
python3 -m pip install -r requirements.txt

python3 cex_scanner.py --demo           # simulated prices, offline
python3 cex_scanner.py --minutes 60     # live exchange prices, paper money

python3 defi_scanner.py --minutes 60    # live on-chain quotes, paper money
python3 defi_scanner.py --size 500      # try bigger trades
```

Every attempt is logged to a timestamped CSV in the current folder. Press
Ctrl+C to stop early and print a summary.

| Option | Scanner | Meaning |
|---|---|---|
| `--demo` | both | Simulated prices (results are meaningless, for testing only) |
| `--minutes N` | both | Stop after N minutes |
| `--interval S` | both | Seconds between price checks |
| `--capital USD` | both | Fake starting money (CEX default $68, DeFi default $300) |
| `--exchanges a,b` | CEX | Exchanges to watch |
| `--size USD` | DeFi | Dollars per simulated trade (default $25) |
| `--include-wrapped` | DeFi | Treat WBTC, cbBTC and BTC as the same coin, to reproduce the bug |

If an exchange shows red, it is probably blocked in your region; leave it
out with `--exchanges`.

### Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

---

## How it works

**Exchanges.** `ccxt` fetches each exchange's public ticker concurrently.
For each pair of exchanges, the net edge is
`bid_sell × (1 − fee_sell) ÷ (ask_buy × (1 + fee_buy)) − 1`.

**On-chain.** The scanner sends `eth_call` requests to Uniswap's
QuoterV2
(`quoteExactInputSingle`), with the ABI encoded by hand, batched per chain
over public JSON-RPC. The quote already includes the pool fee and price
impact for the exact trade size. At startup it tries each fee tier
(0.01%, 0.05%, 0.3%, 1%) and keeps the best-priced pool. Gas cost is the
Quoter's gas estimate plus router overhead, times the live gas price,
converted to USD.

Contract addresses come from Uniswap's official deployment pages for
[Base](https://docs.uniswap.org/contracts/v3/reference/deployments/base-deployments)
and [Arbitrum](https://docs.uniswap.org/contracts/v3/reference/deployments/arbitrum-deployments).

---

## Disclaimer

Educational project. Not financial advice. It places no orders and holds
no keys. Don't send money or wallet access to anyone offering a bot that
"prints" arbitrage profits.

## License

MIT
