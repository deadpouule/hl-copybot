import asyncio, aiohttp, websockets, json, logging, os, signal, time, warnings, re
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
from decimal import Decimal, ROUND_HALF_UP

# --- OFFICIAL NADO PROTOCOL SDK IMPORTS ---
from nado_protocol.client import create_nado_client
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix
from nado_protocol.utils.bytes32 import subaccount_to_hex
from nado_protocol.utils.margin_manager import MarginManager

warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
NADO_ID = os.getenv("NADO_ACCOUNT_ID")
NADO_ENV = "mainnet"

TOP_X_TRADERS = 5
ALLOWED_COINS =["BTC", "ETH", "SOL", "HYPE", "BNB", "PAX", "XAG", "WTI"]

RISK_POS_PCT = 0.10        # 10% of available margin per trade
MIN_ORDER_USD = 11.0       # Exchange minimum

os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s", 
    handlers=[log_handler, logging.StreamHandler()]
)

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Ultimate Nado Execution Engine...")
        self.client = create_nado_client(NADO_ENV, NADO_PK)
        self.owner = self.client.context.engine_client.signer.address
        self.subaccount_hex = subaccount_to_hex(self.owner, "default")
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.all_mids = {}
        
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
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.bot_state, f)

    async def sync_market_data(self):
        """Bypasses SDK objects. Fetches pristine JSON directly from Orderly API."""
        while self.running:
            try:
                logging.info("Downloading Market Rules directly from Orderly REST API...")
                async with self.session.get("https://api-evm.orderly.org/v1/public/info") as r:
                    res = await r.json()
                
                rows = res.get("data", {}).get("rows",[])
                count = 0
                for m in rows:
                    symbol = m.get("symbol", "")
                    if "PERP_" in symbol:
                        coin = symbol.split("_")[1]
                        
                        pid = m.get("product_id")
                        p_tick = m.get("quote_tick")
                        s_tick = m.get("base_tick")
                        min_s = m.get("base_min")
                        
                        if pid is not None and p_tick and s_tick:
                            self.product_map[coin] = {
                                "id": int(pid),
                                "p_tick": float(p_tick),
                                "s_tick": float(s_tick),
                                "min_s": float(min_s)
                            }
                            count += 1
                
                if count > 0:
                    logging.info(f"Market Sync SUCCESS! {count} exact asset pairs mapped.")
                    return True
                else:
                    logging.error("Failed to parse Orderly REST API. Retrying in 10s...")
            except Exception as e:
                logging.error(f"REST Market Sync Exception: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

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
        """Scans HL Leaderboard for top Weekly performers."""
        while self.running:
            try:
                data = await self.api_get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
                if data:
                    raw =[]; self._extract_traders(data, raw)
                    processed = {}
                    for t in raw:
                        addr = t.get("account") or t.get("user") or t.get("ethAddress")
                        roi = float(t.get("roiWeek", t.get("roi", 0)))
                        if addr not in processed or roi > processed[addr]: processed[addr] = roi
                    
                    ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                    top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                    
                    # Prevent closing connections for traders we have active trades with
                    for p in self.bot_state["positions"].values():
                        top_selected.add(p["trader"])

                    new = top_selected - self.tracked_traders
                    old = self.tracked_traders - top_selected
                    self.tracked_traders = top_selected
                    
                    for t in new: self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    for t in old:
                        if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                    
            except Exception as e: logging.error(f"LB Loop Error: {e}")
            await asyncio.sleep(300)

    async def mids_ws_loop(self):
        """Maintains an instant, zero-latency cache of all prices to guarantee <200ms execution."""
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running:
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                    async for msg in ws:
                        if not self.running: break
                        d = json.loads(msg)
                        if d.get("channel") == "allMids": self.all_mids.update(d["data"]["mids"])
            except: await asyncio.sleep(2)

    async def trader_ws_loop(self, trader: str):
        """Listens directly to the HL WebSocket for instant trade signals."""
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
        """Instantly parses signals from the queue and routes to execution."""
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

    def _round_step(self, val, step):
        """Flawless Decimal Rounding to avoid floating-point EVM rejections."""
        if step == 0: return float(val)
        val_d = Decimal(str(val))
        step_d = Decimal(str(step))
        rounded = (val_d / step_d).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step_d
        return float(rounded)

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        if is_close and coin not in self.bot_state["positions"]: return
        
        market = self.product_map.get(coin.upper())
        if not market: return

        try:
            # 1. Instant Proxy Price from Cache (0 ms delay)
            px = float(self.all_mids.get(coin.upper(), 0))
            if px == 0: return

            # 2. Precision Math & Slippage (5% allowed to guarantee market fill)
            target_px = px * (1.05 if is_buy else 0.95)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            final_px = self._round_step(target_px, market["p_tick"])
            
            # 3. Official SDK Margin Manager for Flawless Balance Checks
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            iso_pos = getattr(iso_res, 'isolated_positions',[])
            
            manager = MarginManager(sub_info, iso_pos)
            summary = manager.calculate_account_summary()
            
            available_margin = float(summary.funds_available)
            usd_amt = max(available_margin * RISK_POS_PCT, MIN_ORDER_USD)
            
            if available_margin < usd_amt and not is_close:
                logging.warning(f"Skipping {coin}: Insufficient Margin (Available: ${available_margin:.2f})")
                return
            
            qty = usd_amt / px
            final_qty = self._round_step(qty, market["s_tick"])

            if final_qty < market["min_s"]:
                logging.warning(f"Skipping {coin}: Quantity {final_qty} below exchange min {market['min_s']}")
                return

            # 4. Flawless X18 Orderly Math Integration
            appendix = build_appendix(order_type=OrderType.IOC, reduce_only=is_close)
            
            price_x18 = int((Decimal(str(final_px)) * Decimal("1e18")).to_integral_value())
            amount_x18 = int((Decimal(str(final_qty)) * Decimal("1e18")).to_integral_value())
            if not is_buy: amount_x18 = -amount_x18

            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=self.owner, subaccount_name="default"),
                priceX18=price_x18,
                amount=amount_x18,
                expiration=get_expiration_timestamp(60),
                nonce=gen_order_nonce(),
                appendix=appendix
            )

            logging.info(f"NADO EXECUTION: {coin} | Side: {'BUY' if is_buy else 'SELL'} | Px: {final_px} | Qty: {final_qty}")
            
            # 5. Place the order exactly as documented
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            success = getattr(res, 'success', False)
            if not success and isinstance(res, dict): success = res.get('success', False)
            if not success and "success=true" in str(res).lower(): success = True

            if success:
                logging.info(f"NADO SUCCESS: {coin} Trade Complete!")
                if is_close: 
                    del self.bot_state["positions"][coin]
                else: 
                    self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state()
            else:
                msg = getattr(res, 'message', str(res))
                logging.error(f"NADO REJECTED: {msg}")

        except Exception as e:
            logging.error(f"Order Execution Failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        
        # Block until rules are securely loaded
        await self.sync_market_data()

        # Start background threads concurrently
        asyncio.create_task(self.mids_ws_loop())
        asyncio.create_task(self.leaderboard_loop())
        await self.process_loop()

    async def close(self):
        self.running = False; await self.session.close()

async def main():
    bot = NadoQuantBot()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    def sig_h(): stop.set()
    for s in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(s, sig_h)
    
    t = asyncio.create_task(bot.run())
    await stop.wait()
    await bot.close()
    t.cancel()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
