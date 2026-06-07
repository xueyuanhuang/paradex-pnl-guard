# Paradex PnL Guard

Paradex BTC/ETH paired grid monitor and auto-executor.

The durable project notes, strategy rules, deployment details, and maintenance requirements live in [AGENTS.md](AGENTS.md). Update that file whenever strategy logic or automation behavior changes.

## Current Runtime

Cloud path:

```bash
/home/ubuntu/paradex-pnl-guard
```

Monitor command:

```bash
venv/bin/python src/main.py monitor --repeat-interval 600 --pending-timeout 180
```

## Safety Summary

- Auto trading can submit live Paradex API market orders when enabled in `.env`.
- The bot does not automatically stop loss.
- Paradex `SystemState` must be `ok` before any live auto order.
- REST position PnL is the only strategy trigger source; default polling is every 5 seconds.
- WebSocket BBO is diagnostics-only and does not trigger strategy actions.
- Every submitted BTC/ETH leg is verified from order history. If any leg fails or partially fills, the bot attempts reduce-only flattening, halts, and waits for manual resume.

## Basic Commands

Show current grid state:

```bash
venv/bin/python src/main.py status
```

Clear a halted state after manual position verification:

```bash
venv/bin/python src/main.py resume
```

Start monitor:

```bash
venv/bin/python src/main.py monitor
```

Disable BBO diagnostics for a run:

```bash
venv/bin/python src/main.py monitor --no-ws-bbo
```

## Configuration

Required `.env` values:

```ini
PARADEX_JWT=
TG_BOT_TOKEN=
TG_CHAT_ID=
AUTO_TRADE_ENABLED=true
AUTO_TRADE_DRY_RUN=false
PARADEX_ENV=prod
PARADEX_ACCOUNT_ADDRESS=
PARADEX_SUBKEY_PRIVATE_KEY=
```

Never commit `.env` or credentials.
