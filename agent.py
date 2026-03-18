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
import math
from collections import deque
from rich.table import Table
load_dotenv()
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
file_handler = logging.FileHandler("trading_bot.log", encoding='utf-8')
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)
logger = logging.getLogger("TradingBot")
logger.info("SCRIPT STARTING...")
API_URL = os.environ.get('API_URL', os.environ.get('API_URL', '')).rstrip('/')
TEAM_API_KEY = os.environ.get('TEAM_API_KEY', os.environ.get('TEAM_API_KEY', ''))
if not API_URL or not TEAM_API_KEY:
    print("ERROR: API_URL and TEAM_API_KEY are not set in environment or .env")
    exit(1)
http_session = requests.Session()
def fetch_with_retry(method: str, url: str, headers: dict = None, params: dict = None, json_data: dict = None, max_retries: int = 3, base_delay: float = 2.0):
    attempt = 0
    while attempt <= max_retries:
        try:
            response = http_session.request(method, url, headers=headers, params=params, json=json_data, timeout=30)
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
                    delay = 10.0
                    logger.warning(f"Rate limited (429) hit in exception for {url}. Waiting {delay}s")
                    time.sleep(delay)
                    attempt += 1
                    continue
            error_msg = f"HTTP Error: {he}"
            if hasattr(he, 'response') and he.response is not None:
                try:
                    error_msg += f" | Body: {he.response.text}"
                except:
                    pass
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

class Client:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = API_URL
        self.headers = {
            "X-API-Key": TEAM_API_KEY,
            "Content-Type": "application/json"
        }
        self.active_symbol = 'BTCUSDT'                                                
    def _request(self, method: str, path: str, params: dict = None, json_data: dict = None):
        actual_path = path
        if path == "/fapi/v1/order" and method == "POST":
            symbol = (json_data or {}).get('symbol', 'BTCUSDT')
            self.active_symbol = symbol
            side = (json_data or {}).get('side', 'BUY').upper()
            actual_path = "/api/buy" if side == "BUY" else "/api/sell"
            quantity = (json_data or {}).get('quantity', 0)
            json_data = {"quantity": quantity}
        else:
            path_map = {
                "/fapi/v1/account": "/api/portfolio",
                "/fapi/v1/balance": "/api/portfolio",
                "/fapi/v1/positionRisk": "/api/portfolio",
                "/fapi/v1/order": "/api/history",                             
                "manual_buy": "/api/buy",
                "manual_sell": "/api/sell"
            }
            actual_path = path_map.get(path, path)
        url = f"{self.base_url}{actual_path}"
        logger.info(f"[API] {method} {actual_path} | Params: {params} | Body: {json_data}")
        response, error = fetch_with_retry(method, url, headers=self.headers, params=params, json_data=json_data)
        if response:
            try:
                data = response.json()
                if actual_path == "/api/portfolio":
                    if isinstance(data, dict):
                        cash = float(data.get('cash', 0))
                        net_worth = float(data.get('net_worth', cash))
                        shares = float(data.get('shares', 0))
                        data['assets'] = [{'asset': 'USDT', 'walletBalance': net_worth, 'availableBalance': cash}]
                        positions = []
                        if shares > 0:
                            positions.append({
                                'symbol': self.active_symbol, 
                                'positionAmt': str(shares),
                                'entryPrice': '0',
                                'unRealizedProfit': '0'
                            })
                        data['positions'] = positions
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
             return [resp]
        return resp if isinstance(resp, list) else []
    def futures_position_information(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        resp = self._request("GET", "/fapi/v1/positionRisk", params=params)
        if isinstance(resp, dict) and 'positions' in resp:
            return resp['positions']
        if isinstance(resp, list):
            return [r for r in resp if isinstance(r, dict)]
        return []
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
    def get_symbol_ticker(self, symbol: str):
        return self._request("GET", "/api/price", params={"symbol": symbol})
MARKET_DATA_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'X-API-Key': TEAM_API_KEY
}
user_futures_clients: Dict[str, Client] = {}
user_configs: Dict[str, dict] = {}
user_auto_traders: Dict[str, 'AutoTradeEngine'] = {}                              
clients_lock = Lock()
symbol_precision_cache: Dict[str, dict] = {}
precision_lock = Lock()
DEFAULT_CONFIG = {
    'auto_trading_enabled': True,                                 
    'trading_symbols': ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT'],                           
    'trade_size_percent': 5.0,
    'leverage': 5,
    'tp_percent': 1.5,
    'sl_percent': 1.0,
    'margin_type': 'ISOLATED',
    'max_concurrent_positions': 20,                                                         
    'one_position_per_symbol': True,
    'cooldown_seconds': 300,                                                
    'max_trades_per_day': 999999,                            
    'daily_loss_limit_percent': 2.0,                       
    'strategy': 'scalping',
    'signal_check_interval': 10,                                       
    'min_account_balance': 100.0,                         
    'max_drawdown_percent': 10.0,                       
    'tick_rate_seconds': 10,                                   
    'max_capital_exposure_ratio': 0.6,                               
    'transaction_fee_percent': 0.1,                          
    'cash_decay_rate_per_minute': 0.0002,                              
    'trades_today': 0,
    'daily_pnl': 0.0,
    'start_of_day_balance': 0.0,
    'last_trade_date': '',
    'connected_at': '',
    'bot_status': 'STOPPED',
    'last_signal_check': '',
    'last_decay_time': '',                                  
    'last_trade': None,
    'trade_history': []
}
def calculate_rsi(prices: list) -> float:
    if len(prices) < 10:
        return 50
    deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
    gain = sum([d for d in deltas if d > 0]) / len(deltas)
    loss = abs(sum([d for d in deltas if d < 0])) / len(deltas)
    if loss == 0:
        return 100
    rs = gain / loss
    return round(100 - (100 / (1 + rs)), 2)
class TradeTracker:
    def __init__(self):
        self.returns = []                                    
        self.lock = threading.Lock()
    def add_trade(self, pnl_percent: float):
        with self.lock:
            self.returns.append(pnl_percent)
            logger.info(f"Trade Recorded: {pnl_percent:+.2f}% | Total Trades: {len(self.returns)}")
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
                f"COMPETITION STATS (Tiebreaker Optimization)\n"
                f"{'-'*40}\n"
                f"Count: {len(self.returns)} trades\n"
                f"Mean Return: {mean:+.4f}%\n"
                f"Std Deviation: {std:.4f}%\n"
                f"Estimated Sharpe Ratio: {sharpe:.4f}\n"
                f"{'='*40}"
            )
            logger.info(summary)
class AutoTradeEngine:
    def __init__(self, user_id: str, client: Client, config: dict):
        self.user_id = user_id
        self.client = client
        self.config = config
        self.running = False
        self.paused = False
        self.stop_event = Event()
        self.thread: Optional[threading.Thread] = None
        self.connection_attempts = 0
        self.max_connection_attempts = 5
        self.last_successful_connection = datetime.now()
        self.last_trade_time: Dict[str, datetime] = {}
        self.strategy_config = {
            'scalping': {'tp': 4.0, 'sl': 2.0, 'rsi_buy': 40, 'rsi_sell': 60}, 
            'swing':    {'tp': 10.0, 'sl': 5.0, 'rsi_buy': 30, 'rsi_sell': 70}
        }
        self.open_positions: Set[str] = set()
        self.last_global_trade_time: datetime = datetime.now() - timedelta(seconds=10)                        
        self.tracker = TradeTracker()                                        
        self.lock = Lock()
        logger.info(f"[AUTO] AutoTradeEngine initialized for user {user_id}")
    def start(self):
        if self.running:
            return {"success": False, "error": "Already running"}
        logger.info(f"[AUTO] STARTING TRADING ENGINE for user {self.user_id}")
        self.running = True
        self.paused = False
        self.stop_event.clear()
        self.config['bot_status'] = 'RUNNING'
        self._check_daily_reset()
        self.thread = threading.Thread(target=self._trading_loop, daemon=True)
        self.thread.start()
        logger.info(f"[AUTO] Trading engine STARTED for user {self.user_id}")
        return {"success": True, "message": "Forced auto trading started"}
    def stop(self):
        logger.warning(f"[AUTO] STOPPING BOT - Closing all positions for user {self.user_id}")
        self._close_all_positions()
        self.running = False
        self.stop_event.set()
        self.config['bot_status'] = 'STOPPED'
        logger.info(f"[AUTO] Bot stopped and all positions closed for user {self.user_id}")
        return {"success": True, "message": "Auto trading stopped and positions closed"}
    def pause(self):
        self.paused = True
        self.config['bot_status'] = 'PAUSED'
        logger.info(f"[AUTO] Paused auto trading for user {self.user_id}")
        return {"success": True, "message": "Auto trading paused"}
    def resume(self):
        self.paused = False
        self.config['bot_status'] = 'RUNNING'
        logger.info(f"[AUTO] Resumed auto trading for user {self.user_id}")
        return {"success": True, "message": "Auto trading resumed"}
    def _check_daily_reset(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self.config.get('last_trade_date') != today:
            self.config['trades_today'] = 0
            self.config['daily_pnl'] = 0.0
            self.config['last_trade_date'] = today
            try:
                balance = get_real_futures_balance(self.client)
                self.config['start_of_day_balance'] = balance
            except:
                pass
    def _trading_loop(self):
        logger.info(f"[AUTO] Trading loop started for user {self.user_id}")
        self.connection_attempts = 0
        while self.running and not self.stop_event.is_set():
            try:
                logger.debug(f"LOOP START - user: {self.user_id}, running: {self.running}, paused: {self.paused}, auto_enabled: {self.config.get('auto_trading_enabled')}")
                if self.paused or not self.config.get('auto_trading_enabled', False):
                    if not self.config.get('auto_trading_enabled', False):
                        logger.info(f"[AUTO] Auto trading disabled for user {self.user_id}")
                    time.sleep(10)                                            
                    continue
                self._check_daily_reset()
                if self._check_daily_loss_limit():
                    logger.warning(f"[AUTO] Daily loss limit reached for user {self.user_id}, pausing...")
                    self.pause()
                    continue
                try:
                    self._sync_positions()
                    self.connection_attempts = 0                    
                    self.last_successful_connection = datetime.now()
                except Exception as sync_error:
                    logger.warning(f"[AUTO] Position sync failed: {sync_error}")
                    self.connection_attempts += 1
                    if self.connection_attempts >= self.max_connection_attempts:
                        logger.error(f"[AUTO] Too many connection failures, stopping bot")
                        self.stop()
                        break
                    time.sleep(3)                            
                    continue
                self._apply_cash_decay()
                self._monitor_and_close_positions()
                symbols = self.config.get('trading_symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'LTCUSDT', 'ZECUSDT'])
                signals = []
                for symbol in symbols:
                    if not self.running or self.stop_event.is_set():
                        break
                    data = self._get_market_data_for_symbol(symbol)
                    if data:
                        signal = self._generate_signal(data)
                        logger.info(f"[BOT] {symbol} is in {signal} MODE")
                        if signal != "HOLD":
                            signals.append({'symbol': symbol, 'signal': signal, 'data': data})
                if signals:
                    buy_signals = [s for s in signals if s['signal'] == "BUY"]
                    buy_signals.sort(key=lambda x: x['data']['rsi'])
                    for buy_signal in buy_signals:
                        if self._pass_risk_checks(buy_signal['symbol'], buy_signal['data']['price']):
                            self._execute_trade_logic(buy_signal['symbol'], "BUY", buy_signal['data'])
                            break
                    for s in signals:
                        if s['signal'] == "SELL" and s['symbol'] in self.open_positions:
                            self._execute_trade_logic(s['symbol'], "SELL", s['data'])
                self.config['last_signal_check'] = datetime.now().isoformat()
                interval = self.config.get('signal_check_interval', 10) 
                self.stop_event.wait(timeout=interval)
            except Exception as e:
                logger.error(f"[AUTO] Critical error in trading loop: {e}")
                self.connection_attempts += 1
                if self.connection_attempts >= self.max_connection_attempts:
                    logger.error(f"[AUTO] Maximum connection attempts reached, stopping bot")
                    self.stop()
                    break
                time.sleep(5)                                    
        logger.info(f"[AUTO] Trading loop ended for user {self.user_id}")
    def _check_daily_loss_limit(self) -> bool:
        start_balance = self.config.get('start_of_day_balance', 0)
        if start_balance <= 0 or self.config.get('bot_status') != 'RUNNING':
            return False
        limit_percent = self.config.get('daily_loss_limit_percent', 10.0)                                  
        max_loss = start_balance * (limit_percent / 100)
        try:
            balance_info = get_futures_balance_detailed(self.client)
            wallet_balance = balance_info.get('wallet_balance', 0)
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
        try:
            positions = get_real_positions(self.client)
            tp_percent = float(self.config.get('take_profit_percent', 1.0))
            sl_percent = float(self.config.get('stop_loss_percent', 0.5))
            for pos in positions:
                symbol = pos['token']
                side = pos['side']
                entry_price = float(pos['entry_price'])
                current_price = float(pos['current_price'])
                pnl_percent = float(pos['pnl_percent'])
                should_close = False
                reason = ""
                if pnl_percent >= tp_percent:
                    should_close = True
                    reason = f"TP HIT (Manual Monitor): {pnl_percent:.2f}% >= {tp_percent}%"
                elif pnl_percent <= (sl_percent * -1):
                    should_close = True
                    reason = f"SL HIT (Manual Monitor): {pnl_percent:.2f}% <= -{sl_percent}%"
                if should_close:
                    logger.warning(f"[AUTO] {reason} for {symbol}. Executing Force Close...")
                    try:
                        close_res = close_position(self.client, symbol)
                        if close_res.get('success'):
                            logger.info(f"[AUTO] Auto-Exit Successful for {symbol}")
                            self.tracker.add_trade(pnl_percent)
                        else:
                            error_text = close_res.get('error', 'Unknown error')
                            logger.error(f"[AUTO] Auto-Exit Failed for {symbol}: {error_text}")
                    except Exception as close_ex:
                        logger.error(f"CRITICAL ERROR EXITING {symbol}: {str(close_ex)}")
        except Exception as e:
            logger.error(f"Error in manual monitor: {e}")
    def _sync_positions(self):
        try:
            position_info = self.client.futures_position_information()
            self.open_positions.clear()
            position_count = 0
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
                raise                            
            else:
                logger.error(f"[AUTO] API error syncing positions: {e}")
                raise
    def _close_all_positions(self):
        try:
            logger.warning("CLOSING ALL POSITIONS...")
            positions = self.client.futures_position_information()
            closed_count = 0
            for pos in positions:
                symbol = pos['symbol']
                position_amt = float(pos.get('positionAmt', 0))
                if position_amt != 0:                    
                    side = "SELL" if position_amt > 0 else "BUY"
                    quantity = int(abs(position_amt))
                    logger.info(f"Closing {symbol}: {side} {quantity}")
                    try:
                        try:
                            self.client.futures_cancel_all_open_orders(symbol=symbol)
                        except:
                            pass                                 
                        order = self.client.futures_create_order(
                            symbol=symbol,
                            side=side,
                            type="MARKET",
                            quantity=quantity
                        )
                        closed_count += 1
                        logger.info(f"Closed {symbol} position")
                    except Exception as e:
                        logger.error(f"Failed to close {symbol}: {e}")
            logger.info(f"Total positions closed: {closed_count}")
            self.open_positions.clear()
        except Exception as e:
            logger.error(f"Error closing positions: {e}")
    def _has_open_position(self, symbol: str, signal: str) -> bool:
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for pos in positions:
                if pos['symbol'] == symbol:
                    position_amt = float(pos.get('positionAmt', 0))
                    if position_amt != 0:
                        if (signal == "BUY" and position_amt > 0) or (signal == "SELL" and position_amt < 0):
                            return True
            return False
        except Exception as e:
            logger.error(f"Error checking position for {symbol}: {e}")
            return False
    def _pass_risk_checks(self, symbol: str, current_price: float = 0.0) -> bool:
        try:
            tick_rate = self.config.get('tick_rate_seconds', 10)
            if self.last_global_trade_time:
                elapsed = (datetime.now() - self.last_global_trade_time).total_seconds()
                if elapsed < tick_rate:
                    return False
            balance_info = get_futures_balance_detailed(self.client)
            current_balance = balance_info['wallet_balance']
            if 'min_account_balance' in self.config:
                if current_balance < self.config['min_account_balance']:
                    logger.warning(f"Below minimum balance: ${current_balance:.2f} < ${self.config['min_account_balance']:.2f}")
                    return False
            max_exposure_ratio = self.config.get('max_capital_exposure_ratio', 0.6)
            try:
                positions = self.client.futures_position_information()
                total_notional = 0
                for pos in positions: 
                    if isinstance(pos, dict):
                        # Use current_price of the passed symbol if trading the same token, else fetch it
                        asset = pos.get('symbol', '')
                        if asset == symbol and current_price > 0:
                            mark_price = current_price
                        else:
                            try:
                                # Standardize to symbol for /api/price
                                ticker_sym = asset.replace('USDT', '') if 'USDT' in asset else asset
                                d = self.client.get_symbol_ticker(symbol=ticker_sym)
                                mark_price = float(d.get('price', 0)) if isinstance(d, dict) else 0.0
                            except:
                                mark_price = 0.0
                        
                        m_qty = abs(float(pos.get('positionAmt', 0)))
                        total_notional += (m_qty * mark_price)
                
                # Estimate new trade's notional value
                balance_info = get_futures_balance_detailed(self.client)
                available_balance = balance_info['available_balance']
                trade_amount = available_balance * (self.config['trade_size_percent'] / 100)
                new_notional = trade_amount * self.config['leverage']
                
                exposure_vs_net_worth = (total_notional + new_notional) / current_balance if current_balance > 0 else 0
                
                if exposure_vs_net_worth >= 0.59:
                    logger.warning(f"Exposure limit reached: {exposure_vs_net_worth*100:.1f}% >= 59.0% (Limit: 60%)")
                    return False
            except Exception as e:
                logger.error(f"Error calculating exposure: {e}")
            if 'max_drawdown_percent' in self.config and self.config.get('start_of_day_balance', 0) > 0:
                start_balance = self.config['start_of_day_balance']
                drawdown = ((start_balance - current_balance) / start_balance) * 100
                if drawdown >= self.config['max_drawdown_percent']:
                    logger.warning(f"Max drawdown reached: {drawdown:.2f}% >= {self.config['max_drawdown_percent']:.2f}%")
                    return False
            max_positions = self.config.get('max_concurrent_positions', 20)
            current_positions = len(self.open_positions)
            if current_positions >= max_positions:
                return False
            if self.config.get('one_position_per_symbol', True) and symbol in self.open_positions:
                logger.info(f"Skipping {symbol}: Position already open")
                return False
            return True
        except Exception as e:
            logger.error(f"Error in risk checks: {e}")
            return False
    def _apply_cash_decay(self):
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
            if 'total_decay_applied' not in self.config:
                self.config['total_decay_applied'] = 0.0
            try:
                balance_details = get_futures_balance_detailed(self.client)
                current_cash = balance_details.get('available_balance', 0)
                decay_amount = current_cash * decay_rate * elapsed_minutes
            except:
                decay_amount = 0.0
            self.config['total_decay_applied'] += decay_amount
            self.config['last_decay_time'] = now.isoformat()
            if decay_amount > 0:
                logger.debug(f"Applied cash decay: ${decay_amount:.6f}")
        except Exception as e:
            logger.error(f"Error applying decay: {e}")
    def _can_trade(self, symbol: str) -> tuple:
        max_positions = self.config.get('max_concurrent_positions', 20)
        current_positions = len(self.open_positions)
        logger.debug(f"Current positions: {current_positions}, Max positions: {max_positions}")
        if current_positions >= max_positions:
            return False, f"Max positions reached ({current_positions}/{max_positions})"
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
        try:
            t_url = f"{API_URL}/api/price?symbol={symbol}"
            k_url = f"{API_URL}/api/history?symbol={symbol}"
            t_resp, t_err = fetch_with_retry("GET", t_url)
            if not t_resp:
                return None
            t_res = t_resp.json()
            k_resp, k_err = fetch_with_retry("GET", k_url)
            if not k_resp:
                return None
            k_res = k_resp.json()
            if 'price' not in t_res or not isinstance(k_res, list) or len(k_res) == 0:
                return None
            price = float(t_res['price'])
            
            # Calculate momentum: Compare current price to 10-period average
            closes = [float(tick.get('close', tick.get('price', 0))) for tick in k_res[-14:]]
            avg_price = sum(closes[-10:]) / 10 if len(closes) >= 10 else sum(closes) / len(closes)
            change_percent = ((price - avg_price) / avg_price) * 100 if avg_price > 0 else 0.0
            
            rsi = calculate_rsi(closes)
            result = {
                'price': price,
                'change': change_percent,
                'rsi': rsi,
                'closes': closes
            }
            logger.info(f"[AUTO] Market Data for {symbol}: Price=${price:.2f} | RSI={rsi:.2f} | Momentum={change_percent:+.2f}%")
            return result
        except Exception as e:
            logger.error(f"[AUTO] Error fetching data for {symbol}: {e}")
            return None
    def _generate_signal(self, data: dict) -> str:
        strategy = self.config.get('strategy', 'scalping')
        conf = self.strategy_config.get(strategy, self.strategy_config['scalping'])
        rsi = data['rsi']
        change = data['change']
        closes = data['closes']
        avg_price = sum(closes[-3:]) / 3
        last_price = closes[-1]
        logger.debug(f"RSI: {rsi}, Change: {change:.2f}%, Avg Price: {avg_price:.2f}, Last Price: {last_price:.2f}")
        logger.debug(f"RSI Buy Threshold: {conf['rsi_buy']}, RSI Sell Threshold: {conf['rsi_sell']}")
        if rsi <= conf['rsi_buy'] or (last_price > avg_price and change > 0.05):
            logger.info(f"BUY signal triggered - RSI: {rsi} <= {conf['rsi_buy']} OR momentum up {change:.2f}%")
            return "BUY"
        elif rsi >= conf['rsi_sell'] or (last_price < avg_price and change < -0.05):
            # For a long-only bot, SELL means exit. This is handled by _execute_trade_logic
            logger.info(f"SELL signal triggered - RSI: {rsi} >= {conf['rsi_sell']} OR momentum down {change:.2f}%")
            return "SELL"
        else:
            return "HOLD"                                       
    def _get_market_data_for_symbol(self, symbol: str) -> dict:
        if not symbol.endswith('USDT'):
            symbol = f"{symbol}USDT"
        symbol = symbol.upper()
        if symbol in signal_bot.latest_data:
            global_data = signal_bot.latest_data[symbol]
            return {
                'price': global_data['price'],
                'change': global_data['change'],
                'rsi': global_data['rsi'],
                'closes': [global_data['price']] * 14
            }
        else:
            return self._fetch_market_data(symbol)
    def _execute_trade_logic(self, symbol: str, signal: str, data: dict):
        logger.info(f"EXECUTING: {signal} {symbol} @ ${data['price']:.2f}")
        if signal == "SELL" and symbol not in self.open_positions:
            logger.warning(f"Skipping SELL signal for {symbol}: No open BUY position to close.")
            return
        if signal == "SELL":
            # For SELL, close the existing position using the actual held quantity
            try:
                close_res = close_position(self.client, symbol)
                if close_res.get('success'):
                    self.last_global_trade_time = datetime.now()
                    self.config['trades_today'] = self.config.get('trades_today', 0) + 1
                    self.last_trade_time[symbol] = datetime.now()
                    self.open_positions.discard(symbol)
                    logger.info(f"CLOSED position: {symbol} @ ${data['price']:.2f}")
                else:
                    logger.error(f"FAILED to close {symbol}: {close_res.get('error')}")
            except Exception as e:
                logger.error(f"Error closing position {symbol}: {e}")
            return
        result = self._execute_auto_trade(symbol, signal, data['price'])
        if result.get('success'):
            self.last_global_trade_time = datetime.now()
            self.config['trades_today'] = self.config.get('trades_today', 0) + 1
            self.last_trade_time[symbol] = datetime.now()
            self.open_positions.add(symbol)
            self.config['last_trade'] = {
                'symbol': symbol, 'side': signal, 'price': data['price'],
                'time': datetime.now().isoformat(), 'result': result
            }
            logger.info(f"SUCCESS: {signal} {symbol} @ ${data['price']:.2f}")
        else:
            logger.error(f"FAILED: {symbol} - {result.get('error')}")
    def _execute_auto_trade(self, symbol: str, side: str, current_price: float) -> dict:
        logger.info(f"_execute_auto_trade ENTERED for {symbol} {side}")
        try:
            leverage = self.config.get('leverage', 10)
            trade_size_percent = self.config.get('trade_size_percent', 5.0)
            sl_percent = self.config.get('stop_loss_percent', 1.0)
            tp_percent = self.config.get('take_profit_percent', 2.0)
            margin_type = self.config.get('margin_type', 'ISOLATED')
            try:
                open_orders = self.client.futures_get_open_orders(symbol=symbol)
                if open_orders:
                    logger.info(f"Skipping margin type change - {len(open_orders)} open orders exist")
                else:
                    self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            except Exception as e:
                if 'No need to change' in str(e):
                    pass
                elif 'open orders' in str(e).lower():
                    logger.warning(f"Cannot change margin type due to open orders. Continuing...")
                else:
                    return {"success": False, "error": f"Margin type error: {str(e)}"}
            try:
                open_orders = self.client.futures_get_open_orders(symbol=symbol)
                if open_orders:
                    logger.info("Skipping leverage change - Open orders exist")
                else:
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
        balance_info = get_futures_balance_detailed(self.client)
        available_balance = balance_info.get('available_balance', 0)
        logger.info(f"Balance: {available_balance}, Trade Size: {trade_size_percent}%")
        if available_balance <= 0:
            return {"success": False, "error": "Insufficient balance"}
        trade_amount = available_balance * (trade_size_percent / 100)
        notional_value = trade_amount * leverage
        quantity = notional_value / current_price
        precision = get_symbol_precision(self.client, symbol)
        quantity = int(max(precision['min_qty'], quantity)) # Enforce whole number as per rules
        if quantity < precision['min_qty']:
            logger.warning(f"Quantity {quantity} below minimum {precision['min_qty']}, forcing min_qty {precision['min_qty']}")
            quantity = precision['min_qty']
        if quantity <= 0:
            return {"success": False, "error": f"Quantity {quantity} is zero or negative after rounding"}
        # Add a 1% buffer for fees and price slippage to avoid "Insufficient Margin" 400 errors from API
        required_margin = (quantity * current_price) / leverage
        fee_buffer = required_margin * 0.01 
        if (required_margin + fee_buffer) > available_balance:
            return {"success": False, "error": f"Insufficient margin (with fee buffer). Required: ${(required_margin + fee_buffer):.2f}, Available: ${available_balance:.2f}"}
        if side != 'BUY':
            return {"success": False, "error": "Short selling not permitted. Entry orders must be BUY."}
            
        tp_price_change = (tp_percent / leverage) / 100
        sl_price_change = (sl_percent / leverage) / 100
        
        tp_price = round(current_price * (1 + tp_price_change), precision['price_precision'])
        sl_price = round(current_price * (1 - sl_price_change), precision['price_precision'])
        close_side = 'SELL'
        position_side = "BOTH"
        logger.info(f"Executing {side}: {quantity} {symbol} @ ~${current_price}")
        logger.info(f"  TP: ${tp_price} | SL: ${sl_price} | Leverage: {leverage}x")
        order_params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity
        }
        logger.info(f"Placing Order: {order_params}")
        order = self.client.futures_create_order(**order_params)
        logger.info(f"Order Success: {order.get('orderId')}")
        order_id = order.get('orderId', 'unknown')
        fill_price = float(order.get('avgPrice', current_price)) or current_price
        tp_order = None
        sl_order = None
        logger.info(f"Manual TP/SL tracking enabled: TP @ ${tp_price} | SL @ ${sl_price}")
        return {
            "success": True,
            "message": f"Position opened: {side} {quantity} {symbol}",
            "position": {
                "symbol": symbol,
                "side": side,
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
def get_symbol_precision(client: Client, symbol: str) -> dict:
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
        
        # If still not found, check if it's a direct symbol list or wrapped
        if not symbols and isinstance(exchange_info, dict) and 'price' in exchange_info:
             # Fallback: maybe it's not the full exchangeInfo
             pass
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
    precision = int(round(-math.log10(step_size)))
    return round(quantity - (quantity % step_size), precision)
def get_user_client(user_id: str) -> Optional[Client]:
    with clients_lock:
        return user_futures_clients.get(user_id)
def get_real_futures_balance(client: Client) -> float:
    try:
        account_balance = client.futures_account_balance()
        assets = account_balance if isinstance(account_balance, list) else []
        for asset in assets:
            if isinstance(asset, dict) and asset.get('asset') == 'USDT':
                return float(asset.get('balance', asset.get('walletBalance', 0)))
        acc = client.futures_account()
        if isinstance(acc, dict):
            return float(acc.get('totalWalletBalance', acc.get('cash', 0)))
        return 0.0
    except Exception as e:
        logger.error(f"Error fetching balance: {e}")
        return 0.0

def get_futures_balance_detailed(client: Client) -> dict:
    try:
        account_balance = client.futures_account_balance()
        assets = account_balance if isinstance(account_balance, list) else []
        for asset in assets:
            if isinstance(asset, dict) and asset.get('asset') == 'USDT':
                return {
                    'wallet_balance': float(asset.get('walletBalance', asset.get('balance', 0))),
                    'available_balance': float(asset.get('available_balance', asset.get('availableBalance', asset.get('withdrawAvailable', 0)))),
                    'cross_wallet_balance': float(asset.get('walletBalance', asset.get('balance', 0)))
                }
        acc = client.futures_account()
        if isinstance(acc, dict):
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
    positions = []
    try:
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
            initial_margin = (quantity * entry_price) / leverage if leverage > 0 else (quantity * entry_price)
            pnl_percent = (unrealized_pnl / initial_margin * 100) if initial_margin > 0 else 0
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
                'pnl_percent': pnl_percent,                   
                'price_change_percent': (unrealized_pnl / (quantity * entry_price) * 100) if (quantity * entry_price) > 0 else 0,
                'leverage': leverage,
                'is_real': True
            })
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
    return positions

def close_position(client: Client, symbol: str) -> dict:
    try:
        if not symbol:
            return {"success": False, "error": "No symbol provided"}
        symbol = symbol.strip().upper()
        if not symbol.endswith('USDT'):
            symbol = symbol + 'USDT'
        logger.info(f"MANUAL CLOSE REQUEST: {symbol}")
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
        try:
            quantity = int(quantity) # Enforce whole number close
        except Exception as e:
            logger.warning(f"[CLOSE] Precision fetch warning: {e}")
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
        if position_side and position_side != "BOTH":
            order_params["positionSide"] = position_side
        else:
            order_params["reduceOnly"] = True
        logger.info(f"Creating Close Order: {order_params}")
        try:
            order = client.futures_create_order(**order_params)
            fee_percent = DEFAULT_CONFIG.get('transaction_fee_percent', 0.1)
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

class SignalBot:
    def __init__(self):
        self.symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
        self.latest_data = {}
        self.last_update = datetime.now()
        self.lock = Lock()
        self.strategy_config = {
            'scalping': {'tp': 0.04, 'sl': 0.02, 'rsi_buy': 45, 'rsi_sell': 55}, 
            'swing':    {'tp': 0.10, 'sl': 0.05, 'rsi_buy': 35, 'rsi_sell': 65}
        }
    def fetch_data(self, symbol: str) -> dict:
        try:
            # Leverage AutoTradeEngine's market data logic if possible, 
            # or just replicate the corrected logic here for consistency.
            t_url = f"{API_URL}/api/price?symbol={symbol}"
            k_url = f"{API_URL}/api/history?symbol={symbol}"
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
            if 'price' not in t_res or not isinstance(k_res, list) or len(k_res) == 0:
                return None
            price = float(t_res['price'])
            
            closes = [float(tick.get('close', tick.get('price', 0))) for tick in k_res[-14:]]
            avg_price = sum(closes[-10:]) / 10 if len(closes) >= 10 else sum(closes) / len(closes)
            change = ((price - avg_price) / avg_price) * 100 if avg_price > 0 else 0.0
            
            rsi = calculate_rsi(closes)
            signal = self._gen_signal(rsi, change, closes, 'scalping')
            logger.info(f"[SIGNAL BOT] {symbol} is in {signal} MODE")
            tp, sl = "---", "---"
            conf = self.strategy_config['scalping']
            if signal == "BUY":
                tp = f"{price * (1 + conf['tp']):.2f}"
                sl = f"{price * (1 - conf['sl']):.2f}"
            elif signal == "SELL":
                # Only show EXIT levels if we were to have a position
                tp = f"EXIT @ {price * (1 + conf['tp']):.2f}"
                sl = f"EXIT @ {price * (1 - conf['sl']):.2f}"
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
        with self.lock:
            for symbol in self.symbols:
                symbol_data = self.fetch_data(symbol)
                if symbol_data:
                    self.latest_data[symbol] = symbol_data
            self.last_update = datetime.now()
            return self.latest_data
signal_bot = SignalBot()
def run_signal_updates():
    global signal_bot
    logger.info("Signal bot started...")
    while True:
        try:
            signal_bot.update_all()
            logger.info(f"Signals updated successfully")
            time.sleep(10)                      
        except Exception as e:
            logger.error(f"Signal update error: {e}")
            time.sleep(5)
signal_thread = threading.Thread(target=run_signal_updates, daemon=True)
signal_thread.start()
if __name__ == "__main__":
    logger.info(f"STARTING LR21 Auto Trading Agent - STANDALONE MODE")
    logger.info(f"API_URL: {API_URL}")
    user_id = "default_agent"
    client = Client(TEAM_API_KEY)
    try:
        portfolio = client.futures_account()
        if not portfolio:
             logger.error("Failed to fetch portfolio. Check your API_URL and TEAM_API_KEY.")
             exit(1)
        logger.info(f"Connected to portfolio. Balance: {portfolio.get('cash', 'unknown')}")
    except Exception as e:
        logger.error(f"Connection check failed: {e}")
        exit(1)
    config = DEFAULT_CONFIG.copy()
    trader = AutoTradeEngine(user_id, client, config)
    try:
        trader.start()
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
