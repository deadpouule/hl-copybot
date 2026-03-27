import asyncio, aiohttp, websockets, json, logging, os, signal, time, struct, warnings, re, sys
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
from eth_account import Account
from eth_utils import keccak
from logging.handlers import RotatingFileHandler

# --- AGGRESSIVE NADO IMPORT FIX ---
venv_pkgs = "/root/hl-copybot/venv/lib/python3.10/site-packages"
if venv_pkgs not in sys.path:
    sys.path.insert(0, venv_pkgs)

try:
    from nado import NadoClient
except ImportError:
    try:
        from nado_protocol import NadoClient
    except ImportError:
        from nado_protocol.client import NadoClient

# Suppress harmless network warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
NADO_ID = os.getenv("NADO_ACCOUNT_ID")

if NADO_PK:
    os.environ["NADO_PRIVATE_KEY"] = NADO_PK
    os.environ["ORDERLY_PRIVATE_KEY"] = NADO_PK

TOP_X_TRADERS = 5
ALLOWED_COINS = ["BTC", "ETH", "SOL", "HYPE", "BNB", "PAX", "XAG", "WTI"]
RISK_POS_PCT = 0.10        
MIN_ORDER_USD = 11.0       

os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[handler, logging.StreamHandler()])

class CrossExchangeBot:
    def __init__(self):
        logging.info(f"Initializing Nado Client for: {NADO_ID}")
        self.nado = NadoClient(NADO_ID)
        try: self.nado.private_key = NADO_PK
        except: pass
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

    def _extract_all(self, obj, container):
        """Greedy extraction of all objects that look like traders."""
        if isinstance(obj, dict):
            user = obj.get("account") or obj.get("user") or obj.get("ethAddress")
            if user and re.match(r"^0x[a-fA-F0-9]{40}$", str(user)):
                container.append(obj)
            for v in obj.values(): self._extract_all(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_all(i, container)

    async def leaderboard_loop(self):
        while self.running:
            try:
                logging.info("Scanning HL Leaderboard...")
                data = await self.api_get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
                if data:
                    raw = []; self._extract_all(data, raw)
                    
                    # Deduplicate by address and grab ROI
                    processed = {}
                    for t in raw:
                        addr = t.get("account") or t.get("user") or t.get("ethAddress")
                        roi = 0.0
                        for k, v in t.items():
                            if "roi" in k.lower() and isinstance(v, (int, float)):
                                roi = float(v); break
                        if addr not in processed or roi > processed[addr]:
                            processed[addr] = roi
                    
                    # Sort by ROI descending
                    ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                    top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                    
                    if not top_selected:
                        logging.warning("Leaderboard scan returned no valid addresses.")
                        await asyncio.sleep(60); continue

                    new = top_selected - self.tracked_traders
                    old = self.tracked_traders - top_selected
                    self.tracked_traders = top_selected
                    
                    for t in new:
                        logging.info(f"Subscribing to HL Pro: {t}")
                        self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    for t in old:
                        if t in self.trader_ws_tasks:
                            self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                    
                    logging.info(f"Monitoring {len(self.tracked_traders)} traders.")
            except Exception as e:
                logging.error(f"Leaderboard Loop Error: {e}")
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
                            data["trader_address"] = trader; await self.signal_queue.put(data)
            except:
                if self.running: await asyncio.sleep(5)

    async def process_loop(self):
        while self.running:
            data = await self.signal_queue.get()
            try:
                trader = data.get("trader_address")
                raw_positions = data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", [])
                new_state = {p["position"]["coin"]: float(p["position"]["szi"]) for p in raw_positions if float(p["position"]["szi"]) != 0}
                
                old_state = self.trader_positions.get(trader, {})
                for coin, szi in new_state.items():
                    if coin not in old_state:
                        if coin in ALLOWED_COINS:
                            logging.info(f"SIGNAL: {trader[:6]} opened {coin}. Executing on Nado...")
                            await self.execute_nado_open(coin, "BUY" if szi > 0 else "SELL", trader)
                        else:
                            logging.info(f"Ignored non-major: {coin}")
                for coin in old_state.keys():
                    if coin not in new_state: await self.execute_nado_close(coin, trader)
                self.trader_positions[trader] = new_state
            except Exception as e: logging.error(f"Process Error: {e}")
            finally: self.signal_queue.task_done()

    async def execute_nado_open(self, coin: str, side: str, trader: str):
        if coin in self.bot_state["positions"]: return
        try:
            res_info = await asyncio.to_thread(self.nado.get_account_info)
            if not res_info.success: return
            usdc_bal = 0.0
            for h in res_info.data.get('holdings', []):
                if h['token'] == 'USDC': usdc_bal = float(h['holding'])
            
            order_amt = usdc_bal * RISK_POS_PCT
            if order_amt < MIN_ORDER_USD: order_amt = MIN_ORDER_USD
            
            symbol = f"PERP_{coin}_USDC"
            logging.info(f"NADO: Creating MARKET {side} for {symbol} (${order_amt:.2f})")
            res_order = await asyncio.to_thread(self.nado.create_order, symbol=symbol, order_type="MARKET", side=side, order_amount=order_amt)
            if res_order.success:
                logging.info(f"NADO MIRROR SUCCESS: {coin}")
                self.bot_state["positions"][coin] = {"trader": trader, "side": side}
            else: logging.error(f"NADO REJECTED: {res_order.message}")
        except Exception as e: logging.error(f"Nado Open Ex: {e}")

    async def execute_nado_close(self, coin: str, trader: str):
        if coin not in self.bot_state["positions"] or self.bot_state["positions"][coin]["trader"] != trader: return
        try:
            symbol = f"PERP_{coin}_USDC"
            close_side = "SELL" if self.bot_state["positions"][coin]["side"] == "BUY" else "BUY"
            res_close = await asyncio.to_thread(self.nado.create_order, symbol=symbol, order_type="MARKET", side=close_side, reduce_only=True)
            if res_close.success:
                logging.info(f"NADO CLOSE SUCCESS: {coin}")
                del self.bot_state["positions"][coin]
        except Exception as e: logging.error(f"Nado Close Ex: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        logging.info("Cross-Exchange Bot Live. Sourcing from HL...")
        asyncio.create_task(self.leaderboard_loop())
        await self.process_loop()

    async def close(self):
        self.running = False; await self.session.close()

async def main():
    bot = CrossExchangeBot(); loop = asyncio.get_running_loop(); stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    t = asyncio.create_task(bot.run()); await stop.wait(); await bot.close(); t.cancel()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
