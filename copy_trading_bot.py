import asyncio, aiohttp, websockets, json, logging, os, signal, time, struct, warnings, re, sys
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN

# --- OFFICIAL NADO PROTOCOL SDK IMPORTS ---
from eth_account import Account
from nado_protocol.client import create_nado_client, NadoClientMode
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.math import to_x18
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix
from nado_protocol.utils.bytes32 import subaccount_to_hex
from nado_protocol.utils.margin_manager import MarginManager

# Suppress harmless eth-utils warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("PRIVATE_KEY")
NADO_OWNER_ADDR = os.getenv("SUBACCOUNT_OWNER")
DATA_ENV_STR = os.getenv("DATA_ENV", "nadoMainnet")

TOP_X_TRADERS = 30 
ALLOWED_COINS = ["BTC", "ETH", "SOL", "BNB", "PAX", "XAG", "WTI", "HYPE"]
RISK_POS_PCT = 0.10        # 10% of available margin per trade
MIN_ORDER_USD = 11.0       

# Logging Setup
os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s", 
    handlers=[log_handler, logging.StreamHandler()]
)

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Final Master Nado Engine...")
        
        # 1. Critical Signer Setup (Prevents NoneType error)
        try:
            self.signer = Account.from_key(NADO_PK)
            self.owner = self.signer.address
            logging.info(f"Signer active for address: {self.owner}")
        except Exception as e:
            logging.error(f"CRITICAL: .env PRIVATE_KEY error: {e}")
            sys.exit(1)

        # 2. Client Initialization
        nado_mode = NadoClientMode.MAINNET if DATA_ENV_STR == "nadoMainnet" else NadoClientMode.TESTNET
        self.client = create_nado_client(nado_mode, self.signer)
        self.subaccount_hex = subaccount_to_hex(NADO_OWNER_ADDR or self.owner, "default")
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.orderly_prices = {}
        self.all_mids = {} # HL Proxy cache
        
        # Burst Protection Cache
        self.cached_funds = None
        self.last_funds_check = 0
        
        self.state_file = "nado_bot_state.json"
        self.bot_state = {"positions": {}}
        self.load_state()

        self.signal_queue = asyncio.Queue()
        self.running = False
        self.product_map = {}

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    self.bot_state = json.load(f)
                logging.info(f"Memory: Loaded {len(self.bot_state['positions'])} open mirrors.")
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.bot_state, f)

    def _safe_get(self, obj, key, default=None):
        if isinstance(obj, dict): return obj.get(key, default)
        return getattr(obj, key, default)

    async def sync_market_data(self):
        """Dual-Sync: Tries SDK first, then direct Gateway API for product IDs."""
        while self.running:
            try:
                logging.info("Syncing Market rules from Nado...")
                perps = []
                # Try SDK first
                res = await asyncio.to_thread(self.client.context.engine_client.get_all_products)
                perps = getattr(res, 'perp_products', [])
                
                # If SDK fails, try Direct HTTP
                if not perps:
                    async with self.session.get("https://gateway.prod.nado.xyz/v1/symbols") as r:
                        if r.status == 200: perps = await r.json()

                count = 0
                for p in perps:
                    symbol = str(self._safe_get(p, 'symbol', '')).upper()
                    # STRICT MATCH: only accept perps ending in -PERP
                    if symbol.endswith('-PERP'):
                        coin = symbol.split('-')[0]
                        if coin in ALLOWED_COINS:
                            pid = self._safe_get(p, 'product_id')
                            # Handle string x18 formatting from raw API
                            p_tick = float(self._safe_get(p, 'price_increment_x18', 0)) / 1e18 or float(self._safe_get(p, 'price_increment', 0.0001))
                            s_tick = float(self._safe_get(p, 'size_increment', 0)) or (float(self._safe_get(p, 'size_increment_x18', 0)) / 1e18)
                            min_usd = float(self._safe_get(p, 'min_size', 0)) or (float(self._safe_get(p, 'min_size_x18', 0)) / 1e18)
                            status = self._safe_get(p, 'trading_status', 'live')

                            if pid is not None and p_tick > 0:
                                self.product_map[coin] = {
                                    "id": int(pid), "p_tick_x18": Decimal(str(int(p_tick * 1e18))), 
                                    "s_tick_x18": Decimal(str(int(s_tick * 1e18))), "status": status,
                                    "min_usd": float(min_usd or 10.0)
                                }
                                count += 1
                if count > 0:
                    logging.info(f"Sync SUCCESS: {count} Perpetual assets identified.")
                    return True
            except Exception as e:
                logging.error(f"Sync failed: {e}")
            await asyncio.sleep(10)

    async def orderly_mids_loop(self):
        """Maintain live prices from Nado with STRICT symbol matching."""
        while self.running:
            try:
                async with self.session.get("https://api-evm.orderly.org/v1/public/futures") as r:
                    if r.status == 200:
                        js = await r.json()
                        for row in js.get("data", {}).get("rows", []):
                            symbol = str(row.get("symbol", "")).upper()
                            if symbol.endswith("-PERP"):
                                coin = symbol.split("-")[0]
                                if coin in ALLOWED_COINS:
                                    self.orderly_prices[coin] = float(row.get("mark_price", 0))
            except: pass
            await asyncio.sleep(5)

    async def hl_mids_loop(self):
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running:
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                    async for msg in ws:
                        if not self.running: break
                        d = json.loads(msg)
                        if d.get("channel") == "allMids": self.all_mids.update(d["data"]["mids"])
            except: await asyncio.sleep(5)

    async def leaderboard_loop(self):
        while self.running:
            try:
                async with self.session.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard") as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = []; self._extract_greedy(data, raw)
                        processed = { (t.get("account") or t.get("user") or t.get("ethAddress")): float(t.get("roiWeek", t.get("roi", 0))) for t in raw if (t.get("account") or t.get("user") or t.get("ethAddress")) }
                        ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                        top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                        for p in self.bot_state["positions"].values(): top_selected.add(p["trader"])
                        new = top_selected - self.tracked_traders; old = self.tracked_traders - top_selected
                        self.tracked_traders = top_selected
                        for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        for t in old:
                            if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                        logging.info(f"Following {len(self.tracked_traders)} Pro Traders on HL.")
            except: pass
            await asyncio.sleep(300)

    def _extract_greedy(self, obj, container):
        if isinstance(obj, dict):
            u = obj.get("account") or obj.get("user") or obj.get("ethAddress") or obj.get("address")
            if u and isinstance(u, str) and u.startswith("0x") and len(u) > 30: container.append(obj)
            for v in obj.values(): self._extract_greedy(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_greedy(i, container)

    async def trader_ws_loop(self, trader: str):
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async for msg in ws:
                        if not self.running: break
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
                raw_pos = data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", [])
                new_s = {p["position"]["coin"]: float(p["position"]["szi"]) for p in raw_pos if float(p["position"]["szi"]) != 0}
                
                old_s = self.trader_positions.get(trader, {})
                for c, s in new_s.items():
                    if c not in old_s and c.upper() in ALLOWED_COINS:
                        logging.info(f"HL SIGNAL: {trader[:6]} opened {c}")
                        await self.execute_nado_order(c, s > 0, trader, False)
                        await asyncio.sleep(0.5) 
                for c in old_s.keys():
                    if c not in new_s and c.upper() in ALLOWED_COINS:
                        if c in self.bot_state["positions"]:
                            logging.info(f"HL SIGNAL: {trader[:6]} closed {c}")
                            await self.execute_nado_order(c, False, trader, True)
                            await asyncio.sleep(0.5)
                self.trader_positions[trader] = new_s
            finally: self.signal_queue.task_done()

    def _round_step(self, val, step: Decimal) -> Decimal:
        val_d = Decimal(str(val))
        return (val_d / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step

    async def _get_available_margin(self):
        """Burst Protection: Caches balance for 5s to avoid Error 1000."""
        now = time.time()
        if self.cached_funds and (now - self.last_funds_check < 5): return self.cached_funds
        try:
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            manager = MarginManager(sub_info, getattr(iso_res, 'isolated_positions', []))
            summary = manager.calculate_account_summary()
            self.cached_funds = float(summary.funds_available)
            self.last_funds_check = now
            return self.cached_funds
        except: return self.cached_funds or 0.0

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        if is_close and coin not in self.bot_state["positions"]: return
        market = self.product_map.get(coin.upper())
        if not market: return

        try:
            # Oracle Price Sync
            px = self.orderly_prices.get(coin.upper(), 0.0) or float(self.all_mids.get(coin.upper(), 0.0))
            if px == 0: return

            target_px = px * (1.05 if is_buy else 0.95)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            # Round to Tick using Decimal (Prevents Error 2000)
            final_px_dec = (Decimal(str(target_px)) * Decimal("1e18") / market["p_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["p_tick_x18"]
            
            # Balance Check
            available = await self._get_available_margin()
            usd_amt = max(available * RISK_POS_PCT, MIN_ORDER_USD, market["min_usd"])
            if available < usd_amt and not is_close: return
            
            # Precision Quantity
            qty = usd_amt / px
            final_qty_x18 = (Decimal(str(qty)) * Decimal("1e18") / market["s_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["s_tick_x18"]
            if final_qty_x18 <= 0: return

            # X18 Parameters (Guaranteed Perfect Integers)
            order_exec = OrderType.IOC
            if str(market.get("status", "")).lower() == "post_only" or coin.upper() == "HYPE":
                order_exec = OrderType.POST_ONLY

            order = OrderParams(
                sender=self.subaccount_hex, priceX18=int(final_px_dec), amount=int(final_qty_x18) if is_buy else -int(final_qty_x18),
                expiration=get_expiration_timestamp(60), nonce=gen_order_nonce(),
                appendix=build_appendix(order_type=order_exec, reduce_only=is_close)
            )

            logging.info(f"NADO EXEC PERP: {coin} | Px: {float(final_px_dec)/1e18} | Qty: {float(final_qty_x18)/1e18}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin} trade complete.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state(); self.last_funds_check = 0
            else: logging.error(f"NADO REJECTED: {getattr(res, 'message', str(res))}")
        except Exception as e: logging.error(f"Failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        # BLOCKING START: Guarantees IDs are mapped before any signals process
        await self.sync_market_data()
        asyncio.create_task(self.orderly_mids_loop())
        asyncio.create_task(self.hl_mids_loop())
        asyncio.create_task(self.leaderboard_loop())
        await self.process_loop()

    async def close(self):
        self.running = False; await self.session.close()

    async def api_get(self, url: str):
        async with self.session.get(url, timeout=15) as r:
            if r.status == 200: return await r.json()
        return None

async def main():
    bot = NadoQuantBot(); loop = asyncio.get_running_loop(); stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    t = asyncio.create_task(bot.run()); await stop.wait(); await bot.close(); t.cancel()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
