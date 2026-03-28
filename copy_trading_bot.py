import asyncio, aiohttp, websockets, json, logging, os, signal, time, warnings, re, sys
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv

# Official Nado Protocol SDK Imports
from nado_protocol.client import create_nado_client
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.math import to_pow_10, to_x18
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix
from nado_protocol.utils.bytes32 import subaccount_to_hex

# Suppress harmless eth-utils warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
NADO_ID = os.getenv("NADO_ACCOUNT_ID")
NADO_ENV = "mainnet"

TOP_X_TRADERS = 5
ALLOWED_COINS =["BTC", "ETH", "SOL", "HYPE", "BNB", "PAX", "XAG", "WTI"]
RISK_POS_PCT = 0.10        
MIN_ORDER_USD = 11.0       

os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s[%(levelname)s] %(message)s", 
    handlers=[log_handler, logging.StreamHandler()]
)

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Bulletproof Nado Engine...")
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
        """Forces any SDK object (Pydantic, Response, Tuple) into a standard dictionary."""
        if hasattr(obj, 'data'): obj = obj.data
        if hasattr(obj, 'dict'): return obj.dict()
        if hasattr(obj, 'model_dump'): return obj.model_dump()
        if isinstance(obj, dict): return obj
        if hasattr(obj, '__dict__'): return vars(obj)
        return {}

    async def sync_market_data(self):
        """Safely fetch and parse all precision rules from Nado."""
        try:
            res = await asyncio.to_thread(self.client.market.get_all_engine_markets)
            
            # 1. Safely extract the list of markets
            payload = getattr(res, 'data', res)
            market_list =[]
            
            if isinstance(payload, list):
                market_list = payload
            else:
                parsed_dict = self._safe_parse(payload)
                for key in ['rows', 'markets', 'products', 'data']:
                    if key in parsed_dict and isinstance(parsed_dict[key], list):
                        market_list = parsed_dict[key]
                        break
                if not market_list and isinstance(parsed_dict, dict):
                    market_list = list(parsed_dict.values())

            # 2. Iterate and map products safely
            count = 0
            for m in market_list:
                m_dict = self._safe_parse(m)
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
                logging.info(f"Market Sync Complete: {len(self.product_map)} Perpetual pairs loaded.")
                return True
            else:
                logging.error("Market Sync failed: Extracted empty product list.")
                return False
                
        except Exception as e:
            logging.error(f"Market Sync Failed: {e}")
            return False

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
                    
                    for t in new:
                        self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    for t in old:
                        if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
                    logging.info(f"Monitoring {len(self.tracked_traders)} traders.")
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
                        logging.info(f"SIGNAL: {trader[:6]} opened {coin}")
                        await self.execute_nado_order(coin, szi > 0, trader, is_close=False)
                
                for coin in old_state.keys():
                    if coin not in new_state and coin.upper() in ALLOWED_COINS:
                        logging.info(f"SIGNAL: {trader[:6]} closed {coin}")
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
            async with self.session.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}) as r:
                mids = await r.json()
                px = float(mids.get(coin.upper(), 0))
            if px == 0: return

            target_px = px * (1.03 if is_buy else 0.97)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            final_px = self._round_step(target_px, market["p_tick"])
            
            # Fetch balance safely
            res_bal = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, self.subaccount_hex)
            bal_dict = self._safe_parse(res_bal)
            
            balance = 0.0
            for b in bal_dict.get('spot_balances',[]):
                b_dict = self._safe_parse(b)
                if b_dict.get('product_id') == 0: 
                    balance = float(b_dict.get('balance', 0))
            
            usd_amt = max(balance * RISK_POS_PCT, MIN_ORDER_USD)
            qty = usd_amt / px
            final_qty = self._round_step(qty, market["size_tick"])
            if final_qty < market["min_s"]: return

            appendix = build_appendix(order_type=OrderType.IOC, reduce_only=is_close)

            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=self.owner, subaccount_name="default"),
                priceX18=to_x18(final_px),
                amount=to_pow_10(final_qty if is_buy else -final_qty, 18),
                expiration=get_expiration_timestamp(60),
                nonce=gen_order_nonce(),
                appendix=appendix
            )

            logging.info(f"NADO: Mirroring {coin} | Side: {'BUY' if is_buy else 'SELL'} | Px: {final_px}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin} mirrored.")
                if is_close: 
                    del self.bot_state["positions"][coin]
                else: 
                    self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
                self.save_state()
            else:
                logging.error(f"NADO REJECTED: {res}")

        except Exception as e:
            logging.error(f"Order failure: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        
        # Keep retrying until Nado Market Sync succeeds
        while self.running:
            if await self.sync_market_data():
                break
            logging.critical("Nado Engine sync failed. Retrying in 10s...")
            await asyncio.sleep(10)
            
        if not self.running: return

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
