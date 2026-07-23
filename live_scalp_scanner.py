"""
Nifty Spot 1-Minute Live Continuous Scalping Scanner (Auto-Refresh Loop)
Uses Trained XGBoost Model: nifty_scalper_final_production.json
Continuously scans Nifty Spot live every 60 seconds during market hours.
"""
import os
import sys
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
import pyotp
from SmartApi import SmartConnect

# 1. Load .env Credentials
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

# 2. Compute 20 Features on Live Data
def compute_live_features(df):
    data = df.copy()
    c = data['close']
    h = data['high']
    l = data['low']
    o = data['open']

    # Group 1: Price Action
    data['f1_candle_range'] = h - l
    data['f2_candle_body'] = c - o
    range_safe = np.where(data['f1_candle_range'] == 0, 0.0001, data['f1_candle_range'])
    data['f3_body_ratio'] = (c - o).abs() / range_safe
    data['f4_upper_wick_ratio'] = (h - np.maximum(o, c)) / range_safe
    data['f5_lower_wick_ratio'] = (np.minimum(o, c) - l) / range_safe
    data['f6_candle_direction'] = np.where(c > o, 1, np.where(c < o, -1, 0))

    # Group 2: Moving Averages
    ema5 = c.ewm(span=5, adjust=False).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()

    data['f7_ema5_dist_pct'] = (c - ema5) / c * 100
    data['f8_ema20_dist_pct'] = (c - ema20) / c * 100
    data['f9_ema50_dist_pct'] = (c - ema50) / c * 100
    data['f10_ema200_dist_pct'] = (c - ema200) / c * 100
    data['f11_ema_5_20_cross'] = (ema5 - ema20) / c * 100

    # Group 3: Momentum Oscillators
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

    # Group 4: Volatility
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

    # Group 5: Time Context
    data['datetime'] = pd.to_datetime(data['datetime'])
    data['f19_minute_of_day'] = (data['datetime'].dt.hour - 9) * 60 + (data['datetime'].dt.minute - 15)
    data['f20_day_of_week'] = data['datetime'].dt.dayofweek

    return data

# 3. Main Scanner Runner
def run_live_continuous_scanner(min_probability=0.55):
    model_path = "nifty_scalper_final_production.json"
    if not os.path.exists(model_path):
        print(f"Error: Trained model '{model_path}' not found!")
        return

    print("=" * 65)
    print("LOADING PRODUCTION SCALPER MODEL...")
    print("=" * 65)
    model = XGBClassifier()
    model.load_model(model_path)
    print("Model Loaded Successfully!")

    config = load_env()
    smart_api = SmartConnect(api_key=config.get("ANGEL_API_KEY"))
    totp = pyotp.TOTP(config.get("ANGEL_TOTP_SECRET")).now()
    session = smart_api.generateSession(config.get("ANGEL_CLIENT_ID"), config.get("ANGEL_PASSWORD"), totp)

    if not session.get('status'):
        print(f"Login failed: {session.get('message')}")
        return

    print("Connected to Angel One API!")
    print(f"Starting Continuous 1-Minute Live Scalp Scanner...")
    print(f"Signal Confidence Threshold: {min_probability * 100:.0f}%\n")
    print("=" * 65)

    last_processed_candle = None

    while True:
        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=5)

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
                candle_time = last_row['datetime'].strftime("%Y-%m-%d %H:%M")

                if candle_time != last_processed_candle:
                    last_processed_candle = candle_time
                    candle_features = last_row[feature_cols].values.reshape(1, -1)
                    probs = model.predict_proba(candle_features)[0]

                    prob_bearish = probs[0]
                    prob_sideways = probs[1]
                    prob_bullish = probs[2]

                    spot_close = last_row['close']

                    if prob_bullish >= min_probability:
                        signal = f"[+] BULLISH SPOT SCALP (+10 Pts) | Call Prob: {prob_bullish*100:5.1f}%"
                    elif prob_bearish >= min_probability:
                        signal = f"[-] BEARISH SPOT SCALP (-10 Pts) | Put Prob:  {prob_bearish*100:5.1f}%"
                    else:
                        signal = f"[ ] SIDEWAYS / NO TRADE         | Max Prob:  {max(probs)*100:5.1f}%"

                    print(f"[{candle_time}] Spot: {spot_close:8.2f} -> {signal}")

        except Exception as e:
            print(f"Scanner warning: {e}")

        # Sleep for 15 seconds before checking next candle update
        time.sleep(15)

if __name__ == "__main__":
    run_live_continuous_scanner()
