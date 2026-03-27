import asyncio
import aiohttp
import websockets
import json
import logging
import os
import signal
import time
import warnings
import re
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
from eth_account import Account
from logging.handlers import RotatingFileHandler

# Import from the official nado-protocol package
from nado import NadoClient

# Suppress harmless network warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")

# Hyperliquid (Signal Source)
HL_WALLET = os.getenv("HL_WALLET_ADDRESS")

# Nado (Execution Venue)
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
NADO_ID = os.getenv("NADO_ACCOUNT_ID")

# FILTER SETTINGS
TOP_X_TRADERS = 5
# Only trade these major pairs on Nado
ALLOWED_COINS = ["BTC", "ETH", "SOL", "HYPE", "BNB", "PAX", "XAG", "WTI"]

# RISK SETTINGS
RISK_POS_PCT = 0.10        # 10% of Nado balance per trade
MIN_ORDER_USD = 11.0       # Minimum $11 USDC trade

# ==================== LOGGING ====================
os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[handler, logging.StreamHandler()]
)

# ==================== BOT CLASS ====================
class CrossExchangeBot:
    def __init__(self):
        # Initialize NadoClient from nado-protocol
        self.nado = NadoClient(private_key=NADO_PK, account_id=NADO_ID)
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        
        self.bot_state = {"positions": {}}
        self.signal_queue = asyncio.Queue()
        self.running = False

    async def api_get(self, url: str):
        async with self.session.get(url, timeout=15) as r:
            if r.status == 200: return await r.json()
        return None

    def _extract_traders(self, obj, container):
        if isinstance(obj, dict):
            if any(k in obj for k in ["account", "user", "ethAddress"]) and any("roi" in k.lower() for k in obj):
                container.append(obj)
            for v in obj.values(): self._extract_traders(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_traders(i, container)

    async def leaderboard_loop(self):
        """Fetch Top 5 Weekly Traders from Hyperliquid."""
        while self.running:
            try:
                logging.info(f"Scanning HL for Top {TOP_X_TRADERS} Weekly Traders...")
                data = await self.api_get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
                if data:
                    raw = []; self._extract_traders(data, raw)
                    ranked = sorted([(float(t.get("roiWeek", 0)), t.get("account") or t.get("user")) for t in raw if (t.get("account") or t.get("user"))], key=lambda x: x[0], reverse=True)
                    top_selected = {t[1] for t in ranked[:TOP_X_TRADERS]}
                    
                    new = top_selected - self.tracked_traders; old = self.tracked_traders - top_selected
                    self.tracked_traders = top_selected
                    
                    for t in new:
                        logging.info(f"Subscribing to HL Pro: {t}")
                        self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    for t in old:
                        if t in self.trader_ws_tasks:
                            self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                    logging.info(f"Monitoring {len(self.tracked_traders)} traders.")
            except Exception as e:
                logging.error(f"Leaderboard fetch error: {e}")
            await asyncio.sleep(300)

    async def trader_ws_loop(self, trader: str):
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async for msg in ws:
                        if not self.running or trader not in self.tracked_traders: break
                        data = json.loads(msg)
                        if data.get("channel") == "webData2":
                            data["trader_address"] = trader
                            await self.signal_queue.put(data)
            except:
                if self.running: await asyncio.sleep(5)

    async def process_loop(self):
        while self.running:
            data = await self.signal_queue.get()
            try:
                trader = data.get("trader_address")
                new_state = {p["position"]["coin"]: float(p["position"]["szi"]) for p in data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", []) if float(p["position"]["szi"]) != 0}
                old_state = self.trader_positions.get(trader, {})
                
                for coin, szi in new_state.items():
                    if coin not in old_state:
                        if coin in ALLOWED_COINS:
                            logging.info(f"SIGNAL: {trader[:6]} opened {coin}. Mirroring to Nado...")
                            await self.execute_nado_open(coin, "BUY" if szi > 0 else "SHORT", trader)
                
                for coin in old_state.keys():
                    if coin not in new_state:
                        await self.execute_nado_close(coin, trader)
                
                self.trader_positions[trader] = new_state
            except Exception as e:
                logging.error(f"Signal processing error: {e}")
            finally:
                self.signal_queue.task_done()

    async def execute_nado_open(self, coin: str, side: str, trader: str):
        if coin in self.bot_state["positions"]: return

        try:
            # SDK uses res.success and res.data
            res_info = await asyncio.to_thread(self.nado.get_account_info)
            if not res_info.success:
                logging.error(f"Nado Balance Error: {res_info.message}")
                return

            usdc_bal = 0.0
            for h in res_info.data.get('holdings', []):
                if h['token'] == 'USDC': usdc_bal = float(h['holding'])

            order_amt = usdc_bal * RISK_POS_PCT
            if order_amt < MIN_ORDER_USD: order_amt = MIN_ORDER_USD
            
            symbol = f"PERP_{coin}_USDC"
            logging.info(f"NADO: Market {side} for {symbol} (${order_amt:.2f})")
            
            res_order = await asyncio.to_thread(
                self.nado.create_order,
                symbol=symbol,
                order_type="MARKET",
                side=side,
                order_amount=order_amt
            )
            
            if res_order.success:
                logging.info(f"NADO MIRROR SUCCESS: {coin}")
                self.bot_state["positions"][coin] = {"trader": trader, "side": side}
            else:
                logging.error(f"NADO ORDER REJECTED: {res_order.message}")
        except Exception as e:
            logging.error(f"Nado Open Exception: {e}")

    async def execute_nado_close(self, coin: str, trader: str):
        if coin not in self.bot_state["positions"]: return
        if self.bot_state["positions"][coin]["trader"] != trader: return
        
        try:
            symbol = f"PERP_{coin}_USDC"
            close_side = "SELL" if self.bot_state["positions"][coin]["side"] == "BUY" else "BUY"
            
            logging.info(f"NADO: Closing {symbol}...")
            res_close = await asyncio.to_thread(
                self.nado.create_order,
                symbol=symbol,
                order_type="MARKET",
                side=close_side,
                reduce_only=True
            )
            
            if res_close.success:
                logging.info(f"NADO CLOSE SUCCESS: {coin}")
                del self.bot_state["positions"][coin]
            else:
                logging.error(f"NADO CLOSE FAILED: {res_close.message}")
        except Exception as e:
            logging.error(f"Nado Close Exception: {e}")

    async def run(self):
        self.running = True
        self.session = aiohttp.ClientSession()
        asyncio.create_task(self.leaderboard_loop())
        await self.process_loop()

    async def close(self):
        self.running = False
        if self.session: await self.session.close()

async def main():
    bot = CrossExchangeBot()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    bot_task = asyncio.create_task(bot.run())
    await stop.wait()
    await bot.close()
    bot_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
