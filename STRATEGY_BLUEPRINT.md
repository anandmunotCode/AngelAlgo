# NIFTY DELTA-NEUTRAL IRON CONDOR STRATEGY SPECIFICATION

## 1. STRATEGY OVERVIEW

This is an automated, positional (multi-day) Delta-Neutral Iron Condor strategy for NIFTY 50 weekly options. 
The system enters on the configured cycle start day (default: Wednesday) at 09:18 AM IST, holds positions overnight, dynamically rebalances on strong trending moves, and runs through Tuesday weekly expiry or until hitting risk/profit targets.

> **Note:** The entry day is controlled by `CYCLE_START_DAY` in `config.py` (Python weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri). Change this value to enter on a different day if a cycle was missed.

> **Current Cycle:** `CYCLE_START_DAY = 2` (Wednesday to Tuesday weekly cycle).

---

## 2. SYSTEM PARAMETERS

* Underlying Index: NIFTY 50 (NSE Indices)
* Segment: NFO (Options)
* Cycle Start: Configured via `CYCLE_START_DAY` (default: Wednesday) at 09:18 AM IST
* Expiry Type: Nearest Tuesday Weekly Expiry
* Lot Size: 65 Quantity per lot
* Initial Short Delta: 0.15 Delta (Call and Put)
* Initial Hedge Delta: 0.05 Delta (Call and Put)
* Adjustment Trigger: 50% Short Premium Expansion from Baseline
* Straddle Capital Stop Loss: 2.0% of actual deployed capital
* Straddle Profit Target: 70% decay in combined straddle premium
* OTM Full Decay Target: Both short legs drop to <= Rs. 1.00
* Expiry Auto-Squareoff: Tuesday at 15:15 IST
* Market Close Time: 15:41 IST (extended for F&O CAS — Closing Auction Session)

---

## 3. LIFECYCLE & EXECUTION WORKFLOW

### Phase 1: Initial Entry
* Timing: 09:18 AM IST on Wednesday (allowing 3 minutes after 09:15 market open for order book spreads to stabilize).
* Orders Executed:
  1. Sell Short Call (CE) at approximately +0.15 Delta.
  2. Sell Short Put (PE) at approximately -0.15 Delta.
  3. Buy Long Call (CE) hedge at approximately +0.05 Delta.
  4. Buy Long Put (PE) hedge at approximately -0.05 Delta.
* Recorded Baseline:
  * For each short leg, the entry price is recorded as its initial `surge_baseline_premium`.

---

### Phase 2: Dynamic Adjustments (Non-Straddle Phase)

The system continuously monitors the price of both open short legs in real time.

#### The Trigger:
* An adjustment is triggered ONLY when an open short leg surges by 50% or more from its active baseline:
  * Surge % = (Live Price - Baseline Price) / Baseline Price * 100
  * Condition: Surge % >= 50.0%

#### Rebalancing Steps:
1. Losing Leg (The Surging Side):
   * Remains completely open.
   * Its `surge_baseline_premium` is immediately updated to its exact current market price at the moment of adjustment execution.
   * This ensures the system waits for another 50% move from the newly established price before triggering any subsequent adjustment.
2. Profitable Leg (The Winning Side):
   * The open profitable short leg is closed to lock in realized profit.
   * Its paired hedge leg is also closed.
3. New Inward Roll:
   * The system reads the current absolute delta of the surging losing short leg.
   * It sells a new short leg on the winning side matching that exact delta, restoring net portfolio delta to near zero.
   * It reads the current absolute delta of the active losing hedge leg and buys a new hedge on the winning side matching that exact hedge delta (ensuring dynamic, symmetrical wing protection).
   * The entry price of the new short leg becomes its initial `surge_baseline_premium`.

---

### Phase 3: Straddle Convergence & Risk Defense

As adjustments happen, the profitable side rolls closer to the losing side. When both short legs end up on the exact same strike (Call Strike == Put Strike), the position has converged into a Short Straddle.

#### The Straddle Rule:
* Once Straddle mode is active, ALL STRIKE ADJUSTMENTS ARE PERMANENTLY STOPPED.
* The system switches exclusively to risk management and profit target rules:

1. Dynamic 2.0% Capital Stop Loss:
   * If total portfolio P&L (Realized + Unrealized) drops to -2.0% of the deployed capital, the entire position is squared off immediately.
2. Spot Move Circuit Breaker:
   * If NIFTY spot price moves 1.25% or more away from the straddle strike, the entire position is squared off immediately.
3. 70% Combined Theta Decay Profit Target:
   * If the combined premium of the short Call and short Put decays by 70% or more from the straddle entry price, all legs are closed and profits are booked.

---

### Phase 4: Full Profit & Expiry Exits

1. OTM Full Decay Target:
   * If the position never reaches a Straddle and market remains range-bound, when both short Call and short Put drop to Rs. 1.00 or lower, full profit is captured and all legs are closed.
2. Weekly Expiry Exit:
   * On Tuesday at 15:15 IST, any remaining open legs are squared off at market prices.
   * The weekly cycle concludes and final P&L is recorded.

---

## 5. INFRASTRUCTURE: RELAY PATTERN (GitHub Actions)

The Indian market day (09:15–15:41 IST = 6 hrs 26 min) exceeds GitHub Actions' 6-hour per-job limit. To solve this, the workflow uses a **Relay Pattern** — two sequential jobs within a single workflow file.

### Trigger:
* **Manual only** (`workflow_dispatch`). No automatic cron schedule. The operator clicks "Run workflow" on GitHub Actions each trading day.

| Session | Time (IST) | Duration | Environment Overrides |
|---|---|---|---|
| `morning_session` | ~09:00 → 13:00 | ~4 hrs | `MARKET_CLOSE_HOUR=13`, `MARKET_CLOSE_MINUTE=0` |
| `afternoon_session` | ~13:01 → 15:41 | ~2.7 hrs | `MARKET_CLOSE_HOUR=15`, `MARKET_CLOSE_MINUTE=41` |

### How the Relay Works:
1. The morning job runs the Python engine with `MARKET_CLOSE_HOUR=13`. At 13:00 IST, the engine saves all state to `positions.json`, commits, and pushes to GitHub.
2. The afternoon job (`needs: morning_session`) auto-starts immediately, pulls the latest `positions.json`, reconnects to the broker API, and resumes monitoring until 15:41 IST.
3. Each job gets a fresh 6-hour clock, so both sessions are well within limits.

### Handoff Gap:
* There is a ~30–60 second gap at 13:00 IST while the afternoon server boots. During this time, the bot is offline but all broker-side positions remain active. Upon resuming, the bot instantly fetches live prices and evaluates all stop-loss/adjustment triggers.

### Market Close Time (CAS):
* As of 2025, Indian F&O derivatives markets extended closing to 15:41 IST due to the Closing Auction Session (CAS). The `MARKET_CLOSE_HOUR` and `MARKET_CLOSE_MINUTE` in `config.py` are configurable via environment variables to support this.

---

## 6. EDGE CASE HANDLING & RECOVERY

* Overnight Gaps: If market opens with a large gap up or down, the engine triggers one single adjustment, updates the losing leg's baseline to the gap price, and waits. It will not generate runaway repetitive orders.
* System Restart & Persistence: The full state (all open legs, baseline prices, entry times, lot sizes, and realized P&L) is atomically saved to `positions.json`. If the process restarts, it reloads the active position without taking new initial trades.
* Relay Handoff Recovery: During the midday relay (morning → afternoon), the afternoon session loads `positions.json` from the morning commit. The engine detects existing open legs and resumes monitoring — it does NOT place duplicate entry orders.
* Real-time Data: Live prices are streamed via Angel One WebSocket V2 for sub-second updates, with REST API polling as an automatic fallback.
* Decoupled UI: The monitoring dashboard runs as an independent process that reads `positions.json`, ensuring browser activity never affects trading execution.
* GitHub Actions Minutes: The repository is set to **public** to get unlimited free GitHub Actions minutes (private repos are capped at 2000 min/month).
* Order Audit Trail: Every order execution (initial entries, adjustment closes, new rolls) is logged to `trade_log.csv`. All 8 events per adjustment cycle are captured — not just exits.
