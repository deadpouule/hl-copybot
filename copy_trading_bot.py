import asyncio, aiohttp, websockets, json, logging, os, signal, time, warnings, re, sys
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv

# Official Nado Protocol SDK Imports
from nado_protocol.client import create_nado_client
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.math import to_x18
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix
from nado_protocol.utils.bytes32 import subaccount_to_hex

# OFFICIAL SDK UTILITY: Bypasses manual balance parsing and calculates exact free margin
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
        logging.info("Initializing Ultimate Nado Engine...")
        self.client = create_nado_client(NADO_ENV, NADO_PK)
        self.owner = self.client.context.engine_client.signer.address
        self.subaccount_hex = subaccount_to_hex(self.owner, "default")
        
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
            except: pass

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.bot_state, f)

    def _safe_parse(self, obj):
        try:
            if isinstance(obj, dict): return obj
            if hasattr(obj, 'model_dump'): return obj.model_dump()
            if hasattr(obj, 'dict'): return obj.dict()
            if hasattr(obj, '_asdict'): return obj._asdict()
            if hasattr(obj, '__dict__'): return vars(obj)
        except: pass
        return {}

    def _apply_fallback_markets(self):
        """Hardcoded precision rules to ensure the bot can always format orders perfectly."""
        self.product_map.update({
            "BTC": {"id": 1, "p_tick": 0.1, "s_tick": 0.0001, "min_s": 0.0001},
            "ETH": {"id": 2, "p_tick": 0.1, "s_tick": 0.001, "min_s": 0.001},
            "SOL": {"id": 3, "p_tick": 0.001, "s_tick": 0.1, "min_s": 0.1},
            "BNB": {"id": 4, "p_tick": 0.01, "s_tick": 0.01, "min_s": 0.01},
            "HYPE": {"id": 100, "p_tick": 0.001, "s_tick": 1.0, "min_s": 1.0},
            "PAX": {"id": 5, "p_tick": 0.1, "s_tick": 0.001, "min_s": 0.001}, 
            "XAG": {"id": 6, "p_tick": 0.001, "s_tick": 1.0, "min_s": 1.0},
            "WTI": {"id": 7, "p_tick": 0.01, "s_tick": 0.1, "min_s": 0.1}
        })
        logging.info("Market Sync: Applied default precision rules for Majors.")

    async def sync_market_data(self):
        try:
            res = await asyncio.to_thread(self.client.market.get_all_engine_markets)
            payload = getattr(res, 'data', res)
            market_list = list(payload) if isinstance(payload, (list, tuple, set)) else[]
            
            if not market_list:
                parsed_dict = self._safe_parse(payload)
                for key in['rows', 'markets', 'products', 'data']:
                    if key in parsed_dict and isinstance(parsed_dict[key], (list, tuple)):
                        market_list = list(parsed_dict[key]); break
                if not market_list and isinstance(parsed_dict, dict):
                    market_list = list(parsed_dict.values())

            count = 0
            for m in market_list:
                m_dict = self._safe_parse(m)
                if not m_dict: m_dict = {k: getattr(m, k) for k in dir(m) if not k.startswith('_')}
                
                ticker = str(m_dict.get('symbol', ''))
                if 'PERP_' in ticker:
                    parts = ticker.split('_')
                    if len(parts) > 1:
                        coin = parts[1]
                        self.product_map[coin] = {
                            "id": int(m_dict.get('product_id', 0)),
                            "p_tick": float(m_dict.get('price_increment', 0.0001)),
                            "s_tick": float(m_dict.get('base_tick', 0.0001)),
                            "min_s": float(m_dict.get('min_base_amount', 0.0001))
                        }
                        count += 1
                        
            if count > 0:
                logging.info(f"Market Sync Complete: {len(self.product_map)} pairs loaded.")
                return True
        except Exception as e:
            logging.error(f"Market Sync Exception: {e}")
            
        self._apply_fallback_markets()
        return True

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
                    raw =[]; self._extract_traders(data, raw)
                    processed = {}
                    for t in raw:
                        addr = t.get("account") or t.get("user") or t.get("ethAddress")
                        roi = float(t.get("roiWeek", t.get("roi", 0)))
                        if addr not in processed or roi > processed[addr]: processed[addr] = roi
                    
                    ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                    top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                    
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

    def _round_step(self, val, step):
        if step == 0: return val
        return round(round(val / step) * step, 10)

    async def execute_nado_order(self, coin: str, is_buy: bool, trader: str, is_close: bool):
        if not is_close and coin in self.bot_state["positions"]: return
        if is_close and coin not in self.bot_state["positions"]: return
        
        market = self.product_map.get(coin.upper())
        if not market: return

        try:
            # 1. Fetch Oracle Proxy Price
            async with self.session.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}) as r:
                mids = await r.json()
                px = float(mids.get(coin.upper(), 0))
            if px == 0: return

            target_px = px * (1.05 if is_buy else 0.95)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            final_px = self._round_step(target_px, market["p_tick"])
            
            # ====================================================================
            # 2. OFFICIAL MARGIN MANAGER: Safely calculates exactly what you have
            # ====================================================================
            sub_info = await asyncio.to_thread(self.client.context.engine_client.get_subaccount_info, self.subaccount_hex)
            iso_res = await asyncio.to_thread(self.client.context.engine_client.get_isolated_positions, self.subaccount_hex)
            iso_pos = getattr(iso_res, 'isolated_positions',[])
            
            manager = MarginManager(sub_info, iso_pos)
            summary = manager.calculate_account_summary()
            
            # The exact dollar amount available for new positions
            available_margin = float(summary.funds_available)
            
            usd_amt = max(available_margin * RISK_POS_PCT, MIN_ORDER_USD)
            
            # Check if we actually have enough money to place this
            if available_margin < usd_amt and not is_close:
                logging.warning(f"Skipping {coin}: Insufficient Free Margin. (Available: ${available_margin:.2f} | Needed: ${usd_amt:.2f})")
                return
            
            qty = usd_amt / px
            final_qty = self._round_step(qty, market["s_tick"])
            if final_qty < market["min_s"]: return

            # 3. Create the execution payload
            appendix = build_appendix(order_type=OrderType.IOC, reduce_only=is_close)
            amount_x18 = int((final_qty if is_buy else -final_qty) * 1e18)

            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=self.owner, subaccount_name="default"),
                priceX18=to_x18(final_px),
                amount=amount_x18,
                expiration=get_expiration_timestamp(60),
                nonce=gen_order_nonce(),
                appendix=appendix
            )

            logging.info(f"NADO EXECUTION: {coin} | Side: {'BUY' if is_buy else 'SELL'} | Px: {final_px} | Qty: {final_qty}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            success = getattr(res, 'success', False)
            if not success and isinstance(res, dict): success = res.get('success', False)
            if not success and "success=true" in str(res).lower(): success = True

            if success:
                logging.info(f"NADO SUCCESS: {coin} mirrored.")
                if is_close: 
                    del self.bot_state["positions"][coin]
                else: 
                    self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state()
            else:
                msg = getattr(res, 'message', str(res))
                logging.error(f"NADO REJECTED: {msg}")

        except Exception as e:
            logging.error(f"Order failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        
        await self.sync_market_data()
        logging.info("Cross-Exchange Bot Live. Monitoring HL Pro Traders...")

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
