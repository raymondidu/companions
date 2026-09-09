# Tradehouse execution handoff: USOIL and BTC

Use this specification exactly. It connects Tradehouse execution to the Companion signal service while keeping all non-selected paths in paper testing.

## 1. Live scope

- Broker: `EXNESS`
- Signal cohort: `EXNESS_SURVIVAL_V1`
- Signal source: `https://companion.tradehouseapp.com/api/markets`
- Execute only `USOIL` and `BTC`. Never execute `SILVER` from this integration.
- Permit one live position per instrument and one order per signal ID.
- Never execute all tournament paths. Only the active path named below may create a live order.
- Keep every other path running as paper-only research.

Initial active paths, based on the forward results currently available:

- USOIL: `BALANCED_CLEAN` (the largest realized Oil P&L with more resolved observations than the one-trade paths).
- BTC: `GOLD_M30_LOCAL_STRUCTURE` (the largest realized BTC P&L among the completed non-stop prediction paths, with slightly less drawdown than the next path).

These are provisional paths, not proven winners. Neither has the 30 resolved trades required for a statistically eligible ranking.

## 2. Exness symbol and size validation

Before enabling order placement, query the connected Exness/MT5 account and fail closed unless both exact symbols are enabled and tradeable:

- Oil: broker symbol `USOIL`; market order volume `0.02` lot exactly.
- Bitcoin: broker symbol `BTCUSD`; target trade capital `$200`; target exposure `$2,000` at `10x`.

For BTC, calculate volume at the broker quote:

```text
raw_volume_btc = 2000 / executable_entry_price
volume_lots = floor(raw_volume_btc / broker_volume_step) * broker_volume_step
```

Reject the BTC order if `volume_lots < broker_min_volume`. Do not round upward if doing so would put required margin above `$200` at 10x. Record the actual fill, volume, notional, required margin, and effective leverage. If Exness applies fixed instrument margin rather than selectable 10x leverage, size the order so actual required margin is no more than `$200` and report the resulting effective leverage.

Do not infer symbols from the public catalogue alone. Validate them against the exact connected account.

## 3. Reading a signal

Poll the signal source every 10 seconds. Reject the entire cycle unless all of these are true:

```text
top_level.ok == true
market.ok == true
market.state == "RUNNING"
market.paper_only == true
market.live_authority == false
market.exness_tradability.account_status == "VERIFIED_ON_ACCOUNT"
market.heartbeat_age_seconds <= 120
profile.research_policy.cohort == "EXNESS_SURVIVAL_V1"
profile.profile == configured_active_path
```

`paper_only=true` and `live_authority=false` mean the Companion service supplies research signals but cannot place an order. Tradehouse is the separately authorized execution layer.

For the selected profile, inspect its newest `recent` record. A record is a new executable intent only when:

- `market_key`, `broker_symbol`, `direction`, `opened_at`, and database trade `id` are present;
- the record was not previously acknowledged;
- its age is no more than 120 seconds when first observed;
- there is no Tradehouse-managed live position for that instrument;
- the direction is exactly `LONG` or `SHORT`; and
- the live spread and broker trading state pass Tradehouse validation.

Create this stable idempotency key and store it permanently:

```text
EXNESS_SURVIVAL_V1:{market_key}:{profile}:{paper_trade_id}:{opened_at}
```

Never create a second broker order for the same idempotency key, including after a restart or timeout. If order status is unknown, reconcile by broker order comment and magic number before retrying.

Broker order comment:

```text
TH-COMPANION:{market_key}:{profile}:{paper_trade_id}
```

## 4. Entry execution

- Translate `LONG` to a market buy and `SHORT` to a market sell.
- Use the current executable Exness quote, not `reference_price`, as the requested entry.
- Reject stale quotes, closed markets, disabled symbols, invalid volume, insufficient free margin, or spread above the selected profile's admission ceiling.
- Do not place a hard stop loss at entry.
- Do not place a fixed take profit.
- After fill, persist the broker position ID, fill price, filled volume, signal idempotency key, active profile, and allocated capital.
- A rejected order must not be silently retried after the 120-second signal window. Record the exact rejection reason.

## 5. Profit-lock rule for both instruments

Use net return on allocated strategy capital, not raw instrument price movement:

```text
net_pnl_usd = broker_floating_profit + swap + commission
capital_return_pct = 100 * net_pnl_usd / allocated_capital_usd
```

Allocated capital:

- BTC: `$200`.
- USOIL: `$1,000`, matching the forward-test wallet used for the 0.02-lot Oil paths.

There is no strategy hard stop while the first profit lock is unarmed.

Arm the first lock when the highest observed `capital_return_pct` reaches `+15%`. At that moment, set a broker-side protective stop at the executable price corresponding to at least `+10%` net capital return.

For every additional full `+10` percentage points reached by the peak return, raise the locked return by `+10` percentage points:

```text
if peak_return_pct < 15:
    lock_return_pct = null
else:
    lock_return_pct = 10 + 10 * floor((peak_return_pct - 15) / 10)
```

Examples:

| Peak return | Locked return | BTC minimum locked P&L | USOIL minimum locked P&L |
|---:|---:|---:|---:|
| below 15% | none | none | none |
| 15% to 24.999% | 10% | $20 | $100 |
| 25% to 34.999% | 20% | $40 | $200 |
| 35% to 44.999% | 30% | $60 | $300 |
| 45% to 54.999% | 40% | $80 | $400 |

The lock may only move toward greater profit; it must never loosen. Calculate the stop price from the actual fill, volume, contract size, direction, and estimated closing costs. Round it conservatively to the broker tick size, then verify it satisfies Exness's current stop-distance rule. If the exact lock price is temporarily invalid, keep the prior valid protective stop and retry; never move the stop backward.

Monitor open positions from broker ticks at least once per second. Do not rely on an MT4/MT5 terminal-local trailing-stop feature. Tradehouse must update an actual broker-side stop so the last accepted lock remains present if the Tradehouse process or terminal disconnects.

If a gap or slippage fills below the intended locked return, record the intended lock, actual fill, slippage, and actual realized P&L. A lock is a stop instruction, not a guarantee of its fill price.

## 6. Exit and re-entry

- Normal exit reason: `TH_CAPITAL_STEP_LOCK_EXIT` when the broker-side lock is hit.
- Do not close merely because the Companion paper path closes under a different paper exit model.
- Do not reverse an open live position. Ignore new or opposite signals while the instrument is busy.
- After the live position closes, require a genuinely new paper signal ID before re-entry.
- No averaging down, martingale sizing, grid orders, or additional entries are allowed.

Emergency actions are operational, not strategy stops. Tradehouse must provide a manual kill switch that cancels pending orders and closes the two managed positions, plus a delivery kill switch that prevents new entries.

## 7. Champion selection while paper tests continue

Recalculate the eligible champion separately for USOIL and BTC once per day. A challenger may replace the active path only when all conditions are met:

- same instrument, cohort, sizing basis, and live exit simulation;
- at least 30 resolved forward trades;
- positive net realized P&L after spread, commission, and swap;
- first-lock rate at least 80%;
- no scanner staleness or data errors in the comparison window;
- higher realized P&L than the current path with no materially worse worst capital drawdown; and
- the advantage persists for two consecutive daily evaluations.

Switch only while that instrument has no live position. Store the old path, new path, metrics, reason, timestamp, and approving rule version. Never choose a path from open P&L or a single trade. If no path reaches 30 resolved trades, retain the configured provisional path and label it `PROVISIONAL_UNRANKED`.

## 8. Required acknowledgements and telemetry

Tradehouse must return or expose these events for reconciliation:

- `SIGNAL_ACCEPTED`
- `ORDER_FILLED`
- `ORDER_REJECTED` with exact reason
- `FIRST_LOCK_ARMED`
- `LOCK_RAISED`
- `POSITION_CLOSED` with exact reason and realized net P&L
- `SYMBOL_UNAVAILABLE`
- `SCANNER_STALE`
- `EXECUTION_DISCONNECTED`
- `CHAMPION_CHANGED`

Every event must include timestamp, instrument, active path, signal idempotency key, broker position/order ID, direction, fill/market price, volume, allocated capital, peak return, locked return, open P&L, realized P&L, and error or exit reason.

## 9. Enablement sequence

1. Connect a trade-only Exness account credential; withdrawals must remain disabled.
2. Verify exact account symbols and contract specifications for `USOIL` and `BTCUSD`.
3. Run the complete flow on the Exness demo account, including restart recovery and a forced lock update.
4. Reconcile every demo fill and trailing event against the specification.
5. Enable live entries only after an explicit human toggle. Never promote from paper to live automatically.

