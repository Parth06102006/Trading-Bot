"""
LR21 FULLY AUTOMATED Futures Trading Bot API
Version 3.0 - Parameter-Driven Auto Trading System

Features:
- Continuous market monitoring with auto signal evaluation
- Automatic order execution based on strategy signals
- User-configurable parameters (leverage, SL/TP, trade size, etc.)
- Risk management (daily loss limit, max positions, cooldown)
- Position lifecycle management with TP/SL
- Real-time position sync with custom API
"""
import requests
import time
import os
import threading
import json
import statistics
import logging
import random
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Set, Any
from dataclasses import dataclass, field
from threading import Lock, Event
from dotenv import load_dotenv

# FastAPI removed for standalone execution
import math
from collections import deque
from rich.table import Table

# Load environment variables
load_dotenv()

# ============================================================================
# Logging Configuration
# ============================================================================
# Configure logging to file and console
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# File handler
file_handler = logging.FileHandler("trading_bot.log")
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

# Console handler for terminal visibility
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger("TradingBot")
logger.info("SCRIPT STARTING...")

# Configuration for Testnet vs Mainnet
API_URL = os.environ.get('API_URL', os.environ.get('API_URL', '')).rstrip('/')
TEAM_API_KEY = os.environ.get('TEAM_API_KEY', os.environ.get('TEAM_API_KEY', ''))

if not API_URL or not TEAM_API_KEY:
    print("ERROR: API_URL and TEAM_API_KEY are not set in environment or .env")
    exit(1)

# Global session for connection pooling
http_session = requests.Session()

def fetch_with_retry(method: str, url: str, headers: dict = None, params: dict = None, json_data: dict = None, max_retries: int = 3, base_delay: float = 2.0):
    """
    Perform an HTTP request with exponential backoff on failure/timeout.
    Uses a global session for connection pooling to mitigate timeouts.
    """
    attempt = 0
    while attempt <= max_retries:
        try:
            # Increase timeout to 30s and use session
            response = http_session.request(method, url, headers=headers, params=params, json=json_data, timeout=30)
            
            # Handle 429 specifically
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 10.0
                logger.warning(f"Rate limited (429) on {url}. Waiting {delay}s before retry {attempt+1}/{max_retries}")
                time.sleep(delay)
                attempt += 1
                continue

            response.raise_for_status()
            return response, None
        except requests.exceptions.HTTPError as he:
            if hasattr(he, 'response') and he.response is not None:
                if he.response.status_code == 404:
                    return None, f"404 Not Found: {url}"
                if he.response.status_code == 429:
                    # Should be caught above, but as a fallback
                    delay = 10.0
                    logger.warning(f"Rate limited (429) hit in exception for {url}. Waiting {delay}s")
                    time.sleep(delay)
                    attempt += 1
                    continue
            error_msg = f"HTTP Error: {he}"
        except requests.exceptions.RequestException as re:
            error_msg = f"Request Exception: {re}"
        except Exception as e:
            error_msg = f"Unexpected Exception: {e}"
        
        attempt += 1
        if attempt <= max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(f"Retry {attempt}/{max_retries} for {url} in {delay}s after error: {error_msg}")
            time.sleep(delay)
        else:
            logger.error(f"Max retries reached for {url}. Last error: {error_msg}")
    
    return None, error_msg


# ============================================================================
# Custom Trading Client
# ============================================================================
class Client:
    """
    Generic client that routes all requests to the custom API_URL.
    mirrors necessary parts of the Futures API structure.
    """
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = API_URL
        self.headers = {
            "X-API-Key": TEAM_API_KEY,
            "Content-Type": "application/json"
        }

    def _request(self, method: str, path: str, params: dict = None, json_data: dict = None):
        # Determine actual endpoint
        actual_path = path

        # Mapping for orders (Detect buy vs sell)
        if path == "/fapi/v1/order" and method == "POST":
            side = (json_data or {}).get('side', 'BUY').upper()
            actual_path = "/api/buy" if side == "BUY" else "/api/sell"
            # Strip body to just quantity as per Participant Guide
            quantity = (json_data or {}).get('quantity', 0)
            json_data = {"quantity": quantity}
        else:
            path_map = {
                "/fapi/v1/account": "/api/portfolio",
                "/fapi/v1/balance": "/api/portfolio",
                "/fapi/v1/positionRisk": "/api/portfolio",
                "/fapi/v1/order": "/api/history", # GET orders maps to history
                "manual_buy": "/api/buy",
                "manual_sell": "/api/sell"
            }
            actual_path = path_map.get(path, path)
        
        url = f"{self.base_url}{actual_path}"
        
        # LOG REQUEST DETAILS
        logger.info(f"[API] {method} {actual_path} | Params: {params} | Body: {json_data}")
        
        # Use retry helper
        response, error = fetch_with_retry(method, url, headers=self.headers, params=params, json_data=json_data)
        
        if response:
            try:
                data = response.json()
                # Adapt /api/portfolio to look like Binance Futures responses
                # Guide schema: { cash, shares, net worth, pnl pct, last trade tick }
                if actual_path == "/api/portfolio":
                    if isinstance(data, dict):
                        cash = float(data.get('cash', 0))
                        net_worth = float(data.get('net_worth', cash))
                        shares = float(data.get('shares', 0))
                        # Use net_worth as walletBalance for accurate PnL tracking
                        data['assets'] = [{'asset': 'USDT', 'walletBalance': net_worth, 'availableBalance': cash}]
                        data['positions'] = [{
                            'symbol': 'BTCUSDT', 
                            'positionAmt': str(shares),
                            'entryPrice': '0',
                            'unRealizedProfit': '0'
                        }]
                
                # LOG SUCCESSFUL RESPONSE (Trancated RAW text for clarity)
                resp_text = response.text[:300]
                logger.info(f"[API] Response from {actual_path}: {resp_text}{'...' if len(response.text) > 300 else ''}")
                
                return data
            except Exception as e:
                logger.error(f"Error parsing response from {actual_path}: {e}")
                return {}
        else:
            return {}

    def futures_account(self):
        return self._request("GET", "/fapi/v1/account")

    def futures_account_balance(self):
        resp = self._request("GET", "/fapi/v1/balance")
        if isinstance(resp, dict):
             if 'assets' in resp:
                 return resp['assets']
             # If it's a dict but no 'assets', return it as-is (might be the balance info itself)
             return [resp]
        return resp if isinstance(resp, list) else []

    def futures_position_information(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        resp = self._request("GET", "/fapi/v1/positionRisk", params=params)
        if isinstance(resp, dict) and 'positions' in resp:
            return resp['positions']
        return resp if isinstance(resp, list) else []

    def futures_get_open_orders(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params=params) or []

    def futures_cancel_all_open_orders(self, symbol: str):
        return self._request("DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol})

    def futures_create_order(self, **kwargs):
        return self._request("POST", "/fapi/v1/order", json_data=kwargs)

    def futures_account_trades(self, symbol: str, limit: int = 50):
        params = {"symbol": symbol, "limit": limit}
        return self._request("GET", "/fapi/v1/userTrades", params=params) or []

    def futures_get_position_mode(self):
        return self._request("GET", "/fapi/v1/positionSide/dual")

    def futures_change_margin_type(self, symbol: str, marginType: str):
        return self._request("POST", "/fapi/v1/marginType", json_data={"symbol": symbol, "marginType": marginType})

    def futures_change_leverage(self, symbol: str, leverage: int):
        return self._request("POST", "/fapi/v1/leverage", json_data={"symbol": symbol, "leverage": leverage})

    def futures_exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo")

# Common headers for market data requests
MARKET_DATA_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'X-API-Key': TEAM_API_KEY
}

# ============================================================================
# Global Storage
# ============================================================================
user_futures_clients: Dict[str, Client] = {}
user_configs: Dict[str, dict] = {}
user_auto_traders: Dict[str, 'AutoTradeEngine'] = {}  # NEW: Auto traders per user
clients_lock = Lock()

symbol_precision_cache: Dict[str, dict] = {}
precision_lock = Lock()

# ============================================================================
# Enhanced User Configuration - PRODUCTION READY
# ============================================================================
DEFAULT_CONFIG = {
    # Auto trading settings
    'auto_trading_enabled': True,  # Always enabled for production
    'trading_symbols': ['BTCUSDT', 'ETHUSDT'],  # Reduced symbols for risk management
    
    # More aggressive position sizing for better trading
    'trade_size_percent': 2.0,  # 2% per trade (increased from 1%)
    'leverage': 10,  # 10x leverage (increased from 5x)
    'margin_type': 'ISOLATED',
    
    # Balanced risk management
    'stop_loss_percent': 0.5,  # 0.5% SL (increased from 0.2%)
    'take_profit_percent': 1.0,  # 1.0% TP (increased from 0.5%)
    'max_concurrent_positions': 20,  # Allow 20 positions at a time for user-selected tokens
    'one_position_per_symbol': True,
    'cooldown_seconds': 300,  # 5-minute cooldown between same symbol trades
    'max_trades_per_day': 999999,  # Unlimited trades per day
    'daily_loss_limit_percent': 2.0,  # 2% daily loss limit
    
    # Conservative strategy
    'strategy': 'scalping',
    'signal_check_interval': 10,  # Check every 10 seconds (Guide rule)
    
    # Capital protection
    'min_account_balance': 100.0,  # Minimum $100 to trade
    'max_drawdown_percent': 5.0,  # Stop if 5% drawdown
    
    # Simulation Constraints
    'tick_rate_seconds': 10,       # 1 trade per 10-second tick
    'max_capital_exposure_ratio': 0.6, # Max 60% capital in positions
    'transaction_fee_percent': 0.1,    # 0.1% transaction fee
    'cash_decay_rate_per_minute': 0.0002, # 0.02% per minute cash decay
    
    # Tracking
    'trades_today': 0,
    'daily_pnl': 0.0,
    'start_of_day_balance': 0.0,
    'last_trade_date': '',
    'connected_at': '',
    'bot_status': 'STOPPED',
    'last_signal_check': '',
    'last_decay_time': '',         # Tracking for cash decay
    'last_trade': None,
    'trade_history': []
}

# ============================================================================
# Trade Record for History
# ============================================================================
@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    tp_price: float
    sl_price: float
    timestamp: str
    order_id: str
    status: str  # 'OPEN', 'TP_HIT', 'SL_HIT', 'CLOSED_MANUAL'
    exit_price: float = 0.0
    pnl: float = 0.0
    exit_time: str = ""


# ============================================================================
# AUTO TRADE ENGINE - Core of Automated Trading
# ============================================================================
class TradeTracker:
    """Tracks trade results and calculates Sharpe Ratio for competition tiebreakers"""
    def __init__(self):
        self.returns = [] # List of PnL percentages per trade
        self.lock = threading.Lock()
    
    def add_trade(self, pnl_percent: float):
        with self.lock:
            self.returns.append(pnl_percent)
            logger.info(f"📊 Trade Recorded: {pnl_percent:+.2f}% | Total Trades: {len(self.returns)}")
            self.log_summary()

    def get_sharpe(self) -> float:
        with self.lock:
            if len(self.returns) < 2:
                return 0.0
            mean = statistics.mean(self.returns)
            std = statistics.stdev(self.returns)
            if std == 0:
                return 0.0
            return mean / std

    def log_summary(self):
        with self.lock:
            if not self.returns:
                return
            mean = statistics.mean(self.returns)
            std = statistics.stdev(self.returns) if len(self.returns) > 1 else 0
            sharpe = mean / std if std > 0 else 0
            
            summary = (
                f"\n{'='*40}\n"
                f"🏆 COMPETITION STATS (Tiebreaker Optimization)\n"
                f"{'-'*40}\n"
                f"Count: {len(self.returns)} trades\n"
                f"Mean Return: {mean:+.4f}%\n"
                f"Std Deviation: {std:.4f}%\n"
                f"Estimated Sharpe Ratio: {sharpe:.4f}\n"
                f"{'='*40}"
            )
            logger.info(summary)

class AutoTradeEngine:
    """
    Autonomous trading engine that:
    - Continuously monitors market data
    - Evaluates strategy signals
    - Automatically executes trades based on user parameters
    - Manages position lifecycle (TP/SL)
    - Handles connection stability and retries
    """
    
    def __init__(self, user_id: str, client: Client, config: dict):
        self.user_id = user_id
        self.client = client
        self.config = config
        self.running = False
        self.paused = False
        self.stop_event = Event()
        self.thread: Optional[threading.Thread] = None
        
        # Connection stability
        self.connection_attempts = 0
        self.max_connection_attempts = 5
        self.last_successful_connection = datetime.now()
        
        # Cooldown tracking per symbol
        self.last_trade_time: Dict[str, datetime] = {}
        
        # Signal generation (more aggressive for testing)
        self.strategy_config = {
            'scalping': {'tp': 0.008, 'sl': 0.004, 'rsi_buy': 55, 'rsi_sell': 45}, 
            'short':    {'tp': 0.015, 'sl': 0.008, 'rsi_buy': 60, 'rsi_sell': 40},
            'swing':    {'tp': 0.040, 'sl': 0.020, 'rsi_buy': 65, 'rsi_sell': 35}
        }
        
        # Trading state
        self.open_positions: Set[str] = set()
        self.last_global_trade_time: Optional[datetime] = None  # Tracking for 1 tick per 10s
        self.tracker = TradeTracker() # Performance tracking for Sharpe Ratio
        self.lock = Lock()
        
        logger.info(f"[AUTO] AutoTradeEngine initialized for user {user_id}")
    
    def start(self):
        """Start the auto trading engine"""
        if self.running:
            return {"success": False, "error": "Already running"}
        
        logger.info(f"[AUTO] STARTING TRADING ENGINE for user {self.user_id}")
        self.running = True
        self.paused = False
        self.stop_event.clear()
        self.config['bot_status'] = 'RUNNING'
        
        # Reset daily tracking if new day
        self._check_daily_reset()
        
        # Start background thread
        self.thread = threading.Thread(target=self._trading_loop, daemon=True)
        self.thread.start()
        
        logger.info(f"[AUTO] Trading engine STARTED for user {self.user_id}")
        return {"success": True, "message": "Forced auto trading started"}
    
    def stop(self):
        """Stop the auto trading engine and close all positions"""
        logger.warning(f"[AUTO] STOPPING BOT - Closing all positions for user {self.user_id}")
        
        # First close all open positions
        self._close_all_positions()
        
        # Then stop the engine
        self.running = False
        self.stop_event.set()
        self.config['bot_status'] = 'STOPPED'
        
        logger.info(f"[AUTO] Bot stopped and all positions closed for user {self.user_id}")
        return {"success": True, "message": "Auto trading stopped and positions closed"}
    
    def pause(self):
        """Pause the auto trading engine"""
        self.paused = True
        self.config['bot_status'] = 'PAUSED'
        logger.info(f"[AUTO] Paused auto trading for user {self.user_id}")
        return {"success": True, "message": "Auto trading paused"}
    
    def resume(self):
        """Resume the auto trading engine"""
        self.paused = False
        self.config['bot_status'] = 'RUNNING'
        logger.info(f"[AUTO] Resumed auto trading for user {self.user_id}")
        return {"success": True, "message": "Auto trading resumed"}
    
    def _check_daily_reset(self):
        """Reset daily tracking if it's a new day"""
        today = datetime.now().strftime('%Y-%m-%d')
        if self.config.get('last_trade_date') != today:
            self.config['trades_today'] = 0
            self.config['daily_pnl'] = 0.0
            self.config['last_trade_date'] = today
            
            # Get current balance as start of day balance
            try:
                balance = get_real_futures_balance(self.client)
                self.config['start_of_day_balance'] = balance
            except:
                pass
    
    def _trading_loop(self):
        """Main trading loop - runs continuously with auto-trading logic and connection stability"""
        logger.info(f"[AUTO] Trading loop started for user {self.user_id}")
        self.connection_attempts = 0
        
        while self.running and not self.stop_event.is_set():
            try:
                # LOG LOOP START
                logger.debug(f"LOOP START - user: {self.user_id}, running: {self.running}, paused: {self.paused}, auto_enabled: {self.config.get('auto_trading_enabled')}")

                # Check if paused or auto trading disabled
                if self.paused or not self.config.get('auto_trading_enabled', False):
                    if not self.config.get('auto_trading_enabled', False):
                        logger.info(f"[AUTO] Auto trading disabled for user {self.user_id}")
                    time.sleep(10)  # Follow 10s sleep rule even when disabled
                    continue
                
                # Daily reset check
                self._check_daily_reset()
                
                # Check daily loss limit
                if self._check_daily_loss_limit():
                    logger.warning(f"[AUTO] Daily loss limit reached for user {self.user_id}, pausing...")
                    self.pause()
                    continue
                
                # Sync positions with custom API - with retry logic
                try:
                    self._sync_positions()
                    self.connection_attempts = 0  # Reset on success
                    self.last_successful_connection = datetime.now()
                except Exception as sync_error:
                    logger.warning(f"[AUTO] Position sync failed: {sync_error}")
                    self.connection_attempts += 1
                    if self.connection_attempts >= self.max_connection_attempts:
                        logger.error(f"[AUTO] Too many connection failures, stopping bot")
                        self.stop()
                        break
                    time.sleep(3)  # Short delay before retry
                    continue
                
                # NEW: Apply cash decay simulation
                self._apply_cash_decay()
                
                # Evaluate signals for each symbol
                symbols = self.config.get('trading_symbols', ['BTCUSDT'])
                
                # NEW: Active Monitoring of open positions for Auto-Exit (TP/SL fallback)
                self._monitor_and_close_positions()
                
                for symbol in symbols:
                    # Add small jitter to avoid bursty requests
                    time.sleep(random.uniform(0.1, 0.5))
                    
                    if not self.running or self.stop_event.is_set():
                        break
                    
                    if not self.config.get('auto_trading_enabled', False):
                        break
                    
                    # Enhanced risk management before trading
                    if not self._pass_risk_checks(symbol):
                        continue
                    
                    self._evaluate_and_trade(symbol)
                
                self.config['last_signal_check'] = datetime.now().isoformat()
                
                # Wait for next check interval - STRICT 10s per guide
                interval = self.config.get('signal_check_interval', 10) 
                self.stop_event.wait(timeout=interval)
                
            except Exception as e:
                logger.error(f"[AUTO] Critical error in trading loop: {e}")
                
                # Attempt recovery
                self.connection_attempts += 1
                if self.connection_attempts >= self.max_connection_attempts:
                    logger.error(f"[AUTO] Maximum connection attempts reached, stopping bot")
                    self.stop()
                    break
                
                time.sleep(5)  # Longer delay for critical errors
        
        logger.info(f"[AUTO] Trading loop ended for user {self.user_id}")
    
    def _check_daily_loss_limit(self) -> bool:
        """Check if daily loss limit has been reached"""
        start_balance = self.config.get('start_of_day_balance', 0)
        if start_balance <= 0 or self.config.get('bot_status') != 'RUNNING':
            return False
        
        limit_percent = self.config.get('daily_loss_limit_percent', 10.0) # Increased to 10% for volatility
        max_loss = start_balance * (limit_percent / 100)
        
        try:
            balance_info = get_futures_balance_detailed(self.client)
            wallet_balance = balance_info.get('wallet_balance', 0)
            
            # API PROTECTION: If balance is 0, it's likely an API timeout/error, not a loss.
            # Skip the check if balance is 0 to prevent false-positive pausing.
            if wallet_balance == 0:
                logger.debug("Skipping daily loss check: API returned $0 balance (likely timeout)")
                return False

            daily_pnl = wallet_balance - start_balance
            self.config['daily_pnl'] = daily_pnl
        except Exception as e:
            logger.warning(f"Error in daily loss check: {e}")
            daily_pnl = self.config.get('daily_pnl', 0)
        
        is_limit_reached = daily_pnl < -max_loss
        if is_limit_reached:
             logger.warning(f"Daily loss limit reached: PnL ${daily_pnl:.2f} < -${max_loss:.2f}")
        return is_limit_reached
    
    def _monitor_and_close_positions(self):
        """Actively monitor open positions and close them if TP/SL targets hit (Fallback)"""
        try:
            # Get real positions
            positions = get_real_positions(self.client)
            tp_percent = float(self.config.get('take_profit_percent', 1.0))
            sl_percent = float(self.config.get('stop_loss_percent', 0.5))
            
            for pos in positions:
                symbol = pos['token']
                side = pos['side']
                entry_price = float(pos['entry_price'])
                current_price = float(pos['current_price'])
                pnl_percent = float(pos['pnl_percent'])
                
                # Check if target reached
                should_close = False
                reason = ""
                
                if pnl_percent >= tp_percent:
                    should_close = True
                    reason = f"TP HIT (Manual Monitor): {pnl_percent:.2f}% >= {tp_percent}%"
                elif pnl_percent <= (sl_percent * -1):
                    should_close = True
                    reason = f"SL HIT (Manual Monitor): {pnl_percent:.2f}% <= -{sl_percent}%"
                
                # DEBUG LOG FOR EVERY CHECK (Optional, but good for troubleshooting)
                # with open("bot_monitor.log", "a") as f:
                #    f.write(f"[{datetime.now().isoformat()}] {symbol}: PnL {pnl_percent:.2f}%, TP {tp_percent}%, SL {sl_percent}%\n")

                if should_close:
                    logger.warning(f"[AUTO] {reason} for {symbol}. Executing Force Close...")
                    
                    try:
                        close_res = close_position(self.client, symbol)
                        if close_res.get('success'):
                            logger.info(f"[AUTO] Auto-Exit Successful for {symbol}")
                            # RECORD TRADE FOR SHARPE CALCULATION
                            self.tracker.add_trade(pnl_percent)
                        else:
                            error_text = close_res.get('error', 'Unknown error')
                            logger.error(f"[AUTO] Auto-Exit Failed for {symbol}: {error_text}")
                    except Exception as close_ex:
                        logger.error(f"CRITICAL ERROR EXITING {symbol}: {str(close_ex)}")
                        
        except Exception as e:
            logger.error(f"Error in manual monitor: {e}")

    def _sync_positions(self):
        """Sync open positions with custom API with retry logic"""
        try:
            position_info = self.client.futures_position_information()
            self.open_positions.clear()
            
            position_count = 0
            # Note: client adapter mocks a position named 'BTCUSDT' based on 'shares' field
            for pos in position_info:
                if not isinstance(pos, dict):
                    continue
                pos_amt = float(pos.get('positionAmt', 0))
                if pos_amt != 0:
                    symbol = pos.get('symbol', 'BTCUSDT')
                    self.open_positions.add(symbol)
                    position_count += 1
            
            if position_count > 0:
                logger.info(f"[AUTO] Synced holdings ({position_count} active)")
            
        except Exception as e:
            if 'Timestamp for this request' in str(e) or 'recvWindow' in str(e):
                logger.warning(f"[AUTO] Timestamp sync issue, will retry: {e}")
                raise  # Re-raise for retry logic
            else:
                logger.error(f"[AUTO] API error syncing positions: {e}")
                raise
    
    def _close_all_positions(self):
        """Close all open positions immediately"""
        try:
            logger.warning("🚨 CLOSING ALL POSITIONS...")
            
            # Get all positions
            positions = self.client.futures_position_information()
            closed_count = 0
            
            for pos in positions:
                symbol = pos['symbol']
                position_amt = float(pos.get('positionAmt', 0))
                
                if position_amt != 0:  # Position is open
                    # Determine close side
                    side = "SELL" if position_amt > 0 else "BUY"
                    quantity = abs(position_amt)
                    
                    logger.info(f"Closing {symbol}: {side} {quantity}")
                    
                    try:
                        # Cancel any open orders for this symbol first
                        try:
                            self.client.futures_cancel_all_open_orders(symbol=symbol)
                        except:
                            pass  # Continue even if cancel fails
                        
                        # Place market order to close position
                        order = self.client.futures_create_order(
                            symbol=symbol,
                            side=side,
                            type="MARKET",
                            quantity=quantity
                        )
                        
                        closed_count += 1
                        logger.info(f"✅ Closed {symbol} position")
                        
                    except Exception as e:
                        logger.error(f"Failed to close {symbol}: {e}")
            
            logger.info(f"Total positions closed: {closed_count}")
            self.open_positions.clear()
            
        except Exception as e:
            logger.error(f"Error closing positions: {e}")
    
    def _has_open_position(self, symbol: str, signal: str) -> bool:
        """Check if there's already an open position for this symbol and signal direction"""
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for pos in positions:
                if pos['symbol'] == symbol:
                    position_amt = float(pos.get('positionAmt', 0))
                    if position_amt != 0:
                        # Check if position direction matches signal
                        if (signal == "BUY" and position_amt > 0) or (signal == "SELL" and position_amt < 0):
                            return True
            return False
        except Exception as e:
            logger.error(f"Error checking position for {symbol}: {e}")
            return False
    
    def _pass_risk_checks(self, symbol: str) -> bool:
        """Enhanced risk management checks before trading"""
        try:
            # 1. Check Global Trade Tick Rate (1 trade per 10s)
            tick_rate = self.config.get('tick_rate_seconds', 10)
            if self.last_global_trade_time:
                elapsed = (datetime.now() - self.last_global_trade_time).total_seconds()
                if elapsed < tick_rate:
                    # Not really an error, just waiting for the next tick
                    return False

            # 2. Check minimum account balance
            balance_info = get_futures_balance_detailed(self.client)
            current_balance = balance_info['wallet_balance']

            if 'min_account_balance' in self.config:
                if current_balance < self.config['min_account_balance']:
                    logger.warning(f"Below minimum balance: ${current_balance:.2f} < ${self.config['min_account_balance']:.2f}")
                    return False
            
            # 3. Check Capital Exposure (Max 60% capital)
            max_exposure_ratio = self.config.get('max_capital_exposure_ratio', 0.6)
            # Calculate total margin of all open positions
            try:
                positions = self.client.futures_position_information()
                total_initial_margin = 0.0
                for pos in positions:
                    pos_amt = float(pos.get('positionAmt', 0))
                    if pos_amt != 0:
                        entry_price = float(pos.get('entryPrice', 0))
                        leverage = int(pos.get('leverage', 10))
                        # Initial Margin = (Quantity * Entry Price) / Leverage
                        margin = (abs(pos_amt) * entry_price) / leverage
                        total_initial_margin += margin
                
                exposure_ratio = total_initial_margin / current_balance if current_balance > 0 else 0
                if exposure_ratio >= max_exposure_ratio:
                    logger.warning(f"Max capital exposure reached: {exposure_ratio:.2f} >= {max_exposure_ratio:.2f}")
                    return False
            except Exception as e:
                logger.error(f"Error calculating exposure: {e}")

            # 4. Check drawdown limit
            if 'max_drawdown_percent' in self.config and self.config.get('start_of_day_balance', 0) > 0:
                start_balance = self.config['start_of_day_balance']
                drawdown = ((start_balance - current_balance) / start_balance) * 100
                
                if drawdown >= self.config['max_drawdown_percent']:
                    logger.warning(f"Max drawdown reached: {drawdown:.2f}% >= {self.config['max_drawdown_percent']:.2f}%")
                    return False
            
            # 5. Check if we already have max positions
            max_positions = self.config.get('max_concurrent_positions', 20)
            current_positions = len(self.open_positions)
            if current_positions >= max_positions:
                return False
            
            # 6. Check one position per symbol
            if self.config.get('one_position_per_symbol', True) and symbol in self.open_positions:
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error in risk checks: {e}")
            return False
    
    def _apply_cash_decay(self):
        """Apply cash decay simulation over time"""
        try:
            now = datetime.now()
            last_decay_str = self.config.get('last_decay_time')
            
            if not last_decay_str:
                self.config['last_decay_time'] = now.isoformat()
                return

            last_decay = datetime.fromisoformat(last_decay_str)
            elapsed_minutes = (now - last_decay).total_seconds() / 60
            
            if elapsed_minutes < 1:
                return

            decay_rate = self.config.get('cash_decay_rate_per_minute', 0.0002)
            # 0.02% per minute decay on cash balance
            
            if 'total_decay_applied' not in self.config:
                self.config['total_decay_applied'] = 0.0
                
            decay_amount = self.config.get('start_of_day_balance', 0) * decay_rate * elapsed_minutes
            self.config['total_decay_applied'] += decay_amount
            self.config['last_decay_time'] = now.isoformat()
            
            if decay_amount > 0:
                logger.debug(f"Applied cash decay: ${decay_amount:.6f}")
        except Exception as e:
            logger.error(f"Error applying decay: {e}")

    def _can_trade(self, symbol: str) -> tuple:
        """Check if we can trade this symbol. Returns (can_trade, reason)"""
        # Check max positions
        max_positions = self.config.get('max_concurrent_positions', 20)
        current_positions = len(self.open_positions)
        logger.debug(f"Current positions: {current_positions}, Max positions: {max_positions}")
        if current_positions >= max_positions:
            return False, f"Max positions reached ({current_positions}/{max_positions})"
        
        # Check one position per symbol - DISABLED for user-selected tokens to allow multiple strategies
        # if self.config.get('one_position_per_symbol', True):
        #     if symbol in self.open_positions:
        #         return False, f"Already have position in {symbol}"
        
        # Check daily trade limit - REMOVED for unlimited trading
        # max_trades = self.config.get('max_trades_per_day', 999999)
        # trades_today = self.config.get('trades_today', 0)
        # print(f"[DEBUG] Trades today: {trades_today}, Max trades: {max_trades}")
        # if trades_today >= max_trades:
        #     return False, f"Daily trade limit reached ({trades_today}/{max_trades})"
        
        # Check cooldown
        cooldown = self.config.get('cooldown_seconds', 10)
        last_trade = self.last_trade_time.get(symbol)
        if last_trade:
            elapsed = (datetime.now() - last_trade).total_seconds()
            logger.debug(f"Cooldown check - Elapsed: {elapsed:.1f}s, Required: {cooldown}s")
            if elapsed < cooldown:
                return False, f"Cooldown active ({int(cooldown - elapsed)}s remaining)"
        
        logger.debug(f"All checks passed for {symbol}")
        return True, "OK"
    
    def _fetch_market_data(self, symbol: str) -> dict:
        """Fetch current market data from Asset Alpha Exchange"""
        try:
            # URLs for Asset Alpha Exchange
            t_url = f"{API_URL}/api/price"
            k_url = f"{API_URL}/api/history"
            
            # Fetch Price
            t_resp, t_err = fetch_with_retry("GET", t_url)
            if not t_resp:
                return None
            t_res = t_resp.json()
            
            # Fetch History (Klines)
            k_resp, k_err = fetch_with_retry("GET", k_url)
            if not k_resp:
                return None
            k_res = k_resp.json()

            if 'price' not in t_res or not isinstance(k_res, list):
                return None
            
            price = float(t_res['price'])
            change = 0.0 # Asset Alpha Exchange doesn't provide 24h change in /api/price
            
            # Extract closes from history dict list
            closes = [float(tick.get('close', tick.get('price', 0))) for tick in k_res[-14:]]
            
            rsi = self._calculate_rsi(closes)
            
            result = {
                'price': price,
                'change': change,
                'rsi': rsi,
                'closes': closes
            }
            
            # LOG MARKET DATA SUMMARY
            logger.info(f"[AUTO] Market Data for {symbol}: Price=${price:.2f} | RSI={rsi:.2f} | Closes={closes}")
            
            return result
        except Exception as e:
            logger.error(f"[AUTO] Error fetching data for {symbol}: {e}")
            return None
    
    def _calculate_rsi(self, prices: list) -> float:
        """Calculate RSI from price list"""
        if len(prices) < 10:
            return 50
        
        deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        gain = sum([d for d in deltas if d > 0]) / len(deltas)
        loss = abs(sum([d for d in deltas if d < 0])) / len(deltas)
        
        if loss == 0:
            return 100
        
        rs = gain / loss
        return round(100 - (100 / (1 + rs)), 2)
    
    def _generate_signal(self, data: dict) -> str:
        """Generate trading signal based on strategy"""
        strategy = self.config.get('strategy', 'scalping')
        conf = self.strategy_config.get(strategy, self.strategy_config['scalping'])
        
        rsi = data['rsi']
        change = data['change']
        closes = data['closes']
        
        avg_price = sum(closes[-3:]) / 3
        last_price = closes[-1]
        
        # Signal generation logic - MORE AGGRESSIVE
        logger.debug(f"RSI: {rsi}, Change: {change:.2f}%, Avg Price: {avg_price:.2f}, Last Price: {last_price:.2f}")
        logger.debug(f"RSI Buy Threshold: {conf['rsi_buy']}, RSI Sell Threshold: {conf['rsi_sell']}")
        
        if rsi <= conf['rsi_buy'] or (last_price > avg_price and change > 0.05):
            logger.debug(f"BUY signal triggered - RSI: {rsi} <= {conf['rsi_buy']} OR momentum up {change:.2f}%")
            return "BUY"
        elif rsi >= conf['rsi_sell'] or (last_price < avg_price and change < -0.05):
            logger.debug(f"SELL signal triggered - RSI: {rsi} >= {conf['rsi_sell']} OR momentum down {change:.2f}%")
            return "SELL"
        else:
            logger.debug(f"HOLD signal - RSI: {rsi} (between {conf['rsi_sell']} and {conf['rsi_buy']})") 
            return "HOLD"  # FIX: Added missing return statement
    
    def _evaluate_and_trade(self, symbol: str):
        """Evaluate signal and execute trade for all selected tokens"""
        if not symbol.endswith('USDT'):
            symbol = f"{symbol}USDT"
        symbol = symbol.upper()
        
        logger.info(f"🔍 Evaluating {symbol} for trading opportunity")
            
        # Check if we can trade this symbol
        can_trade, reason = self._can_trade(symbol)
        if not can_trade:
            logger.warning(f"❌ Cannot trade {symbol}: {reason}")
            return
        
        # Check enhanced risk management for all tokens
        if not self._pass_risk_checks(symbol):
            logger.warning(f"❌ Risk check failed for {symbol}")
            return
        
        # Fetch market data - USE GLOBAL SIGNALS FOR CONSISTENCY WITH UI
        symbol_key = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
        data = None
        
        # Access the global signal bot's data
        if symbol_key in signal_bot.latest_data:
            global_data = signal_bot.latest_data[symbol_key]
            # Convert SignalBot data format to AutoTradeEngine data format
            data = {
                'price': global_data['price'],
                'change': global_data['change'],
                'rsi': global_data['rsi'],
                'closes': [global_data['price']] * 14 # Fallback for momentum check
            }
            logger.info(f"📡 Using global signal data for {symbol_key}")
        else:
            # Fallback to manual fetch if not in global signals
            data = self._fetch_market_data(symbol)
            logger.info(f"🔄 Fetched fresh data for {symbol}")
            
        if not data:
            logger.error(f"❌ No market data for {symbol}")
            return
        
        # Generate signal
        signal = self._generate_signal(data)
        
        # LOG TO FILE FOR DEBUGGING
        logger.info(f"📊 Signal: {symbol} = {signal} (RSI: {data['rsi']:.2f})")
        
        # Handle HOLD signals
        if signal == "HOLD":
            return
        
        # Guide Rule Check: No short selling (can only sell shares you already hold)
        if signal == "SELL" and symbol not in self.open_positions:
            logger.warning(f"⚠️ Skipping SELL signal for {symbol}: Short selling not permitted.")
            return
        
        # Execute trade for all user-selected tokens
        logger.info(f"💰 EXECUTING: {signal} {symbol} @ ${data['price']:.2f}")
        if signal == "BUY":
            logger.info(f"ℹ️ Note: Buying will reduce 'Cash' balance as funds move to 'Shares' holdings.")
            
        result = self._execute_auto_trade(symbol, signal, data['price'])
        
        if result.get('success'):
            # Update tracking
            self.last_global_trade_time = datetime.now()  # Store global trade tick
            self.config['trades_today'] = self.config.get('trades_today', 0) + 1
            self.last_trade_time[symbol] = datetime.now()
            self.open_positions.add(symbol)
            
            # Apply 0.1% transaction fee to tracking
            fee_percent = self.config.get('transaction_fee_percent', 0.1)
            trade_value = data['price'] * result['position']['quantity']
            fee_amount = trade_value * (fee_percent / 100)
            result['position']['fee_paid'] = fee_amount
            
            # Record trade
            self.config['last_trade'] = {
                'symbol': symbol,
                'side': signal,
                'price': data['price'],
                'fee': fee_amount,
                'time': datetime.now().isoformat(),
                'result': result
            }
            
            logger.info(f"✅ SUCCESS: {signal} {symbol} @ ${data['price']:.2f} | Fee: ${fee_amount:.4f}")
        else:
            error_msg = result.get('error', 'Unknown error')
            logger.error(f"❌ FAILED: {symbol} - {error_msg}")
    
    def _execute_auto_trade(self, symbol: str, side: str, current_price: float) -> dict:
        """Execute an automated trade with all configured parameters"""
        logger.info(f"_execute_auto_trade ENTERED for {symbol} {side}")
        try:
            # Get config values
            leverage = self.config.get('leverage', 10)
            trade_size_percent = self.config.get('trade_size_percent', 5.0)
            sl_percent = self.config.get('stop_loss_percent', 1.0)
            tp_percent = self.config.get('take_profit_percent', 2.0)
            margin_type = self.config.get('margin_type', 'ISOLATED')
            
            # Set margin type - SKIP if there are existing orders
            try:
                # Check if there are any open orders first
                open_orders = self.client.futures_get_open_orders(symbol=symbol)
                if open_orders:
                    logger.info(f"Skipping margin type change - {len(open_orders)} open orders exist")
                else:
                    # Only set margin type if no open orders
                    self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            except Exception as e:
                if 'No need to change' in str(e):
                    pass
                elif 'open orders' in str(e).lower():
                    logger.warning(f"Cannot change margin type due to open orders. Continuing...")
                else:
                    return {"success": False, "error": f"Margin type error: {str(e)}"}
            
            # Set leverage - SKIP if there are existing orders
            try:
                # Check if there are any open orders first
                open_orders = self.client.futures_get_open_orders(symbol=symbol)
                if open_orders:
                    logger.info("Skipping leverage change - Open orders exist")
                else:
                    # Only set leverage if no open orders
                    self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            except Exception as e:
                if 'No need to change' in str(e):
                    pass
                elif 'open orders' in str(e).lower():
                     logger.warning("Cannot change leverage due to open orders. Continuing...")
                else:
                    logger.error(f"Leverage error: {str(e)}")
                    return {"success": False, "error": f"Leverage error: {str(e)}"}
        

        except Exception as e:
            logger.warning(f"Setup warning: {e}")

        # Get available balance
        balance_info = get_futures_balance_detailed(self.client)
        available_balance = balance_info.get('available_balance', 0)
        
        logger.info(f"Balance: {available_balance}, Trade Size: {trade_size_percent}%")
        
        if available_balance <= 0:
            return {"success": False, "error": "Insufficient balance"}
            
        # Calculate position size
        trade_amount = available_balance * (trade_size_percent / 100)
        notional_value = trade_amount * leverage
        quantity = notional_value / current_price
        
        # Get symbol precision for price rounding and min_qty check
        precision = get_symbol_precision(self.client, symbol)
        
        # Guide Rule Check: No fractional shares (Quantity must be a positive whole number)
        quantity = math.floor(quantity)
        
        # Validate quantity
        if quantity < precision['min_qty']:
            # Fallback for very small accounts or high prices
            logger.warning(f"Quantity {quantity} below minimum {precision['min_qty']}, forcing min_qty 1")
            quantity = max(1, int(precision['min_qty']))
        
        # Validate margin requirement
        # Check if quantity is zero after floor
        if quantity <= 0:
            return {"success": False, "error": "Quantity rounded to zero after floor (Fractional shares not permitted)"}

        required_margin = (quantity * current_price) / leverage
        if required_margin > available_balance:
            return {"success": False, "error": f"Insufficient margin. Required: ${required_margin:.2f}, Available: ${available_balance:.2f}"}
        
        # Calculate TP/SL prices based on ROE% (Leverage adjusted)
        # Target Price Change = Target ROE / Leverage
        # Example: 2% ROE with 10x leverage -> 0.2% Price move
        tp_price_change = (tp_percent / leverage) / 100
        sl_price_change = (sl_percent / leverage) / 100

        if side == 'BUY':
            tp_price = round(current_price * (1 + tp_price_change), precision['price_precision'])
            sl_price = round(current_price * (1 - sl_price_change), precision['price_precision'])
            close_side = 'SELL'
        else:
            tp_price = round(current_price * (1 - tp_price_change), precision['price_precision'])
            sl_price = round(current_price * (1 + sl_price_change), precision['price_precision'])
            close_side = 'BUY'
        
        # Determine position side for hedge mode accounts
        position_side = "BOTH"
        try:
            position_mode = self.client.futures_get_position_mode()
            if position_mode.get("dualSidePosition"):
                position_side = "LONG" if side == "BUY" else "SHORT"
        except Exception:
            pass

        logger.info(f"Executing {side}: {quantity} {symbol} @ ~${current_price}")
        logger.info(f"  TP: ${tp_price} | SL: ${sl_price} | Leverage: {leverage}x | Side: {position_side}")
        
        # Execute MARKET order
        order_params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity
        }
        if position_side != "BOTH":
            order_params["positionSide"] = position_side
            
        logger.info(f"Placing Order: {order_params}")
             
        order = self.client.futures_create_order(**order_params)
        
        logger.info(f"Order Success: {order.get('orderId')}")
             
        order_id = order.get('orderId', 'unknown')
        fill_price = float(order.get('avgPrice', current_price)) or current_price
        
        tp_order = None
        sl_order = None

        # API-based TP/SL orders removed to avoid 'scalping' issue on simplified API. 
        # The bot manages exits manually in _monitor_and_close_positions via price checking.
        logger.info(f"Manual TP/SL tracking enabled: TP @ ${tp_price} | SL @ ${sl_price}")
        
        return {
            "success": True,
            "message": f"Position opened: {side} {quantity} {symbol}",
            "position": {
                "symbol": symbol,
                "side": "LONG" if side == "BUY" else "SHORT",
                "entry_price": fill_price,
                "quantity": quantity,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "leverage": leverage,
                "order_id": order_id,
                "tp_order": tp_order,
                "sl_order": sl_order
            }
        }

            



# ============================================================================
# Helper Functions
# ============================================================================

def get_symbol_precision(client: Client, symbol: str) -> dict:
    """Get symbol precision info for correct order sizing"""
    global symbol_precision_cache
    
    with precision_lock:
        if symbol in symbol_precision_cache:
            return symbol_precision_cache[symbol]
    
    try:
        exchange_info = client.futures_exchange_info()
        symbols = []
        if isinstance(exchange_info, dict):
            symbols = exchange_info.get('symbols', [])
        elif isinstance(exchange_info, list):
            symbols = exchange_info
            
        for s in symbols:
            if not isinstance(s, dict): continue
            if s.get('symbol') == symbol:
                precision_info = {
                    'price_precision': s.get('pricePrecision', 2),
                    'quantity_precision': s.get('quantityPrecision', 0),
                    'min_qty': 1.0,
                    'step_size': 1.0
                }
                
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        precision_info['min_qty'] = float(f.get('minQty', 1.0))
                        precision_info['step_size'] = float(f.get('stepSize', 1.0))
                
                with precision_lock:
                    symbol_precision_cache[symbol] = precision_info
                
                return precision_info
    except Exception as e:
        logger.error(f"Error getting precision from exchange info for {symbol}: {e}")
    
    # Robust fallbacks for Asset Alpha Exchange
    fallback_info = {
        'price_precision': 4,
        'quantity_precision': 0,
        'min_qty': 1.0,
        'step_size': 1.0
    }
    with precision_lock:
        symbol_precision_cache[symbol] = fallback_info
    return fallback_info
    
    return {'price_precision': 2, 'quantity_precision': 3, 'min_qty': 0.001, 'step_size': 0.001}


def round_step_size(quantity: float, step_size: float) -> float:
    """Round quantity to valid step size"""
    precision = int(round(-math.log10(step_size)))
    return round(quantity - (quantity % step_size), precision)


def get_user_client(user_id: str) -> Optional[Client]:
    """Get API client for a user"""
    with clients_lock:
        return user_futures_clients.get(user_id)


def get_real_futures_balance(client: Client) -> float:
    """Get real USDT balance from Futures"""
    try:
        account_balance = client.futures_account_balance()
        assets = account_balance if isinstance(account_balance, list) else []
        
        for asset in assets:
            if asset.get('asset') == 'USDT':
                return float(asset.get('balance', asset.get('walletBalance', 0)))
        
        # Fallback to futures_account if balance endpoint isn't helpful
        acc = client.futures_account()
        if isinstance(acc, dict):
            return float(acc.get('totalWalletBalance', acc.get('cash', 0)))
            
        return 0.0
    except Exception as e:
        logger.error(f"Error fetching balance: {e}")
        return 0.0


def get_futures_balance_detailed(client: Client) -> dict:
    """Get detailed balance info from Futures"""
    try:
        account_balance = client.futures_account_balance()
        assets = account_balance if isinstance(account_balance, list) else []
        
        for asset in assets:
            if asset.get('asset') == 'USDT':
                # Prefer net_worth/totalBalance for wallet_balance
                return {
                    'wallet_balance': float(asset.get('walletBalance', asset.get('balance', 0))),
                    'available_balance': float(asset.get('availableBalance', asset.get('withdrawAvailable', 0))),
                    'cross_wallet_balance': float(asset.get('walletBalance', asset.get('balance', 0)))
                }
        
        # Fallback to futures_account
        acc = client.futures_account()
        if isinstance(acc, dict):
             # Simplified API mapping
             total = float(acc.get('net_worth', acc.get('totalWalletBalance', acc.get('cash', 0))))
             cash = float(acc.get('cash', 0))
             return {
                'wallet_balance': total,
                'available_balance': cash,
                'cross_wallet_balance': total
             }
        return {'wallet_balance': 0, 'available_balance': 0, 'cross_wallet_balance': 0}
    except Exception as e:
        logger.error(f"Error getting detailed balance: {e}")
        return {'wallet_balance': 0, 'available_balance': 0, 'cross_wallet_balance': 0}


def get_real_positions(client: Client) -> List[dict]:
    """Get real open positions from Futures"""
    positions = []
    try:
        # Also get open orders for TP/SL info
        open_orders = {}
        try:
            orders = client.futures_get_open_orders()
            for order in orders:
                symbol = order['symbol']
                position_side = order.get('positionSide', 'BOTH')
                key = f"{symbol}:{position_side}"
                if key not in open_orders:
                    open_orders[key] = {'tp': None, 'sl': None}
                
                order_type = order.get('type')
                if order_type in ('TAKE_PROFIT_MARKET', 'TAKE_PROFIT'):
                    open_orders[key]['tp'] = float(order['stopPrice'])
                elif order_type in ('STOP_MARKET', 'STOP'):
                    open_orders[key]['sl'] = float(order['stopPrice'])
        except:
            pass
        
        position_info = client.futures_position_information()
        
        logger.debug(f"Fetched {len(position_info)} positions raw")
        
        for pos in position_info:
            pos_amt = float(pos.get('positionAmt', 0))
            if pos_amt == 0:
                continue
            
            symbol = pos.get('symbol', 'UNKNOWN')
            position_side = pos.get('positionSide', 'BOTH')
            entry_price = float(pos.get('entryPrice', 0))
            mark_price = float(pos.get('markPrice', 0))
            unrealized_pnl = float(pos.get('unRealizedProfit', 0))
            leverage = int(pos.get('leverage', 10))
            liq_price = float(pos.get('liquidationPrice', 0)) if pos.get('liquidationPrice') else 0.0
            
            side = 'LONG' if pos_amt > 0 else 'SHORT'
            quantity = abs(pos_amt)
            
            # Calculate ROE% (Return on Equity) - This is what users usually expect
            # ROE = (Unrealized PnL / Initial Margin) * 100
            # Initial Margin = (Quantity * Entry Price) / Leverage
            initial_margin = (quantity * entry_price) / leverage if leverage > 0 else (quantity * entry_price)
            pnl_percent = (unrealized_pnl / initial_margin * 100) if initial_margin > 0 else 0
            
            # Get TP/SL from open orders
            order_key = f"{symbol}:{position_side}"
            tp_price = open_orders.get(order_key, {}).get('tp', '---')
            sl_price = open_orders.get(order_key, {}).get('sl', '---')
            
            positions.append({
                'token': symbol,
                'current_price': mark_price,
                'entry_price': entry_price,
                'side': side,
                'quantity': quantity,
                'tp_price': tp_price if tp_price else '---',
                'sl_price': sl_price if sl_price else '---',
                'liq_price': liq_price,
                'pnl': unrealized_pnl,
                'pnl_percent': pnl_percent, # This is now ROE%
                'price_change_percent': (unrealized_pnl / (quantity * entry_price) * 100) if (quantity * entry_price) > 0 else 0,
                'leverage': leverage,
                'is_real': True
            })
            
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
    
    return positions


def close_position(client: Client, symbol: str) -> dict:
    """Close a specific position by symbol"""
    try:
        if not symbol:
            return {"success": False, "error": "No symbol provided"}
            
        symbol = symbol.strip().upper()
        if not symbol.endswith('USDT'):
            symbol = symbol + 'USDT'
        
        # Log attempt
        logger.info(f"MANUAL CLOSE REQUEST: {symbol}")
             
        # Get positions to find the exact quantity and side
        try:
            positions = client.futures_position_information(symbol=symbol)
        except Exception as be:
            return {"success": False, "error": f"API error fetching position: {str(be)}"}
            
        position = None
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            if float(pos.get('positionAmt', 0)) != 0:
                position = pos
                break
        
        if not position:
            return {"success": False, "error": f"No open position found for {symbol}"}
        
        pos_amt = float(position.get('positionAmt', 0))
        quantity = abs(pos_amt)
        close_side = 'SELL' if pos_amt > 0 else 'BUY'
        position_side = position.get('positionSide', 'BOTH')
        
        # Get symbol precision and round quantity precisely
        try:
            precision = get_symbol_precision(client, symbol)
            quantity = round_step_size(quantity, precision['step_size'])
        except Exception as e:
            logger.warning(f"[CLOSE] Precision fetch warning: {e}")

        # Cancel all open orders first to release margin/allow close
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
            logger.info(f"Cancelled all orders for {symbol} before close")
        except Exception as e:
            logger.warning(f"[CLOSE] Cancel orders warning: {e}")
        
        order_params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": quantity,
        }
        
        # Determine if we use positionSide (Hedge Mode) or reduceOnly (One-way)
        if position_side and position_side != "BOTH":
            order_params["positionSide"] = position_side
        else:
            order_params["reduceOnly"] = True
            
        logger.info(f"Creating Close Order: {order_params}")
             
        try:
            order = client.futures_create_order(**order_params)
            
            # Calculate fee
            fee_percent = DEFAULT_CONFIG.get('transaction_fee_percent', 0.1)
            # We use current price as a fallback if fill price isn't in order (Mock client)
            fill_price = float(order.get('avgPrice', 0)) or 0
            trade_value = float(quantity) * fill_price
            fee_amount = trade_value * (fee_percent / 100)

            logger.info(f"MANUAL CLOSE SUCCESS: {symbol} Qty: {quantity} OrderId: {order.get('orderId')} | Fee: ${fee_amount:.4f}")
                 
            return {
                "success": True, 
                "message": f"Position closed for {symbol}", 
                "order_id": order.get('orderId', 'unknown'),
                "fee_paid": fee_amount
            }
        except Exception as be:
            error_msg = f"API Error: {str(be)}"
            logger.error(f"TRADE CLOSE FAILED: {error_msg}")
            return {"success": False, "error": error_msg}
        except Exception as e:
            error_msg = f"Unexpected Error during order: {str(e)}"
            logger.error(f"TRADE CLOSE FAILED: {error_msg}")
            return {"success": False, "error": error_msg}

        
    except Exception as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def close_all_positions(client: Client) -> dict:
    """Close all open positions"""
    try:
        positions = client.futures_position_information()
        closed = []
        errors = []
        
        for pos in positions:
            if float(pos['positionAmt']) == 0:
                continue
            
            result = close_position(client, pos['symbol'])
            if result['success']:
                closed.append(pos['symbol'])
            else:
                errors.append(f"{pos['symbol']}: {result['error']}")
        
        return {
            "success": len(errors) == 0,
            "closed": closed,
            "errors": errors,
            "message": f"Closed {len(closed)} positions"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def normalize_symbol(symbol: str) -> str:
    if not symbol:
        return ''
    symbol = symbol.upper()
    if not symbol.endswith('USDT'):
        symbol = f"{symbol}USDT"
    return symbol


# ============================================================================
# Signal Generator Bot (for data updates)
# ============================================================================
class SignalBot:
    def __init__(self):
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
        self.latest_data = {}
        self.last_update = datetime.now()
        self.lock = Lock()
        
        self.strategy_config = {
            'scalping': {'tp': 0.008, 'sl': 0.004, 'rsi_buy': 48, 'rsi_sell': 52}, 
            'short':    {'tp': 0.015, 'sl': 0.008, 'rsi_buy': 45, 'rsi_sell': 55},
            'swing':    {'tp': 0.040, 'sl': 0.020, 'rsi_buy': 40, 'rsi_sell': 60}
        }
    
    def fetch_data(self, symbol: str) -> dict:
        """Fetch market data for SignalBot"""
        try:
            t_url = f"{API_URL}/api/price"
            k_url = f"{API_URL}/api/history"
            
            t_resp, t_err = fetch_with_retry("GET", t_url)
            k_resp, k_err = fetch_with_retry("GET", k_url)
            
            if not t_resp or not k_resp:
                return None
                
            try:
                t_res = t_resp.json()
                k_res = k_resp.json()
            except requests.exceptions.JSONDecodeError:
                logger.error(f"SignalBot JSON Error ({symbol}): T-RT={t_resp.text[:100]} | K-RT={k_resp.text[:100]}")
                return None

            if 'price' not in t_res or not isinstance(k_res, list):
                return None
            
            price = float(t_res['price'])
            change = 0.0 # Placeholder
            closes = [float(tick.get('close', tick.get('price', 0))) for tick in k_res[-14:]]
            
            rsi = self._calc_rsi(closes)
            signal = self._gen_signal(rsi, change, closes, 'scalping')
            
            tp, sl = "---", "---"
            conf = self.strategy_config['scalping']
            if signal == "BUY":
                tp = f"{price * (1 + conf['tp']):.2f}"
                sl = f"{price * (1 - conf['sl']):.2f}"
            elif signal == "SELL":
                tp = f"{price * (1 - conf['tp']):.2f}"
                sl = f"{price * (1 + conf['sl']):.2f}"
            
            return {
                'symbol': symbol.replace('USDT', ''),
                'price': price,
                'change': change,
                'rsi': rsi,
                'signal': signal,
                'tp': tp,
                'sl': sl,
                'last_updated': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            return None
    
    def _calc_rsi(self, prices):
        if len(prices) < 10:
            return 50
        deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        gain = sum([d for d in deltas if d > 0]) / len(deltas)
        loss = abs(sum([d for d in deltas if d < 0])) / len(deltas)
        if loss == 0:
            return 100
        rs = gain / loss
        return round(100 - (100 / (1 + rs)), 2)
    
    def _gen_signal(self, rsi, change, closes, strategy):
        conf = self.strategy_config[strategy]
        avg = sum(closes[-3:]) / 3
        last = closes[-1]
        
        if rsi <= conf['rsi_buy'] or (last > avg and change > 0.1):
            return "BUY"
        elif rsi >= conf['rsi_sell'] or (last < avg and change < -0.1):
            return "SELL"
        return "HOLD"
    
    def update_all(self):
        """Fetch market data ONCE per tick and update all symbols (single asset competition)"""
        with self.lock:
            # Fetch data once since API is likely single-asset
            data = self.fetch_data("PRIMARY")
            if data:
                # Distribute the same data across all tracked symbols
                for symbol in self.symbols:
                    self.latest_data[symbol] = data
            self.last_update = datetime.now()
            return self.latest_data


# Global signal bot
signal_bot = SignalBot()


def run_signal_updates():
    """Background thread for signal updates"""
    global signal_bot
    logger.info("Signal bot started...")
    while True:
        try:
            signal_bot.update_all()
            logger.info(f"Signals updated successfully")
            time.sleep(10) # 10-second tick sync
        except Exception as e:
            logger.error(f"Signal update error: {e}")
            time.sleep(5)


# Start signal update thread
signal_thread = threading.Thread(target=run_signal_updates, daemon=True)
signal_thread.start()




# ============================================================================
# Main Entry Point - Standalone Agent
# ============================================================================
if __name__ == "__main__":
    logger.info(f"STARTING LR21 Auto Trading Agent - STANDALONE MODE")
    logger.info(f"API_URL: {API_URL}")
    
    # Initialize components
    user_id = "default_agent"
    client = Client(TEAM_API_KEY)
    
    # Check connection
    try:
        portfolio = client.futures_account()
        if not portfolio:
             logger.error("Failed to fetch portfolio. Check your API_URL and TEAM_API_KEY.")
             exit(1)
        logger.info(f"Connected to portfolio. Balance: {portfolio.get('cash', 'unknown')}")
    except Exception as e:
        logger.error(f"Connection check failed: {e}")
        exit(1)

    # Initialize and Start Signal Bot
    # (Thread is already started globally above)
    
    # Initialize and Start Trading Engine
    config = DEFAULT_CONFIG.copy()
    trader = AutoTradeEngine(user_id, client, config)
    
    try:
        # Start trading
        trader.start()
        
        # Keep main thread alive
        while trader.running:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.warning("\nShutdown signal received (Ctrl+C)...")
        trader.stop()
        logger.info("Agent exited cleanly.")
        os._exit(0)
    except Exception as e:
        logger.error(f"Fatal error in agent: {e}")
        if 'trader' in locals():
            trader.stop()
        os._exit(1)
