"""
Train Quant ML Model Version 2:
1. Dynamic ATR-based Target Labels (1.2 * ATR_14 instead of fixed 15 pts)
2. Active Session Filtering (Morning 09:15-11:30 & Afternoon 13:15-15:15, excluding midday chop)
3. Balanced Class Weighting (Giving equal importance to Bullish/Bearish trends)
"""
import os
import sys
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.metrics import classification_report, accuracy_score
from sklearn.utils.class_weight import compute_sample_weight

def train_v2_model(input_csv="nifty_1min_5y_features.csv"):
    if not os.path.exists(input_csv):
        print(f"Error: {input_csv} not found!")
        return

    print("=" * 65)
    print("QUANT MODEL VERSION 2: DYNAMIC TARGETS & SESSION FILTERING")
    print("=" * 65)

    df = pd.read_csv(input_csv)
    df['datetime'] = pd.to_datetime(df['datetime'])

    # --- 1. DYNAMIC ATR-BASED TARGET (1.2 x ATR_14) ---
    c = df['close']
    atr = df['f16_atr_14']
    future_c_15m = c.shift(-15)
    diff = future_c_15m - c

    dynamic_threshold = (1.2 * atr).clip(lower=12.0) # At least 12 pts threshold

    df['target_v2'] = np.where(diff >= dynamic_threshold, 1,
                      np.where(diff <= -dynamic_threshold, -1, 0))

    target_mapping = {-1: 0, 0: 1, 1: 2}
    df['target_v2_mapped'] = df['target_v2'].map(target_mapping)

    # --- 2. ACTIVE SESSION FILTER (Exclude 11:30 AM - 01:15 PM chop) ---
    # f19_minute_of_day: 0 = 09:15 AM
    # Morning: 0 to 135 (09:15 AM to 11:30 AM)
    # Afternoon: 240 to 360 (01:15 PM to 03:15 PM)
    morning_mask = (df['f19_minute_of_day'] >= 0) & (df['f19_minute_of_day'] <= 135)
    afternoon_mask = (df['f19_minute_of_day'] >= 240) & (df['f19_minute_of_day'] <= 360)
    session_mask = morning_mask | afternoon_mask

    df_filtered = df[session_mask].dropna().reset_index(drop=True)
    print(f"Original candles: {len(df):,}")
    print(f"Active session candles (choppy mid-day removed): {len(df_filtered):,}")

    feature_cols = [c for c in df_filtered.columns if c.startswith('f')]

    # --- 3. CHRONOLOGICAL SPLIT ---
    split_date = '2025-01-01'
    train_mask = df_filtered['datetime'] < split_date
    test_mask = df_filtered['datetime'] >= split_date

    X_train = df_filtered.loc[train_mask, feature_cols]
    y_train = df_filtered.loc[train_mask, 'target_v2_mapped']

    X_test = df_filtered.loc[test_mask, feature_cols]
    y_test = df_filtered.loc[test_mask, 'target_v2_mapped']

    print(f"\nTraining set (2021-2024): {len(X_train):,} candles")
    print(f"Testing set  (2025-2026): {len(X_test):,} candles")

    print("\nV2 Training Class Distribution:")
    print(y_train.value_counts(normalize=True).rename(index={0: 'Bearish (-1)', 1: 'Sideways (0)', 2: 'Bullish (+1)'}) * 100)

    # --- 4. COMPUTE BALANCED SAMPLE WEIGHTS ---
    sample_weights = compute_sample_weight('balanced', y_train)

    # --- 5. TRAIN MODEL V2 ---
    print("\n" + "=" * 65)
    print("TRAINING XGBOOST V2 MODEL WITH BALANCED CLASS WEIGHTS...")
    print("=" * 65)

    model_v2 = XGBClassifier(
        n_estimators=350,
        max_depth=6,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        eval_metric='mlogloss'
    )

    model_v2.fit(X_train, y_train, sample_weight=sample_weights)
    print("V2 Model Training Complete!")

    # --- 6. EVALUATE ON UNSEEN TEST DATA ---
    print("\n" + "=" * 65)
    print("V2 PERFORMANCE ON UNSEEN DATA (2025 - 2026)")
    print("=" * 65)

    y_pred = model_v2.predict(X_test)
    y_prob = model_v2.predict_proba(X_test)

    acc = accuracy_score(y_test, y_pred)
    print(f"\nOverall Model Accuracy: {acc * 100:.2f}%\n")

    target_names = ['Bearish (-1)', 'Sideways (0)', 'Bullish (+1)']
    print(classification_report(y_test, y_pred, target_names=target_names))

    # --- 7. PROBABILITY THRESHOLD EVALUATION (Confidence Filters) ---
    print("=" * 65)
    print("HIGH CONFIDENCE SIGNAL WIN-RATES")
    print("=" * 65)

    test_df = df_filtered.loc[test_mask].copy().reset_index(drop=True)
    test_df['prob_bearish'] = y_prob[:, 0]
    test_df['prob_sideways'] = y_prob[:, 1]
    test_df['prob_bullish'] = y_prob[:, 2]

    for conf_threshold in [0.45, 0.50, 0.55, 0.60]:
        print(f"\n--- THRESHOLD: PROBABILITY >= {conf_threshold*100:.0f}% ---")
        
        # Bullish Signals
        bull_sig = test_df[test_df['prob_bullish'] >= conf_threshold]
        if len(bull_sig) > 0:
            bull_wins = (bull_sig['target_v2_mapped'] == 2).sum()
            bull_rate = (bull_wins / len(bull_sig)) * 100
            print(f"[+] BULLISH  Trades: {len(bull_sig):5,} | Wins (+1.2x ATR): {bull_wins:5,} | Win Rate: {bull_rate:6.2f}%")
        else:
            print(f"[+] BULLISH  Trades: 0")

        # Bearish Signals
        bear_sig = test_df[test_df['prob_bearish'] >= conf_threshold]
        if len(bear_sig) > 0:
            bear_wins = (bear_sig['target_v2_mapped'] == 0).sum()
            bear_rate = (bear_wins / len(bear_sig)) * 100
            print(f"[-] BEARISH  Trades: {len(bear_sig):5,} | Wins (-1.2x ATR): {bear_wins:5,} | Win Rate: {bear_rate:6.2f}%")
        else:
            print(f"[-] BEARISH  Trades: 0")

    # Save V2 Model
    model_v2.save_model("nifty_xgb_v2_model.json")
    print(f"\n[SAVE] Model V2 saved to nifty_xgb_v2_model.json!")

if __name__ == "__main__":
    train_v2_model()
