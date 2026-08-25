#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# AngelAlgo - AWS EC2 One-Time Setup Script (Ubuntu)
# Run this ONCE after SSH into your EC2 instance.
# ═══════════════════════════════════════════════════════════════

set -e

echo "═══════════════════════════════════════════════════════════"
echo "  AngelAlgo - EC2 Setup Starting..."
echo "═══════════════════════════════════════════════════════════"

# ─── 1. Set Timezone to IST ──────────────────────────────────
echo "[1/7] Setting timezone to IST..."
sudo timedatectl set-timezone Asia/Kolkata
echo "  ✓ Timezone: $(timedatectl | grep 'Time zone')"

# ─── 2. Install Python, pip, git ─────────────────────────────
echo "[2/7] Installing Python3, pip, git..."
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv git
echo "  ✓ Python: $(python3 --version)"
echo "  ✓ Git: $(git --version)"

# ─── 3. Clone the repo ──────────────────────────────────────
echo "[3/7] Cloning AngelAlgo from GitHub..."
cd /home/ubuntu
if [ -d "AngelAlgo" ]; then
    echo "  → AngelAlgo folder already exists. Pulling latest..."
    cd AngelAlgo
    git pull origin aws-deploy
else
    git clone -b aws-deploy https://github.com/anandmunotCode/AngelAlgo.git
    cd AngelAlgo
fi
echo "  ✓ Repo ready at /home/ubuntu/AngelAlgo"

# ─── 4. Install Python dependencies ─────────────────────────
echo "[4/7] Installing Python dependencies..."
pip3 install -r Delta_Neutral_Nifty/requirements.txt --break-system-packages
echo "  ✓ Dependencies installed"

# ─── 5. Create .env file ────────────────────────────────────
echo "[5/7] Creating .env file..."
if [ ! -f .env ]; then
    cat > .env << 'EOF'
# Angel One SmartAPI Credentials
ANGEL_API_KEY=d9uCodqx
ANGEL_CLIENT_ID=AAAI339820
ANGEL_PASSWORD=5163
ANGEL_TOTP_SECRET=ECDCQA36UAHKEWO7TAAZVY4V6A
EOF
    echo "  ✓ .env created"
else
    echo "  → .env already exists, skipping"
fi

# ─── 6. Make scripts executable ──────────────────────────────
echo "[6/7] Setting file permissions..."
chmod +x aws/run_trading.sh
chmod +x aws/setup_ec2.sh
echo "  ✓ Scripts in aws/ are executable"

# ─── 7. Install systemd service (auto-restart on crash) ─────
echo "[7/7] Installing systemd service for auto-restart..."
sudo cp aws/angelalgo.service /etc/systemd/system/angelalgo.service
sudo systemctl daemon-reload
sudo systemctl enable angelalgo.service
echo "  ✓ systemd service installed and enabled"

# ─── Setup Cron Job ──────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Setting up cron job (Mon-Fri 9:00 AM IST)..."
echo "═══════════════════════════════════════════════════════════"

# Remove any existing angelalgo cron entries, then add new one
(crontab -l 2>/dev/null | grep -v "angelalgo"; echo "0 9 * * 1-5 sudo systemctl start angelalgo >> /home/ubuntu/AngelAlgo/logs/cron.log 2>&1") | crontab -
echo "  ✓ Cron job set: 9:00 AM IST, Mon-Fri"

# Add market close stop cron (3:42 PM IST - 2 mins after engine auto-stops at 15:40)
(crontab -l 2>/dev/null | grep -v "angelalgo-stop"; echo "42 15 * * 1-5 sudo systemctl stop angelalgo >> /home/ubuntu/AngelAlgo/logs/cron.log 2>&1 # angelalgo-stop") | crontab -
echo "  ✓ Cron stop set: 3:42 PM IST, Mon-Fri (safety cleanup)"

# ─── Create logs directory ───────────────────────────────────
mkdir -p /home/ubuntu/AngelAlgo/logs

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✅ SETUP COMPLETE!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. Set GitHub PAT for pushing trade logs:"
echo "     git remote set-url origin https://<YOUR_PAT>@github.com/anandmunotCode/AngelAlgo.git"
echo ""
echo "  2. Test manually:"
echo "     sudo systemctl start angelalgo"
echo "     sudo journalctl -u angelalgo -f    (see live logs)"
echo ""
echo "  3. Check cron jobs:"
echo "     crontab -l"
echo ""
echo "  Trading will auto-start Mon-Fri at 9:00 AM IST!"
echo ""
