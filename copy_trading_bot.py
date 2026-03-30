import asyncio, aiohttp, websockets, json, logging, os, signal, sys, warnings
from logging.handlers import RotatingFileHandler
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv

# --- OFFICIAL NADO PROTOCOL SDK IMPORTS ---
from eth_account import Account
from nado_protocol.client import create_nado_client, NadoClientMode
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix
from nado_protocol.utils.bytes32 import subaccount_to_hex
from nado_protocol.utils.margin_manager import MarginManager

# Suppress harmless warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("PRIVATE_KEY")
NADO_OWNER_ADDR = os.getenv("SUBACCOUNT_OWNER")
NADO_ENV = NadoClientMode.MAINNET 

TOP_X_TRADERS = 10
ALLOWED_COINS = {"BTC", "ETH", "SOL", "BNB", "HYPE", "PAX", "WTI"}
RISK_POS_PCT = 0.10        
MIN_ORDER_USD = 11.0       

# Logging Setup
os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s", 
    handlers=[log_handler, logging.StreamHandler(sys.stdout)]
)

class NadoQuantBot:
    def __init__(self):
        logging.info("--- INITIALIZING NADO QUANT ENGINE (INK MAINNET) ---")
        try:
            self.signer = Account.from_key(NADO_PK)
            self.owner = self.signer.address
            logging.info(f"[AUTH] Signer active: {self.owner}")
        except Exception as e:
            logging.error(f"[AUTH] Private Key error: {e}"); sys.exit(1)

        self.client = create_nado_client(NADO_ENV, self.signer)
        self.subaccount_hex = subaccount_to_hex(NADO_OWNER_ADDR or self.owner, "default")
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.state_file = "nado_bot_state.json"
        self.bot_state = {"positions": {}}
        self.load_state()

        self.signal_queue = asyncio.Queue()
        self.running = False
        self.product_map = {}
        self.locked_coins = set()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f: self.bot_state = json.load(f)
                logging.info(f"[MEMORY] Recovered {len(self.bot_state.get('positions', {}))} positions.")
            except: self.bot_state = {"positions": {}}

    def save_state(self):
        try:
            with open(self.state_file, "w") as f: json.dump(self.bot_state, f, indent=4)
        except Exception as e: logging.error(f"[MEMORY] Save error: {e}")

    async def sync_market_data(self):
        """PRICE-DETECT SYNC: Identifies coins by ID and Price to bypass SDK bugs."""
        while self.running:
            try:
                logging.info("[SYNC] Connecting to Nado Engine...")
                res = await asyncio.to_thread(self.client.context.engine_client.get_all_products)
                perps = getattr(res, 'perp_products', [])
                if not perps:
                    logging.warning("[SYNC] Engine returned no products. Retrying..."); await asyncio.sleep(10); continue

                count = 0
                for p in perps:
                    pid = getattr(p, 'product_id', None)
                    price_x18 = int(getattr(p, 'oracle_price_x18', 0))
                    px = float(price_x18) / 1e18
                    
                    coin = None
                    if px > 30000: coin = "BTC"
                    elif 1500 < px < 6000: coin = "ETH"
                    elif 50 < px < 400: coin = "SOL" if pid == 4 else "BNB"
                    elif 1.0 < px < 30.0: coin = "HYPE" if pid == 1 else "PAX"
                    elif 40.0 < px < 150.0: coin = "WTI"

                    if coin and coin in ALLOWED_COINS:
                        b_info = getattr(p, 'book_info', None)
                        p_tick = getattr(b_info, 'price_increment_x18', "100000000000000")
                        s_tick = getattr(b_info, 'size_increment', "1000000000000000")
                        m_size = getattr(b_info, 'min_size', 0)

                        self.product_map[coin] = {
                            "id": int(pid), "p_tick_x18": Decimal(str(p_tick)), 
                            "s_tick_x18": Decimal(str(s_tick)),
                            "min_usd": float(Decimal(str(m_size)) / Decimal("1e18")) if m_size else 0.0
                        }
                        count += 1
                
                if count > 0:
                    logging.info(f"[SYNC] Success: {count} assets auto-detected: {list(self.product_map.keys())}")
                    return True
            except Exception as e: logging.error(f"[SYNC] Error: {e}")
            await asyncio.sleep(10)

    async def leaderboard_loop(self):
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        while self.running:
            try:
                logging.info("[LEADERBOARD] Fetching Hyperliquid whales...")
                async with self.session.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", headers=headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = []; self._extract_traders(data, raw)
                        
                        processed = {}
                        for t in raw:
                            # HL often switches keys between ethAddress, account, and user
                            addr = t.get("ethAddress") or t.get("account") or t.get("user")
                            if addr:
                                val = t.get("roiWeek") or t.get("roi") or 0
                                try: processed[addr] = float(val)
                                except: processed[addr] = 0.0
                        
                        ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                        top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                        
                        for p in self.bot_state["positions"].values(): top_selected.add(p["trader"])
                        
                        new = top_selected - self.tracked_traders
                        old = self.tracked_traders - top_selected
                        self.tracked_traders = top_selected
                        
                        for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        for t in old:
                            if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                        logging.info(f"[TRACKER] Monitoring {len(self.tracked_traders)} traders.")
                    else:
                        logging.warning(f"[LEADERBOARD] API error: {r.status}")
            except Exception as e: logging.error(f"[LEADERBOARD] Loop error: {e}")
            await asyncio.sleep(300)

    def _extract_traders(self, obj, container):
        if isinstance(obj, dict):
            # Greedy search for any key that looks like an address
            u = obj.get("ethAddress") or obj.get("account") or obj.get("user") or obj.get("address")
            if u and isinstance(u, str) and u.startswith("0x") and len(u) > 30: 
                container.append(obj)
            for v in obj.values(): self._extract_traders(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_traders(i, container)

    async def trader_ws_loop(self, trader: str):
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async def pinger():
                        while not ws.closed: 
                            try: await ws.send(json.dumps({"method": "ping"})); await asyncio.sleep(50)
                            except: break
                    p_task = asyncio.create_task(pinger())
                    try:
                        async for msg in ws:
                            d = json.loads(msg)
                            if d.get("channel") == "webData2":
                                d["trader_address"] = trader; await self.signal_queue.put(d)
                    finally: p_task.cancel()
            except: await asyncio.sleep(5)

    async def process_loop(self):
        while self.running:
            data = await self.signal_queue.get()
            try:
                trader = data.get("trader_address")
                raw_pos = data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", [])
                new_state = {p["position"]["coin"]: float(p["position"]["szi"]) for p in raw_pos if float(p["position"]["szi"]) != 0}
                old_state = self.trader_positions.get(trader, {})
                for coin in ALLOWED_COINS:
                    o_dir = 1 if old_state.get(coin,0)>0 else (-1 if old_state.get(coin,0)<0 else 0)
                    n_dir = 1 if new_state.get(coin,0)>0 else (-1 if new_state.get(coin,0)<0 else 0)
                    if o_dir != n_dir: asyncio.create_task(self.handle_signal(coin, o_dir, n_dir, trader))
                self.trader_positions[trader] = new_state
            finally: self.signal_queue.task_done()

    async def handle_signal(self, coin, o_dir, n_dir, trader):
        if coin in self.bot_state["positions"] and self.bot_state["positions"][coin]["trader"] == trader:
            await self.execute_nado_order(coin, False, trader, True)
        if n_dir != 0 and coin not in self.bot_state["positions"]:
            if coin not in self.locked_coins:
                self.locked_coins.add(coin)
                try: await self.execute_nado_order(coin, n_dir==1, trader, False)
                finally: self.locked_coins.discard(coin)

    async def execute_nado_order(self, coin, is_buy, trader, is_close):
        market = self.product_map.get(coin)
        if not market: return
        try:
            p_res = await asyncio.to_thread(self.client.perp.get_prices, market["id"])
            mark_x18 = int(getattr(p_res, 'mark_price_x18', 0))
            if mark_x18 == 0: return
            
            if is_close:
                pos = self.bot_state["positions"].get(coin)
                if not pos: return
                is_buy = not pos["is_buy"]; qty_x18 = pos["qty_x18"]
                slip = 1.10 if is_buy else 0.90
            else:
                sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
                iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
                mgr = MarginManager(sub_info, getattr(iso_res, 'isolated_positions', []))
                avail = float(mgr.calculate_account_summary().funds_available)
                usd = max(avail * RISK_POS_PCT, MIN_ORDER_USD, market["min_usd"])
                if avail < usd: return
                qty_x18 = int((Decimal(str(usd)) * Decimal("1e18") / (Decimal(str(mark_x18))/Decimal("1e18")) / market["s_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["s_tick_x18"])
                slip = 1.05 if is_buy else 0.95

            px_x18 = int((Decimal(str(mark_x18)) * Decimal(str(slip)) / market["p_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["p_tick_x18"])
            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=NADO_OWNER_ADDR or self.owner, subaccount_name="default"),
                priceX18=px_x18, amount=qty_x18 if is_buy else -qty_x18,
                expiration=get_expiration_timestamp(60), nonce=gen_order_nonce(),
                appendix=build_appendix(order_type=OrderType.IOC, reduce_only=is_close)
            )
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            if "success" in str(res).lower() or "ok" in str(res).lower():
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy, "qty_x18": qty_x18}
                self.save_state(); logging.info(f"[SUCCESS] {coin} mirrored.")
        except Exception as e: logging.error(f"[EXEC] Error: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        if await self.sync_market_data():
            asyncio.create_task(self.leaderboard_loop())
            await self.process_loop()

    async def close(self): self.running = False; await self.session.close()

async def main():
    bot = NadoQuantBot(); stop = asyncio.Event(); loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM): 
        try: loop.add_signal_handler(s, stop.set)
        except: pass
    t = asyncio.create_task(bot.run()); await stop.wait(); await bot.close(); t.cancel()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
