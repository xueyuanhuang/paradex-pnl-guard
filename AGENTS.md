# Paradex PnL Guard Project Notes

This file is the durable operating context for the project. Keep it updated whenever strategy logic, automation, deployment, or risk controls change.

## Project

- Repository: `https://github.com/xueyuanhuang/paradex-pnl-guard`
- Cloud path: `/home/ubuntu/paradex-pnl-guard`
- Runtime: Python in `venv`
- Main process: `src/main.py monitor`
- Cloud session: tmux session `paradex`
- Current live command:
  `venv/bin/python src/main.py monitor --repeat-interval 600 --pending-timeout 180`

## Strategy Logic

The bot monitors BTC/ETH Paradex perpetual positions as a paired grid strategy. The base asset size is `$1,000`; thresholds scale linearly if `--asset` changes.

Direction convention:

- `direction = 1`: LONG BTC / SHORT ETH
- `direction = -1`: SHORT BTC / LONG ETH

Levels and thresholds at base `$1,000`:

- `L1_TP`: total PnL `>= +17 USDC`; close L1 and open a new reverse L1.
- `L2_open`: total PnL `<= -17 USDC`; add L2 in the current direction.
- `L2_close`: total PnL `>= +32 USDC`; close the L2 lot only.
- `L3_open`: total PnL `<= -68 USDC`; add L3 in the current direction.
- `L3_close`: total PnL `>= +33 USDC`; close the L3 lot only.
- `warning`: total PnL `<= -170 USDC`; warning only.

Opening notionals at base `$1,000`:

- L1: BTC `$1,000`, ETH `$667`
- L2: BTC `$2,000`, ETH `$1,333`
- L3: BTC `$3,000`, ETH `$2,000`

The bot does not automatically stop loss. Deep loss only sends warnings.

## Automation Behavior

When auto trading is enabled, the bot submits standard Paradex API market orders:

- Close actions are reduce-only and use the actual lot size when fills can reconstruct it.
- Open actions are specified by target notional; the bot converts notional to quantity using current price marks.
- Every automated operation sends Telegram notices for prepare, submitted, confirmed, failure, and blocked states.
- Stablecoin transfers are monitored and notified separately.

The user-facing requirements are:

- On close, always tell the explicit BTC/ETH quantity being closed.
- On open, always tell the target notional value.
- If a manual action is needed, repeat reminders until the state changes.
- Full automation is enabled, but automatic stop-loss is not enabled.

## PnL Sources

REST position PnL is the only strategy trigger source. The default monitor poll interval is `5s`.

WebSocket BBO is diagnostics-only. It can be logged to compare executable-price PnL and spread quality, but it must not trigger strategy actions or Telegram circuit-breaker notices.

- LONG leg: `size * (bid - entry)`
- SHORT leg: `size * (entry - ask)`

Open and close thresholds are evaluated from REST position PnL only.

## Risk Controls

System state:

- Before any live auto order, the bot calls Paradex `/v1/system/state`.
- Orders are allowed only when `SystemState = ok`.
- If the system is `cancel_only`, `maintenance`, or unknown, the action is blocked and Telegram is notified.

BBO diagnostics:

- BBO diagnostics are optional and enabled by default for logs only.
- The bot may log BBO executable-price PnL and BTC/ETH spreads.
- BBO does not trigger L1_TP, L2/L3 open, L2/L3 close, or warning actions.
- BBO diagnostics must not send circuit-breaker/recovery Telegram messages.

## 2026-06-05 Incident

At `2026-06-05 04:45 UTC`, WS BBO produced `-20.32 USDC` while REST position PnL was about `+0.65 USDC`. The bot attempted `L2_open`, but Paradex rejected the order with `SYSTEM_STATUS_CANCEL_ONLY`. No L2 position was opened and no pending action remained.

The likely cause was abnormal BBO during the Paradex maintenance/release window. The mitigation is now:

- use REST as the only strategy trigger source,
- block orders unless SystemState is `ok`,
- keep BBO as diagnostics only.

## Maintenance Rules

- Never commit secrets, private keys, JWTs, or `.env`.
- Do not assume local `/Users/xueyuanhuang/Projects/paradex-strategy` is the live repo; the live repo is on the cloud path above.
- Prefer small, focused commits.
- After every strategy or automation change:
  - update this `AGENTS.md`,
  - run Python syntax checks,
  - commit and push to GitHub,
  - pull/deploy on the cloud host,
  - restart the tmux monitor,
  - verify logs show the expected mode.
