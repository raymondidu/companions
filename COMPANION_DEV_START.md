# Companion Markets Dedicated Paper Server

This repository is the source of truth for Silver, Oil and BTC companion research. Gold production must remain untouched.

## Required server boundary
Use a separate DigitalOcean Droplet or other VM dedicated to companion research. Clone `raymondidu/companions` and use the `main` branch. Do not deploy this stack on the Gold server.

## Required environment
Create `.env` from `.env.example` and provide:
- `COMPANION_OANDA_BASE_URL`
- `COMPANION_OANDA_TOKEN`
- `COMPANION_OANDA_ACCOUNT_ID`
- optional exact provider-symbol overrides
- optional `COMPANION_COINBASE_BASE_URL` for the credential-free BTC paper feed
- optional `COMPANION_BINANCE_BASE_URL` for BTC market-breadth direction gating
- `COMPANION_DATA_DIR=/app/companion-data`
- `COMPANION_SCAN_INTERVAL_SECONDS=60`

Do not copy Gold's `.env`. Do not mount Gold volumes.

## Start paper research
`docker compose up -d --build`

Verify:
- scanner is running
- dashboard is running on port 8082
- `/health` reports `paper_only=true`
- `/health` reports `live_authority=false`
- `/health` reports `tradehouse_delivery=false`
- each market reports RUNNING or an explicit provider configuration state
- every market shows an advancing `scan_count` and a non-stale heartbeat

## Tournament rules
Each market keeps the five baseline profiles and adds five Gold-transfer profiles: HTF precision, EMA pullback, breakout retest, M30 local structure and M30 liquidity sweep. BTC also runs two Clean Path 10x lanes with identical entries: the -6% capital stop / +6% trigger / +4% first-lock policy and a no-stop challenger. No-stop position sizing is calibrated for survival against the 0.006 XAUUSD reference: Silver uses the Exness minimum 0.01 lot and USOIL deliberately rounds down to 0.02 lot instead of the nearer 0.03 notional match. The BTC Clean Path lanes remain a separate experiment using $200 margin at 10x ($2,000 simulated exposure). Every profile receives the same forward data but owns a separate research wallet/database. Only one position may be open per profile wallet.

A profile cannot be ranked before 30 forward trades and cannot become `promotion_ready` before 200 resolved forward trades, at least 80% first-lock rate and worst adverse excursion no worse than 2 ATR. `promotion_ready` is evidence only. There is no automatic live promotion.
