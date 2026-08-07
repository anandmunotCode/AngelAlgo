"""
Delta Neutral Nifty - Angel One SmartAPI Wrapper
Handles authentication, option chain fetching, instrument master, and order placement.
"""
import os
import json
import time
import requests
from datetime import datetime
import pyotp
from SmartApi import SmartConnect

from . import config
from .utils import setup_logger, now_ist, get_current_expiry

logger = setup_logger("angel_api")


class AngelOneAPI:
    """Wrapper for Angel One SmartAPI with option chain support."""

    def __init__(self, env_path=".env"):
        self.env_path = env_path
        self.credentials = self._load_env()
        self.smart_api = None
        self.auth_token = None
        self.feed_token = None
        self.client_code = None
        self.instrument_cache = {}  # {token: instrument_info}
        self.nifty_options = {}     # {expiry_str: {strike: {CE: token, PE: token}}}

    def _load_env(self):
        """Load credentials from environment variables or .env file."""
        creds = {}
        keys = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]

        # Environment variables first (GitHub Actions)
        for k in keys:
            val = os.environ.get(k)
            if val:
                creds[k] = val

        # Fallback to .env file
        if os.path.exists(self.env_path):
            with open(self.env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k not in creds:
                            creds[k] = v
        return creds

    def login(self):
        """Authenticate with Angel One using TOTP."""
        api_key = self.credentials.get("ANGEL_API_KEY")
        client_id = self.credentials.get("ANGEL_CLIENT_ID")
        password = self.credentials.get("ANGEL_PASSWORD")
        totp_secret = self.credentials.get("ANGEL_TOTP_SECRET")

        if not all([api_key, client_id, password, totp_secret]):
            raise ValueError("Missing Angel One credentials!")

        self.smart_api = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        session = self.smart_api.generateSession(client_id, password, totp)

        if not session.get("status"):
            raise PermissionError(f"Angel One Login Failed: {session.get('message', session)}")

        self.auth_token = session["data"]["jwtToken"]
        self.feed_token = self.smart_api.getfeedToken()
        self.client_code = client_id

        logger.info(f"[AUTH SUCCESS] Logged in as {client_id}")
        return True

    def ensure_session(self):
        """Re-login if session expired."""
        try:
            self.get_spot_ltp()
        except Exception:
            logger.warning("Session expired, re-authenticating...")
            self.login()

    # ─── INSTRUMENT MASTER ────────────────────────────────────────

    def fetch_instrument_master(self, force_refresh=False):
        """
        Download Angel One instrument master and filter for NIFTY options.
        Caches locally to avoid repeated downloads.
        """
        cache_path = config.INSTRUMENT_MASTER_CACHE
        today_str = now_ist().strftime("%Y-%m-%d")

        # Check if cache is from today
        if not force_refresh and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("date") == today_str:
                    self.nifty_options = cached.get("options", {})
                    logger.info(f"Loaded instrument master from cache ({len(self.nifty_options)} expiries)")
                    return
            except (json.JSONDecodeError, KeyError):
                pass

        logger.info("Downloading instrument master from Angel One...")
        resp = requests.get(config.INSTRUMENT_MASTER_URL, timeout=60)
        resp.raise_for_status()
        instruments = resp.json()

        # Filter NIFTY options only
        nifty_opts = {}
        for inst in instruments:
            if (inst.get("exch_seg") == "NFO" and
                inst.get("name") == "NIFTY" and
                inst.get("instrumenttype") in ("OPTIDX",) and
                inst.get("strike") and inst.get("symbol")):

                expiry = inst.get("expiry", "")
                strike = float(inst["strike"]) / 100  # Angel stores strike * 100
                token = inst["token"]
                symbol = inst["symbol"]

                # Determine CE or PE from symbol
                if symbol.endswith("CE"):
                    opt_type = "CE"
                elif symbol.endswith("PE"):
                    opt_type = "PE"
                else:
                    continue

                if expiry not in nifty_opts:
                    nifty_opts[expiry] = {}
                if strike not in nifty_opts[expiry]:
                    nifty_opts[expiry][strike] = {}

                nifty_opts[expiry][strike][opt_type] = {
                    "token": token,
                    "symbol": symbol,
                    "lotsize": int(inst.get("lotsize", config.LOT_SIZE))
                }

        self.nifty_options = nifty_opts

        # Cache to disk
        cache_data = {"date": today_str, "options": nifty_opts}
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f)

        logger.info(f"Instrument master downloaded: {len(nifty_opts)} expiries found")

    def get_option_chain_for_expiry(self, expiry_date):
        """
        Get all strikes and their tokens for a specific expiry.
        Returns: {strike: {"CE": {"token", "symbol"}, "PE": {"token", "symbol"}}}
        """
        if not self.nifty_options:
            self.fetch_instrument_master()

        # Format expiry to match Angel One format (e.g., "07AUG2026")
        expiry_str = expiry_date.strftime("%d%b%Y").upper()

        # Try exact match first
        if expiry_str in self.nifty_options:
            return self.nifty_options[expiry_str]

        # Try alternate matching
        for key in self.nifty_options:
            if expiry_date.strftime("%Y-%m-%d") in key or expiry_str in key:
                return self.nifty_options[key]

        # Fallback: return nearest valid option chain from master
        from datetime import datetime, date
        today_date = expiry_date if isinstance(expiry_date, date) else date.today()
        upcoming = []
        for exp_k, chain_data in self.nifty_options.items():
            try:
                dt_exp = datetime.strptime(exp_k, "%d%b%Y").date()
                if dt_exp >= today_date:
                    upcoming.append((dt_exp, chain_data))
            except Exception:
                pass
        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            return upcoming[0][1]

        logger.warning(f"No option chain found for target expiry {expiry_str}")
        return {}

    def get_nearest_expiry_date(self, from_date=None):
        """Get exact nearest future expiry date object from Angel One master."""
        if not self.nifty_options:
            self.fetch_instrument_master()
        from datetime import datetime, date
        today_date = from_date if isinstance(from_date, date) else date.today()
        upcoming = []
        for exp_k in self.nifty_options.keys():
            try:
                dt_exp = datetime.strptime(exp_k, "%d%b%Y").date()
                if dt_exp >= today_date:
                    upcoming.append(dt_exp)
            except Exception:
                pass
        if upcoming:
            upcoming.sort()
            return upcoming[0]
        return today_date

    # ─── MARKET DATA ──────────────────────────────────────────────

    def get_spot_ltp(self):
        """Get current Nifty spot LTP with rate-limit protection."""
        for attempt in range(3):
            try:
                data = self.smart_api.ltpData(
                    config.NIFTY_SPOT_EXCHANGE,
                    config.NIFTY_SYMBOL,
                    config.NIFTY_SPOT_TOKEN
                )
                if data and data.get("data") and "ltp" in data["data"]:
                    return float(data["data"]["ltp"])
            except Exception as e:
                if "exceeding access rate" in str(e).lower():
                    time.sleep(0.4 * (attempt + 1))
                else:
                    logger.debug(f"get_spot_ltp error: {e}")
        # Return fallback from current positions if available
        if hasattr(self, "_last_spot") and self._last_spot:
            return self._last_spot
        raise RuntimeError("Failed to fetch Nifty spot LTP due to rate limits")

    def get_option_ltp(self, symbol, token):
        """Get LTP for a specific option contract with rate-limit protection."""
        for attempt in range(3):
            try:
                data = self.smart_api.ltpData(
                    config.NIFTY_OPTIONS_EXCHANGE,
                    symbol,
                    token
                )
                if data and data.get("data") and "ltp" in data["data"]:
                    return float(data["data"]["ltp"])
            except Exception as e:
                if "exceeding access rate" in str(e).lower():
                    time.sleep(0.3 * (attempt + 1))
                else:
                    break
        return None

    def get_multiple_option_ltps(self, option_list):
        """
        Get LTPs for multiple option contracts concurrently.
        option_list: list of {"symbol": str, "token": str, "strike": float, "type": str}
        Returns: {(strike, type): ltp}
        """
        results = {}
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch(opt):
            try:
                ltp = self.get_option_ltp(opt["symbol"], opt["token"])
                if ltp is not None:
                    return (opt["strike"], opt["type"]), ltp
            except Exception:
                pass
            return None, None

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(_fetch, opt) for opt in option_list]
            for future in as_completed(futures):
                key, ltp = future.result()
                if key and ltp is not None:
                    results[key] = ltp
        return results

    def get_option_chain_ltps(self, expiry_date, spot_price, range_pct=0.08):
        """
        Get LTPs for all relevant strikes around current spot price.
        range_pct: how far from ATM to scan (8% = ±2000 pts / 40+ strikes)
        Returns: {"CE": {strike: ltp}, "PE": {strike: ltp}}
        """
        chain = self.get_option_chain_for_expiry(expiry_date)
        if not chain:
            return {"CE": {}, "PE": {}}

        lower_bound = spot_price * (1 - range_pct)
        upper_bound = spot_price * (1 + range_pct)

        ce_ltps = {}
        pe_ltps = {}
        fetch_list = []

        for strike, opts in chain.items():
            strike_f = float(strike)
            if lower_bound <= strike_f <= upper_bound:
                if "CE" in opts:
                    fetch_list.append({
                        "symbol": opts["CE"]["symbol"],
                        "token": opts["CE"]["token"],
                        "strike": strike_f,
                        "type": "CE"
                    })
                if "PE" in opts:
                    fetch_list.append({
                        "symbol": opts["PE"]["symbol"],
                        "token": opts["PE"]["token"],
                        "strike": strike_f,
                        "type": "PE"
                    })

        ltps = self.get_multiple_option_ltps(fetch_list)

        for (strike, opt_type), ltp in ltps.items():
            if opt_type == "CE":
                ce_ltps[strike] = ltp
            else:
                pe_ltps[strike] = ltp

        return {"CE": ce_ltps, "PE": pe_ltps}

    def get_token_info(self, expiry_date, strike, option_type):
        """Get token and symbol for a specific option contract."""
        chain = self.get_option_chain_for_expiry(expiry_date)
        strike_key = float(strike)

        # Try exact match
        if strike_key in chain and option_type in chain[strike_key]:
            return chain[strike_key][option_type]

        # Try string key
        for k, v in chain.items():
            if abs(float(k) - strike_key) < 1 and option_type in v:
                return v[option_type]

        return None

    # ─── ORDER MANAGEMENT ─────────────────────────────────────────

    def place_order(self, symbol, token, transaction_type, quantity,
                    price=0, order_type="MARKET", product_type="CARRYFORWARD"):
        """
        Place order on Angel One.
        transaction_type: "BUY" or "SELL"
        order_type: "MARKET" or "LIMIT"
        product_type: "CARRYFORWARD" (NRML) or "INTRADAY"
        """
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": transaction_type,
            "exchange": config.NIFTY_OPTIONS_EXCHANGE,
            "ordertype": order_type,
            "producttype": product_type,
            "duration": "DAY",
            "price": str(price),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }
        response = self.smart_api.placeOrder(order_params)
        logger.info(f"[ORDER] {transaction_type} {quantity}x {symbol} @ {order_type} -> {response}")
        return response

    def get_positions(self):
        """Get all current positions from Angel One."""
        return self.smart_api.position()
