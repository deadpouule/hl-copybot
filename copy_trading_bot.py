import asyncio, aiohttp, websockets, json, logging, os, signal, time, warnings, re, sys
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv

# Official Nado Protocol SDK Imports based on Documentation
from nado_protocol.client import create_nado_client
from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.math import to_pow_10, to_x18
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.subaccount import SubaccountParams
from nado_protocol.utils.order import build_appendix

# Suppress harmless eth-utils warnings
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
NADO_PK = os.getenv("NADO_PRIVATE_KEY")
NADO_ID = os.getenv("NADO_ACCOUNT_ID")
NADO_ENV = "mainnet"

# HL Sourcing Settings
TOP_X_TRADERS = 5
ALLOWED_COINS = ["BTC", "ETH", "SOL", "HYPE", "BNB", "PAX", "XAG", "WTI"]

# Risk Settings
RISK_POS_PCT = 0.10        # 10% of balance
MIN_ORDER_USD = 11.0       

os.makedirs("logs", exist_ok=True)
handler = logging.handlers.RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[handler, logging.StreamHandler()])

class NadoQuantBot:
    def __init__(self):
        logging.info("Initializing Nado Protocol Execution Engine...")
        self.client = create_nado_client(NADO_ENV, NADO_PK)
        self.owner = self.client.context.engine_client.signer.address
        
        self.session = None
        self.tracked_traders = set()
        self.trader_positions = {}
        self.trader_ws_tasks = {}
        self.bot_state = {"positions": {}}
        self.signal_queue = asyncio.Queue()
        self.running = False
        
        # Cache for product details (Tick sizes and IDs)
        self.product_map = {}

    async def sync_market_data(self):
        """Fetch all engine markets to map Symbols to IDs and get precision rules."""
        try:
            # Based on docs: MarketAPI.get_all_engine_markets()
            res = await asyncio.to_thread(self.client.market.get_all_engine_markets)
            # The SDK returns a list or a response object depending on version
            markets = res.data if hasattr(res, 'data') else res
            
            for m in markets:
                # Nado tickers are usually 'PERP_BTC_USDC'
                ticker = m.get('symbol', '')
                if 'PERP_' in ticker:
                    coin = ticker.split('_')[1] # Extract 'BTC'
                    self.product_map[coin] = {
                        "id": m.get('product_id'),
                        "price_tick": float(m.get('price_increment', 0)),
                        "size_tick": float(m.get('base_tick', 0)),
                        "min_size": float(m.get('min_base_amount', 0))
                    }
            logging.info(f"Market Sync Complete: Loaded {len(self.product_map)} Perpetual pairs.")
            return True
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
                    raw = []; self._extract_traders(data, raw)
                    processed = {}
                    for t in raw:
                        addr = t.get("account") or t.get("user") or t.get("ethAddress")
                        roi = 0.0
                        for k, v in t.items():
                            if "roi" in k.lower() and isinstance(v, (int, float)):
                                roi = float(v); break
                        if addr not in processed or roi > processed[addr]: processed[addr] = roi
                    
                    ranked = sorted(processed.items(), key=lambda x: x[1], reverse=True)
                    top_selected = {r[0] for r in ranked[:TOP_X_TRADERS]}
                    self.tracked_traders = top_selected
                    
                    for t in self.tracked_traders:
                        if t not in self.trader_ws_tasks:
                            self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    logging.info(f"Following {len(self.tracked_traders)} traders on HL.")
            except Exception as e: logging.error(f"Leaderboard Loop Error: {e}")
            await asyncio.sleep(300)

    async def trader_ws_loop(self, trader: str):
        uri = "wss://api.hyperliquid.xyz/ws"
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect(uri) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async for msg in ws:
                        data = json.loads(msg)
                        if data.get("channel") == "webData2":
                            data["trader_address"] = trader; await self.signal_queue.put(data)
            except: await asyncio.sleep(5)

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
        # 1. Validation
        if not is_close and coin in self.bot_state["positions"]: return
        market = self.product_map.get(coin.upper())
        if not market:
            logging.warning(f"Skipping {coin}: Not listed on Nado engine.")
            return

        try:
            # 2. Get Proxy Price from HL
            async with self.session.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}) as r:
                mids = await r.json()
                px = float(mids.get(coin.upper(), 0))
            if px == 0: return

            # 3. Apply Precision & Slippage (X18 Safe)
            # Market behavior: Slip price by 3% to ensure execution
            target_px = px * (1.03 if is_buy else 0.97)
            if is_close:
                is_buy = not self.bot_state["positions"][coin]["is_buy"]
                target_px = px * (1.05 if is_buy else 0.95)

            final_px = self._round_step(target_px, market["p_tick"])
            
            # 4. Determine Balance & Quantity
            res_bal = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, self.owner)
            balance = 0.0
            # Search spot_balances for USDC
            balances = res_bal.data.get('spot_balances', []) if hasattr(res_bal, 'data') else []
            for b in balances:
                if b.get('product_id') == 0: # 0 is always USDC/Quote
                    balance = float(b.get('balance', 0))
            
            usd_amt = max(balance * RISK_POS_PCT, MIN_ORDER_USD)
            qty = usd_amt / px
            final_qty = self._round_step(qty, market["size_tick"])
            
            if final_qty < market["min_size"]: return

            # 5. Build bit-packed Appendix (Official Docs logic)
            # Execution: IOC (Immediate or Cancel) for instant market entry
            appendix = build_appendix(
                order_type=OrderType.IOC,
                reduce_only=is_close
            )

            # 6. Construct X18 Order Object
            order = OrderParams(
                sender=SubaccountParams(subaccount_owner=self.owner, subaccount_name="default"),
                priceX18=to_x18(final_px),
                amount=int((final_qty if is_buy else -final_qty) * 10**18),
                expiration=get_expiration_timestamp(60), # 1 minute validity
                nonce=gen_order_nonce(),
                appendix=appendix
            )

            # 7. Execute Order
            logging.info(f"NADO: Executing {coin} Side {'BUY' if is_buy else 'SELL'} Px: {final_px}")
            res = await asyncio.to_thread(self.client.market.place_order, PlaceOrderParams(product_id=market["id"], order=order))
            
            if "success" in str(res).lower():
                logging.info(f"NADO SUCCESS: {coin} trade mirrored.")
                if is_close: del self.bot_state["positions"][coin]
                else: self.bot_state["positions"][coin] = {"trader": trader, "is_buy": is_buy}
            else:
                logging.error(f"NADO REJECTED: {res}")

        except Exception as e:
            logging.error(f"Critical Execution Error: {e}")

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession()
        # Ensure we have market rules before starting
        if not await self.sync_market_data():
            logging.critical("Could not sync with Nado Engine. Shutting down.")
            return

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
