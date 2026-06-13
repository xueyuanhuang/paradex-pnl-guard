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
- `warning`: total PnL `<= -170 USDC`; log warning only. Telegram trade notices are limited to open/close results.

Opening notionals at base `$1,000`:

- L1: BTC `$1,000`, ETH `$667`
- L2: BTC `$2,000`, ETH `$1,333`
- L3: BTC `$3,000`, ETH `$2,000`

The bot does not automatically stop loss. Deep loss is logged but does not send a Telegram trade notice.

## Automation Behavior

When auto trading is enabled, the bot submits standard Paradex API market orders:

- Close actions are reduce-only and use the recorded auto-opened lot size.
- Open actions are specified by target notional; the bot converts notional to quantity using current price marks.
- Auto-opened L1/L2/L3 lots are recorded in `state.json` under `lots`. Close actions use these recorded lot sizes instead of reconstructing lots from fills.
- Before any reduce-only close, the planned close quantity is capped by the current actual BTC/ETH position size to avoid reduce-only orders that would flip the position.
- If recorded lots are missing or their total BTC/ETH size differs from the current actual position, the bot halts instead of guessing from trade history.
- Paradex batch orders are not treated as atomic. After every submission, the bot fetches order history by client id and verifies each BTC/ETH leg is fully filled.
- `L1_TP` is executed in two phases: close old L1 first, verify fills, then open the new reverse L1. Closing and reopening must not be submitted in the same batch.
- If any leg is missing, partially filled, cancelled, or absent from order history, the bot attempts reduce-only flattening of all remaining BTC/ETH positions, sets `halted.active = true`, and stops all further threshold trading until `resume` is run after manual verification.
- Automatic Telegram trade notices are limited to open/close result messages. They include actual filled BTC/ETH size, filled notional, and remaining BTC/ETH position size, notional, and PnL. They do not include Entry/Mark fields.
- Stablecoin transfers are monitored and notified separately.

The user-facing requirements are:

- On close, tell the actual BTC/ETH filled quantity and notional, plus remaining BTC/ETH position quantity, notional, and PnL.
- On open, tell the actual BTC/ETH filled quantity and notional, plus remaining BTC/ETH position quantity, notional, and PnL.
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
- If the system is `cancel_only`, `maintenance`, or unknown at action time, the action is blocked, a same-format open/close abnormal notice is sent, and the bot is halted until manual verification.

Position integrity:

- BTC and ETH legs must both be present when any position is open.
- BTC and ETH sides must be paired as LONG BTC / SHORT ETH or SHORT BTC / LONG ETH.
- If an unpaired or severely imbalanced BTC/ETH position is detected, the bot attempts reduce-only flattening of all BTC/ETH positions, sends a close-abnormal notice, sets `halted.active = true`, and stops trading.

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

## 2026-06-07 Incident

At `2026-06-06 04:52 UTC`, an `L3_open` batch submitted both BTC and ETH legs. Paradex filled the ETH BUY leg (`1.3097 ETH`) but cancelled the BTC SELL leg (`0.04985 BTC`) with `NOT_ENOUGH_MARGIN`. Later retries were also cancelled for insufficient margin. This confirmed that Paradex `orders/batch` is not atomic.

The mitigation is now:

- verify every submitted BTC/ETH leg from `orders-history` by `client_id`,
- treat submitted-but-unfilled or partially filled legs as action failure,
- flatten all remaining BTC/ETH exposure on any leg failure,
- halt the bot until manual verification,
- execute L1 close and reverse-open in separate verified phases.

## 2026-06-12 Incident

At `2026-06-12 15:10 UTC`, an `L1_TP` close attempted to reduce-only close the previous L1 sizes (`BTC 0.01622`, `ETH 0.4194`) while the current L1 sizes were smaller (`BTC 0.0158`, `ETH 0.3934`). Paradex rejected both close orders with `REDUCE_ONLY_WILL_INCREASE`. The protective flattening path then used the actual current position sizes and successfully flattened the account.

Root cause: after an L1 take-profit reversal, the old close fills and new open fills have the same BUY/SELL side. The legacy fill-reconstruction logic matched by side and could mistake the old close fills for the current L1 open fills.

The mitigation is now:

- auto-opened L1/L2/L3 lots are stored in `state.json`,
- close actions use recorded lots rather than fill reconstruction,
- reduce-only close quantities are capped to current actual position size,
- missing or mismatched recorded lots halt the bot instead of guessing.

## Maintenance Rules

- Never commit secrets, private keys, JWTs, or `.env`.
- Do not assume local `/Users/xueyuanhuang/Projects/paradex-strategy` is the live repo; the live repo is on the cloud path above.
- Prefer small, focused commits.
- After every strategy or automation change:
  - update this `AGENTS.md`,
  - run Python syntax checks,
  - commit and push to GitHub,
  - pull/deploy on the cloud host,
  - restart the tmux monitor only if the user has approved running the bot,
  - verify logs show the expected mode when it is restarted.
