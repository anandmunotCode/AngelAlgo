"""
Retrain Final Production Option Scalper Model on 100% of 5.5 Years Data (2021 - July 2026)
This model learns from all 476,400+ historical candles for Live Trading Execution.
"""
import os
import sys
import pandas as pd
import numpy as np
from xgboost import XGBClassifier

def train_production_model(features_csv="nifty_1min_5y_features.csv"):
    if not os.path.exists(features_csv):
        print(f"Error: {features_csv} not found!")
        return

    print("=" * 65)
    print("RETRAINING FINAL PRODUCTION SCALPER MODEL ON 100% DATA (2021-2026)")
    print("=" * 65)

    df = pd.read_csv(features_csv)
    df['datetime'] = pd.to_datetime(df['datetime'])

    c = df['close']
    h = df['high']
    l = df['low']

    # Target: 10 Nifty Index Points (+5 Option Points) within next 7 minutes
    indexer = pd.api.indexers.FixedForwardWindowIndexer(window_size=7)
    future_max = h.rolling(window=indexer).max()
    future_min = l.rolling(window=indexer).min()

    bullish_hit = (future_max - c) >= 10.0
    bearish_hit = (c - future_min) >= 10.0

    df['target_scalp'] = np.where(bullish_hit & ~bearish_hit, 1,
                         np.where(bearish_hit & ~bullish_hit, -1, 0))

    target_mapping = {-1: 0, 0: 1, 1: 2}
    df['target_mapped'] = df['target_scalp'].map(target_mapping)

    # Active Session Filter
    active_mask = (df['f19_minute_of_day'] >= 10) & (df['f19_minute_of_day'] <= 360)
    df_clean = df[active_mask].dropna().reset_index(drop=True)

    feature_cols = [col for col in df_clean.columns if col.startswith('f')]

    X_all = df_clean[feature_cols]
    y_all = df_clean['target_mapped']

    print(f"Total Production Candles for Training: {len(X_all):,}")
    print("\nFull Dataset Class Distribution:")
    print(y_all.value_counts(normalize=True).rename(index={0: 'Put Scalp (-10p)', 1: 'No Scalp (0)', 2: 'Call Scalp (+10p)'}) * 100)

    print("\nTraining Final Production XGBoost Model...")
    final_model = XGBClassifier(
        n_estimators=350,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        eval_metric='mlogloss'
    )

    final_model.fit(X_all, y_all)
    print("Final Production Model Retrained Successfully!")

    # Save Final Production Model
    output_filename = "nifty_scalper_final_production.json"
    final_model.save_model(output_filename)
    print(f"\n[SUCCESS] Final Production Model saved to {output_filename}!")

if __name__ == "__main__":
    train_production_model()
