import datetime as dt
import json, math, os, re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
BP = 'https://api.bettingpros.com/v3'
SPORTS = ('NBA', 'NFL', 'MLB', 'NHL')
UTC = dt.timezone.utc

def fetch(url, headers=None):
    req = Request(url, headers={'User-Agent': 'KalshiResearchDashboard/3.0', **(headers or {})})
    with urlopen(req, timeout=35) as res:
        return json.load(res)

def normalize(s):
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').lower()).strip()

def number(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None

def market_type(s):
    s = normalize(s)
    aliases = {
        'points': ('points', 'pts'), 'rebounds': ('rebounds', 'reb'),
        'assists': ('assists', 'ast'), 'strikeouts': ('strikeouts', 'strikeout', 'pitcher ks'),
        'receptions': ('receptions', 'catches'), 'passing yards': ('passing yards', 'pass yards'),
        'receiving yards': ('receiving yards', 'rec yards'), 'rushing yards': ('rushing yards', 'rush yards'),
        'three pointers': ('three pointers', 'three point', '3 pointers', '3pt', 'threes'),
        'hits': ('hits',), 'home runs': ('home runs', 'homeruns'), 'saves': ('saves',),
        'shots on goal': ('shots on goal',), 'goals': ('goals',)
    }
    matches = [key for key, words in aliases.items() if any(re.search(r'\b'+re.escape(word)+r'\b', s) for word in words)]
    # Generic 'yards' and 'goals' must not be guessed from ambiguous text.
    matches = [x for x in matches if x != 'goals' or 'shots on goal' not in matches]
    return matches[0] if len(matches) == 1 else None

def bp_market_name(prop, names):
    mid = prop.get('market_id')
    val = names.get(str(mid), '')
    for key in ('market_name', 'market_label', 'stat_type', 'stat'):
        val += ' ' + str(prop.get(key) or '')
    m = prop.get('market')
    if isinstance(m, dict): val += ' ' + str(m.get('name') or m.get('label') or '')
    elif isinstance(m, str): val += ' ' + m
    return val

def exact_match(prop, market, sport, names):
    participant = prop.get('participant') or {}
    if not isinstance(participant, dict): return None
    name = normalize(participant.get('name'))
    if len(name.split()) < 2: return None
    title = ' '.join(str(market.get(k) or '') for k in ('title', 'subtitle', 'yes_sub_title'))
    text = normalize(title)
    if not re.search(r'\b' + re.escape(name) + r'\b', text): return None
    # Sports must be identifiable in the Kalshi ticker or event ticker.
    ids = (str(market.get('ticker') or '') + ' ' + str(market.get('event_ticker') or '')).upper()
    if sport not in ids: return None
    bpstat = market_type(bp_market_name(prop, names))
    kmstat = market_type(title)
    if not bpstat or bpstat != kmstat: return None
    over = prop.get('over') or {}
    if not isinstance(over, dict): return None
    line = number(over.get('line'))
    if line is None or abs(line - (math.floor(line) + .5)) > .001: return None
    threshold = math.floor(line) + 1
    if not re.search(r'(?<!\d)' + str(threshold) + r'(?!\d)', text): return None
    # Confirm YES means over / at least threshold, not a range or exact count.
    if not re.search(r'\b(at least|or more|more than|over)\b', text): return None
    if re.search(r'\b(between|exactly|under|fewer|less than)\b', text): return None
    return {'player': name, 'stat': bpstat, 'threshold': threshold, 'line': line}

def main():
    Path('data').mkdir(exist_ok=True)
    now = dt.datetime.now(UTC)
    output = {
        'updated_utc': now.isoformat(),
        'kalshi': {'status': 'unavailable', 'markets': []},
        'bettingpros': {'status': 'not_configured'},
        'scanner': {'status': 'insufficient_data', 'review': [], 'scanned_props': 0,
                    'usable_projections': 0, 'matches_checked': 0,
                    'rejections': {}, 'note': 'Review candidates only; not verified orders.'}
    }
    markets = []
    try:
        cursor = None
        # 15 pages, capped to avoid unbounded runtime; still not guaranteed exhaustive.
        for _ in range(15):
            params = {'status': 'open', 'limit': 1000, 'mve_filter': 'exclude'}
            if cursor: params['cursor'] = cursor
            data = fetch(KALSHI + '/markets?' + urlencode(params))
            markets.extend(data.get('markets') or [])
            cursor = data.get('cursor')
            if not cursor: break
        output['kalshi'] = {
            'status': 'ok', 'market_count': len(markets), 'more_pages': bool(cursor),
            'markets': [{k: m.get(k) for k in (
                'ticker', 'event_ticker', 'title', 'subtitle', 'yes_sub_title',
                'yes_bid_dollars', 'yes_ask_dollars', 'no_bid_dollars', 'no_ask_dollars',
                'volume_fp', 'close_time')} for m in markets]
        }
    except Exception as ex:
        output['kalshi']['error'] = str(ex)[:140]
    key = os.environ.get('BETTINGPROS_API_KEY', '').strip()
    if key:
        props, errors, successful = [], [], 0
        names = {}
        for sport in SPORTS:
            try:
                data = fetch(BP + '/props?' + urlencode({'sport': sport, 'limit': 100}), {'x-api-key': key})
                if not isinstance(data, dict) or not isinstance(data.get('props'), list):
                    errors.append(sport + ': unexpected props response shape')
                    continue
                successful += 1
                props.extend((sport, p) for p in data['props'] if isinstance(p, dict))
            except Exception as ex:
                errors.append(sport + ': ' + str(ex)[:90])
        # Market names are needed because BettingPros prop rows often contain only market_id.
        for sport in SPORTS:
            try:
                data = fetch(BP + '/markets?' + urlencode({'sport': sport, 'limit': 200}), {'x-api-key': key})
                for m in data.get('markets', []) if isinstance(data, dict) else []:
                    if isinstance(m, dict):
                        names[str(m.get('id') or m.get('market_id'))] = str(m.get('name') or m.get('label') or '')
            except Exception as ex:
                errors.append(sport + ' market labels: ' + str(ex)[:75])
        output['bettingpros'] = {'status': 'connected' if successful else 'connection_error',
                                 'successful_queries': successful, 'sports_queried': list(SPORTS),
                                 'market_labels': len(names), 'errors': errors}
        output['scanner']['scanned_props'] = len(props)
        usable = 0
        stats_known = 0
        for sport, prop in props:
            projection = prop.get('projection') or {}
            if not isinstance(projection, dict): continue
            side = str(projection.get('recommended_side') or '').lower()
            probability = number(projection.get('probability'))
            if side not in ('over', 'under') or probability is None or not 0 < probability < 1:
                continue
            usable += 1
            if market_type(bp_market_name(prop, names)): stats_known += 1
            p_over = probability if side == 'over' else 1 - probability
            for m in markets:
                match = exact_match(prop, m, sport, names)
                if not match: continue
                output['scanner']['matches_checked'] += 1
                try: close = dt.datetime.fromisoformat(str(m.get('close_time')).replace('Z', '+00:00'))
                except (TypeError, ValueError): continue
                if not now < close <= now + dt.timedelta(hours=30): continue
                yes, no = number(m.get('yes_ask_dollars')), number(m.get('no_ask_dollars'))
                if yes is None or no is None or not (0 < yes < 1 and 0 < no < 1): continue
                for direction, price, p in (('YES', yes, p_over), ('NO', no, 1-p_over)):
                    # Fixed buffer is illustrative, NOT a verified Kalshi fee quote.
                    edge = p - price - .03
                    if edge <= .05: continue
                    output['scanner']['review'].append({
                        'ticker': m.get('ticker'), 'title': m.get('title'), 'side': direction,
                        'ask': round(price, 4), 'model_probability': round(p, 4),
                        'edge_after_buffer': round(edge, 4), 'close_time': m.get('close_time'),
                        'volume': m.get('volume_fp'), 'match': match,
                        'warning': 'UNVERIFIED: confirm exact settlement, current executable ask, and fees.'
                    })
        output['scanner']['usable_projections'] = usable
        output['scanner']['stats_identified'] = stats_known
        output['scanner']['review'].sort(key=lambda m: m['edge_after_buffer'], reverse=True)
        # One side per ticker, no duplicated recommendations.
        unique = {}
        for c in output['scanner']['review']:
            unique.setdefault(c['ticker'], c)
        output['scanner']['review'] = list(unique.values())[:20]
        output['scanner']['status'] = 'review_candidates' if unique else 'no_qualified_matches'
        output['scanner']['diagnostic'] = (
            'No usable BettingPros projection probabilities' if usable == 0 else
            'BettingPros stat names unavailable: market_id mapping needed' if stats_known == 0 else
            'No identical player/stat/threshold YES definitions found within sampled markets' if output['scanner']['matches_checked'] == 0 else
            'Matches found but no positive edge passed timing/price/buffer filters' if not unique else
            'Review candidates found; rules not independently verified'
        )
    Path('data/latest.json').write_text(json.dumps(output, separators=(',', ':')), encoding='utf-8')
    print('Kalshi:', output['kalshi']['status'], len(markets), 'BettingPros:', output['bettingpros']['status'])
    print('Props:', output['scanner']['scanned_props'], 'usable probabilities:', output['scanner']['usable_projections'],
          'matched:', output['scanner']['matches_checked'], 'candidates:', len(output['scanner']['review']))
    print('Diagnostic:', output['scanner'].get('diagnostic', 'not configured'))
    print('BettingPros market labels:', output['bettingpros'].get('market_labels', 0))
    print('API errors:', output['bettingpros'].get('errors', []))

if __name__ == '__main__': main()
