#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# AngelAlgo - Daily Trading Runner for AWS EC2
# Called by systemd service (auto-restart on crash)
# ═══════════════════════════════════════════════════════════════

set -e

ALGO_DIR="/home/ubuntu/AngelAlgo"
cd "$ALGO_DIR"

DATE=$(date '+%Y-%m-%d')
LOG_FILE="$ALGO_DIR/logs/runner_${DATE}.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ─── 1. Pull latest code from main branch ───────────────────
log "Pulling latest code from GitHub..."
git pull origin main --quiet 2>/dev/null || log "WARNING: git pull failed, using local code"

# ─── 2. Install/update dependencies (silent) ────────────────
log "Checking dependencies..."
pip3 install -r Delta_Neutral_Nifty/requirements.txt --quiet --break-system-packages 2>/dev/null

# ─── 3. Start trading engine ───────────────────────────────
# Default is PAPER trading (reads live market prices, simulated orders, zero real money risk).
# Set TRADING_MODE=live in .env when ready for real broker orders.
TRADING_MODE=${TRADING_MODE:-"paper"}
if [ -f .env ]; then
    set -a
    source .env 2>/dev/null || true
    set +a
fi

if [ "$TRADING_MODE" = "live" ]; then
    log "═══════════════════════════════════════════════════════════"
    log "  STARTING LIVE TRADING ENGINE (REAL BROKER ORDERS)"
    log "═══════════════════════════════════════════════════════════"
    python3 -m Delta_Neutral_Nifty --live 2>&1 | tee -a "$LOG_FILE"
else
    log "═══════════════════════════════════════════════════════════"
    log "  STARTING PAPER TRADING ENGINE (LIVE QUOTES, ZERO REAL ORDERS)"
    log "═══════════════════════════════════════════════════════════"
    python3 -m Delta_Neutral_Nifty 2>&1 | tee -a "$LOG_FILE"
fi

# ─── 4. After engine stops (market close) → Push to GitHub ──
log "Trading engine stopped. Pushing state to GitHub..."

git config user.name "aws-trading-bot"
git config user.email "aws-bot@angelalgo"

git add positions.json trade_log.csv logs/ instrument_master.json
git diff --cached --quiet 2>/dev/null
if [ $? -ne 0 ]; then
    git commit -m "Market close: final state ${DATE} $(date '+%H:%M') IST [skip ci]"
    git push origin main 2>&1 | tee -a "$LOG_FILE"
    log "✓ State pushed to GitHub (main branch)"
else
    log "No changes to commit."
fi

# ─── 5. Auto Power-Off EC2 to Save AWS Credits ──────────────
log "═══════════════════════════════════════════════════════════"
log "  SESSION COMPLETE - SHUTTING DOWN EC2 TO SAVE CREDITS"
log "═══════════════════════════════════════════════════════════"

sudo shutdown -h now 2>/dev/null || sudo poweroff 2>/dev/null || log "Warning: shutdown command failed"
