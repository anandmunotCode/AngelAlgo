# AngelAlgo — AWS EC2 Deployment Guide

## Quick Start (3 Commands)

SSH into your EC2 instance and run:

```bash
# 1. SSH into EC2
ssh -i "your-key.pem" ubuntu@<YOUR_ELASTIC_IP>

# 2. Download & run setup from aws/ folder
curl -O https://raw.githubusercontent.com/anandmunotCode/AngelAlgo/main/aws/setup_ec2.sh
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
│                    │  + logs      │    (main branch)   │
│                    └──────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

---

## Daily Flow (Fully Automatic)

| Time | What Happens |
|------|-------------|
| 9:00 AM | Cron triggers `systemctl start angelalgo` |
| 9:00 AM | `aws/run_trading.sh` pulls latest code, installs deps |
| 9:15 AM | Market opens: Evaluates opening gap breach vs short legs |
| 9:15 AM | If normal: Unwinds overnight shield; If gap breach: Retains Iron Condor armor |
| 9:18 AM | Wed entry window → new cycle positions taken (if clean start) |
| 9:18 - Close | Real-time tick monitoring + 50% surge auto-adjustments |
| 3:29 PM / 3:44 PM | Overnight Shield: Dynamic 100-pt gap hedges bought (3:29 Expiry / 3:44 Non-Expiry) |
| 3:30 PM / 3:45 PM | Session close: Engine cleanly exits after market close |
| Post-Close | Positions + logs automatically committed & pushed to GitHub (main) |
| 3:50 PM | Cron safety stop `systemctl stop angelalgo` |

---

## Useful Commands (EC2 pe)

```bash
# Start algo manually
sudo systemctl start angelalgo

# Stop algo manually
sudo systemctl stop angelalgo

# Check status
sudo systemctl status angelalgo

# View live trading logs (real-time stream)
tail -f /home/ubuntu/AngelAlgo/logs/runner_$(date +%F).log

# View systemd logs
sudo journalctl -u angelalgo -f

# Check positions directly
cat /home/ubuntu/AngelAlgo/positions.json | python3 -m json.tool
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
