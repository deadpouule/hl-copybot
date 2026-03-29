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

TOP_X_TRADERS = 5
ALLOWED_COINS = ["BTC", "ETH", "SOL", "HYPE", "BNB", "PAX", "XAG", "WTI"]
RISK_POS_PCT = 0.10        # 10% of available margin per trade
MIN_ORDER_USD = 11.0       # Exchange minimum

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
        logging.info("Initializing Master Nado Engine...")
        
        try:
            self.signer = Account.from_key(NADO_PK)
            self.owner = self.signer.address
            logging.info(f"Signer active: {self.owner}")
        except Exception as e:
            logging.error(f"CRITICAL: .env PRIVATE_KEY error: {e}")
            sys.exit(1)

        nado_mode = NadoClientMode.MAINNET if DATA_ENV_STR == "nadoMainnet" else NadoClientMode.TESTNET
        self.client = create_nado_client(nado_mode, self.signer)
        self.subaccount_hex = subaccount_to_hex(NADO_OWNER_ADDR or self.owner, "default")
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.all_mids = {} # HL cache
        self.orderly_prices = {} # Nado cache
        
        self.state_file = "nado_bot_state.json"
        self.bot_state = {"positions": {}}
        self.load_state()

        self.signal_queue = asyncio.Queue()
        self.running = False
        
        # 3. CORRECTED ID MAP: BTC=1, ETH=2 (Verified from your earlier logs)
        self.product_map = {
            "BTC": {"id": 1, "p_tick": Decimal("0.1"), "s_tick": Decimal("0.00001"), "status": "live"},
            "ETH": {"id": 2, "p_tick": Decimal("0.01"), "s_tick": Decimal("0.0001"), "status": "live"},
            "SOL": {"id": 3, "p_tick": Decimal("0.001"), "s_tick": Decimal("0.01"), "status": "live"},
            "BNB": {"id": 4, "p_tick": Decimal("0.01"), "s_tick": Decimal("0.001"), "status": "live"},
            "HYPE": {"id": 100, "p_tick": Decimal("0.001"), "s_tick": Decimal("0.01"), "status": "live"}
        }

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    self.bot_state = json.load(f)
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.bot_state, f)

    def _safe_get(self, obj, key, default=None):
        if isinstance(obj, dict): return obj.get(key, default)
        return getattr(obj, key, default)

    async def sync_market_data(self):
        """Background sync to update rules dynamically."""
        while self.running:
            try:
                res = await asyncio.to_thread(self.client.context.engine_client.get_all_products)
                perps = getattr(res, 'perp_products', [])
                for p in perps:
                    symbol = str(getattr(p, 'symbol', '')).upper()
                    for coin in ALLOWED_COINS:
                        if coin in symbol:
                            pid = getattr(p, 'product_id', None)
                            p_tick = float(getattr(p, 'price_increment_x18', 0)) / 1e18
                            s_tick = float(getattr(p, 'base_tick_x18', 0)) / 1e18
                            if pid and p_tick > 0:
                                self.product_map[coin] = {
                                    "id": int(pid), "p_tick": Decimal(str(p_tick)), 
                                    "s_tick": Decimal(str(s_tick)), "status": getattr(p, 'trading_status', 'live')
                                }
            except: pass
            await asyncio.sleep(600)

    async def orderly_mids_loop(self):
        """Oracle Price Watcher."""
        while self.running:
            try:
                async with self.session.get("https://api-evm.orderly.org/v1/public/futures") as r:
                    if r.status == 200:
                        js = await r.json()
                        for row in js.get("data", {}).get("rows", []):
                            symbol = row.get("symbol", "").upper()
                            for coin in ALLOWED_COINS:
                                if coin in symbol:
                                    self.orderly_prices[coin] = float(row.get("mark_price", 0))
            except: pass
            await asyncio.sleep(5)

    async def hl_mids_loop(self):
        """Backup HL Price cache."""
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
                        raw = []; self._extract_traders(data, raw)
                        processed = {}
                        for t in raw:
                            addr = t.get("account") or t.get("user") or t.get("ethAddress")
                            roi = float(t.get("roiWeek", t.get("roi", 0)))
                            if addr not in processed or roi > processed[addr]: processed[addr] = roi
                        ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                        top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                        for p in self.bot_state["positions"].values(): top_selected.add(p["trader"])
                        new = top_selected - self.tracked_traders
                        old = self.tracked_traders - top_selected
                        self.tracked_traders = top_selected
                        for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        for t in old:
                            if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                        logging.info(f"Monitoring {len(self.tracked_traders)} Pro Traders on HL.")
            except: pass
            await asyncio.sleep(300)

    def _extract_traders(self, obj, container):
        if isinstance(obj, dict):
            user = obj.get("account") or obj.get("user") or obj.get("ethAddress")
            if user and re.match(r"^0x[a-fA-F0-9]{40}$", str(user)): container.append(obj)
            for v in obj.values(): self._extract_traders(v, container)
        elif isinstance(obj, list):
            for i in obj: self._extract_traders(i, container)

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
                new_state = {p["position"]["coin"]: float(p["position"]["szi"]) for p in raw_pos if float(p["position"]["szi"]) != 0}
                old_state = self.trader_positions.get(trader, {})
                for coin, szi in new_state.items():
                    if coin not in old_state and coin.upper() in ALLOWED_COINS:
                        logging.info(f"HL SIGNAL: {trader[:6]} opened {coin}")
                        await self.execute_nado_order(coin, szi > 0, trader, is_close=False)
                for coin in old_state.keys():
                    if coin not in new_state and coin.upper() in ALLOWED_COINS:
                        logging.info(f"HL SIGNAL: {trader[:6]} closed {coin}")
                        await self.execute_nado_order(coin, False, trader, is_close=True)
                self.trader_positions[trader] = new_state
            finally: self.signal_queue.task_done()

    def _round_step(self, val, step: Decimal) -> Decimal:
        val_d = Decimal(str(val))
        return (val_d / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        if is_close and coin not in self.bot_state["positions"]: return
        market = self.product_map.get(coin.upper())
        if not market:
            logging.warning(f"Skip {coin}: ID not mapped yet.")
            return

        try:
            # Price lookup with backup
            px = self.orderly_prices.get(coin.upper(), 0.0)
            if px == 0: px = float(self.all_mids.get(coin.upper(), 0.0))
            if px == 0: 
                logging.error(f"Skip {coin}: Price missing.")
                return

            target_px = px * (1.05 if is_buy else 0.95)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            final_px_dec = self._round_step(target_px, market["p_tick"])
            
            # Margin Manager
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            manager = MarginManager(sub_info, getattr(iso_res, 'isolated_positions', []))
            summary = manager.calculate_account_summary()
            
            available = float(summary.funds_available)
            usd_amt = max(available * RISK_POS_PCT, MIN_ORDER_USD)
            if available < usd_amt and not is_close:
                logging.warning(f"Skip {coin}: Balance low (${available:.2f})")
                return
            
            final_qty_dec = self._round_step(usd_amt / px, market["s_tick"])
            if final_qty_dec <= 0: return

            amount_x18 = int((final_qty_dec * Decimal("1e18")).to_integral_value())
            if not is_buy: amount_x18 = -amount_x18
            price_x18 = int((final_px_dec * Decimal("1e18")).to_integral_value())

            order_exec = OrderType.IOC
            if market.get("status") == "post_only": order_exec = Order
