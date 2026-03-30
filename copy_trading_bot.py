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

# Suppress harmless warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("PRIVATE_KEY")
NADO_OWNER_ADDR = os.getenv("SUBACCOUNT_OWNER")
# Nado Mainnet (Built on Ink)
NADO_ENV = NadoClientMode.MAINNET 

TOP_X_TRADERS = 10
ALLOWED_COINS = ["BTC", "ETH", "SOL", "BNB", "HYPE", "PAX", "WTI"]
RISK_POS_PCT = 0.10        # 10% of available margin per trade
MIN_ORDER_USD = 11.0       

# Logging Setup (Talkative)
os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s", 
    handlers=[log_handler, logging.StreamHandler()]
)

class NadoQuantBot:
    def __init__(self):
        logging.info("--- INITIALIZING NADO QUANT ENGINE (INK MAINNET) ---")
        
        try:
            self.signer = Account.from_key(NADO_PK)
            self.owner = self.signer.address
            logging.info(f"[AUTH] Signer active for address: {self.owner}")
        except Exception as e:
            logging.error(f"[AUTH] CRITICAL: Private Key error: {e}"); sys.exit(1)

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

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    self.bot_state = json.load(f)
                logging.info(f"[MEMORY] Recovered {len(self.bot_state['positions'])} mirrors.")
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.bot_state, f)

    async def sync_market_data(self):
        """Discovers True Product IDs from Nado's Context API."""
        while self.running:
            try:
                logging.info("[SYNC] Fetching Perpetual IDs and Ticks from Nado...")
                res = await asyncio.to_thread(self.client.context.engine_client.get_all_products)
                perps = getattr(res, 'perp_products', [])
                
                count = 0
                for p in perps:
                    symbol = str(getattr(p, 'symbol', '')).upper()
                    # Nado strictly uses format "BTC-PERP"
                    if symbol.endswith('-PERP'):
                        coin = symbol.split('-')[0]
                        if coin in ALLOWED_COINS:
                            pid = getattr(p, 'product_id', None)
                            p_tick_x18 = Decimal(str(getattr(p, 'price_increment_x18', 0)))
                            s_tick_x18 = Decimal(str(getattr(p, 'base_tick_x18', 0)))
                            
                            if pid is not None:
                                self.product_map[coin] = {
                                    "id": int(pid), 
                                    "p_tick_x18": p_tick_x18, 
                                    "s_tick_x18": s_tick_x18,
                                    "min_usd": float(Decimal(str(getattr(p, 'min_size', 0))) / Decimal("1e18"))
                                }
                                count += 1
                if count > 0:
                    logging.info(f"[SYNC] Success: {count} Nado Perpetual assets mapped.")
                    return True
            except Exception as e:
                logging.error(f"[SYNC] Error: {e}")
            await asyncio.sleep(10)

    async def leaderboard_loop(self):
        while self.running:
            try:
                logging.info("[LEADERBOARD] Scanning for top performers...")
                async with self.session.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard") as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = []; self._extract_traders(data, raw)
                        processed = { (t.get("account") or t.get("user")): float(t.get("roiWeek", t.get("roi", 0))) for t in raw if (t.get("account") or t.get("user")) }
                        ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                        top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                        
                        for p in self.bot_state["positions"].values(): top_selected.add(p["trader"])
                        new = top_selected - self.tracked_traders; old = self.tracked_traders - top_selected
                        self.tracked_traders = top_selected
                        
                        for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        for t in old:
                            if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                        logging.info(f"[TRACKER] Monitoring {len(self.tracked_traders)} Pro Traders on HL.")
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
                        logging.info(f"[SIGNAL] {trader[:6]} opened {coin}. Attempting Nado mirror...")
                        await self.execute_nado_order(coin, szi > 0, trader, False)
                for coin in old_state.keys():
                    if coin not in new_state:
                        if coin in self.bot_state["positions"]:
                            logging.info(f"[SIGNAL] {trader[:6]} closed {coin}. Closing Nado mirror...")
                            await self.execute_nado_order(coin, False, trader, True)
                self.trader_positions[trader] = new_state
            finally: self.signal_queue.task_done()

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        market = self.product_map.get(coin.upper())
        if not market: 
            logging.warning(f"[EXEC] Skip {coin}: Rules not yet synced from Nado.")
            return

        try:
            # 1. Official SDK Price Fetch (Kills Error 2007)
            # This asks the Nado Engine for the price of THIS specific ID
            price_res = await asyncio.to_thread(self.client.perp.get_prices, market["id"])
            mark_px_x18 = int(getattr(price_res, 'mark_price_x18', 0))
            if mark_px_x18 == 0:
                logging.error(f"[PRICE] Could not fetch mark price for {coin}"); return
            
            px_float = float(mark_px_x18) / 1e18

            # 2. Add Slippage (5%) & Round perfectly to Tick
            target_px_x18 = Decimal(str(mark_px_x18)) * Decimal("1.05" if is_buy else "0.95")
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px_x18 = Decimal(str(mark_px_x18)) * Decimal("1.10" if is_buy else "0.90")

            final_px_x18 = (target_px_x18 / market["p_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["p_tick_x18"]
            
            # 3. Official Margin Manager Check
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            manager = MarginManager(sub_info, getattr(iso_res, 'isolated_positions', []))
            summary = manager.calculate_account_summary()
            
            available = float(summary.funds_available)
            usd_amt = max(available * RISK_POS_PCT, MIN_ORDER_USD, market["min_usd"])
            if available < usd_amt and not is_close:
                logging.warning(f"[ACCOUNT] Skip {coin}: Available Margin ${available:.2f} is too low."); return
            
            # 4. Precision Quantity
            qty_x18 = (Decimal(str(usd_amt)) * Decimal("1e18") / Decimal(str(px_float)) / market["s_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["s_tick_x18"]
            if qty_x18 <= 0:
                logging.error(f"[MATH] Quantity for {coin} rounded to zero."); return

            # 5. Construct Official OrderParams (From Nado Docs)
            sender_params = SubaccountParams(subaccount_owner=NADO_OWNER_ADDR or self.owner, subaccount_name="default")
            
            order = OrderParams(
                sender=sender_params, 
                priceX18=int(final_px_x18), 
                amount=int(qty_x18) if is_buy else -int(qty_x18),
                expiration=get_expiration_timestamp(60), 
                nonce=gen_order_nonce(),
                appendix=build_appendix(order_type=OrderType.IOC, reduce_only=is_close)
            )

            logging.info(f"[EXEC] Sending Order: {coin} | Side: {'BUY' if is_buy else 'SELL'} | Px: {px_float:.4f}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"[SUCCESS] {coin} mirror complete on Nado.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state()
            else:
                logging.error(f"[FAILED] Nado rejected trade: {getattr(res, 'message', str(res))}")

        except Exception as e: logging.error(f"[CRITICAL] Execution failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        # BLOCKING START: Guarantees IDs are matched before any signals process
        await self.sync_market_data()
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
