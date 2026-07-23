"""
Feature Engineering Script for Nifty Spot 1-Minute Historical Data
Generates 20 Technical, Price Action, Volatility & Time Features
Output: nifty_1min_5y_features.csv
"""
import os
import sys
import pandas as pd
import numpy as np

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def build_features(input_csv="nifty_1min_5y_master.csv", output_csv="nifty_1min_5y_features.csv"):
    if not os.path.exists(input_csv):
        print(f"Error: {input_csv} not found!")
        return

    print(f"Loading {input_csv}...")
    df = pd.read_csv(input_csv)
    print(f"Loaded {len(df):,} rows.")

    print("Calculating 20 Features...")

    # Ensure datetime format & sort
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)

    c = df['close']
    h = df['high']
    l = df['low']
    o = df['open']

    # --- Category 1: Price Action & Candle Geometry (6 Features) ---
    range_pts = (h - l).replace(0, 0.05) # Avoid division by zero
    body_pts = c - o
    
    df['f1_candle_range'] = (h - l).round(2)
    df['f2_candle_body'] = body_pts.round(2)
    df['f3_body_ratio'] = (body_pts.abs() / range_pts).round(4)
    df['f4_upper_wick_ratio'] = ((h - np.maximum(o, c)) / range_pts).round(4)
    df['f5_lower_wick_ratio'] = ((np.minimum(o, c) - l) / range_pts).round(4)
    df['f6_candle_direction'] = np.where(c > o, 1, np.where(c < o, -1, 0))

    # --- Category 2: Moving Average Distances % (5 Features) ---
    ema5 = c.ewm(span=5, adjust=False).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()

    df['f7_ema5_dist_pct'] = (((c - ema5) / c) * 100).round(4)
    df['f8_ema20_dist_pct'] = (((c - ema20) / c) * 100).round(4)
    df['f9_ema50_dist_pct'] = (((c - ema50) / c) * 100).round(4)
    df['f10_ema200_dist_pct'] = (((c - ema200) / c) * 100).round(4)
    df['f11_ema_5_20_cross'] = (((ema5 - ema20) / ema20) * 100).round(4)

    # --- Category 3: Momentum & Oscillators (4 Features) ---
    df['f12_rsi_14'] = calculate_rsi(c, 14).round(2)
    df['f13_roc_5min'] = (((c - c.shift(5)) / c.shift(5)) * 100).round(4)
    df['f14_roc_15min'] = (((c - c.shift(15)) / c.shift(15)) * 100).round(4)
    
    # MACD Histogram
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df['f15_macd_hist'] = (macd_line - signal_line).round(2)

    # --- Category 4: Volatility & Range Expansion (3 Features) ---
    # ATR 14
    tr = np.maximum(h - l, np.maximum((h - c.shift(1)).abs(), (l - c.shift(1)).abs()))
    df['f16_atr_14'] = tr.rolling(14).mean().round(2)

    # Bollinger Band Width
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    bb_upper = bb_mid + (2 * bb_std)
    bb_lower = bb_mid - (2 * bb_std)
    df['f17_bb_width_pct'] = (((bb_upper - bb_lower) / bb_mid) * 100).round(4)

    # Range Expansion (Current candle height vs 20-candle average height)
    avg_range_20 = range_pts.rolling(20).mean()
    df['f18_range_expansion'] = (range_pts / (avg_range_20 + 1e-9)).round(2)

    # --- Category 5: Time & Calendar Context (2 Features) ---
    time_dt = pd.to_datetime(df['datetime'])
    # Minute of trading day: 09:15 AM -> 0 min, 15:30 -> 375 min
    df['f19_minute_of_day'] = ((time_dt.dt.hour * 60 + time_dt.dt.minute) - (9 * 60 + 15)).astype(int)
    df['f20_day_of_week'] = time_dt.dt.dayofweek # Monday=0, Friday=4

    # --- TARGET LABEL FOR MACHINE LEARNING ---
    # Target: What happens 15 minutes later?
    # 1 if Close after 15 mins > Current Close by +15 points
    # -1 if Close after 15 mins < Current Close by -15 points
    # 0 if sideways
    future_close_15m = c.shift(-15)
    price_diff_15m = future_close_15m - c

    df['target_15m_dir'] = np.where(price_diff_15m >= 15.0, 1, np.where(price_diff_15m <= -15.0, -1, 0))

    # Drop NaNs created by rolling windows / lag features
    df_clean = df.dropna().reset_index(drop=True)

    print(f"Features created successfully! Total Rows: {len(df_clean):,}")
    print(f"Saving to {output_csv}...")
    df_clean.to_csv(output_csv, index=False)
    file_size_mb = os.path.getsize(output_csv) / (1024 * 1024)
    print(f"SUCCESS! Output File: {output_csv} ({file_size_mb:.2f} MB)")

if __name__ == "__main__":
    build_features()
