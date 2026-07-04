import requests, json

series_list = [
    'KXHIGHMIA', 'KXHIGHNY', 'KXHIGHCHI', 'KXHIGHLAX', 'KXHIGHDFW',
    'KXHIGHIAH', 'KXHIGHSAT', 'KXHIGHDCA', 'KXHIGHPHL', 'KXHIGHORD',
    'KXHIGHBOS', 'KXHIGHSFO', 'KXHIGHDEN', 'KXHIGHATL', 'KXHIGHPHX',
    'KXHIGHLAS', 'KXHIGHSTL', 'KXHIGHMSP', 'KXHIGHDET', 'KXHIGHCLE'
]

for series in series_list:
    r = requests.get(
        'https://external-api.kalshi.com/trade-api/v2/markets',
        params={'series_ticker': series, 'status': 'open'},
        timeout=10
    )
    markets = r.json().get('markets', [])
    if markets:
        m = markets[0]
        rules = m.get('rules_primary', '')
        print(f'\n=== {series} ===')
        print(rules[:300])
    else:
        print(f'{series}: no markets')