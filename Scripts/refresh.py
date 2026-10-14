import datetime as dt
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
BETTINGPROS = 'https://api.bettingpros.com/v3'

def fetch(url, headers=None):
    request = Request(url, headers={'User-Agent': 'PersonalMarketResearch/1.0', **(headers or {})})
    with urlopen(request, timeout=30) as response:
        return json.load(response)

def main():
    Path('data').mkdir(exist_ok=True)
    snapshot = {'updated_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'kalshi': {'status': 'unavailable', 'markets': []},
                'bettingpros': {'status': 'not_configured'}}
    try:
        markets, cursor = [], None
        for _ in range(5):  # bounded, ~5,000 markets; never imply exhaustive coverage
            args = {'status': 'open', 'limit': 1000, 'mve_filter': 'exclude'}
            if cursor:
                args['cursor'] = cursor
            response = fetch(KALSHI + '/markets?' + urlencode(args))
            markets.extend(response.get('markets', []))
            cursor = response.get('cursor')
            if not cursor:
                break
        fields = ('ticker', 'title', 'subtitle', 'event_ticker', 'yes_bid_dollars',
                  'yes_ask_dollars', 'no_bid_dollars', 'no_ask_dollars', 'volume_fp', 'close_time')
        snapshot['kalshi'] = {'status': 'ok', 'market_count': len(markets),
                              'more_pages': bool(cursor),
                              'markets': [{key: market.get(key) for key in fields} for market in markets]}
    except Exception as error:
        snapshot['kalshi']['error'] = str(error)[:160]
    key = os.environ.get('BETTINGPROS_API_KEY', '').strip()
    if key:
        # Test the documented authenticated endpoint. Keep premium data PRIVATE: public
        # GitHub Pages and the repository must not redistribute subscription content.
        try:
            data = fetch(BETTINGPROS + '/props?' + urlencode({'sport': 'NBA', 'limit': 1}),
                         {'x-api-key': key})
            snapshot['bettingpros'] = {'status': 'connected', 'test_sport': 'NBA',
                                      'returned_props': len(data.get('props', [])),
                                      'note': 'Premium rows intentionally not published to public Pages.'}
        except Exception as error:
            snapshot['bettingpros'] = {'status': 'connection_error', 'error': str(error)[:160]}
    Path('data/latest.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    print('Kalshi:', snapshot['kalshi']['status'], snapshot['kalshi'].get('market_count', 0))
    print('BettingPros:', snapshot['bettingpros']['status'])

if __name__ == '__main__':
    main()
