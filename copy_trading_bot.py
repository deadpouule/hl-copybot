import asyncio, aiohttp, websockets, json, logging, os, signal, time, struct, warnings, re, sys
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

# Official Nado SDK Imports
from nado_protocol.client import create_nado_client
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.math import to_pow_10, to_x18
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix

warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
NADO_ID = os.getenv("NADO_ACCOUNT_ID")
NADO_ENV = "mainnet"

TOP_X_TRADERS = 5
ALLOWED_COINS = ["BTC", "ETH", "SOL", "HYPE", "BNB"]
RISK_POS_PCT = 0.10        
MIN_ORDER_USD = 11.0       

# Ensure these match Orderly Network Mainnet IDs
SYMBOL_TO_ID = {
    "BTC": 1, "ETH": 2, "SOL": 3, "BNB": 4, "HYPE": 100
}

os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[handler, logging.StreamHandler()])

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Nado Copy-Trader...")
        self.client = create_nado_client(NADO_ENV, NADO_PK)
        self.owner = self.client.context.engine_client.signer.address
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.bot_state = {"positions": {}}
        self.signal_queue = asyncio.Queue()
        self.running = False
        self.market_rules = {}

    async def refresh_market_rules(self):
        """Fetch precision rules. If this fails, we log it clearly."""
        try:
            res = await asyncio.to_thread(self.client.market.get_all_products)
            if res.success:
                for p in res.data:
                    self.market_rules[p['product_id']] = {
                        "price_tick": float(p['price_increment']),
                        "size_tick": float(p['base_tick']),
                        "min_size": float(p['min_base_amount'])
                    }
                logging.info(f"Rules Cached: Successfully loaded {len(self.market_rules)} assets from Nado.")
            else:
                logging.error(f"Rules Error: Nado rejected product list fetch: {res.message}")
        except Exception as e:
            logging.error(f"Rules Error: Could not connect to Nado to fetch precision: {e}")

    async def api_get(self, url: str):
        async with self.session.get(url, timeout=15) as r:
            if r.status == 200: return await r.json()
        return None

    def _extract_traders(self, obj, container):
        if isinstance(obj, dict):
            user = obj.get("account") or obj.get("user") or obj.get("ethAddress")
            if user and re.match(r"^0x[a-fA-F0-9]{40}$", str(user)): container.append(obj)
            for v in obj.values(): self._extract_traders(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_traders(i, container)

    async def leaderboard_loop(self):
        while self.running:
            try:
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
                    self.tracked_traders = top_selected
                    for t in self.tracked_traders:
                        if t not in self.trader_ws_tasks:
                            self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    logging.info(f"Monitoring {len(self.tracked_traders)} HL traders.")
            except Exception as e: logging.error(f"Leaderboard Loop Error: {e}")
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
                    if coin not in old_state and coin.upper() in ALLOWED_COINS:
                        logging.info(f"SIGNAL: {trader[:6]} opened {coin}.")
                        await self.execute_nado_order(coin, szi > 0, trader, is_close=False)
                for coin in old_state.keys():
                    if coin not in new_state and coin.upper() in ALLOWED_COINS:
                        logging.info(f"SIGNAL: {trader[:6]} closed {coin}.")
                        await self.execute_nado_order(coin, False, trader, is_close=True)
                self.trader_positions[trader] = new_state
            finally: self.signal_queue.task_done()

    def _round_to_tick(self, value, tick):
        return round(value / tick) * tick

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]:
            logging.info(f"Skipping {coin}: Position already open in memory.")
            return

        pid = SYMBOL_TO_ID.get(coin.upper())
        if not pid:
            logging.warning(f"Skipping {coin}: No Product ID mapping found.")
            return
        if pid not in self.market_rules:
            logging.warning(f"Skipping {coin}: Product ID {pid} not found in Nado market rules.")
            return

        try:
            # 1. Get Live Price from HL (as proxy)
            async with self.session.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}) as r:
                mids = await r.json()
                px = float(mids.get(coin.upper(), 0))
            if px == 0:
                logging.error(f"Price Error: Could not get price for {coin}.")
                return

            # 2. Precision Rules
            rules = self.market_rules[pid]
            # Use aggressive slip for market orders
            raw_target_px = px * 1.05 if is_buy else px * 0.95
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                raw_target_px = px * 1.10 if is_buy else px * 0.90

            final_px = self._round_to_tick(raw_target_px, rules["price_tick"])
            
            # 3. Balance & Size
            res_bal = await asyncio.to_thread(self.client.spot.get_holdings, self.owner)
            balance = 0.0
            if res_bal.success:
                for h in res_bal.data:
                    if h['token'] == 'USDC': balance = float(h['holding'])
            
            usd_amount = max(balance * RISK_POS_PCT, MIN_ORDER_USD)
            raw_qty = usd_amount / px
            final_qty = self._round_to_tick(raw_qty, rules["size_tick"])

            if final_qty < rules["min_size"]:
                logging.warning(f"Skipping {coin}: Quantity {final_qty} is below Nado min {rules['min_size']}.")
                return

            # 4. Execute using SDK X18 Math
            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=self.owner, subaccount_name="default"),
                priceX18=to_x18(final_px),
                amount=int(final_qty * 10**18),
                expiration=get_expiration_timestamp(60),
                nonce=gen_order_nonce(),
                appendix=build_appendix(OrderType.DEFAULT)
            )

            logging.info(f"NADO: Executing {coin} | ID {pid} | Side {'BUY' if is_buy else 'SELL'} | Px {final_px}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=pid, order=order))
            
            if res.success:
                logging.info(f"NADO SUCCESS: {coin} trade complete.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
            else:
                logging.error(f"NADO REJECTED: {res.message} (Code: {res.status})")

        except Exception as e: logging.error(f"Nado Order Exception: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        await self.refresh_market_rules()
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
