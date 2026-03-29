cd /root/hl-copybot
source venv/bin/activate
python3 -c "
import os
from nado_protocol.client import create_nado_client
from dotenv import load_dotenv

load_dotenv('/root/hl-copybot/.env')
client = create_nado_client('mainnet', os.getenv('NADO_PRIVATE_KEY'))

print('\n--- COPY THIS TO SEND TO ME ---')
try:
    res = client.market.get_all_engine_markets()
    markets = getattr(res, 'data', res)
    if isinstance(markets, dict): markets = list(markets.values())
    
    for m in markets:
        m_dict = m if isinstance(m, dict) else vars(m)
        sym = m_dict.get('symbol', '')
        if 'PERP_' in sym:
            coin = sym.split('_')[1]
            if coin in['BTC', 'ETH', 'SOL', 'HYPE', 'BNB', 'PAX', 'XAG', 'WTI']:
                print(f'\"{coin}\": {{\"id\": {m_dict.get(\"product_id\", m_dict.get(\"productId\"))}, \"p_tick\": {float(m_dict.get(\"price_increment_x18\", 0))/1e18}, \"s_tick\": {float(m_dict.get(\"base_tick_x18\", 0))/1e18}}},')
except Exception as e:
    print(f'Error fetching from Nado: {e}')
print('-------------------------------\n')
"