"""
Nifty Option Scalping Paper Trading Engine with Strict Risk Management
Tracks ATM Call/Put Options with Real-Time Risk Rules:
- Target: +5 Option Points
- Stop Loss: -4 Option Points
- Time Exit: 7 Minutes Max Holding Time
Logs all trades to paper_trades_log.csv
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

class PaperTradingEngine:
    def __init__(self, target_pts=5.0, sl_pts=4.0, max_minutes=7, min_prob=0.60):
        self.target_pts = target_pts
        self.sl_pts = sl_pts
        self.max_minutes = max_minutes
        self.min_prob = min_prob
        self.active_trade = None
        self.trade_history = []
        self.log_file = "paper_trades_log.csv"
        
        if os.path.exists(self.log_file):
            self.trade_df = pd.read_csv(self.log_file)
        else:
            self.trade_df = pd.DataFrame(columns=[
                "entry_time", "symbol", "option_type", "spot_entry", "option_entry", 
                "exit_time", "spot_exit", "option_exit", "pnl_pts", "status", "reason"
            ])

    def get_atm_strike(self, spot_price):
        return int(round(spot_price / 50.0) * 50)

    def process_candle(self, timestamp, spot_open, spot_high, spot_low, spot_close, call_prob, put_prob):
        if self.active_trade is not None:
            trade = self.active_trade
            entry_spot = trade['spot_entry']
            entry_opt = trade['option_entry']
            opt_type = trade['option_type']
            entry_time = trade['entry_time']

            if opt_type == "CALL":
                opt_high = entry_opt + (spot_high - entry_spot) * 0.50
                opt_low = entry_opt - (entry_spot - spot_low) * 0.50
                opt_close = entry_opt + (spot_close - entry_spot) * 0.50
            else: # PUT
                opt_high = entry_opt + (entry_spot - spot_low) * 0.50
                opt_low = entry_opt - (spot_high - entry_spot) * 0.50
                opt_close = entry_opt - (spot_close - entry_spot) * 0.50

            mins_elapsed = (timestamp - entry_time).total_seconds() / 60.0

            exit_reason = None
            exit_opt_price = None

            if opt_high >= entry_opt + self.target_pts:
                exit_reason = "TARGET HIT (+5 Pts)"
                exit_opt_price = entry_opt + self.target_pts
            elif opt_low <= entry_opt - self.sl_pts:
                exit_reason = "STOPLOSS HIT (-4 Pts)"
                exit_opt_price = entry_opt - self.sl_pts
            elif mins_elapsed >= self.max_minutes:
                exit_reason = "TIME EXIT (7 Mins)"
                exit_opt_price = opt_close

            if exit_reason:
                pnl = exit_opt_price - entry_opt
                status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")

                trade_record = {
                    "entry_time": entry_time.strftime("%Y-%m-%d %H:%M"),
                    "symbol": f"NIFTY_{trade['strike']}_{opt_type}",
                    "option_type": opt_type,
                    "spot_entry": entry_spot,
                    "option_entry": entry_opt,
                    "exit_time": timestamp.strftime("%Y-%m-%d %H:%M"),
                    "spot_exit": spot_close,
                    "option_exit": round(exit_opt_price, 2),
                    "pnl_pts": round(pnl, 2),
                    "status": status,
                    "reason": exit_reason
                }

                print("\n" + "=" * 65)
                print(f"[EXIT] TRADE CLOSED [{status}] -> Reason: {exit_reason}")
                print(f"       Entry: {entry_opt:.2f} | Exit: {exit_opt_price:.2f} | PnL: {pnl:+.2f} Option Pts")
                print("=" * 65 + "\n")

                self.trade_df = pd.concat([self.trade_df, pd.DataFrame([trade_record])], ignore_index=True)
                self.trade_df.to_csv(self.log_file, index=False)
                self.active_trade = None

        if self.active_trade is None:
            atm_strike = self.get_atm_strike(spot_close)
            simulated_opt_price = 100.0

            if call_prob >= self.min_prob:
                print(f"\n[+] [PAPER TRADE ENTRY] BUY CALL | Strike: {atm_strike} CE | Spot: {spot_close:.2f} | Prob: {call_prob*100:.1f}%")
                self.active_trade = {
                    "entry_time": timestamp,
                    "strike": atm_strike,
                    "option_type": "CALL",
                    "spot_entry": spot_close,
                    "option_entry": simulated_opt_price
                }
            elif put_prob >= self.min_prob:
                print(f"\n[-] [PAPER TRADE ENTRY] BUY PUT  | Strike: {atm_strike} PE | Spot: {spot_close:.2f} | Prob: {put_prob*100:.1f}%")
                self.active_trade = {
                    "entry_time": timestamp,
                    "strike": atm_strike,
                    "option_type": "PUT",
                    "spot_entry": spot_close,
                    "option_entry": simulated_opt_price
                }

def run_live_paper_trading_loop(min_prob=0.60):
    model_path = "nifty_scalper_final_production.json"
    if not os.path.exists(model_path):
        print(f"Error: Model file {model_path} not found!")
        return

    model = XGBClassifier()
    model.load_model(model_path)
    engine = PaperTradingEngine(target_pts=5.0, sl_pts=4.0, max_minutes=7, min_prob=min_prob)

    config = load_env()
    smart_api = SmartConnect(api_key=config.get("ANGEL_API_KEY"))
    totp = pyotp.TOTP(config.get("ANGEL_TOTP_SECRET")).now()
    session = smart_api.generateSession(config.get("ANGEL_CLIENT_ID"), config.get("ANGEL_PASSWORD"), totp)

    if not session.get('status'):
        print(f"Login failed: {session.get('message')}")
        return

    print("=" * 65)
    print("NIFTY LIVE MARKET PAPER TRADING ENGINE RUNNING...")
    print("=" * 65)
    print(f"Signal Confidence Threshold: >= {min_prob*100:.0f}%\n")

    last_processed_candle = None

    while True:
        try:
            to_date = datetime.now()
            # Fetch today's candles starting from 09:15 AM
            from_date = to_date.replace(hour=9, minute=15, second=0, microsecond=0)

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
                candle_time = pd.to_datetime(last_row['datetime'])

                if str(candle_time) != str(last_processed_candle):
                    last_processed_candle = candle_time
                    candle_features = last_row[feature_cols].values.reshape(1, -1)
                    probs = model.predict_proba(candle_features)[0]

                    engine.process_candle(
                        timestamp=candle_time,
                        spot_open=last_row['open'],
                        spot_high=last_row['high'],
                        spot_low=last_row['low'],
                        spot_close=last_row['close'],
                        call_prob=probs[2],
                        put_prob=probs[0]
                    )

        except Exception as e:
            print(f"Paper trading loop warning: {e}")

        time.sleep(15)

if __name__ == "__main__":
    run_live_paper_trading_loop(min_prob=0.60)
