import asyncio, aiohttp, websockets, json, logging, os, signal, time, warnings, re, sys
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

# Official Nado SDK Imports from your documentation
from nado_protocol.client import create_nado_client
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.math import to_pow_10, to_x18
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix

# Suppress harmless network warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
# Nado Mainnet is "mainnet"
NADO_ENV = "mainnet"

# TRADING LIMITS
TOP_X_TRADERS = 5
ALLOWED_COINS = ["BTC", "ETH", "SOL", "HYPE", "BNB"] # Major pairs
RISK_POS_PCT = 0.10        
MIN_ORDER_USD = 11.0       

# PRODUCT ID MAPPING (Nado/Orderly Mainnet Standards)
# If a coin is missing, check Nado API or contact support for the ID
SYMBOL_TO_ID = {
    "BTC": 1,
    "ETH": 2,
    "SOL": 3,
    "BNB": 4,
    "HYPE": 100, # Example ID for HYPE
}

os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[handler, logging.StreamHandler()])

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Official Nado Client...")
        self.client = create_nado_client(NADO_ENV, NADO_PK)
        self.owner = self.client.context.engine_client.signer.address
        
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
            user = obj.get("account") or obj.get("user") or obj.get("ethAddress")
            if user and re.match(r"^0x[a-fA-F0-9]{40}$", str(user)):
                container.append(obj)
            for v in obj.values(): self._extract_traders(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_traders(i, container)

    async def leaderboard_loop(self):
        while self.running:
            try:
                logging.info("Scanning Hyperliquid Leaderboard...")
                data = await self.api_get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
                if data:
                    raw = []; self._extract_traders(data, raw)
                    processed = {}
                    for t in raw:
                        addr = t.get("account") or t.get("user") or t.get("ethAddress")
                        roi = 0.0
                        for k, v in t.items():
                            if "roi" in k.lower() and isinstance(v, (int, float)):
                                roi = float(v); break
                        if addr not in processed or roi > processed[addr]: processed[addr] = roi
                    
                    ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                    top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                    
                    new = top_selected - self.tracked_traders
                    old = self.tracked_traders - top_selected
                    self.tracked_traders = top_selected
                    
                    for t in new:
                        logging.info(f"Monitoring HL Pro: {t}")
                        self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    for t in old:
                        if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
            except Exception as e: logging.error(f"LB error: {e}")
            await asyncio.sleep(300)

    async def trader_ws_loop(self, trader: str):
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async for msg in ws:
                        data = json.loads(msg)
                        if data.get("channel") == "webData2":
                            data["trader_address"] = trader; await self.signal_queue.put(data)
            except: await asyncio.sleep(5)

    async def process_loop(self):
        while self.running:
            data = await self.signal_queue.get()
            try:
                trader = data.get("trader_address")
                raw_pos = data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", [])
                new_state = {p["position"]["coin"]: float(p["position"]["szi"]) for p in raw_pos if float(p["position"]["szi"]) != 0}
                
                old_state = self.trader_positions.get(trader, {})
                for coin, szi in new_state.items():
                    if coin not in old_state:
                        if coin.upper() in ALLOWED_COINS:
                            logging.info(f"SIGNAL: {trader[:6]} opened {coin} on HL.")
                            await self.execute_nado_order(coin, szi > 0, trader, is_close=False)
                
                for coin in old_state.keys():
                    if coin not in new_state:
                        logging.info(f"SIGNAL: {trader[:6]} closed {coin} on HL.")
                        await self.execute_nado_order(coin, False, trader, is_close=True)
                
                self.trader_positions[trader] = new_state
            finally: self.signal_queue.task_done()

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        if is_close and coin not in self.bot_state["positions"]: return

        pid = SYMBOL_TO_ID.get(coin.upper())
        if not pid:
            logging.error(f"Mapping missing for {coin}. Please add ID to SYMBOL_TO_ID.")
            return

        try:
            # Get current price from HL mid-price API for order estimation
            async with self.session.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}) as r:
                mids = await r.json()
                px = float(mids.get(coin.upper(), 0))

            if px == 0: return

            # SDK expects exact math types
            # Note: For Market orders, we slip the price by 5% to ensure fill
            target_px = px * 1.05 if is_buy else px * 0.95
            if is_close: # Invert side for close
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * 1.05 if is_buy else px * 0.95

            # Calculate amount (10% of $11 min logic)
            # In a real Market order on Nado SDK, we use a large price and specific amount
            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=self.owner, subaccount_name="default"),
                priceX18=to_x18(target_px),
                amount=to_pow_10(1, 15), # Small test size (0.001 units) - Adjust as needed
                expiration=get_expiration_timestamp(60), # 60 second window
                nonce=gen_order_nonce(),
                appendix=build_appendix(OrderType.POST_ONLY if False else OrderType.DEFAULT)
            )

            logging.info(f"NADO: Placing {'CLOSE' if is_close else 'OPEN'} on Product {pid}")
            # Place the order
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=pid, order=order))
            
            if "status" in str(res).lower() and "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin}")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
            else:
                logging.error(f"NADO FAILURE: {res}")

        except Exception as e:
            logging.error(f"Order Exception: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        logging.info("Bot Live. Monitoring HL for Nado Execution...")
        asyncio.create_task(self.leaderboard_loop())
        await self.process_loop()

    async def close(self):
        self.running = False; await self.session.close()

async def main():
    bot = NadoQuantBot(); loop = asyncio.get_running_loop(); stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    t = asyncio.create_task(bot.run()); await stop.wait(); await bot.close(); t.cancel()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
