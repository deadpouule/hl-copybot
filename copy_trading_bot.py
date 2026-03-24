import asyncio
import aiohttp
import websockets
import json
import logging
import os
import signal
import time
import struct
import warnings
from typing import Dict, List, Optional
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from logging.handlers import RotatingFileHandler

warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")

RISK_POS_PCT = 0.10
RISK_SL_PCT = 0.03
RISK_TP_PCT = 0.06
MAX_OPEN_TRADES = 5
MAX_DIRECTIONAL_TRADES = 3
MAX_DRAWDOWN_PCT = 0.20
MIN_FREE_MARGIN_PCT = 0.20
MIN_ORDER_USD = 11.0

# ==================== LOGGING ====================
os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[handler, logging.StreamHandler()]
)

# ==================== MSGPACK & SIGNING ====================
def mini_msgpack_packb(obj):
    if obj is None: return b'\xc0'
    elif isinstance(obj, bool): return b'\xc3' if obj else b'\xc2'
    elif isinstance(obj, int):
        if 0 <= obj <= 127: return bytes([obj])
        elif -32 <= obj <= -1: return bytes([256 + obj])
        elif 0 <= obj <= 255: return b'\xcc' + struct.pack('>B', obj)
        elif -128 <= obj <= 127: return b'\xd0' + struct.pack('>b', obj)
        elif 0 <= obj <= 65535: return b'\xcd' + struct.pack('>H', obj)
        elif -32768 <= obj <= 32767: return b'\xd1' + struct.pack('>h', obj)
        elif 0 <= obj <= 4294967295: return b'\xce' + struct.pack('>I', obj)
        elif -2147483648 <= obj <= 2147483647: return b'\xd2' + struct.pack('>i', obj)
        else: return b'\xcf' + struct.pack('>Q', obj)
    elif isinstance(obj, float): return b'\xcb' + struct.pack('>d', obj)
    elif isinstance(obj, str):
        encoded = obj.encode('utf-8')
        l = len(encoded)
        if l <= 31: return bytes([0xa0 | l]) + encoded
        elif l <= 255: return b'\xd9' + struct.pack('>B', l) + encoded
        elif l <= 65535: return b'\xda' + struct.pack('>H', l) + encoded
        else: return b'\xdb' + struct.pack('>I', l)
    elif isinstance(obj, list):
        l = len(obj)
        if l <= 15: res = bytes([0x90 | l])
        elif l <= 65535: res = b'\xdc' + struct.pack('>H', l)
        else: res = b'\xdd' + struct.pack('>I', l)
        for item in obj: res += mini_msgpack_packb(item)
        return res
    elif isinstance(obj, dict):
        l = len(obj)
        if l <= 15: res = bytes([0x80 | l])
        elif l <= 65535: res = b'\xde' + struct.pack('>H', l)
        else: res = b'\xdf' + struct.pack('>I', l)
        for k, v in obj.items():
            res += mini_msgpack_packb(k)
            res += mini_msgpack_packb(v)
        return res
    raise ValueError("Unsupported type: " + str(type(obj)))

def sign_l1_action(wallet: Account, action: dict, nonce: int) -> dict:
    action_bytes = mini_msgpack_packb(action)
    nonce_bytes = nonce.to_bytes(8, 'big')
    vault_bytes = b'\x00'
    connection_id = keccak(action_bytes + nonce_bytes + vault_bytes)
    
    signable_msg = encode_typed_data(
        domain_data={"name": "Exchange", "version": "1", "chainId": 1337, "verifyingContract": "0x0000000000000000000000000000000000000000"},
        message_types={"Agent":[{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}]},
        message_data={"source": "a", "connectionId": connection_id}
    )
    signed = wallet.sign_message(signable_msg)
    return {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v if signed.v >= 27 else signed.v + 27}

def _round_price(price: float) -> str:
    s = f"{float(f'{price:.5g}'):f}"
    if '.' in s: s = s.rstrip('0').rstrip('.')
    return s

def _round_size(sz: float, decimals: int) -> str:
    s = f"{sz:.{decimals}f}"
    if '.' in s: s = s.rstrip('0').rstrip('.')
    return s

# ==================== MAIN BOT CLASS ====================
class CopyBot:
    def __init__(self):
        self.wallet = Account.from_key(PRIVATE_KEY)
        self.session = None
        
        self.asset_meta = {}
        self.all_mids = {}
        
        self.tracked_traders = set()
        self.trader_positions = {}
        
        # Dedicated websocket tasks per trader
        self.trader_ws_tasks = {}
        self.mids_task = None
        
        self.bot_state = {"positions": {}, "peak_equity": 0.0, "is_paused": False}
        self.cum_pnl = 0.0
        
        self.signal_queue = asyncio.Queue()
        self.running = False
        
        self.load_state()

    def load_state(self):
        if os.path.exists("bot_state.json"):
            with open("bot_state.json", "r") as f:
                data = json.load(f)
                self.bot_state.update(data)

    def save_state(self):
        with open("bot_state.json", "w") as f:
            json.dump(self.bot_state, f)

    def append_ledger(self, coin, pnl, acct):
        self.cum_pnl += pnl
        record = {
            "ts": time.time(),
            "coin": coin,
            "pnl": round(pnl, 4),
            "cum_pnl": round(self.cum_pnl, 4),
            "acct": round(acct, 4),
            "peak": round(self.bot_state["peak_equity"], 4)
        }
        with open("compound_ledger.json", "a") as f:
            f.write(json.dumps(record) + "\n")

    async def api_post(self, url: str, payload: dict) -> dict:
        for attempt in range(4):
            try:
                async with self.session.post(url, json=payload, timeout=10) as r:
                    if r.status == 200:
                        return await r.json()
            except Exception as e:
                pass
            if attempt < 3: await asyncio.sleep(2 ** attempt)
        raise Exception("Max retries reached for POST: " + url)

    async def api_get(self, url: str) -> dict:
        for attempt in range(4):
            try:
                async with self.session.get(url, timeout=10) as r:
                    if r.status == 200:
                        return await r.json()
            except Exception as e:
                pass
            if attempt < 3: await asyncio.sleep(2 ** attempt)
        return[]

    async def startup_reconciliation(self):
        meta = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "meta"})
        for i, asset in enumerate(meta['universe']):
            self.asset_meta[asset['name']] = {"index": i, "decimals": asset['szDecimals']}
        
        logging.info("Reconciling internal bot state vs live exchange...")
        state = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
        
        live_positions = {}
        for pos in state.get("assetPositions", []):
            coin = pos["position"]["coin"]
            szi = float(pos["position"]["szi"])
            if szi != 0: live_positions[coin] = szi

        to_remove =[]
        for coin, mem_pos in self.bot_state["positions"].items():
            if coin not in live_positions:
                logging.warning("Ghost trade detected. Exchange has 0 size for " + coin + ". Marking closed.")
                to_remove.append(coin)
        for c in to_remove:
            del self.bot_state["positions"][c]
            
        for coin, szi in live_positions.items():
            if coin not in self.bot_state["positions"]:
                logging.warning("Untracked position active on exchange: " + coin + " size: " + str(szi))
                
        self.save_state()

    def _extract_traders_safely(self, data, container):
        if isinstance(data, list):
            for item in data: self._extract_traders_safely(item, container)
        elif isinstance(data, dict):
            if "user" in data or "address" in data or "account" in data:
                container.append(data)
            for val in data.values():
                self._extract_traders_safely(val, container)

    async def leaderboard_loop(self):
        while self.running:
            try:
                data = await self.api_get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
                
                raw_traders =[]
                self._extract_traders_safely(data, raw_traders)
                
                merged_stats = {}
                for t in raw_traders:
                    user = t.get("user") or t.get("address") or t.get("account")
                    if not user: continue
                    if user not in merged_stats:
                        merged_stats[user] = {"m": 0.0, "w": 0.0, "d": 0.0}
                        
                    if "roiMonth" in t: merged_stats[user]["m"] = float(t["roiMonth"])
                    elif "monthlyRoi" in t: merged_stats[user]["m"] = float(t["monthlyRoi"])
                    elif t.get("window") == "month": merged_stats[user]["m"] = float(t.get("roi", 0))

                    if "roiWeek" in t: merged_stats[user]["w"] = float(t["roiWeek"])
                    elif t.get("window") == "week": merged_stats[user]["w"] = float(t.get("roi", 0))

                    if "roiDay" in t: merged_stats[user]["d"] = float(t["roiDay"])
                    elif t.get("window") == "day": merged_stats[user]["d"] = float(t.get("roi", 0))

                traders_ranked =[]
                for user, stats in merged_stats.items():
                    # Relaxed hard filters slightly so we don't accidentally get 0 traders on a bad market day
                    if stats["m"] < 0.02 or stats["w"] < -0.05 or stats["d"] < -0.10:
                        continue
                    sharpe = stats["m"] / (abs(stats["m"] - stats["w"] * 4) + 0.01)
                    score = stats["m"] * 0.5 + stats["w"] * 0.3 + stats["d"] * 0.1 + sharpe * 0.1
                    traders_ranked.append((score, user))
                
                traders_ranked.sort(key=lambda x: x[0], reverse=True)
                top_10 = {t[1] for t in traders_ranked[:10]}
                
                # Keep active traders in list so we can monitor their closures
                for p in self.bot_state["positions"].values():
                    if "trader" in p: top_10.add(p["trader"])

                if not top_10:
                    logging.warning("No traders found passed the ranking filters. Waiting 5 minutes.")
                else:
                    new_traders = top_10 - self.tracked_traders
                    old_traders = self.tracked_traders - top_10
                    self.tracked_traders = top_10
                    
                    logging.info("Leaderboard refreshed. Monitoring " + str(len(self.tracked_traders)) + " traders in real-time.")

                    # Cancel old websocket tasks
                    for t in old_traders:
                        if t in self.trader_ws_tasks:
                            self.trader_ws_tasks[t].cancel()
                            del self.trader_ws_tasks[t]

                    # Spin up isolated connections for new traders
                    for t in new_traders:
                        self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                        
            except Exception as e:
                logging.error("Leaderboard refresh error: " + str(e))
                
            await asyncio.sleep(300)

    async def mids_ws_loop(self):
        """Dedicated connection solely for fetching live prices."""
        while self.running:
            try:
                async with websockets.connect("wss://api.hyperliquid.xyz/ws") as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                    async for msg in ws:
                        if not self.running: break
                        data = json.loads(msg)
                        if data.get("channel") == "allMids":
                            self.all_mids.update(data["data"]["mids"])
            except Exception as e:
                if self.running: await asyncio.sleep(2)

    async def trader_ws_loop(self, trader: str):
        """Dedicated isolated connection for ONE trader to fix Hyperliquid's data blindness."""
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect("wss://api.hyperliquid.xyz/ws") as ws:
                    # Keepalive ping required by Hyperliquid
                    async def keepalive():
                        while self.running and not ws.closed and trader in self.tracked_traders:
                            try:
                                await ws.send(json.dumps({"method": "ping"}))
                                await asyncio.sleep(40)
                            except:
                                break
                    asyncio.create_task(keepalive())

                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async for msg in ws:
                        if not self.running or trader not in self.tracked_traders: break
                        
                        data = json.loads(msg)
                        if data.get("channel") == "webData2":
                            # CRITICAL FIX: Inject the trader's address into the payload ourselves!
                            data["trader_address"] = trader
                            await self.signal_queue.put(data)
            except Exception as e:
                if self.running and trader in self.tracked_traders:
                    await asyncio.sleep(2)

    async def process_loop(self):
        while self.running:
            data = await self.signal_queue.get()
            try:
                await self.handle_signal(data)
            except Exception as e:
                logging.error("Signal processor error: " + str(e))
            finally:
                self.signal_queue.task_done()

    async def handle_signal(self, data):
        trader = data.get("trader_address") # Retrieved from our isolated WS injection
        if not trader: return
        
        new_state = {}
        # Safely drill into the clearinghouseState payload
        for pos in data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", []):
            c = pos["position"]["coin"]
            szi = float(pos["position"]["szi"])
            if szi != 0: new_state[c] = szi
            
        old_state = self.trader_positions.get(trader, {})
        
        for coin, szi in new_state.items():
            old_szi = old_state.get(coin, 0)
            if old_szi == 0:
                logging.info("Signal: Trader " + trader[:6] + " opened " + coin)
                await self.execute_open(coin, "LONG" if szi > 0 else "SHORT", trader)
            elif (szi > 0 and old_szi < 0) or (szi < 0 and old_szi > 0):
                logging.info("Signal: Trader " + trader[:6] + " reversed position on " + coin)
                await self.execute_close(coin, trader)
                await self.execute_open(coin, "LONG" if szi > 0 else "SHORT", trader)
                
        for coin in old_state.keys():
            if coin not in new_state:
                logging.info("Signal: Trader " + trader[:6] + " closed " + coin)
                await self.execute_close(coin, trader)
                
        self.trader_positions[trader] = new_state

    async def execute_open(self, coin: str, dir: str, trader: str):
        if coin in self.bot_state["positions"]: return
        if self.bot_state["is_paused"]: return
        if coin not in self.asset_meta: return
        
        longs = sum(1 for p in self.bot_state["positions"].values() if p["dir"] == "LONG")
        shorts = sum(1 for p in self.bot_state["positions"].values() if p["dir"] == "SHORT")
        
        if len(self.bot_state["positions"]) >= MAX_OPEN_TRADES: return
        if dir == "LONG" and longs >= MAX_DIRECTIONAL_TRADES: return
        if dir == "SHORT" and shorts >= MAX_DIRECTIONAL_TRADES: return
        
        state = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
        account_val = float(state["marginSummary"]["accountValue"])
        margin_used = float(state["marginSummary"]["totalMarginUsed"])
        
        if account_val > self.bot_state["peak_equity"]:
            self.bot_state["peak_equity"] = account_val
        elif account_val < self.bot_state["peak_equity"] * (1.0 - MAX_DRAWDOWN_PCT):
            logging.critical("Max drawdown breached. Pausing bot.")
            self.bot_state["is_paused"] = True
            self.save_state()
            return
            
        sz_usd = account_val * RISK_POS_PCT
        if sz_usd < MIN_ORDER_USD: return
        
        free_margin = account_val - margin_used
        if (free_margin - sz_usd) < (account_val * MIN_FREE_MARGIN_PCT):
            return

        current_px = float(self.all_mids.get(coin, 0))
        if current_px == 0: return
        
        sz = sz_usd / current_px
        decimals = self.asset_meta[coin]["decimals"]
        asset_idx = self.asset_meta[coin]["index"]
        
        is_buy = (dir == "LONG")
        entry_px = current_px * 1.05 if is_buy else current_px * 0.95
        tp_px = current_px * (1 + RISK_TP_PCT) if is_buy else current_px * (1 - RISK_TP_PCT)
        sl_px = current_px * (1 - RISK_SL_PCT) if is_buy else current_px * (1 + RISK_SL_PCT)

        orders =[
            {"a": asset_idx, "b": is_buy, "p": _round_price(entry_px), "s": _round_size(sz, decimals), "r": False, "t": {"limit": {"tif": "Ioc"}}},
            {"a": asset_idx, "b": not is_buy, "p": _round_price(tp_px), "s": _round_size(sz, decimals), "r": True, "t": {"trigger": {"isMarket": True, "triggerPx": _round_price(tp_px), "tpsl": "tp"}}},
            {"a": asset_idx, "b": not is_buy, "p": _round_price(sl_px), "s": _round_size(sz, decimals), "r": True, "t": {"trigger": {"isMarket": True, "triggerPx": _round_price(sl_px), "tpsl": "sl"}}}
        ]
        
        action = {"type": "order", "orders": orders, "grouping": "na"}
        nonce = int(time.time() * 1000)
        
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": sign_l1_action(self.wallet, action, nonce)
        }
        
        res = await self.api_post("https://api.hyperliquid.xyz/exchange", payload)
        if res.get("status") == "ok":
            statuses = res.get("response", {}).get("data", {}).get("statuses",[])
            tp_oid = None
            sl_oid = None
            if len(statuses) >= 3:
                tp_oid = statuses[1].get("resting", {}).get("oid")
                sl_oid = statuses[2].get("resting", {}).get("oid")
                
            self.bot_state["positions"][coin] = {
                "dir": dir,
                "trader": trader,
                "tp_oid": tp_oid,
                "sl_oid": sl_oid
            }
            self.save_state()
            logging.info("Successfully mirrored OPEN for " + coin + ". Assigned TP/SL.")

    async def execute_close(self, coin: str, trader: str):
        if coin not in self.bot_state["positions"]: return
        pos_data = self.bot_state["positions"][coin]
        
        if pos_data.get("trader") != trader:
            return

        asset_idx = self.asset_meta[coin]["index"]
        state = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
        
        pnl = 0.0
        open_sz = 0.0
        
        for pos in state.get("assetPositions",[]):
            if pos["position"]["coin"] == coin:
                pnl = float(pos["position"]["unrealizedPnl"])
                open_sz = abs(float(pos["position"]["szi"]))
                break
                
        if open_sz == 0.0:
            del self.bot_state["positions"][coin]
            self.save_state()
            return

        cancels =[]
        if pos_data.get("tp_oid"): cancels.append({"a": asset_idx, "o": pos_data["tp_oid"]})
        if pos_data.get("sl_oid"): cancels.append({"a": asset_idx, "o": pos_data["sl_oid"]})
        
        if cancels:
            cancel_action = {"type": "cancel", "cancels": cancels}
            nonce = int(time.time() * 1000)
            await self.api_post("https://api.hyperliquid.xyz/exchange", {"action": cancel_action, "nonce": nonce, "signature": sign_l1_action(self.wallet, cancel_action, nonce)})

        is_buy = (pos_data["dir"] == "SHORT")
        current_px = float(self.all_mids.get(coin, 0))
        close_px = current_px * 1.05 if is_buy else current_px * 0.95
        
        decimals = self.asset_meta[coin]["decimals"]
        close_order = {
            "a": asset_idx,
            "b": is_buy,
            "p": _round_price(close_px),
            "s": _round_size(open_sz, decimals),
            "r": True,
            "t": {"limit": {"tif": "Ioc"}}
        }
        
        close_action = {"type": "order", "orders": [close_order], "grouping": "na"}
        nonce2 = int(time.time() * 1000)
        
        res = await self.api_post("https://api.hyperliquid.xyz/exchange", {
            "action": close_action,
            "nonce": nonce2,
            "signature": sign_l1_action(self.wallet, close_action, nonce2)
        })
        
        if res.get("status") == "ok":
            logging.info("Successfully mirrored CLOSE for " + coin)
            del self.bot_state["positions"][coin]
            self.save_state()
            
            state_after = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
            final_acct = float(state_after["marginSummary"]["accountValue"])
            self.append_ledger(coin, pnl, final_acct)

    async def run(self):
        self.running = True
        self.session = aiohttp.ClientSession()
        await self.startup_reconciliation()
        
        self.mids_task = asyncio.create_task(self.mids_ws_loop())
        t_lb = asyncio.create_task(self.leaderboard_loop())
        t_proc = asyncio.create_task(self.process_loop())
        
        await asyncio.gather(t_lb, t_proc)

    async def close(self):
        self.running = False
        if self.mids_task: self.mids_task.cancel()
        for t in self.trader_ws_tasks.values(): t.cancel()
        if self.session and not self.session.closed: await self.session.close()

async def run_bot():
    bot = CopyBot()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logging.info("Interrupt received, shutting down gracefully...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    bot_task = asyncio.create_task(bot.run())
    await stop_event.wait()
    
    await bot.close()
    bot_task.cancel()
    try:
        await asyncio.wait_for(bot_task, timeout=5.0)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(run_bot())
