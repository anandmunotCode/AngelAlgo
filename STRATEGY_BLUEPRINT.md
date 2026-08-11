# NIFTY DELTA-NEUTRAL IRON CONDOR STRATEGY SPECIFICATION

## 1. STRATEGY OVERVIEW

This is an automated, positional (multi-day) Delta-Neutral Iron Condor strategy for NIFTY 50 weekly options. 
The system enters on Wednesday at 09:18 AM IST (start of the new weekly cycle), holds positions overnight, dynamically rebalances on strong trending moves, and runs through Tuesday weekly expiry or until hitting risk/profit targets.

---

## 2. SYSTEM PARAMETERS

* Underlying Index: NIFTY 50 (NSE Indices)
* Segment: NFO (Options)
* Cycle Start: Wednesday at 09:18 AM IST
* Expiry Type: Nearest Tuesday Weekly Expiry
* Lot Size: 65 Quantity per lot
* Initial Short Delta: 0.15 Delta (Call and Put)
* Initial Hedge Delta: 0.05 Delta (Call and Put)
* Adjustment Trigger: 50% Short Premium Expansion from Baseline
* Straddle Capital Stop Loss: 2.0% of actual deployed capital
* Straddle Profit Target: 70% decay in combined straddle premium
* OTM Full Decay Target: Both short legs drop to <= Rs. 1.00
* Expiry Auto-Squareoff: Tuesday at 15:15 IST

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

## 4. EDGE CASE HANDLING & RECOVERY

* Overnight Gaps: If market opens with a large gap up or down, the engine triggers one single adjustment, updates the losing leg's baseline to the gap price, and waits. It will not generate runaway repetitive orders.
* System Restart & Persistence: The full state (all open legs, baseline prices, entry times, lot sizes, and realized P&L) is atomically saved to `positions.json`. If the process restarts, it reloads the active position without taking new initial trades.
* Real-time Data: Live prices are streamed via Angel One WebSocket V2 for sub-second updates, with REST API polling as an automatic fallback.
* Decoupled UI: The monitoring dashboard runs as an independent process that reads `positions.json`, ensuring browser activity never affects trading execution.
