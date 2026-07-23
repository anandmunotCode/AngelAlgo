"""
Train XGBoost Model on 5-Year Nifty 1-Minute Features Dataset
Train Set: 2021 - 2024 (4 Years of Historical Data)
Test Set: 2025 - 2026 (Unseen Out-Of-Sample Data)
"""
import os
import sys
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.metrics import classification_report, accuracy_score

def train_nifty_quant_model(features_csv="nifty_1min_5y_features.csv"):
    if not os.path.exists(features_csv):
        print(f"Error: {features_csv} not found!")
        return

    print("=" * 60)
    print("LOADING ENGINEERED FEATURES DATASET...")
    print("=" * 60)
    
    df = pd.read_csv(features_csv)
    df['datetime'] = pd.to_datetime(df['datetime'])
    
    # Feature columns
    feature_cols = [c for c in df.columns if c.startswith('f')]
    target_col = 'target_15m_dir'

    print(f"Total rows in dataset: {len(df):,}")
    print(f"Features count: {len(feature_cols)}")
    print(f"Feature names: {feature_cols}")

    # Map Target: -1 -> 0 (Bearish), 0 -> 1 (Sideways), 1 -> 2 (Bullish)
    target_mapping = {-1: 0, 0: 1, 1: 2}
    df['target_mapped'] = df[target_col].map(target_mapping)

    # ----------------------------------------------------
    # CHRONOLOGICAL TRAIN/TEST SPLIT
    # Train: 2021 to 2024
    # Test: 2025 to 2026 (Unseen data)
    # ----------------------------------------------------
    split_date = '2025-01-01'
    train_mask = df['datetime'] < split_date
    test_mask = df['datetime'] >= split_date

    X_train = df.loc[train_mask, feature_cols]
    y_train = df.loc[train_mask, 'target_mapped']

    X_test = df.loc[test_mask, feature_cols]
    y_test = df.loc[test_mask, 'target_mapped']

    print("\n" + "=" * 60)
    print("DATASET SPLIT SUMMARY")
    print("=" * 60)
    print(f"Training Set (2021 - 2024): {len(X_train):,} candles ({len(X_train)/len(df)*100:.1f}%)")
    print(f"Testing Set  (2025 - 2026): {len(X_test):,} candles ({len(X_test)/len(df)*100:.1f}%)")
    
    print("\nTraining Class Distribution:")
    print(y_train.value_counts(normalize=True).rename(index={0: 'Bearish (-1)', 1: 'Sideways (0)', 2: 'Bullish (+1)'}) * 100)

    # ----------------------------------------------------
    # TRAIN XGBOOST MODEL
    # ----------------------------------------------------
    print("\n" + "=" * 60)
    print("TRAINING XGBOOST QUANT MODEL...")
    print("=" * 60)

    model = XGBClassifier(
        n_estimators=250,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        eval_metric='mlogloss'
    )

    model.fit(X_train, y_train)
    print("Model Training Complete!")

    # ----------------------------------------------------
    # EVALUATE ON UNSEEN TEST DATA (2025 - 2026)
    # ----------------------------------------------------
    print("\n" + "=" * 60)
    print("EVALUATING MODEL ON UNSEEN TEST DATA (2025 - 2026)")
    print("=" * 60)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)

    acc = accuracy_score(y_test, y_pred)
    print(f"\nOverall Model Accuracy on Unseen Data: {acc * 100:.2f}%\n")

    print("Classification Report:")
    target_names = ['Bearish (-1)', 'Sideways (0)', 'Bullish (+1)']
    print(classification_report(y_test, y_pred, target_names=target_names))

    # ----------------------------------------------------
    # HIGH CONFIDENCE PROBABILITY FILTER EVALUATION
    # ----------------------------------------------------
    print("\n" + "=" * 60)
    print("HIGH CONFIDENCE SIGNAL SIMULATION")
    print("=" * 60)

    test_df = df.loc[test_mask].copy().reset_index(drop=True)
    test_df['prob_bearish'] = y_prob[:, 0]
    test_df['prob_sideways'] = y_prob[:, 1]
    test_df['prob_bullish'] = y_prob[:, 2]

    # Threshold 1: High Bullish Confidence (> 50% Bullish Probability)
    bullish_signals = test_df[test_df['prob_bullish'] >= 0.50]
    if len(bullish_signals) > 0:
        bull_win = (bullish_signals['target_mapped'] == 2).sum()
        bull_win_rate = (bull_win / len(bullish_signals)) * 100
        print(f"[+] High Bullish Probability (>= 50%):")
        print(f"   Total Signals Triggered: {len(bullish_signals):,}")
        print(f"   Successful Trades (+15 pts gain): {bull_win:,}")
        print(f"   Win Rate: {bull_win_rate:.2f}%")

    # Threshold 2: High Bearish Confidence (> 50% Bearish Probability)
    bearish_signals = test_df[test_df['prob_bearish'] >= 0.50]
    if len(bearish_signals) > 0:
        bear_win = (bearish_signals['target_mapped'] == 0).sum()
        bear_win_rate = (bear_win / len(bearish_signals)) * 100
        print(f"\n[-] High Bearish Probability (>= 50%):")
        print(f"   Total Signals Triggered: {len(bearish_signals):,}")
        print(f"   Successful Trades (-15 pts fall): {bear_win:,}")
        print(f"   Win Rate: {bear_win_rate:.2f}%")

    # ----------------------------------------------------
    # FEATURE IMPORTANCE RANKING
    # ----------------------------------------------------
    print("\n" + "=" * 60)
    print("TOP FEATURE IMPORTANCE RANKING (What AI Values Most)")
    print("=" * 60)

    importance_df = pd.DataFrame({
        'Feature': feature_cols,
        'Importance': model.feature_importances_
    }).sort_values('Importance', ascending=False).reset_index(drop=True)

    for idx, row in importance_df.iterrows():
        print(f"Rank {idx+1:2d}: {row['Feature']:<22} - Importance: {row['Importance']*100:5.2f}%")

    # ----------------------------------------------------
    # SAVE TRAINED MODEL TO FILE
    # ----------------------------------------------------
    model_filename = "nifty_xgb_model.json"
    model.save_model(model_filename)
    print(f"\n[SAVE] Model saved to {model_filename} for live trading execution!")

if __name__ == "__main__":
    train_nifty_quant_model()
