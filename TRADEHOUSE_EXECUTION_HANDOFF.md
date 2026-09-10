# TradeHouse execution handoff: USOIL and BTC

This is the authoritative boundary between Companion intelligence and TradeHouse execution.

## 1. Live scope

- Signal cohort: `EXNESS_SURVIVAL_V1`
- Companion source: `https://companion.tradehouseapp.com/api/markets`
- Deliverable instruments only: `USOIL` and `BTCUSD`
- Never deliver `SILVER` from this integration.
- Deliver only the currently selected path for each market:
  - USOIL: `BALANCED_CLEAN`
  - BTC: `GOLD_M30_LOCAL_STRUCTURE`
- Every other strategy remains paper-only research.

## 2. Ownership boundary

Companion decides only:

- whether a fresh qualifying trade exists;
- instrument;
- strategy direction (`LONG` or `SHORT`);
- current executable/reference market price at signal creation;
- signal creation timestamp;
- stable signal id;
- selected prediction path/cohort metadata.

TradeHouse owns all execution decisions tied to the user account, including user capital, position sizing, lot size, leverage/margin, broker/account selection, order placement, trailing/protection, closing and realized P&L.

Companion must never instruct TradeHouse to use a fixed dollar amount, fixed wallet size, fixed lot size, or fixed leverage. Paper-tournament sizing exists only to compare prediction paths consistently and must not cross the live transport boundary.

## 3. Live transport payload

The current TradeHouse Companion ingest contract requires `LONG` / `SHORT` exactly. Do not translate these values to `BUY` / `SELL`.

```json
{
  "signal_id": "OIL-or-BTC-stable-id",
  "direction": "LONG",
  "instrument": "USOIL",
  "cohort": "EXNESS_SURVIVAL_V1",
  "path": "BALANCED_CLEAN",
  "entry": 0.0,
  "signal_created_at": "UTC ISO-8601 timestamp"
}
```

For BTC, `instrument` is `BTCUSD` and the active path is `GOLD_M30_LOCAL_STRUCTURE`.

Do not add capital, wallet, lot, margin, leverage, stop-loss, take-profit, or sizing instructions to the live payload.

## 4. Freshness and duplicate protection

- A signal must be created from the current live quote used by the Companion scan.
- The signal must be no older than 120 seconds when Companion attempts delivery.
- Repeated scans of the same setup candle must reuse the same stable signal id.
- TradeHouse must treat `signal_id` as an idempotency key and never open the same signal twice.
- Companion must not manufacture a synthetic signal merely to test transport.

## 5. Delivery behavior

Companion posts approved signals to:

```text
POST {COMPANION_EXECUTOR_BASE_URL}/api/companion/ingest
```

with:

```text
x-executor-secret: <shared TradeHouse executor secret>
Content-Type: application/json
```

HTTP 200/202 proves only that TradeHouse received the instruction. A real trade is confirmed only by a TradeHouse lifecycle callback containing an `OPENED` event and a real `broker_position_id`.

## 6. Callback truth

TradeHouse sends lifecycle events to:

```text
POST https://companion.tradehouseapp.com/api/companion/callback
```

Callbacks must be HMAC authenticated and sequenced. Supported lifecycle events include:

- `ACCEPTED`
- `RECEIVED`
- `OPENING`
- `OPENED`
- `POSITION_UPDATE`
- `PROTECTION_ARMED`
- `PROTECTION_RAISED`
- `CLOSE_REQUESTED`
- `CLOSED`
- `OPEN_FAILED`
- `POSITION_LOST`
- `RECOVERED_AFTER_RESTART`

`OPENED` must include `broker_position_id`. `CLOSED` must include the broker-authoritative realized result when available. `OPEN_FAILED` details should be preserved so the dashboard can show the broker failure reason and distinguish failed fan-out account opens from failed prediction signals.

## 7. Prediction-path policy

Companion may evaluate all tournament paths on paper, but only the configured live champion may be delivered.

Current provisional champions:

- USOIL: `BALANCED_CLEAN`
- BTC: `GOLD_M30_LOCAL_STRUCTURE`

Do not replace either path because of one or two unusually profitable trades. Promotion requires a meaningful forward sample and must compare profit, first-lock/true-confidence quality, and adverse excursion. A challenger remains paper-only until promoted explicitly.

## 8. Fail-closed rules

Do not deliver when any of these is true:

- instrument is not in the approved live set;
- wrong cohort or path;
- no fresh qualifying setup;
- signal timestamp is stale;
- invalid direction;
- TradeHouse executor URL/secret is not configured;
- live market-quality gates reject the setup.

Do not weaken market-quality gates merely to increase signal count.

## 9. Dashboard interpretation

The dashboard must distinguish these stages:

- `Generated`: an approved Companion signal identity exists;
- `Sent`: an HTTP delivery attempt was made;
- `Accepted`: TradeHouse acknowledged acceptance;
- `Opened`: TradeHouse callback confirmed a broker position id;
- `Closed`: TradeHouse callback confirmed closure;
- `Failed signals`: number of distinct signals with at least one failed fan-out broker open;
- `Failed account opens`: number of individual TradeHouse account positions that reported `OPEN_FAILED`.

A paper trade is not a TradeHouse trade. HTTP acceptance is not a broker fill. Only callback-confirmed broker lifecycle data is live execution truth.
