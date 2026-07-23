"""
Quant Option Scalping Model (XGBoost)
Target: 10 Nifty Index Points (+5 Option Points on ATM Delta 0.50)
Holding Period: Next 7 Minutes (7 Candles)
"""
import os
import sys
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.metrics import classification_report, accuracy_score

def train_scalper_model(features_csv="nifty_1min_5y_features.csv"):
    if not os.path.exists(features_csv):
        print(f"Error: {features_csv} not found!")
        return

    print("=" * 65)
    print("OPTION SCALPER QUANT MODEL: 10 INDEX PTS (+5 OPTION PTS)")
    print("=" * 65)

    df = pd.read_csv(features_csv)
    df['datetime'] = pd.to_datetime(df['datetime'])

    c = df['close']
    h = df['high']
    l = df['low']

    # Look ahead 7 minutes (7 candles)
    # Check if max price reached in next 7 mins is >= Close + 10 points (Bullish Call Scalp)
    # Check if min price reached in next 7 mins is <= Close - 10 points (Bearish Put Scalp)
    
    indexer = pd.api.indexers.FixedForwardWindowIndexer(window_size=7)
    future_max = h.rolling(window=indexer).max()
    future_min = l.rolling(window=indexer).min()

    bullish_hit = (future_max - c) >= 10.0
    bearish_hit = (c - future_min) >= 10.0

    # Target: 1 for Bullish Scalp, -1 for Bearish Scalp, 0 for Neither
    df['target_scalp'] = np.where(bullish_hit & ~bearish_hit, 1,
                         np.where(bearish_hit & ~bullish_hit, -1, 0))

    target_mapping = {-1: 0, 0: 1, 1: 2}
    df['target_mapped'] = df['target_scalp'].map(target_mapping)

    # Active Trading Hours Filter (Exclude 09:15-09:20 opening volatility and 03:20-03:30 closing)
    active_mask = (df['f19_minute_of_day'] >= 10) & (df['f19_minute_of_day'] <= 360)
    df_clean = df[active_mask].dropna().reset_index(drop=True)

    feature_cols = [col for col in df_clean.columns if col.startswith('f')]

    print(f"Total candles analyzed: {len(df_clean):,}")
    
    # Chronological Split (2021-2024 Train, 2025-2026 Unseen Test)
    split_date = '2025-01-01'
    train_mask = df_clean['datetime'] < split_date
    test_mask = df_clean['datetime'] >= split_date

    X_train = df_clean.loc[train_mask, feature_cols]
    y_train = df_clean.loc[train_mask, 'target_mapped']

    X_test = df_clean.loc[test_mask, feature_cols]
    y_test = df_clean.loc[test_mask, 'target_mapped']

    print(f"Train Set (2021-2024): {len(X_train):,} candles")
    print(f"Test Set  (2025-2026): {len(X_test):,} candles")

    print("\nScalping Class Distribution in Training Data:")
    print(y_train.value_counts(normalize=True).rename(index={0: 'Put Scalp (-10p)', 1: 'No Scalp (0)', 2: 'Call Scalp (+10p)'}) * 100)

    # Train XGBoost Scalper Model
    print("\n" + "=" * 65)
    print("TRAINING SCALPER XGBOOST MODEL...")
    print("=" * 65)

    model = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        eval_metric='mlogloss'
    )

    model.fit(X_train, y_train)
    print("Scalper Model Training Complete!")

    # Evaluate on Unseen 2025-2026 Test Data
    y_prob = model.predict_proba(X_test)
    test_df = df_clean.loc[test_mask].copy().reset_index(drop=True)
    test_df['prob_bearish'] = y_prob[:, 0]
    test_df['prob_sideways'] = y_prob[:, 1]
    test_df['prob_bullish'] = y_prob[:, 2]

    print("\n" + "=" * 65)
    print("SCALPER PROBABILITY WIN-RATES (2025 - 2026 UNSEEN DATA)")
    print("=" * 65)

    for threshold in [0.40, 0.45, 0.50, 0.55, 0.60]:
        print(f"\n--- PROBABILITY THRESHOLD >= {threshold*100:.0f}% ---")
        
        # CALL BUY SIGNALS (+10 pts move)
        call_trades = test_df[test_df['prob_bullish'] >= threshold]
        if len(call_trades) > 0:
            call_wins = (call_trades['target_mapped'] == 2).sum()
            call_win_rate = (call_wins / len(call_trades)) * 100
            print(f"[+] CALL BUY  (ATM +5 pts): {len(call_trades):5,} Trades | Wins: {call_wins:5,} | Win Rate: {call_win_rate:6.2f}%")
        else:
            print(f"[+] CALL BUY: 0 Trades")

        # PUT BUY SIGNALS (-10 pts move)
        put_trades = test_df[test_df['prob_bearish'] >= threshold]
        if len(put_trades) > 0:
            put_wins = (put_trades['target_mapped'] == 0).sum()
            put_win_rate = (put_wins / len(put_trades)) * 100
            print(f"[-] PUT BUY   (ATM +5 pts): {len(put_trades):5,} Trades | Wins: {put_wins:5,} | Win Rate: {put_win_rate:6.2f}%")
        else:
            print(f"[-] PUT BUY: 0 Trades")

    model.save_model("nifty_scalper_xgb.json")
    print(f"\n[SAVE] Scalper Model saved to nifty_scalper_xgb.json!")

if __name__ == "__main__":
    train_scalper_model()
