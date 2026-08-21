# AngelAlgo — AWS EC2 Deployment Guide

## Quick Start (3 Commands)

SSH into your EC2 instance and run:

```bash
# 1. SSH into EC2
ssh -i "your-key.pem" ubuntu@<YOUR_ELASTIC_IP>

# 2. Download & run setup from aws/ folder
curl -O https://raw.githubusercontent.com/anandmunotCode/AngelAlgo/aws-deploy/aws/setup_ec2.sh
chmod +x setup_ec2.sh
./setup_ec2.sh

# 3. Set GitHub PAT for pushing trade logs
cd /home/ubuntu/AngelAlgo
git remote set-url origin https://<YOUR_GITHUB_PAT>@github.com/anandmunotCode/AngelAlgo.git
```

**That's it!** Trading will auto-start Mon-Fri at 9:00 AM IST.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    AWS EC2 (Ubuntu)                      │
│                                                         │
│  ┌──────────┐    ┌──────────────────┐ ┌───────────────┐│
│  │  Cron    │───>│aws/run_trading.sh│>│  Trading      ││
│  │ 9:00 AM  │    └──────────────────┘ │  Engine       ││
│  └──────────┘             │           │  (Python)     ││
│                           │           └───────┬───────┘│
│  ┌──────────┐             │                   │        │
│  │ systemd  │ (auto       │           ┌───────▼───────┐│
│  │ restart  │  restart    │           │  Angel One    ││
│  │ on crash)│  if crash)  │           │  SmartAPI     ││
│  └──────────┘             │           └───────────────┘│
│                           ▼                            │
│                    ┌──────────────┐                    │
│                    │  Git Push    │                    │
│                    │  positions   │──> GitHub          │
│                    │  + logs      │    (aws-deploy)    │
│                    └──────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

---

## Daily Flow (Fully Automatic)

| Time | What Happens |
|------|-------------|
| 9:00 AM | Cron triggers `systemctl start angelalgo` |
| 9:00 AM | `aws/run_trading.sh` pulls latest code, installs deps |
| 9:15 AM | Market opens, engine starts monitoring |
| 9:18 AM | Entry window → positions taken |
| 9:18 - 3:40 PM | Real-time monitoring + auto-adjustments |
| 3:40 PM | Engine auto-stops (market close logic in code) |
| 3:40 PM | Positions + logs pushed to GitHub (aws-deploy) |
| 3:42 PM | Cron safety stop `systemctl stop angelalgo` |

---

## Useful Commands (EC2 pe)

```bash
# Start algo manually
sudo systemctl start angelalgo

# Stop algo
sudo systemctl stop angelalgo

# See live logs
sudo journalctl -u angelalgo -f

# Check algo status
sudo systemctl status angelalgo

# Check cron jobs
crontab -l

# See today's trading log
tail -f /home/ubuntu/AngelAlgo/logs/runner_$(date +%Y-%m-%d).log
```

---

## Security Checklist

- [ ] EC2 Security Group: Allow **outbound HTTPS (443)** — needed for Angel One API
- [ ] EC2 Security Group: Allow **inbound SSH (22)** — only from your IP
- [ ] `.env` file has **600 permissions** (`chmod 600 .env`)
- [ ] GitHub PAT has **minimum permissions** (only `repo` scope)

---

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Local development, paper testing |
| `aws-deploy` | AWS EC2 production deployment |
