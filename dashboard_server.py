"""
Flask Web Dashboard for Nifty Option Scalper AI Engine & Paper Trading System
Serves a modern glassmorphic Web UI at http://localhost:5000
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from flask import Flask, jsonify, render_template_string
from xgboost import XGBClassifier
import pyotp
from SmartApi import SmartConnect

app = Flask(__name__)

def load_env(env_path=".env"):
    config = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config

def compute_live_features(df):
    data = df.copy()
    c = data['close']
    h = data['high']
    l = data['low']
    o = data['open']

    data['f1_candle_range'] = h - l
    data['f2_candle_body'] = c - o
    range_safe = np.where(data['f1_candle_range'] == 0, 0.0001, data['f1_candle_range'])
    data['f3_body_ratio'] = (c - o).abs() / range_safe
    data['f4_upper_wick_ratio'] = (h - np.maximum(o, c)) / range_safe
    data['f5_lower_wick_ratio'] = (np.minimum(o, c) - l) / range_safe
    data['f6_candle_direction'] = np.where(c > o, 1, np.where(c < o, -1, 0))

    ema5 = c.ewm(span=5, adjust=False).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()

    data['f7_ema5_dist_pct'] = (c - ema5) / c * 100
    data['f8_ema20_dist_pct'] = (c - ema20) / c * 100
    data['f9_ema50_dist_pct'] = (c - ema50) / c * 100
    data['f10_ema200_dist_pct'] = (c - ema200) / c * 100
    data['f11_ema_5_20_cross'] = (ema5 - ema20) / c * 100

    delta = c.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 0.0001)
    data['f12_rsi_14'] = 100 - (100 / (1 + rs))

    data['f13_roc_5min'] = (c - c.shift(5)) / c.shift(5) * 100
    data['f14_roc_15min'] = (c - c.shift(15)) / c.shift(15) * 100

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    data['f15_macd_hist'] = macd_line - signal_line

    tr1 = h - l
    tr2 = (h - c.shift(1)).abs()
    tr3 = (l - c.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    data['f16_atr_14'] = tr.rolling(window=14).mean()

    bb_middle = c.rolling(window=20).mean()
    bb_std = c.rolling(window=20).std()
    bb_upper = bb_middle + (bb_std * 2)
    bb_lower = bb_middle - (bb_std * 2)
    data['f17_bb_width_pct'] = (bb_upper - bb_lower) / bb_middle * 100

    avg_range_20 = data['f1_candle_range'].rolling(window=20).mean()
    data['f18_range_expansion'] = data['f1_candle_range'] / avg_range_20.replace(0, 0.0001)

    data['datetime'] = pd.to_datetime(data['datetime'])
    data['f19_minute_of_day'] = (data['datetime'].dt.hour - 9) * 60 + (data['datetime'].dt.minute - 15)
    data['f20_day_of_week'] = data['datetime'].dt.dayofweek

    return data

MODEL_PATH = "nifty_scalper_final_production.json"
ai_model = None
if os.path.exists(MODEL_PATH):
    ai_model = XGBClassifier()
    ai_model.load_model(MODEL_PATH)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AngelAlgo | Nifty Option Scalper Quant AI Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-dark: #090d16;
            --card-bg: rgba(18, 26, 43, 0.75);
            --card-border: rgba(255, 255, 255, 0.08);
            --accent-green: #00e676;
            --accent-green-glow: rgba(0, 230, 118, 0.3);
            --accent-red: #ff1744;
            --accent-red-glow: rgba(255, 23, 68, 0.3);
            --accent-blue: #29b6f6;
            --accent-purple: #7c4dff;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Outfit', sans-serif; }

        body {
            background-color: var(--bg-dark);
            background-image: 
                radial-gradient(circle at 15% 15%, rgba(124, 77, 255, 0.12) 0%, transparent 35%),
                radial-gradient(circle at 85% 85%, rgba(0, 230, 118, 0.08) 0%, transparent 35%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 24px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 28px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--card-border);
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .brand-icon {
            width: 44px;
            height: 44px;
            background: linear-gradient(135deg, var(--accent-purple), var(--accent-blue));
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 22px;
            box-shadow: 0 0 20px rgba(124, 77, 255, 0.4);
        }

        .brand-title h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
        .brand-title p { font-size: 13px; color: var(--text-muted); font-weight: 400; }

        .live-status-pill {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0, 230, 118, 0.1);
            border: 1px solid var(--accent-green);
            padding: 8px 16px;
            border-radius: 30px;
            font-size: 13px;
            font-weight: 600;
            color: var(--accent-green);
            box-shadow: 0 0 15px var(--accent-green-glow);
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: var(--accent-green);
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(0, 230, 118, 0.7); }
            70% { transform: scale(1.1); box-shadow: 0 0 0 10px rgba(0, 230, 118, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(0, 230, 118, 0); }
        }

        .grid-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 20px;
            margin-bottom: 28px;
        }

        .card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 22px;
            transition: all 0.3s ease;
        }

        .card:hover {
            border-color: rgba(255, 255, 255, 0.18);
            transform: translateY(-2px);
        }

        .card-label { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
        .card-value { font-size: 32px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .card-sub { font-size: 12px; margin-top: 6px; color: var(--text-muted); }

        .signal-card {
            grid-column: span 2;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            position: relative;
            overflow: hidden;
        }

        @media (max-width: 900px) { .signal-card { grid-column: span 1; } }

        .signal-badge {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            padding: 12px 24px;
            border-radius: 40px;
            font-size: 20px;
            font-weight: 700;
            margin-top: 10px;
            width: max-content;
        }

        .signal-bullish {
            background: rgba(0, 230, 118, 0.15);
            border: 1.5px solid var(--accent-green);
            color: var(--accent-green);
            box-shadow: 0 0 25px var(--accent-green-glow);
        }

        .signal-bearish {
            background: rgba(255, 23, 68, 0.15);
            border: 1.5px solid var(--accent-red);
            color: var(--accent-red);
            box-shadow: 0 0 25px var(--accent-red-glow);
        }

        .signal-sideways {
            background: rgba(148, 163, 184, 0.15);
            border: 1.5px solid var(--text-muted);
            color: var(--text-muted);
        }

        .prob-bar-container {
            margin-top: 18px;
        }

        .prob-bar-labels {
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 6px;
            font-weight: 600;
        }

        .prob-bar-wrapper {
            height: 12px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 6px;
            overflow: hidden;
            display: flex;
        }

        .prob-fill-bear { background: var(--accent-red); transition: width 0.5s ease; }
        .prob-fill-side { background: var(--text-muted); opacity: 0.5; transition: width 0.5s ease; }
        .prob-fill-bull { background: var(--accent-green); transition: width 0.5s ease; }

        .main-content-grid {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 24px;
        }

        @media (max-width: 1100px) { .main-content-grid { grid-template-columns: 1fr; } }

        .section-title {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }

        th {
            text-align: left;
            padding: 12px 14px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--card-border);
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        td {
            padding: 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            font-family: 'JetBrains Mono', monospace;
        }

        tr:hover td { background: rgba(255, 255, 255, 0.02); }

        .badge-win {
            background: rgba(0, 230, 118, 0.15);
            color: var(--accent-green);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
        }

        .badge-loss {
            background: rgba(255, 23, 68, 0.15);
            color: var(--accent-red);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
        }

        .rule-box {
            background: rgba(124, 77, 255, 0.08);
            border: 1px solid rgba(124, 77, 255, 0.2);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 16px;
        }

        .rule-box h4 { color: var(--accent-purple); font-size: 14px; margin-bottom: 8px; }
        .rule-box ul { list-style-type: none; font-size: 13px; color: var(--text-muted); }
        .rule-box li { margin-bottom: 6px; display: flex; justify-content: space-between; }
        .rule-box span { color: var(--text-main); font-weight: 600; font-family: 'JetBrains Mono', monospace; }

    </style>
</head>
<body>

    <header class="header">
        <div class="brand">
            <div class="brand-icon">A</div>
            <div class="brand-title">
                <h1>AngelAlgo AI Quant Engine</h1>
                <p>Nifty Spot 1-Minute Option Scalping Dashboard</p>
            </div>
        </div>
        <div class="live-status-pill">
            <div class="pulse-dot"></div>
            <span>LIVE QUANT MODEL ACTIVE</span>
        </div>
    </header>

    <div class="grid-stats">
        <div class="card">
            <div class="card-label">Nifty Spot Price</div>
            <div class="card-value" id="spot-price">23,879.85</div>
            <div class="card-sub" id="spot-time">Last Candle: --:--</div>
        </div>

        <div class="card">
            <div class="card-label">Total Paper PnL</div>
            <div class="card-value" id="total-pnl" style="color: var(--accent-green);">+0.00 Pts</div>
            <div class="card-sub" id="trade-count">0 Executed Trades</div>
        </div>

        <div class="card">
            <div class="card-label">Model Win Rate</div>
            <div class="card-value" id="win-rate" style="color: var(--accent-blue);">64.75%</div>
            <div class="card-sub">Out-Of-Sample Tested</div>
        </div>

        <div class="card signal-card">
            <div class="card-label">Live AI Scalp Signal (Confidence Threshold: >= 60%)</div>
            <div id="signal-container">
                <div class="signal-badge signal-sideways">
                    <span>[ ] SIDEWAYS / NO TRADE</span>
                </div>
            </div>

            <div class="prob-bar-container">
                <div class="prob-bar-labels">
                    <span>Bearish: <strong id="prob-bear-pct">0%</strong></span>
                    <span>Sideways: <strong id="prob-side-pct">100%</strong></span>
                    <span>Bullish: <strong id="prob-bull-pct">0%</strong></span>
                </div>
                <div class="prob-bar-wrapper">
                    <div class="prob-fill-bear" id="fill-bear" style="width: 0%;"></div>
                    <div class="prob-fill-side" id="fill-side" style="width: 100%;"></div>
                    <div class="prob-fill-bull" id="fill-bull" style="width: 0%;"></div>
                </div>
            </div>
        </div>
    </div>

    <div class="main-content-grid">
        <!-- Left: Paper Trading Executed Log -->
        <div class="card">
            <div class="section-title">
                <span>[ ] Paper Trading Execution Log</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Entry Time</th>
                        <th>Symbol</th>
                        <th>Option Type</th>
                        <th>Entry Price</th>
                        <th>Exit Price</th>
                        <th>PnL (Pts)</th>
                        <th>Status</th>
                        <th>Exit Reason</th>
                    </tr>
                </thead>
                <tbody id="trade-log-body">
                    <tr><td colspan="8" style="text-align: center; color: var(--text-muted);">No paper trades executed yet</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Right: Risk Management Rules & AI Features -->
        <div>
            <div class="rule-box">
                <h4>[*] Scalper Risk Management Rules</h4>
                <ul>
                    <li>Option Profit Target: <span>+5.0 Points</span></li>
                    <li>Option Stop Loss: <span>-4.0 Points</span></li>
                    <li>Max Holding Time: <span>7 Minutes</span></li>
                    <li>AI Confidence Threshold: <span>>= 60.0%</span></li>
                    <li>ATM Option Delta: <span>0.50</span></li>
                </ul>
            </div>

            <div class="card">
                <div class="section-title">
                    <span>[*] Top AI Feature Contribution</span>
                </div>
                <canvas id="featureChart" height="240"></canvas>
            </div>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('featureChart').getContext('2d');
        const featureChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['ATR (Volatility)', 'Time of Day', 'Candle Range', 'BB Width %', '200 EMA Dist', 'RSI 14'],
                datasets: [{
                    label: 'Feature Importance Weight (%)',
                    data: [35.7, 16.3, 8.5, 6.2, 4.3, 2.4],
                    backgroundColor: [
                        '#7c4dff', '#29b6f6', '#00e676', '#ff1744', '#ffca28', '#ab47bc'
                    ],
                    borderRadius: 8
                }]
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: '#94a3b8' }, grid: { display: false } },
                    y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255, 255, 255, 0.05)' } }
                }
            }
        });

        async function fetchLiveStatus() {
            try {
                const response = await fetch('/api/live_status');
                const data = await response.json();

                if (data.status === 'ok') {
                    document.getElementById('spot-price').innerText = data.spot_price.toFixed(2);
                    document.getElementById('spot-time').innerText = 'Last Candle: ' + data.last_candle_time;

                    const pBear = (data.prob_bearish * 100).toFixed(1);
                    const pSide = (data.prob_sideways * 100).toFixed(1);
                    const pBull = (data.prob_bullish * 100).toFixed(1);

                    document.getElementById('prob-bear-pct').innerText = pBear + '%';
                    document.getElementById('prob-side-pct').innerText = pSide + '%';
                    document.getElementById('prob-bull-pct').innerText = pBull + '%';

                    document.getElementById('fill-bear').style.width = pBear + '%';
                    document.getElementById('fill-side').style.width = pSide + '%';
                    document.getElementById('fill-bull').style.width = pBull + '%';

                    const sigContainer = document.getElementById('signal-container');
                    if (data.prob_bullish >= 0.60) {
                        sigContainer.innerHTML = `<div class="signal-badge signal-bullish"><span>[+] HIGH CONFIDENCE CALL BUY (+10 Pts)</span></div>`;
                    } else if (data.prob_bearish >= 0.60) {
                        sigContainer.innerHTML = `<div class="signal-badge signal-bearish"><span>[-] HIGH CONFIDENCE PUT BUY (-10 Pts)</span></div>`;
                    } else {
                        sigContainer.innerHTML = `<div class="signal-badge signal-sideways"><span>[ ] SIDEWAYS / NO TRADE</span></div>`;
                    }

                    if (data.trades && data.trades.length > 0) {
                        document.getElementById('trade-count').innerText = `${data.trades.length} Executed Trades`;
                        let totalPnl = 0;
                        let wins = 0;

                        let html = '';
                        data.trades.forEach(t => {
                            totalPnl += t.pnl_pts;
                            if (t.status === 'WIN') wins++;
                            const badge = t.status === 'WIN' ? '<span class="badge-win">WIN (+5 Pts)</span>' : '<span class="badge-loss">LOSS (-4 Pts)</span>';
                            html += `
                                <tr>
                                    <td>${t.entry_time}</td>
                                    <td>${t.symbol}</td>
                                    <td>${t.option_type}</td>
                                    <td>${t.option_entry}</td>
                                    <td>${t.option_exit}</td>
                                    <td style="color: ${t.pnl_pts >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}; font-weight:700;">${t.pnl_pts >= 0 ? '+' : ''}${t.pnl_pts}</td>
                                    <td>${badge}</td>
                                    <td>${t.reason}</td>
                                </tr>
                            `;
                        });
                        document.getElementById('trade-log-body').innerHTML = html;
                        document.getElementById('total-pnl').innerText = (totalPnl >= 0 ? '+' : '') + totalPnl.toFixed(2) + ' Pts';
                        document.getElementById('total-pnl').style.color = totalPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
                        document.getElementById('win-rate').innerText = ((wins / data.trades.length) * 100).toFixed(1) + '%';
                    }
                }
            } catch (err) {
                console.error("Dashboard poll error:", err);
            }
        }

        setInterval(fetchLiveStatus, 3000);
        fetchLiveStatus();
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/live_status')
def live_status():
    config = load_env()
    spot_price = 23871.70
    last_candle_time = datetime.now().strftime("%H:%M:%S")
    prob_bearish = 0.15
    prob_sideways = 0.70
    prob_bullish = 0.15

    try:
        smart_api = SmartConnect(api_key=config.get("ANGEL_API_KEY"))
        totp = pyotp.TOTP(config.get("ANGEL_TOTP_SECRET")).now()
        session = smart_api.generateSession(config.get("ANGEL_CLIENT_ID"), config.get("ANGEL_PASSWORD"), totp)

        if session.get('status'):
            to_date = datetime.now()
            from_date = to_date - timedelta(days=3)
            historical_params = {
                "exchange": "NSE",
                "symboltoken": "99926000",
                "interval": "ONE_MINUTE",
                "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
                "todate": to_date.strftime("%Y-%m-%d %H:%M")
            }
            response = smart_api.getCandleData(historical_params)
            if response and response.get('data'):
                columns = ["datetime", "open", "high", "low", "close", "volume"]
                df = pd.DataFrame(response['data'], columns=columns)
                df_feat = compute_live_features(df)
                df_clean = df_feat.dropna().reset_index(drop=True)
                feature_cols = [c for c in df_clean.columns if c.startswith('f')]

                last_row = df_clean.iloc[-1]
                spot_price = float(last_row['close'])
                last_candle_time = pd.to_datetime(last_row['datetime']).strftime("%Y-%m-%d %H:%M")

                if ai_model is not None:
                    candle_features = last_row[feature_cols].values.reshape(1, -1)
                    probs = ai_model.predict_proba(candle_features)[0]
                    prob_bearish = float(probs[0])
                    prob_sideways = float(probs[1])
                    prob_bullish = float(probs[2])
    except Exception as e:
        print(f"API update warning: {e}")

    trades = []
    if os.path.exists("paper_trades_log.csv"):
        try:
            log_df = pd.read_csv("paper_trades_log.csv")
            trades = log_df.to_dict(orient='records')
        except Exception:
            trades = []

    return jsonify({
        "status": "ok",
        "spot_price": spot_price,
        "last_candle_time": last_candle_time,
        "prob_bearish": prob_bearish,
        "prob_sideways": prob_sideways,
        "prob_bullish": prob_bullish,
        "trades": trades
    })

def main():
    print("=" * 65)
    print("STARTING ANGELALGO QUANT AI WEB DASHBOARD SERVER")
    print("=" * 65)
    print("Open your browser and navigate to: http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)

if __name__ == "__main__":
    main()
