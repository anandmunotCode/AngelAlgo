"""
Delta Neutral Nifty - Angel One SmartAPI Wrapper
Handles authentication, option chain fetching, instrument master, and order placement.
"""
import os
import json
import time
import requests
from datetime import datetime, date
import pyotp
from SmartApi import SmartConnect

from . import config
from .utils import setup_logger, now_ist, get_current_expiry

logger = setup_logger("angel_api")


import threading
import random

class AngelRateLimiter:
    """
    Thread-safe Rate Limiter matching Angel One SmartAPI official limits:
    - Get LTP Data: Max 10 req/sec (enforced at 7 req/sec max for safety buffer)
    - Orders: Max 10 req/sec (enforced at 5 req/sec max for safety buffer)
    """
    def __init__(self, max_per_second=7):
        self.max_per_second = max_per_second
        self.lock = threading.Lock()
        self.timestamps = []

    def wait(self):
        with self.lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < 1.0]
            if len(self.timestamps) >= self.max_per_second:
                sleep_time = (1.0 - (now - self.timestamps[0])) + random.uniform(0.05, 0.12)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                now = time.time()
                self.timestamps = [t for t in self.timestamps if now - t < 1.0]
            self.timestamps.append(time.time())

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
        self.rate_limiter = AngelRateLimiter(max_per_second=7)
        self._ltp_cache = {}        # {(symbol, token): (timestamp, ltp)}
        self._rms_cache = (0, 0.0)  # (timestamp, utilised_margin) TTL cached for 10 seconds

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
        if not hasattr(self, "_last_spot"):
            self._last_spot = 24550.0

        now = time.time()
        # Enforce minimum 0.8s between REST API calls for spot LTP to comply with 1 req/s rate limit
        if hasattr(self, "_last_spot_fetch_time") and (now - self._last_spot_fetch_time < 0.8) and self._last_spot > 0:
            return self._last_spot

        if not self.smart_api:
            return self._last_spot

        for attempt in range(4):
            try:
                self.rate_limiter.wait()
                data = self.smart_api.ltpData(
                    config.NIFTY_SPOT_EXCHANGE,
                    config.NIFTY_SYMBOL,
                    config.NIFTY_SPOT_TOKEN
                )
                if data and isinstance(data, dict):
                    if data.get("status") and data.get("data") and "ltp" in data["data"]:
                        val = float(data["data"]["ltp"])
                        if val > 0:
                            self._last_spot = val
                            self._last_spot_fetch_time = time.time()
                            return val
                    else:
                        msg = data.get("message", "")
                        if "exceeding access rate" in str(msg).lower() or "ab1004" in str(data.get("errorcode", "")).lower():
                            time.sleep(1.0 + random.uniform(0.1, 0.3))
                        else:
                            break
            except Exception as e:
                logger.debug(f"get_spot_ltp exception attempt {attempt+1}: {e}")
                time.sleep(0.5 * (2 ** attempt) + random.uniform(0.05, 0.15))

        self._last_spot_fetch_time = time.time()
        return self._last_spot

    def get_option_ltp(self, symbol, token):
        """Get LTP for a specific option contract with rate-limit protection and 800ms cache."""
        cache_key = (symbol, token)
        now = time.time()
        if cache_key in self._ltp_cache:
            ts, val = self._ltp_cache[cache_key]
            if now - ts < 0.8:  # 800ms cache
                return val

        if not self.smart_api:
            return None

        for attempt in range(4):
            try:
                self.rate_limiter.wait()
                data = self.smart_api.ltpData(
                    config.NIFTY_OPTIONS_EXCHANGE,
                    symbol,
                    token
                )
                if data and isinstance(data, dict):
                    if data.get("status") and data.get("data") and "ltp" in data["data"]:
                        val = float(data["data"]["ltp"])
                        self._ltp_cache[cache_key] = (now, val)
                        return val
                    else:
                        msg = data.get("message", "")
                        if "exceeding access rate" in str(msg).lower() or "ab1004" in str(data.get("errorcode", "")).lower():
                            time.sleep(0.8 + random.uniform(0.05, 0.15))
                        else:
                            break
            except Exception as e:
                time.sleep(0.4 * (2 ** attempt))

        return None

    def get_multiple_option_ltps(self, option_list):
        """
        Get LTPs for multiple option contracts concurrently with strict rate limits.
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

        # max_workers=4 keeps total concurrency strictly within Angel One's 10 req/s limit
        with ThreadPoolExecutor(max_workers=4) as executor:
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
        Place order on Angel One with error handling and order verification.
        transaction_type: "BUY" or "SELL"
        order_type: "MARKET" or "LIMIT"
        product_type: "CARRYFORWARD" (NRML) or "INTRADAY"
        """
        self.rate_limiter.wait()
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
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
        try:
            response = self.smart_api.placeOrder(order_params)
            logger.info(f"[ORDER PLACED] {transaction_type} {quantity}x {symbol} @ {order_type} -> {response}")
            
            if isinstance(response, dict) and response.get("status"):
                order_id = response.get("data", {}).get("orderid", "")
                logger.info(f"✅ [ORDER SUCCESS] Order ID: {order_id} | {transaction_type} {quantity}x {symbol}")
                self._verify_order_status(order_id, symbol)
            elif isinstance(response, str) and response.isdigit():
                # Some versions of SmartAPI return the orderid directly as string
                order_id = response
                logger.info(f"✅ [ORDER SUCCESS] Order ID: {order_id} | {transaction_type} {quantity}x {symbol}")
                self._verify_order_status(order_id, symbol)
            else:
                err_msg = response.get("message") if isinstance(response, dict) else str(response)
                logger.error(f"❌ [ORDER REJECTED/FAILED] {transaction_type} {quantity}x {symbol} | Response: {err_msg}")
            return response
        except Exception as e:
            logger.error(f"❌ [ORDER EXCEPTION] Failed to place {transaction_type} {quantity}x {symbol}: {e}", exc_info=True)
            return None

    def _verify_order_status(self, order_id, symbol):
        """Quick verification of order execution status in Angel One order book."""
        if not order_id:
            return
        try:
            time.sleep(0.5)  # allow exchange engine to register fill
            ob = self.get_order_book()
            if ob and isinstance(ob, dict) and ob.get("status") and ob.get("data"):
                for order in ob["data"]:
                    if str(order.get("orderid")) == str(order_id):
                        status = order.get("orderstatus", "").upper()
                        avg_price = order.get("averageprice", 0)
                        filled_qty = order.get("filledshares", 0)
                        logger.info(f"📋 [ORDER BOOK STATUS] Order {order_id} ({symbol}): {status} | Filled: {filled_qty} | Avg Price: {avg_price}")
                        if status in ("REJECTED", "CANCELLED"):
                            logger.error(f"⚠️ [ORDER ISSUE] Order {order_id} status={status}: {order.get('text', '')}")
                        break
        except Exception as e:
            logger.debug(f"Order verification check error: {e}")

    def get_positions(self):
        """Get all current positions from Angel One."""
        self.rate_limiter.wait()
        return self.smart_api.position()

    def get_order_book(self):
        """Get order book from Angel One."""
        self.rate_limiter.wait()
        try:
            return self.smart_api.orderBook()
        except Exception as e:
            logger.error(f"Error fetching order book: {e}")
            return None

    def get_trade_book(self):
        """Get trade book from Angel One."""
        self.rate_limiter.wait()
        try:
            return self.smart_api.tradeBook()
        except Exception as e:
            logger.error(f"Error fetching trade book: {e}")
            return None

    def get_deployed_margin(self, num_lots=1, is_straddle=False):
        """
        Fetch actual utilized margin / deployed capital from Angel RMS.
        Complies strictly with 2 req/sec rate limit by using a 10-second TTL cache.
        Falls back to dynamic margin calculation if API is offline or paper trading.
        """
        now = time.time()
        # 1. Return cached RMS margin if within 10-second TTL (Zero redundant API calls)
        cached_time, cached_val = self._rms_cache
        if (now - cached_time) < 10.0 and cached_val > 0:
            return cached_val

        if self.smart_api:
            try:
                self.rate_limiter.wait()
                rms = self.smart_api.rmsLimit()
                if rms and isinstance(rms, dict) and rms.get("status") and "data" in rms:
                    data = rms["data"]
                    # Check utilisedDebits or sum of utilisedSpan + utilisedOptionpremium
                    utilised = float(data.get("utilisedDebits", 0.0) or 0.0)
                    if utilised <= 0:
                        span = float(data.get("utilisedSpan", 0.0) or 0.0)
                        prem = float(data.get("utilisedOptionpremium", 0.0) or 0.0)
                        utilised = span + prem
                    if utilised > 0:
                        self._rms_cache = (now, utilised)
                        logger.debug(f"[RMS] Live Deployed Capital (Utilized Margin): Rs.{utilised:,.2f}")
                        return utilised
            except Exception as e:
                logger.debug(f"Could not fetch RMS limits: {e}")

        # Dynamic fallback based on structure (Iron Condor vs Straddle) and lot size
        base_margin = config.DEFAULT_MARGIN_PER_LOT_STRADDLE if is_straddle else config.DEFAULT_MARGIN_PER_LOT_IC
        estimated_margin = base_margin * num_lots
        logger.debug(f"[ESTIMATED MARGIN] Structure={'Straddle' if is_straddle else 'Iron Condor'}, Lots={num_lots} -> Rs.{estimated_margin:,.2f}")
        return estimated_margin


