# NIFTY DELTA-NEUTRAL IRON CONDOR STRATEGY SPECIFICATION

## 1. STRATEGY OVERVIEW

This is an automated, institutional-grade, positional (multi-day) Delta-Neutral Iron Condor strategy for NIFTY 50 weekly options on Angel One SmartAPI.
The system enters on the configured cycle start day (default: Wednesday) at 09:18 AM IST, holds positions overnight, dynamically rebalances on strong trending moves (50% premium expansion trigger), and runs through Tuesday weekly expiry (15:15 IST) or until hitting risk/profit targets.

> **Note:** The entry day is controlled by `CYCLE_START_DAY` in `config.py` (Python weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri). Change this value to enter on a different day if a cycle was missed.

> **Current Cycle:** `CYCLE_START_DAY = 2` (Wednesday to Tuesday weekly cycle).

---

## 2. SYSTEM PARAMETERS

* **Underlying Index:** NIFTY 50 (NSE Indices)
* **Segment:** NFO (Options)
* **Cycle Start:** Configured via `CYCLE_START_DAY` (default: Wednesday) at 09:18 AM IST
* **Expiry Type:** Nearest Tuesday Weekly Expiry
* **Lot Size:** 65 Quantity per lot
* **Initial Short Delta:** ~0.15 Delta (Call and Put)
* **Initial Hedge Delta:** ~0.05 Delta (Call and Put)
* **Adjustment Trigger:** 50% Short Premium Expansion from Active Baseline
* **Straddle Capital Stop Loss:** 2.0% of actual deployed capital (utilized margin)
* **Straddle Profit Target:** 70% decay in combined straddle premium
* **OTM Full Decay Target:** Both short legs drop to <= Rs. 1.00
* **Expiry Auto-Squareoff:** Tuesday at 15:15 IST
* **Market Close Time:** 15:40 IST (aligned with NSE F&O Closing Auction Session / CAS Standard)

---

## 3. LIFECYCLE & EXECUTION WORKFLOW

### Phase 1: Initial Entry
* **Timing:** 09:18 AM IST on Wednesday (allowing 3 minutes after 09:15 market open for order book spreads to stabilize).
* **Orders Executed:**
  1. Sell Short Call (CE) at approximately +0.15 Delta.
  2. Sell Short Put (PE) at approximately -0.15 Delta.
  3. Buy Long Call (CE) hedge at approximately +0.05 Delta.
  4. Buy Long Put (PE) hedge at approximately -0.05 Delta.
* **Recorded Baseline:**
  * For each short leg, the entry price is recorded as its initial `surge_baseline_premium`.

---

### Phase 2: Dynamic Adjustments (Non-Straddle Phase)

The system continuously monitors the price of both open short legs in real time.

#### The Trigger:
* An adjustment is triggered ONLY when an open short leg surges by 50% or more from its active baseline:
  * $\text{Surge \%} = \frac{\text{Live Price} - \text{Baseline Price}}{\text{Baseline Price}} \times 100$
  * **Condition:** $\text{Surge \%} \ge 50.0\%$

#### Rebalancing Steps:
1. **Losing Leg (The Surging Side):**
   * Remains completely open.
   * Its `surge_baseline_premium` is immediately updated to its exact current market price at the moment of adjustment execution.
   * This ensures the system waits for another 50% move from the newly established price before triggering any subsequent adjustment.
2. **Profitable Leg (The Winning Side):**
   * The open profitable short leg is closed to lock in realized profit.
   * Its paired hedge leg is also closed.
3. **New Inward Roll:**
   * The system reads the current absolute delta of the surging losing short leg.
   * It sells a new short leg on the winning side matching that exact delta, restoring net portfolio delta to near zero.
   * It reads the current absolute delta of the active losing hedge leg and buys a new hedge on the winning side matching that exact hedge delta (ensuring dynamic, symmetrical wing protection).
   * The entry price of the new short leg becomes its initial `surge_baseline_premium`.

---

### Phase 3: Straddle Convergence & Risk Defense

As adjustments happen, the profitable side rolls closer to the losing side. When both short legs end up on the exact same strike (Call Strike == Put Strike), the position has converged into a Short Straddle.

#### The Straddle Rule:
* Once Straddle mode is active, **ALL STRIKE ADJUSTMENTS ARE PERMANENTLY STOPPED**.
* The system switches exclusively to risk management and profit target rules:

1. **Dynamic 2.0% Capital Stop Loss:**
   * If total portfolio P&L (Realized + Unrealized) drops to -2.0% of the deployed capital, the entire position is squared off immediately.
2. **Spot Move Circuit Breaker:**
   * If NIFTY spot price moves 1.25% or more away from the straddle strike, the entire position is squared off immediately.
3. **70% Combined Theta Decay Profit Target:**
   * If the combined premium of the short Call and short Put decays by 70% or more from the straddle entry price, all legs are closed and profits are booked.

---

### Phase 4: Full Profit & Expiry Exits

1. **OTM Full Decay Target:**
   * If the position never reaches a Straddle and market remains range-bound, when both short Call and short Put drop to Rs. 1.00 or lower, full profit is captured and all legs are closed.
2. **Weekly Expiry Exit:**
   * On Tuesday at 15:15 IST, any remaining open legs are squared off at market prices.
   * The weekly cycle concludes and final P&L is recorded.

---

## 4. INFRASTRUCTURE & PRODUCTION DEPLOYMENT (AWS EC2)

The live production trading engine runs 24/7 on a dedicated **AWS EC2 Virtual Machine** in Mumbai to ensure zero reliance on local hardware, home internet, or laptop power.

### Production Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                   AWS Cloud — Mumbai (ap-south-1)                      │
│                                                                        │
│  EC2 Instance: t3.micro (Ubuntu 24.04 LTS) | Static Elastic IP         │
│                                                                        │
│  ┌──────────────────┐      ┌─────────────────────┐                     │
│  │   Cron Daemon    │ ───> │  aws/run_trading.sh │                     │
│  │ (Mon-Fri 09:00)  │      └──────────┬──────────┘                     │
│  └──────────────────┘                 │                                │
│                                       ▼                                │
│  ┌──────────────────┐      ┌─────────────────────┐    WebSocket / REST │
│  │ systemd Service  │ <──> │    Trading Engine   │ ──────────────────> │
│  │ (Auto-Restart)   │      │   (--live session)  │  Angel One SmartAPI │
│  └──────────────────┘      └──────────┬──────────┘                     │
│                                       │ (Market Close at 15:40 IST)    │
│                                       ▼                                │
│                            ┌─────────────────────┐                     │
│                            │    Git Auto-Push    │ ──> GitHub          │
│                            │  (State Persistence)│   (aws-deploy)      │
│                            └─────────────────────┘                     │
└────────────────────────────────────────────────────────────────────────┘
```

### AWS EC2 Specifications & Configuration:
* **Region:** `ap-south-1` (Asia Pacific - Mumbai)
* **Instance Type:** `t3.micro` (1 vCPU, 1 GB RAM, Ubuntu 24.04 LTS)
* **Instance ID:** `i-07c6c37fcfb9e000e`
* **Static Elastic IP:** `65.1.179.137`
* **Key Pair:** `angel-key` (`angel-key.pem` stored securely locally, gitignored)
* **Security Group Rules:**
  * **Inbound:** TCP Port 22 (SSH)
  * **Outbound:** All Traffic (HTTPS Port 443 for Angel One SmartAPI endpoints)
* **Timezone:** `Asia/Kolkata (IST, +0530)`

---

## 5. BRANCHING STRATEGY & REPOSITORY STRUCTURE

To maintain institutional cleanliness, the repository strictly isolates local development from cloud production:

| Branch | Environment | Purpose |
|---|---|---|
| `main` | Local Laptop / Dev | Code updates, backtesting, paper trading, and strategy experimentation. |
| `aws-deploy` | AWS EC2 Cloud Production | Dedicated production deployment branch. Contains EC2 service & automation scripts. |

### Dedicated AWS Files (`aws/` folder on `aws-deploy` branch):
* `aws/setup_ec2.sh` — One-time Ubuntu EC2 provisioning script (installs Python 3, dependencies, sets IST timezone, configures cron and systemd).
* `aws/run_trading.sh` — Daily execution orchestrator (pulls latest `aws-deploy`, runs `--live` engine, pushes final state to GitHub on market close).
* `aws/angelalgo.service` — Linux systemd service ensuring **zero downtime auto-restart within 10 seconds** if any unhandled error occurs.
* `aws/README.md` — Full technical documentation for AWS commands and operations.

---

## 6. DAILY OPERATIONAL SCHEDULE (MON-FRI)

| Time (IST) | Component | Action |
|---|---|---|
| **09:00 AM** | Crontab | Triggers `sudo systemctl start angelalgo`. |
| **09:00 AM** | `run_trading.sh` | Pulls latest code from `aws-deploy`, verifies Python packages. |
| **09:15 AM** | Engine | Connects to Angel One WebSocket V2, begins live streaming. |
| **09:18 AM** | Strategy | Initial entry window opens (if Wednesday / cycle start day). |
| **09:18 → 15:40** | Strategy | Real-time monitoring, Greeks calculation, and 50% surge adjustments. |
| **15:40 PM** | Engine | Market Close (CAS standard) — stops trading, saves state atomically. |
| **15:40 PM** | `run_trading.sh` | Commits `positions.json`, `trade_log.csv`, and logs to GitHub (`aws-deploy`). |
| **15:42 PM** | Crontab | Safety cleanup stop (`sudo systemctl stop angelalgo`). |

---

## 7. STATE PERSISTENCE & FAULT TOLERANCE

* **State Continuity (Zero Loss):** All active positions, baseline surge prices, strikes, quantities, and realized PnL are persisted in `positions.json` and `trade_log.csv`. When the EC2 instance boots or restarts, `position_manager.py` restores the active cycle without taking duplicate trades.
* **Auto-Restart on Crash:** `angelalgo.service` is configured with `Restart=on-failure` and `RestartSec=10` to automatically recover from network hiccups.
* **WebSocket V2 Streaming with REST Fallback:** Real-time spot and option tick data with automated reconnection logic (3s backoff).
* **Weekend Shutdown:** To optimize cloud costs, the EC2 instance can be stopped over the weekend (`aws ec2 stop-instances`) and started before Monday 09:00 AM (`aws ec2 start-instances`).
