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
- BTC-only Crypto Clean Path: identical 5x entries split between the confirmed -6% capital stop / +6% trigger / +4% first-lock policy and a no-stop challenger

BTC Clean Path admission is governed by live, volume-weighted crypto-market breadth. A lane records its exact latest rejection reason when it does not enter. Ranking begins after 30 trades; no lane can present promotion evidence before 200 resolved forward trades. Promotion still requires explicit human review and a separate code change.

See `COMPANION_DEV_START.md` for deployment and isolation requirements.
