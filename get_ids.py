import requests

print('\n--- COPY THIS TO SEND TO ME ---')
try:
    res = requests.get("https://api-evm.orderly.org/v1/public/info")
    data = res.json()
    
    for m in data.get('data', {}).get('rows',[]):
        sym = m.get('symbol', '')
        if 'PERP_' in sym:
            coin = sym.split('_')[1]
            if coin in['BTC', 'ETH', 'SOL', 'HYPE', 'BNB', 'PAX', 'XAG', 'WTI']:
                pid = m.get('product_id')
                p_tick = m.get('quote_tick')
                s_tick = m.get('base_tick')
                min_s = m.get('base_min')
                print(f'"{coin}": {{"id": {pid}, "p_tick": {p_tick}, "s_tick": {s_tick}, "min_s": {min_s}}},')
except Exception as e:
    print(f'Error: {e}')
print('-------------------------------\n')