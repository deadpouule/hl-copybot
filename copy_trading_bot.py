import asyncio, aiohttp, websockets, json, logging, os, signal, time, struct, warnings, re, sys
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN

# --- OFFICIAL NADO PROTOCOL SDK IMPORTS ---
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
NADO_OWNER = os.getenv("SUBACCOUNT_OWNER")
# Nado Mainnet Environment
NADO_ENV = NadoClientMode.MAINNET if os.getenv("DATA_ENV", "nadoMainnet") == "nadoMainnet" else NadoClientMode.TESTNET

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
        logging.info("Initializing Final Master Nado Engine...")
        self.client = create_nado_client(NADO_ENV, NADO_PK)
        self.subaccount_hex = subaccount_to_hex(NADO_OWNER, "default")
        self.owner = self.client.context.signer.address
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.orderly_prices = {}
        
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
                logging.info(f"Memory: Loaded {len(self.bot_state['positions'])} active positions.")
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.bot_state, f)

    def _safe_get(self, obj, key, default=None):
        if isinstance(obj, dict): return obj.get(key, default)
        return getattr(obj, key, default)

    async def sync_market_data(self):
        """Fetches precision rules directly from Nado /symbols endpoint."""
        while self.running:
            try:
                logging.info("Syncing Market Rules from Nado Engine...")
                res = await asyncio.to_thread(self.client.context.engine_client.get_symbols)
                items = res if isinstance(res, list) else getattr(res, 'data', [])
                
                count = 0
                for m in items:
                    symbol = str(self._safe_get(m, 'symbol', ''))
                    if symbol.endswith('-PERP'):
                        coin = symbol.split('-')[0].upper()
                        pid = self._safe_get(m, 'product_id')
                        p_tick_x18 = self._safe_get(m, 'price_increment_x18')
                        s_tick_x18 = self._safe_get(m, 'size_increment_x18') or self._safe_get(m, 'size_increment')
                        min_size_x18 = self._safe_get(m, 'min_size')

                        if pid is not None and p_tick_x18:
                            self.product_map[coin] = {
                                "id": int(pid),
                                "p_tick_x18": Decimal(str(p_tick_x18)),
                                "s_tick_x18": Decimal(str(s_tick_x18)),
                                "min_size_x18": Decimal(str(min_size_x18 or "0"))
                            }
                            count += 1
                
                if count > 0:
                    logging.info(f"Sync SUCCESS: {count} Nado symbols active.")
                    return True
            except Exception as e:
                logging.error(f"Sync failed: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

    async def orderly_prices_loop(self):
        """Oracle Price Watcher to ensure we stay within the 20-500% mandatory range."""
        while self.running:
            try:
                async with self.session.get("https://api-evm.orderly.org/v1/public/futures") as r:
                    if r.status == 200:
                        js = await r.json()
                        for row in js.get("data", {}).get("rows", []):
                            symbol = row.get("symbol", "")
                            if symbol.endswith("-PERP"):
                                coin = symbol.split("-")[0].upper()
                                self.orderly_prices[coin] = float(row.get("mark_price", 0))
            except: pass
            await asyncio.sleep(5)

    async def leaderboard_loop(self):
        """Monitors HL Leaderboard for Top 5 Performers."""
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
                        
                        for t in new:
                            self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
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
        """Mathematically perfect rounding to satisfy Orderly's divisibility rules."""
        val_d = Decimal(str(val))
        return (val_d / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        # Position existence checks
        if not is_close and coin in self.bot_state["positions"]: return
        if is_close and coin not in self.bot_state["positions"]: return
        market = self.product_map.get(coin.upper())
        if not market: return

        try:
            # 1. Oracle Price Check (Prevent Error 2007)
            px = self.orderly_prices.get(coin.upper(), 0.0)
            if px == 0: return

            # 2. Add 5% Slippage to ensure aggressive fill
            target_px = px * (1.05 if is_buy else 0.95)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            final_px_x18 = (Decimal(str(target_px)) * Decimal("1e18") / market["p_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["p_tick_x18"]
            
            # 3. Margin Manager (SDK Official Logic)
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            manager = MarginManager(sub_info, getattr(iso_res, 'isolated_positions', []))
            summary = manager.calculate_account_summary()
            
            available = float(summary.funds_available)
            usd_amt = max(available * RISK_POS_PCT, MIN_ORDER_USD)
            if available < usd_amt and not is_close:
                logging.warning(f"Skipping {coin}: Funds low (${available:.2f})")
                return
            
            # 4. Quantity Precision (Prevent Error 2000/2001)
            raw_qty = usd_amt / px
            final_qty_x18 = (Decimal(str(raw_qty)) * Decimal("1e18") / market["s_tick_x18"]).quantize(Decimal('1'), rounding=ROUND_DOWN) * market["s_tick_x18"]

            if final_qty_x18 <= 0: return
            
            amount_x18_signed = int(final_qty_x18)
            if not is_buy: amount_x18_signed = -amount_x18_signed

            # 5. Build Final Nado Order
            appendix = build_appendix(order_type=OrderType.IOC, reduce_only=is_close)
            order = OrderParams(
                sender=self.subaccount_hex,
                priceX18=int(final_px_x18),
                amount=amount_x18_signed,
                expiration=get_expiration_timestamp(60),
                nonce=gen_order_nonce(),
                appendix=appendix
            )

            logging.info(f"NADO EXECUTE: {coin} | Side: {'BUY' if is_buy else 'SELL'} | Px: {float(final_px_x18)/1e18}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin} trade complete.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state()
            else:
                msg = getattr(res, 'message', str(res))
                if '2117' in msg: logging.warning(f"SKIPPED: {coin} is in Post-Only mode.")
                else: logging.error(f"NADO REJECTED: {msg}")

        except Exception as e: logging.error(f"Execution failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        await self.sync_market_data()
        asyncio.create_task(self.orderly_prices_loop())
        asyncio.create_task(self.leaderboard_loop())
        await self.process_loop()

    async def close(self):
        self.running = False; await self.session.close()

async def main():
    bot = NadoQuantBot(); loop = asyncio.get_running_loop(); stop = asyncio.Event()
    def sig_h(): stop.set()
    for s in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(s, sig_h)
    t = asyncio.create_task(bot.run()); await stop.wait(); await bot.close(); t.cancel()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
