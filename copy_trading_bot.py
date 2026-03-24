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
import re
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from logging.handlers import RotatingFileHandler

# Suppress harmless network warnings from eth_utils
warnings.filterwarnings("ignore", category=UserWarning, module="eth_utils")

# ==================== CONFIGURATION ====================
load_dotenv("/root/hl-copybot/.env")
WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")

RISK_POS_PCT = 0.10        # 10% of live account value per trade
RISK_SL_PCT = 0.03         # 3% Stop Loss
RISK_TP_PCT = 0.06         # 6% Take Profit
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
        else: return b'\xdb' + struct.pack('>I', l) + encoded
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
    domain_data={"name": "Exchange", "version": "1", "chainId": 1337, "verifyingContract": "0x0000000000000000000000000000000000000000"}
    message_types={"Agent":[{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}]}
    signable_msg = encode_typed_data(domain_data=domain_data, message_types=message_types, message_data={"source": "a", "connectionId": connection_id})
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
                data = json.load(f); self.bot_state.update(data)

    def save_state(self):
        with open("bot_state.json", "w") as f: json.dump(self.bot_state, f)

    def append_ledger(self, coin, pnl, acct):
        self.cum_pnl += pnl
        record = {"ts": time.time(), "coin": coin, "pnl": round(pnl, 4), "cum_pnl": round(self.cum_pnl, 4), "acct": round(acct, 4), "peak": round(self.bot_state["peak_equity"], 4)}
        with open("compound_ledger.json", "a") as f: f.write(json.dumps(record) + "\n")

    async def api_post(self, url: str, payload: dict) -> dict:
        for attempt in range(4):
            try:
                async with self.session.post(url, json=payload, timeout=10) as r:
                    if r.status == 200: return await r.json()
            except: pass
            await asyncio.sleep(1)
        return {}

    async def api_get(self, url: str) -> dict:
        for attempt in range(4):
            try:
                async with self.session.get(url, timeout=15) as r:
                    if r.status == 200: return await r.json()
            except: pass
            await asyncio.sleep(2)
        return None

    async def startup_reconciliation(self):
        logging.info("Reconciling internal bot state vs live exchange...")
        meta = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "meta"})
        for i, asset in enumerate(meta.get('universe', [])):
            self.asset_meta[asset['name']] = {"index": i, "decimals": asset['szDecimals']}
        state = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
        live_positions = {p["position"]["coin"]: float(p["position"]["szi"]) for p in state.get("assetPositions", []) if float(p["position"]["szi"]) != 0}
        to_remove = [c for c in self.bot_state["positions"].keys() if c not in live_positions]
        for c in to_remove: del self.bot_state["positions"][c]
        self.save_state()

    def _deep_search_traders(self, obj):
        """Greedy recursive search for anything looking like a trader object."""
        found = []
        if isinstance(obj, dict):
            # Check if this dict has an address-like value
            potential_user = None
            for k, v in obj.items():
                if isinstance(v, str) and re.match(r"^0x[a-fA-F0-9]{40}$", v):
                    potential_user = v
                    break
            if potential_user:
                found.append(obj)
            for v in obj.values():
                found.extend(self._deep_search_traders(v))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._deep_search_traders(item))
        return found

    async def leaderboard_loop(self):
        while self.running:
            try:
                logging.info("Fetching Leaderboard data...")
                data = await self.api_get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
                
                if data is None:
                    logging.error("Failed to reach Stats API. Retrying in 60s...")
                    await asyncio.sleep(60); continue

                # Recursive search for all traders in the JSON tree
                all_entries = self._deep_search_traders(data)
                
                traders_ranked = []
                for t in all_entries:
                    user = t.get("account") or t.get("user") or t.get("ethAddress") or t.get("address")
                    if not user: continue
                    
                    # Try to find ANY ROI field (roi, roiWeek, weeklyRoi, etc.)
                    roi = 0.0
                    for k, v in t.items():
                        if "roi" in k.lower() and isinstance(v, (int, float)):
                            roi = float(v)
                            break
                    
                    # Relaxed filter: If they exist on leaderboard, they are likely good enough to monitor
                    traders_ranked.append((roi, user))
                
                # Sort by ROI descending
                traders_ranked.sort(key=lambda x: x[0], reverse=True)
                top_10 = {t[1] for t in traders_ranked[:10]}
                
                # Add existing positions
                for p in self.bot_state["positions"].values():
                    if "trader" in p: top_10.add(p["trader"])

                if not top_10:
                    logging.warning(f"Greedy scan failed. Total potential objects found: {len(all_entries)}")
                    logging.info(f"API Structure Sample: {str(data)[:200]}")
                else:
                    new_traders = top_10 - self.tracked_traders
                    old_traders = self.tracked_traders - top_10
                    self.tracked_traders = top_10
                    logging.info(f"SUCCESS: Monitoring {len(self.tracked_traders)} traders.")
                    for t in new_traders:
                        logging.info(f"New connection for trader: {t}")
                        self.trader_ws_tasks[t] = asyncio.create_task(self.trader_ws_loop(t))
                    for t in old_traders:
                        if t in self.trader_ws_tasks: self.trader_ws_tasks[t].cancel(); del self.trader_ws_tasks[t]
            except Exception as e: 
                logging.error(f"Leaderboard critical error: {e}")
            await asyncio.sleep(300)

    async def mids_ws_loop(self):
        while self.running:
            try:
                async with websockets.connect("wss://api.hyperliquid.xyz/ws") as ws:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                    async for msg in ws:
                        if not self.running: break
                        d = json.loads(msg)
                        if d.get("channel") == "allMids": self.all_mids.update(d["data"]["mids"])
            except: await asyncio.sleep(2)

    async def trader_ws_loop(self, trader: str):
        while self.running and trader in self.tracked_traders:
            try:
                async with websockets.connect("wss://api.hyperliquid.xyz/ws") as ws:
                    async def ping():
                        while self.running and not ws.closed:
                            try: await ws.send(json.dumps({"method": "ping"})); await asyncio.sleep(40)
                            except: break
                    asyncio.create_task(ping())
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "webData2", "user": trader}}))
                    async for msg in ws:
                        if not self.running or trader not in self.tracked_traders: break
                        data = json.loads(msg)
                        if data.get("channel") == "webData2":
                            data["trader_address"] = trader
                            await self.signal_queue.put(data)
            except: 
                if self.running and trader in self.tracked_traders: await asyncio.sleep(2)

    async def process_loop(self):
        while self.running:
            data = await self.signal_queue.get()
            try:
                trader = data.get("trader_address")
                if not trader: continue
                new_state = {p["position"]["coin"]: float(p["position"]["szi"]) for p in data.get("data", {}).get("clearinghouseState", {}).get("assetPositions", []) if float(p["position"]["szi"]) != 0}
                old_state = self.trader_positions.get(trader, {})
                for coin, szi in new_state.items():
                    old_szi = old_state.get(coin, 0)
                    if old_szi == 0:
                        logging.info("Signal: " + trader[:6] + " opened " + coin)
                        await self.execute_open(coin, "LONG" if szi > 0 else "SHORT", trader)
                    elif (szi > 0 and old_szi < 0) or (szi < 0 and old_szi > 0):
                        logging.info("Signal: " + trader[:6] + " reversed " + coin)
                        await self.execute_close(coin, trader); await self.execute_open(coin, "LONG" if szi > 0 else "SHORT", trader)
                for coin in old_state.keys():
                    if coin not in new_state:
                        logging.info("Signal: " + trader[:6] + " closed " + coin)
                        await self.execute_close(coin, trader)
                self.trader_positions[trader] = new_state
            except Exception as e: logging.error("Process error: " + str(e))
            finally: self.signal_queue.task_done()

    async def execute_open(self, coin: str, dir: str, trader: str):
        if coin in self.bot_state["positions"] or self.bot_state["is_paused"]: return
        if coin not in self.asset_meta: return
        state = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
        if not state: return
        acc_v = float(state["marginSummary"]["accountValue"]); m_used = float(state["marginSummary"]["totalMarginUsed"])
        if acc_v > self.bot_state["peak_equity"]: self.bot_state["peak_equity"] = acc_v
        elif acc_v < self.bot_state["peak_equity"] * (1.0-MAX_DRAWDOWN_PCT): self.bot_state["is_paused"]=True; self.save_state(); return
        sz_usd = acc_v * RISK_POS_PCT
        if sz_usd < MIN_ORDER_USD or (acc_v - m_used - sz_usd) < (acc_v * MIN_FREE_MARGIN_PCT): return
        px = float(self.all_mids.get(coin, 0))
        if px == 0: return
        sz = sz_usd / px; dec = self.asset_meta[coin]["decimals"]; is_b = (dir == "LONG")
        e_px = px * 1.05 if is_b else px * 0.95; t_px = px * (1+RISK_TP_PCT) if is_b else px * (1-RISK_TP_PCT); s_px = px * (1-RISK_SL_PCT) if is_b else px * (1+RISK_SL_PCT)
        orders = [
            {"a": self.asset_meta[coin]["index"], "b": is_b, "p": _round_price(e_px), "s": _round_size(sz, dec), "r": False, "t": {"limit": {"tif": "Ioc"}}},
            {"a": self.asset_meta[coin]["index"], "b": not is_b, "p": _round_price(t_px), "s": _round_size(sz, dec), "r": True, "t": {"trigger": {"isMarket": True, "triggerPx": _round_price(t_px), "tpsl": "tp"}}},
            {"a": self.asset_meta[coin]["index"], "b": not is_b, "p": _round_price(s_px), "s": _round_size(sz, dec), "r": True, "t": {"trigger": {"isMarket": True, "triggerPx": _round_price(s_px), "tpsl": "sl"}}}
        ]
        action = {"type": "order", "orders": orders, "grouping": "na"}; nonce = int(time.time()*1000)
        res = await self.api_post("https://api.hyperliquid.xyz/exchange", {"action": action, "nonce": nonce, "signature": sign_l1_action(self.wallet, action, nonce)})
        if res.get("status") == "ok":
            st = res.get("response", {}).get("data", {}).get("statuses", [])
            tp = st[1].get("resting", {}).get("oid") if len(st) > 1 else None
            sl = st[2].get("resting", {}).get("oid") if len(st) > 2 else None
            self.bot_state["positions"][coin] = {"dir": dir, "trader": trader, "tp_oid": tp, "sl_oid": sl}; self.save_state(); logging.info("OPEN SUCCESS: " + coin)

    async def execute_close(self, coin: str, trader: str):
        if coin not in self.bot_state["positions"] or self.bot_state["positions"][coin].get("trader") != trader: return
        pd = self.bot_state["positions"][coin]; state = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
        pnl = 0.0; sz = 0.0
        for p in state.get("assetPositions", []):
            if p["position"]["coin"] == coin: pnl = float(p["position"]["unrealizedPnl"]); sz = abs(float(p["position"]["szi"])); break
        if sz > 0:
            cancels = []
            if pd.get("tp_oid"): cancels.append({"a": self.asset_meta[coin]["index"], "o": pd["tp_oid"]})
            if pd.get("sl_oid"): cancels.append({"a": self.asset_meta[coin]["index"], "o": pd["sl_oid"]})
            if cancels:
                act = {"type": "cancel", "cancels": cancels}; n = int(time.time()*1000)
                await self.api_post("https://api.hyperliquid.xyz/exchange", {"action": act, "nonce": n, "signature": sign_l1_action(self.wallet, act, n)})
            is_b = (pd["dir"] == "SHORT"); px = float(self.all_mids.get(coin, 0)); c_px = px * 1.05 if is_b else px * 0.95
            o = {"a": self.asset_meta[coin]["index"], "b": is_b, "p": _round_price(c_px), "s": _round_size(sz, self.asset_meta[coin]["decimals"]), "r": True, "t": {"limit": {"tif": "Ioc"}}}
            act = {"type": "order", "orders": [o], "grouping": "na"}; n = int(time.time()*1000)
            res = await self.api_post("https://api.hyperliquid.xyz/exchange", {"action": act, "nonce": n, "signature": sign_l1_action(self.wallet, act, n)})
            if res.get("status") == "ok":
                logging.info("CLOSE SUCCESS: " + coin); del self.bot_state["positions"][coin]; self.save_state()
                st_a = await self.api_post("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": WALLET_ADDRESS})
                self.append_ledger(coin, pnl, float(st_a["marginSummary"]["accountValue"]))

    async def run(self):
        self.running = True; self.session = aiohttp.ClientSession(); await self.startup_reconciliation()
        self.mids_task = asyncio.create_task(self.mids_ws_loop()); t_lb = asyncio.create_task(self.leaderboard_loop()); t_proc = asyncio.create_task(self.process_loop())
        await asyncio.gather(t_lb, t_proc)

    async def close(self):
        self.running = False; self.mids_task.cancel()
        for t in self.trader_ws_tasks.values(): t.cancel()
        if self.session: await self.session.close()

async def run_bot():
    bot = CopyBot(); stop = asyncio.Event()
    def sig_h(): stop.set()
    for s in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(s, sig_h)
    bt = asyncio.create_task(bot.run()); await stop.wait(); await bot.close(); bt.cancel()

if __name__ == "__main__": asyncio.run(run_bot())
