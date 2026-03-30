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

TOP_X_TRADERS = 30 
ALLOWED_COINS = ["BTC", "ETH", "SOL", "BNB", "PAX", "XAG", "WTI", "HYPE"]
RISK_POS_PCT = 0.10        # 10% of available margin per trade
MIN_ORDER_USD = 11.0       

# Logging Setup
os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[log_handler, logging.StreamHandler()])

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Master Nado Execution Engine...")
        try:
            self.signer = Account.from_key(NADO_PK)
            self.owner = self.signer.address
            logging.info(f"Signer active: {self.owner}")
        except Exception as e:
            logging.error(f"Signer Error: {e}"); sys.exit(1)

        mode = NadoClientMode.MAINNET if DATA_ENV_STR == "nadoMainnet" else NadoClientMode.TESTNET
        self.client = create_nado_client(mode, self.signer)
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
        
        # Hardcoded Verified Perp IDs for Mainnet (Backup Truth)
        self.product_map = {
            "BTC": {"id": 2, "p_tick_x18": Decimal("100000000000000000"), "s_tick_x18": Decimal("10000000000000")},
            "ETH": {"id": 3, "p_tick_x18": Decimal("10000000000000000"), "s_tick_x18": Decimal("100000000000000")},
            "SOL": {"id": 4, "p_tick_x18": Decimal("1000000000000000"), "s_tick_x18": Decimal("10000000000000000")},
            "BNB": {"id": 5, "p_tick_x18": Decimal("10000000000000000"), "s_tick_x18": Decimal("10000000000000000")},
            "HYPE": {"id": 100, "p_tick_x18": Decimal("1000000000000000"), "s_tick_x18": Decimal("100000000000000000")}
        }

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f: self.bot_state = json.load(f)
                logging.info(f"Memory: Loaded {len(self.bot_state['positions'])} mirrors.")
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f: json.dump(self.bot_state, f)

    def _get_val(self, obj, key):
        """Pydantic-safe extractor."""
        if isinstance(obj, dict): return obj.get(key)
        return getattr(obj, key, None)

    async def sync_market_data(self):
        """Bypasses SDK objects to fetch pure Symbol data from Nado REST Gateway."""
        while self.running:
            try:
                async with self.session.get("https://gateway.prod.nado.xyz/v1/symbols") as r:
                    if r.status == 200:
                        data = await r.json()
                        for s in data:
                            sym = str(s.get('symbol', '')).upper()
                            if sym.endswith('-PERP'):
                                coin = sym.split('-')[0]
                                if coin in ALLOWED_COINS:
                                    pid = s.get('product_id')
                                    p_tick_x18 = Decimal(str(s.get('price_increment_x18', 0)))
                                    s_tick_x18 = Decimal(str(s.get('size_increment_x18', s.get('size_increment', 0))))
                                    if pid and p_tick_x18 > 0:
                                        self.product_map[coin] = {
                                            "id": int(pid), "p_tick_x18": p_tick_x18, 
                                            "s_tick_x18": s_tick_x18, "status": s.get('trading_status', 'live'),
                                            "min_usd": float(Decimal(str(s.get('min_size', 0))) / Decimal("1e18"))
                                        }
                if self.product_map:
                    logging.info(f"Sync SUCCESS: {len(self.product_map)} Perpetual IDs verified.")
                    return True
            except: pass
            await asyncio.sleep(10)

    async def sync_nado_memory(self):
        while self.running:
            try:
                sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
                live_coins = {str(getattr(p, 'symbol', '')).split('-')[0].upper() for p in getattr(sub_info, 'perp_positions', []) if abs(float(getattr(p, 'amount', 0))) > 1e-5}
                to_clear = [c for c in self.bot_state["positions"].keys() if c not in live_coins]
                for c in to_clear:
                    logging.info(f"Manual Close: Removing {c} from memory.")
                    del self.bot_state["positions"][c]
                if to_clear: self.save_state()
            except: pass
            await asyncio.sleep(30)

    async def leaderboard_loop(self):
        while self.running:
            try:
                async with self.session.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard") as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = []; self._extract_greedy(data, raw)
                        processed = { (t.get("account") or t.get("user")): float(t.get("roiWeek", t.get("roi", 0))) for t in raw if (t.get("account") or t.get("user")) }
                        ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                        top = {r[0] for r in ranked[:TOP_X_TRADERS]}
                        for p in self.bot_state["positions"].values(): top.add(p["trader"])
                        new = top - self.tracked_traders; old = self.tracked_traders - top
                        self.tracked_traders = top
                        for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        for t in old:
                            if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                        logging.info(f"Monitoring {len(self.tracked_traders)} Pro Traders on HL.")
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
                        logging.info(f"SIGNAL: {trader[:6]} opened {c}")
                        await self.execute_nado_order(c, s > 0, trader, False)
                for c in old_s.keys():
                    if c not in new_s and c.upper() in ALLOWED_COINS:
                        if c in self.bot_state["positions"]:
                            logging.info(f"SIGNAL: {trader[:6]} closed {c}")
                            await self.execute_nado_order(c, False, trader, True)
                self.trader_positions[trader] = new_s
            finally: self.signal_queue.task_done()

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        market = self.product_map.get(coin.upper())
        if not market: return

        try:
            # 1. IMPLEMENTING client.perp.get_prices AS REQUESTED
            # This is the FIX for Error 2007. It fetches the price NADO expects for THIS specific ID.
            price_res = await asyncio.to_thread(self.client.perp.get_prices, market["id"])
            mark_px_x18 = int(getattr(price_res, 'mark_price_x18', 0))
            if mark_px_x18 == 0: return

            # 2. Add Slippage and Round perfectly (Fixes Error 2000)
            target_px_x18 = Decimal(str(mark_px_x18)) * Decimal("1.03" if is_buy else "0.97")
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px_x18 = Decimal(str(mark_px_x18)) * Decimal("1.05" if is_buy else "0.95")

            final_px_x18 = (target_px_x18 / market["p_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["p_tick_x18"]
            
            # 3. Official Margin Manager
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            manager = MarginManager(sub_info, getattr(iso_res, 'isolated_positions', []))
            summary = manager.calculate_account_summary()
            
            available = float(summary.funds_available)
            usd_amt = max(available * RISK_POS_PCT, MIN_ORDER_USD)
            if available < usd_amt and not is_close: return
            
            # 4. Precision Quantity
            px_float = float(from_x18(mark_px_x18))
            qty_x18 = (Decimal(str(usd_amt)) * Decimal("1e18") / Decimal(str(px_float)) / market["s_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["s_tick_x18"]
            if qty_x18 <= 0: qty_x18 = market["s_tick_x18"] * 10 

            # 5. Build Final Nado Order
            order_exec = OrderType.IOC
            if str(market.get("status", "")).lower() == "post_only": order_exec = OrderType.POST_ONLY

            order = OrderParams(
                sender=self.subaccount_hex, priceX18=int(final_px_x18), amount=int(qty_x18) if is_buy else -int(qty_x18),
                expiration=get_expiration_timestamp(60), nonce=gen_order_nonce(),
                appendix=build_appendix(order_type=order_exec, reduce_only=is_close)
            )

            logging.info(f"NADO EXEC PERP: {coin} (ID:{market['id']}) | Px: {float(final_px_x18)/1e18}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin} trade mirrored.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state()
            else: logging.error(f"NADO REJECTED: {getattr(res, 'message', str(res))}")
        except Exception as e: logging.error(f"Failure: {e}")

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
