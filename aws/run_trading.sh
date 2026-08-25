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

# ─── 1. Pull latest code from aws-deploy branch ─────────────
log "Pulling latest code from GitHub..."
git pull origin aws-deploy --quiet 2>/dev/null || log "WARNING: git pull failed, using local code"

# ─── 2. Install/update dependencies (silent) ────────────────
log "Checking dependencies..."
pip3 install -r Delta_Neutral_Nifty/requirements.txt --quiet --break-system-packages 2>/dev/null

# ─── 3. Start trading engine (LIVE mode) ────────────────────
log "═══════════════════════════════════════════════════════════"
log "  STARTING LIVE TRADING ENGINE"
log "═══════════════════════════════════════════════════════════"

python3 -m Delta_Neutral_Nifty --live 2>&1 | tee -a "$LOG_FILE"

# ─── 4. After engine stops (market close) → Push to GitHub ──
log "Trading engine stopped. Pushing state to GitHub..."

git config user.name "aws-trading-bot"
git config user.email "aws-bot@angelalgo"

git add positions.json trade_log.csv logs/ instrument_master.json
git diff --cached --quiet 2>/dev/null
if [ $? -ne 0 ]; then
    git commit -m "Market close: final state ${DATE} $(date '+%H:%M') IST [skip ci]"
    git push origin aws-deploy 2>&1 | tee -a "$LOG_FILE"
    log "✓ State pushed to GitHub (aws-deploy branch)"
else
    log "No changes to commit."
fi

log "═══════════════════════════════════════════════════════════"
log "  SESSION COMPLETE"
log "═══════════════════════════════════════════════════════════"
