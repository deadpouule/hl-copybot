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
from nado_protocol.utils.math import to_x18, from_x18
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

TOP_X_TRADERS = 10 
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
        logging.info("Initializing Master Nado Execution Engine...")
        
        # 1. Signer Setup
        try:
            self.signer = Account.from_key(NADO_PK)
            self.owner = self.signer.address
            logging.info(f"Signer active: {self.owner}")
        except Exception as e:
            logging.error(f"CRITICAL: .env PRIVATE_KEY error: {e}"); sys.exit(1)

        # 2. Client Initialization
        nado_mode = NadoClientMode.MAINNET if DATA_ENV_STR == "nadoMainnet" else NadoClientMode.TESTNET
        self.client = create_nado_client(nado_mode, self.signer)
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

        # Burst Protection
        self.cached_funds = None
        self.last_funds_check = 0

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
        """Discovers True Product IDs. Strictly filters for PERPETUALS."""
        while self.running:
            try:
                logging.info("Syncing Market rules from Nado Engine...")
                res = await asyncio.to_thread(self.client.context.engine_client.get_symbols)
                items = res if isinstance(res, list) else getattr(res, 'data', [])
                
                count = 0
                for m in items:
                    # Nado Documented format: "BTC-PERP"
                    symbol = str(self._safe_get(m, 'symbol', '')).upper()
                    if symbol.endswith('-PERP'):
                        coin = symbol.split('-')[0]
                        if coin in ALLOWED_COINS:
                            pid = self._safe_get(m, 'product_id')
                            p_tick_x18 = Decimal(str(self._safe_get(m, 'price_increment_x18', 0)))
                            s_tick_x18 = Decimal(str(self._safe_get(m, 'size_increment_x18') or self._safe_get(m, 'size_increment', 0)))
                            status = self._safe_get(m, 'trading_status', 'live')

                            if pid is not None and p_tick_x18 > 0:
                                self.product_map[coin] = {
                                    "id": int(pid), 
                                    "p_tick_x18": p_tick_x18, 
                                    "s_tick_x18": s_tick_x18, 
                                    "status": status,
                                    "min_usd": float(Decimal(str(self._safe_get(m, 'min_size', 0))) / Decimal("1e18"))
                                }
                                count += 1
                if count > 0:
                    logging.info(f"Sync SUCCESS: {count} Perp asset IDs active.")
                    return True
            except Exception as e:
                logging.error(f"Sync failed: {e}. Retrying...")
            await asyncio.sleep(10)

    async def leaderboard_loop(self):
        while self.running:
            try:
                async with self.session.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard") as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = []; self._extract_traders(data, raw)
                        processed = { (t.get("account") or t.get("user") or t.get("ethAddress")): float(t.get("roiWeek", t.get("roi", 0))) for t in raw if (t.get("account") or t.get("user") or t.get("ethAddress")) }
                        ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                        top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                        for p in self.bot_state["positions"].values(): top_selected.add(p["trader"])
                        new = top_selected - self.tracked_traders; old = self.tracked_traders - top_selected
                        self.tracked_traders = top_selected
                        for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        for t in old:
                            if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                        logging.info(f"Monitoring {len(self.tracked_traders)} Pro Traders on HL.")
            except: pass
            await asyncio.sleep(300)

    def _extract_traders(self, obj, container):
        if isinstance(obj, dict):
            u = obj.get("account") or obj.get("user") or obj.get("ethAddress") or obj.get("address")
            if u and isinstance(u, str) and u.startswith("0x") and len(u) > 30: container.append(obj)
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
                        if coin in self.bot_state["positions"]:
                            logging.info(f"HL SIGNAL: {trader[:6]} closed {coin}")
                            await self.execute_nado_order(coin, False, trader, is_close=True)
                self.trader_positions[trader] = new_state
            finally: self.signal_queue.task_done()

    def _round_step(self, val, step: Decimal) -> Decimal:
        val_d = Decimal(str(val))
        return (val_d / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step

    async def _get_available_margin(self):
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
            # 1. Official SDK Price Fetch (ID-STRICT)
            # Fixes Error 2007: ensuring the price matches exactly this product_id
            price_res = await asyncio.to_thread(self.client.perp.get_prices, market["id"])
            px_x18 = int(getattr(price_res, 'mark_price_x18', 0))
            if px_x18 == 0: return
            px = float(from_x18(px_x18))

            # 2. Precision Math
            target_px_x18 = Decimal(str(px_x18)) * Decimal("1.03" if is_buy else "0.97")
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px_x18 = Decimal(str(px_x18)) * Decimal("1.05" if is_buy else "0.95")

            final_px_x18 = (target_px_x18 / market["p_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["p_tick_x18"]
            
            # 3. Margin & Quantity
            available = await self._get_available_margin()
            usd_amt = max(available * RISK_POS_PCT, MIN_ORDER_USD, market["min_usd"])
            if available < usd_amt and not is_close: return
            
            qty = usd_amt / px
            final_qty_x18 = (Decimal(str(qty)) * Decimal("1e18") / market["s_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["s_tick_x18"]
            if final_qty_x18 <= 0: return

            # 4. Handle Post-Only mode
            order_exec = OrderType.IOC
            if str(market.get("status", "")).lower() == "post_only": order_exec = OrderType.POST_ONLY

            # 5. Build Nado Order object
            order = OrderParams(
                sender=self.subaccount_hex, 
                priceX18=int(final_px_x18), 
                amount=int(final_qty_x18) if is_buy else -int(final_qty_x18),
                expiration=get_expiration_timestamp(60), 
                nonce=gen_order_nonce(),
                appendix=build_appendix(order_type=order_exec, reduce_only=is_close)
            )

            logging.info(f"NADO EXEC: {coin} | Px: {float(final_px_x18)/1e18} | Qty: {float(final_qty_x18)/1e18}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin} trade complete.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state(); self.last_funds_check = 0
            else:
                logging.error(f"NADO REJECTED: {getattr(res, 'message', str(res))}")

        except Exception as e: logging.error(f"Execution failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        # Ensure we wait for IDs before starting detection
        await self.sync_market_data()
        asyncio.create_task(self.leaderboard_loop())
        asyncio.create_task(self.sync_nado_memory()) 
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
