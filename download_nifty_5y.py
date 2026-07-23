"""
Download Nifty Spot 1-Minute Historical Data from Angel One SmartAPI
Period: Calendar Years 2021, 2022, 2023, 2024, 2025 + 2026 YTD
Output: CSV files for each year + Master Combined CSV
"""
import os
import sys
import time
import calendar
from datetime import datetime, date
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# 1. Helper to read .env credentials
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

# 2. Login function
def get_angel_session(config):
    api_key = config.get("ANGEL_API_KEY")
    client_id = config.get("ANGEL_CLIENT_ID")
    password = config.get("ANGEL_PASSWORD")
    totp_secret = config.get("ANGEL_TOTP_SECRET")

    if not all([api_key, client_id, password, totp_secret]):
        raise ValueError("Missing one or more required Angel credentials in .env file!")

    smart_api = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(totp_secret).now()
    session = smart_api.generateSession(client_id, password, totp)
    if not session.get("status"):
        raise PermissionError(f"Angel One Login Failed: {session}")
    print(f"[AUTH SUCCESS] Logged in successfully as Client ID: {client_id}")
    return smart_api

# 3. Main Data Downloader
def download_nifty_data():
    config = load_env(".env")
    smart_api = get_angel_session(config)

    symbol_token = "99926000"  # Nifty Spot Token
    exchange = "NSE"
    interval = "ONE_MINUTE"

    current_year = datetime.now().year
    current_month = datetime.now().month

    years = [2021, 2022, 2023, 2024, 2025, 2026]
    all_year_dfs = []

    print("\n" + "="*70)
    print(" STARTING 5-YEAR + 2026 YTD NIFTY 1-MIN DATA DOWNLOAD")
    print("="*70 + "\n")

    total_chunks = 0
    for yr in years:
        max_m = 12 if yr < current_year else current_month
        total_chunks += max_m

    chunk_counter = 0

    for yr in years:
        max_m = 12 if yr < current_year else current_month
        year_rows = []

        print(f"\n---> [YEAR {yr}] Processing {max_m} months...")

        for m in range(1, max_m + 1):
            chunk_counter += 1
            last_day = calendar.monthrange(yr, m)[1]

            # Adjust end date for current month if needed
            if yr == current_year and m == current_month:
                today_day = min(datetime.now().day, last_day)
                from_str = f"{yr:04d}-{m:02d}-01 09:15"
                to_str = f"{yr:04d}-{m:02d}-{today_day:02d} 15:30"
            else:
                from_str = f"{yr:04d}-{m:02d}-01 09:15"
                to_str = f"{yr:04d}-{m:02d}-{last_day:02d} 15:30"

            month_name = calendar.month_abbr[m]
            print(f"  [{chunk_counter}/{total_chunks}] Fetching {month_name} {yr} ({from_str[:10]} to {to_str[:10]})... ", end="", flush=True)

            # API Call with Retry Logic
            retries = 3
            fetched_data = None

            for attempt in range(retries):
                try:
                    param = {
                        "exchange": exchange,
                        "symboltoken": symbol_token,
                        "interval": interval,
                        "fromdate": from_str,
                        "todate": to_str
                    }
                    res = smart_api.getCandleData(param)

                    if res and res.get("status") and res.get("data"):
                        fetched_data = res["data"]
                        break
                    elif res and not res.get("status") and "jwt" in str(res).lower():
                        # Session expired, re-login
                        print("(Session refresh) ", end="", flush=True)
                        smart_api = get_angel_session(config)
                    else:
                        time.sleep(1)
                except Exception as e:
                    time.sleep(1)

            if fetched_data:
                count = len(fetched_data)
                year_rows.extend(fetched_data)
                print(f"OK ({count:,} candles)")
            else:
                print("FAILED / NO DATA")

            # 0.5 sec sleep between API calls to honor rate limits cleanly
            time.sleep(0.5)

        # Process yearly dataframe
        if year_rows:
            # Angel candle format: [timestamp, open, high, low, close, volume]
            df_year = pd.DataFrame(year_rows, columns=["raw_datetime", "open", "high", "low", "close", "volume"])
            
            # Format timestamps
            df_year["datetime"] = pd.to_datetime(df_year["raw_datetime"])
            df_year["date"] = df_year["datetime"].dt.strftime("%Y-%m-%d")
            df_year["time"] = df_year["datetime"].dt.strftime("%H:%M")
            df_year["datetime"] = df_year["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

            # Format numeric columns
            df_year["open"] = df_year["open"].round(2)
            df_year["high"] = df_year["high"].round(2)
            df_year["low"] = df_year["low"].round(2)
            df_year["close"] = df_year["close"].round(2)

            # Reorder columns cleanly
            cols = ["datetime", "date", "time", "open", "high", "low", "close", "volume"]
            df_year = df_year[cols].drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

            # Save individual yearly CSV
            yearly_filename = f"nifty_1min_{yr}.csv" if yr < 2026 else f"nifty_1min_{yr}_ytd.csv"
            df_year.to_csv(yearly_filename, index=False)
            print(f"  [SAVED] {yearly_filename} ({len(df_year):,} total candles)")
            all_year_dfs.append(df_year)

    # Combine into Master CSV
    if all_year_dfs:
        print("\n" + "="*70)
        print(" MERGING ALL YEARS INTO MASTER CSV...")
        print("="*70)

        master_df = pd.concat(all_year_dfs, ignore_index=True)
        master_df = master_df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

        master_filename = "nifty_1min_5y_master.csv"
        master_df.to_csv(master_filename, index=False)

        file_size_mb = os.path.getsize(master_filename) / (1024 * 1024)

        print(f"\n SUCCESS! Master Data File Created: {master_filename}")
        print(f" Total Rows (Candles) : {len(master_df):,}")
        print(f" Total Trading Days   : {master_df['date'].nunique():,}")
        print(f" Date Range           : {master_df['date'].min()} to {master_df['date'].max()}")
        print(f" File Size            : {file_size_mb:.2f} MB")
        print("="*70 + "\n")

if __name__ == "__main__":
    download_nifty_data()
