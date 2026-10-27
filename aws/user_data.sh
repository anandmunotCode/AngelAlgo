#!/bin/bash
exec > >(tee -a /var/log/angelalgo_setup.log) 2>&1
echo "=== AngelAlgo Initial Setup Starting: $(date) ==="

# 1. Set Timezone to Asia/Kolkata
timedatectl set-timezone Asia/Kolkata

# 2. Update and install packages
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git curl

# 3. Clone repository
cd /home/ubuntu
if [ ! -d "AngelAlgo" ]; then
    git clone -b main https://github.com/anandmunotCode/AngelAlgo.git
fi
chown -R ubuntu:ubuntu /home/ubuntu/AngelAlgo

# 4. Install dependencies
cd /home/ubuntu/AngelAlgo
pip3 install -r Delta_Neutral_Nifty/requirements.txt --break-system-packages

# 5. Create .env file
cat > /home/ubuntu/AngelAlgo/.env << 'EOF'
# Angel One SmartAPI Credentials
ANGEL_API_KEY=d9uCodqx
ANGEL_CLIENT_ID=AAAI339820
ANGEL_PASSWORD=5163
ANGEL_TOTP_SECRET=ECDCQA36UAHKEWO7TAAZVY4V6A
TRADING_MODE=paper
EOF
chown ubuntu:ubuntu /home/ubuntu/AngelAlgo/.env
chmod 600 /home/ubuntu/AngelAlgo/.env

# 6. Ensure executable permissions
chmod +x /home/ubuntu/AngelAlgo/aws/run_trading.sh
chmod +x /home/ubuntu/AngelAlgo/aws/setup_ec2.sh
mkdir -p /home/ubuntu/AngelAlgo/logs
chown -R ubuntu:ubuntu /home/ubuntu/AngelAlgo/logs

# 7. Configure systemd service
cp /home/ubuntu/AngelAlgo/aws/angelalgo.service /etc/systemd/system/angelalgo.service
systemctl daemon-reload
systemctl enable angelalgo.service

echo "=== AngelAlgo Setup Complete! ==="
echo "Stopping instance now so it remains safely paused until 09:00 AM IST tomorrow."
sleep 5
shutdown -h now
