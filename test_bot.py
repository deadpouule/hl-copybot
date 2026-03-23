import asyncio
import aiohttp
import websockets
import json
import os
import time
import struct
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

load_dotenv("/root/hl-copybot/.env")
WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")

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
    elif isinstance(obj, float):
        return b'\xcb' + struct.pack('>d', obj)
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

def sign_l1_action(wallet, action, nonce):
    action_bytes = mini_msgpack_packb(action)
    nonce_bytes = nonce.to_bytes(8, 'big')
    vault_bytes = b'\x00'
    connection_id = keccak(action_bytes + nonce_bytes + vault_bytes)
    
    domain_data = {
        "name": "Exchange",
        "version": "1",
        "chainId": 1337,
        "verifyingContract": "0x0000000000000000000000000000000000000000"
    }
    message_types = {
        "Agent":[
            {"name": "source", "type": "string"},
            {"name": "connectionId", "type": "bytes32"}
        ]
    }
    signable_msg = encode_typed_data(
        domain_data=domain_data,
        message_types=message_types,
        message_data={"source": "a", "connectionId": connection_id}
    )
    
    signed = wallet.sign_message(signable_msg)
    return {
        "r": hex(signed.r),
        "s": hex(signed.s),
        "v": signed.v if signed.v >= 27 else signed.v + 27
    }

def _round_price(price: float) -> str:
    s = f"{float(f'{price:.5g}'):f}"
    if '.' in s: s = s.rstrip('0').rstrip('.')
    return s

def _round_size(sz: float, decimals: int) -> str:
    s = f"{sz:.{decimals}f}"
    if '.' in s: s = s.rstrip('0').rstrip('.')
    return s

async def main():
    print("--- HYPERLIQUID TEST BOT ---")
    account = Account.from_key(PRIVATE_KEY)
    
    async with aiohttp.ClientSession() as session:
        print("\n1. Fetching /info (type: meta)...")
        async with session.post("https://api.hyperliquid.xyz/info", json={"type": "meta"}) as r:
            meta = await r.json()
            btc_meta = next(u for u in meta['universe'] if u['name'] == 'BTC')
            btc_idx = meta['universe'].index(btc_meta)
            btc_decimals = btc_meta['szDecimals']
            print("BTC Asset Index: " + str(btc_idx) + " | Size Decimals: " + str(btc_decimals))

        print("\n2. Fetching Leaderboard...")
        async with session.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard") as r:
            lb = await r.json()
            print("Leaderboard fetched. Type: " + str(type(lb)))

        print("\n3. Testing WebSocket allMids...")
        async with websockets.connect("wss://api.hyperliquid.xyz/ws") as ws:
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
            msg = await ws.recv()
            data = json.loads(msg)
            mids = data.get("data", {}).get("mids", {})
            btc_price = float(mids.get("BTC", 0))
            print("Live BTC Mid Price: $" + str(btc_price))

        print("\n4. Testing L1 Action EIP-712 Signing ($11 test order)...")
        if btc_price == 0:
            print("Failed to get BTC price, aborting test.")
            return

        test_px = btc_price * 0.5
        test_sz = 12.0 / test_px
        
        order_wire = {
            "a": btc_idx,
            "b": True,
            "p": _round_price(test_px),
            "s": _round_size(test_sz, btc_decimals),
            "r": False,
            "t": {"limit": {"tif": "Ioc"}}
        }
        
        action = {
            "type": "order",
            "orders":[order_wire],
            "grouping": "na"
        }
        
        nonce = int(time.time() * 1000)
        signature = sign_l1_action(account, action, nonce)
        
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature
        }
        
        print("Dispatching order...")
        async with session.post("https://api.hyperliquid.xyz/exchange", json=payload) as r:
            res = await r.json()
            print("Exchange response: " + str(res))
            if res.get("status") == "ok":
                print("\n[SUCCESS] EIP-712 Signature works perfectly.")
            else:
                print("\n[ERROR] Signature or payload formatting rejected.")

if __name__ == "__main__":
    asyncio.run(main())
