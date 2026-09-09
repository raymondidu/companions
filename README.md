# Companions

Standalone forward-paper research for Silver (XAGUSD), WTI Oil and Bitcoin companions.

This repository is intentionally separate from `raymondidu/goldsignal`.

- paper only
- no live authority
- no TradeHouse delivery
- separate data stores and processes
- no Gold database, wallet, position capacity or runtime dependency

## Forward strategy tournament

Every scan evaluates three isolated families against the same forward data:

- baseline companion profiles
- Gold-transfer profiles: HTF precision, EMA pullback, breakout retest, M30 local structure and M30 liquidity sweep
- BTC-only Crypto Clean Path: identical 10x entries split between the confirmed -6% capital stop / +6% trigger / +4% first-lock policy and a no-stop challenger

No-stop position sizing is calibrated for survival against the 0.006 XAUUSD reference: Silver uses the Exness minimum 0.01 lot and USOIL deliberately rounds down to 0.02 lot instead of the nearer 0.03 notional match. Every BTC paper path uses $200 margin at 10x ($2,000 simulated exposure).

Prediction V2 runs in new, clean forward paths beside the preserved original controls. It requires H1/H4 structure, momentum and EMA-slope agreement; fresh two-candle M15 follow-through; confirmed candle quality; and ADX, volume, spread and extension gates. BTC V2 additionally requires strongly aligned market pressure, volume-weighted breadth and at least two aligned major assets. A permissive transition state is no longer sufficient for a V2 BTC entry.

The Exness public catalogue supports the configured execution symbols XAGUSD, USOIL and BTCUSD. Account-level availability is reported separately and remains unverified until the exact symbols exported from the target Exness account are supplied through `COMPANION_EXNESS_ACCOUNT_SYMBOLS`. Paper research can continue while this is unverified, but a configured missing symbol fails closed.

BTC Clean Path admission is governed by live, volume-weighted crypto-market breadth. A lane records its exact latest rejection reason when it does not enter. Qualified ranking begins only after 30 resolved trades; no lane can present promotion evidence before 200 resolved forward trades. Promotion still requires explicit human review and a separate code change. Full prediction scans remain at the configured cadence, while open positions are marked from fresh quotes every 10 seconds by default to reduce stop and profit-lock overshoot.

See `COMPANION_DEV_START.md` for deployment and isolation requirements.
