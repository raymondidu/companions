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
Each of Silver, USOIL and BTC has five independent entry profiles: STRICT_PRECISION, BALANCED_CLEAN, TREND_ONLY, BREAKOUT_ONLY and ELITE_ONLY. Each profile receives the same forward data but owns a separate $1,000 research wallet/database. Only one position may be open per profile wallet. Policies are ATR-normalized and independent of Gold.

A profile cannot become `promotion_ready` before at least 30 forward trades, at least 80% first-lock rate and worst adverse excursion no worse than 2 ATR. `promotion_ready` is evidence only. There is no automatic live promotion.
