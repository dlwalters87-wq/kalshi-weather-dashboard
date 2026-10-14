import datetime as dt
import json, math, os, re
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
BP = 'https://api.bettingpros.com/v3'
SPORTS = ('NBA', 'NFL', 'MLB', 'NHL')
UTC = dt.timezone.utc


def fetch(url, headers=None):
    req = Request(url, headers={'User-Agent': 'KalshiResearchDashboard/4.0', **(headers or {})})
    with urlopen(req, timeout=35) as res:
        return json.load(res)


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').lower()).strip()


def number(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


STAT_ALIASES = {
    'points': ('points', 'pts'), 'rebounds': ('rebounds', 'reb'),
    'assists': ('assists', 'ast'), 'strikeouts': ('strikeouts', 'strikeout', 'pitcher ks'),
    'receptions': ('receptions', 'catches'),
    'passing yards': ('passing yards', 'pass yards'),
    'receiving yards': ('receiving yards', 'rec yards'),
    'rushing yards': ('rushing yards', 'rush yards'),
    'three pointers': ('three pointers', 'three point', '3 pointers', '3pt', 'threes'),
    'hits': ('hits',), 'home runs': ('home runs', 'homeruns'),
    'saves': ('saves',), 'shots on goal': ('shots on goal',),
}


def stat_type(s):
    s = norm(s)
    matches = [key for key, aliases in STAT_ALIASES.items()
               if any(re.search(r'\b' + re.escape(a) + r'\b', s) for a in aliases)]
    return matches[0] if len(matches) == 1 else None


def rows(data, key):
    if isinstance(data, list): return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict): return []
    candidates = [data.get(key), data.get('data'), data.get('results')]
    for val in candidates:
        if isinstance(val, list): return [r for r in val if isinstance(r, dict)]
        if isinstance(val, dict):
            nested = val.get(key) or val.get('items')
            if isinstance(nested, list): return [r for r in nested if isinstance(r, dict)]
    return []


def market_label(m):
    for key in ('stat','name', 'label', 'title', 'display_name', 'slug', 'key'):
        if isinstance(m.get(key), str) and m[key].strip(): return m[key]
    return ''


def prop_stat_label(p, mapping, sport):
    mid = str(p.get('market_id'))
    vals = [mapping.get((sport, mid), '')]
    for key in ('market_name', 'market_label', 'stat_type', 'stat'):
        if isinstance(p.get(key), str): vals.append(p[key])
    m = p.get('market')
    if isinstance(m, dict): vals.append(market_label(m))
    elif isinstance(m, str): vals.append(m)
    return ' '.join(vals)


def exact_match(p, m, sport, mapping):
    player = p.get('participant')
    if not isinstance(player, dict): return None
    name = norm(player.get('name'))
    if len(name.split()) < 2: return None
    label = ' '.join(str(m.get(k) or '') for k in ('title', 'subtitle', 'yes_sub_title'))
    text = norm(label)
    if not re.search(r'\b' + re.escape(name) + r'\b', text): return None
    bpstat, kmstat = stat_type(prop_stat_label(p, mapping, sport)), stat_type(label)
    if not bpstat or bpstat != kmstat: return None
    over = p.get('over')
    if not isinstance(over, dict): return None
    line = number(over.get('line'))
    if line is None or abs(line - (math.floor(line) + .5)) > .001: return None
    threshold = math.floor(line) + 1
    if not re.search(r'(?<!\d)' + str(threshold) + r'(?!\d)', text): return None
    if not re.search(r'\b(at least|or more|more than|over)\b', text): return None
    if re.search(r'\b(between|exactly|under|fewer|less than)\b', text): return None
    return {'player': name, 'stat': bpstat, 'threshold': threshold, 'line': line}


def main():
    Path('data').mkdir(exist_ok=True)
    now = dt.datetime.now(UTC)
    out = {
        'updated_utc': now.isoformat(),
        'kalshi': {'status': 'unavailable', 'markets': []},
        'bettingpros': {'status': 'not_configured'},
        'scanner': {'status': 'insufficient_data', 'review': [], 'scanned_props': 0,
                    'usable_projections': 0, 'matches_checked': 0, 'stats_identified': 0}
    }
    markets = []
    try:
        cursor = None
        for _ in range(15):
            params = {'status': 'open', 'limit': 1000, 'mve_filter': 'exclude'}
            if cursor: params['cursor'] = cursor
            data = fetch(KALSHI + '/markets?' + urlencode(params))
            markets.extend(rows(data, 'markets'))
            cursor = data.get('cursor') if isinstance(data, dict) else None
            if not cursor: break
        fields = ('ticker', 'event_ticker', 'title', 'subtitle', 'yes_sub_title',
                  'yes_bid_dollars', 'yes_ask_dollars', 'no_bid_dollars', 'no_ask_dollars',
                  'volume_fp', 'close_time')
        out['kalshi'] = {'status': 'ok', 'market_count': len(markets), 'more_pages': bool(cursor),
                         'markets': [{k: m.get(k) for k in fields} for m in markets]}
    except Exception as ex:
        out['kalshi']['error'] = str(ex)[:140]
    key = os.getenv('BETTINGPROS_API_KEY', '').strip()
    if key:
        props, mapping, errors = [], {}, []
        diagnostics = {'market_response_top_keys': [], 'market_row_keys': [],
                       'prop_row_keys': [], 'prop_market_ids': [], 'market_ids': [],
                       'matched_market_ids': 0, 'mapped_label_examples': [],
                       'stat_counts': {}, 'player_name_matches': 0}
        success = 0
        headers = {'x-api-key': key}
        for sport in SPORTS:
            try:
                data = fetch(BP + '/props?' + urlencode({'sport': sport, 'limit': 20}), headers)
                found = rows(data, 'props')
                if found:
                    diagnostics['prop_row_keys'] = sorted(found[0].keys())
                props.extend((sport, p) for p in found)
                success += 1
            except Exception as ex:
                errors.append(sport + ' props: ' + str(ex)[:100])
        for sport in SPORTS:
            try:
                data = fetch(BP + '/markets?' + urlencode({'sport': sport, 'limit': 200}), headers)
                if isinstance(data, dict): diagnostics['market_response_top_keys'] = sorted(data.keys())
                found = rows(data, 'markets')
                if found: diagnostics['market_row_keys'] = sorted(found[0].keys())
                for m in found:
                    mid = m.get('id') if m.get('id') is not None else m.get('market_id')
                    if mid is not None:
                        mapping[(sport, str(mid))] = market_label(m)
            except Exception as ex:
                errors.append(sport + ' markets: ' + str(ex)[:100])
        mids = {(sport, str(p.get('market_id'))) for sport, p in props if p.get('market_id') is not None}
        diagnostics['prop_market_ids'] = sorted({mid for _, mid in mids})[:12]
        diagnostics['market_ids'] = sorted({mid for _, mid in mapping})[:12]
        diagnostics['matched_market_ids'] = sum(mid in mapping for mid in mids)
        diagnostics['mapped_label_examples'] = sorted({mapping[mid] for mid in mids if mid in mapping and mapping[mid]})[:8]
        out['bettingpros'] = {'status': 'connected' if success else 'connection_error',
                              'successful_queries': success, 'market_labels': len(mapping),
                              'errors': errors}
        out['scanner']['scanned_props'] = len(props)
        usable, stats, name_hits = 0, Counter(), 0
        for sport, p in props:
            proj = p.get('projection')
            if not isinstance(proj, dict): continue
            side = str(proj.get('recommended_side') or '').lower()
            prob = number(proj.get('probability'))
            if side not in ('over', 'under') or prob is None or not 0 < prob < 1: continue
            usable += 1
            label = prop_stat_label(p, mapping, sport)
            stat = stat_type(label)
            if stat: stats[stat] += 1
            participant = p.get('participant')
            player = norm(participant.get('name')) if isinstance(participant, dict) else ''
            # Count player-name matches without publishing names or raw projections.
            candidates = [m for m in markets if len(player.split()) >= 2 and
                          re.search(r'\b' + re.escape(player) + r'\b',
                                    norm(' '.join(str(m.get(k) or '') for k in ('title','subtitle','yes_sub_title'))))]
            name_hits += len(candidates)
            for m in candidates:
                match = exact_match(p, m, sport, mapping)
                if not match: continue
                out['scanner']['matches_checked'] += 1
                try: close = dt.datetime.fromisoformat(str(m.get('close_time')).replace('Z', '+00:00'))
                except (TypeError, ValueError): continue
                if not now < close <= now + dt.timedelta(hours=30): continue
                p_over = prob if side == 'over' else 1 - prob
                for direction, price_field, p_side in (
                    ('YES', 'yes_ask_dollars', p_over), ('NO', 'no_ask_dollars', 1-p_over)):
                    price = number(m.get(price_field))
                    if price is None or not 0 < price < 1: continue
                    edge = p_side - price - .03
                    if edge <= .05: continue
                    out['scanner']['review'].append({
                        'ticker': m.get('ticker'), 'title': m.get('title'), 'side': direction,
                        'ask': round(price, 4), 'model_probability': round(p_side, 4),
                        'edge_after_buffer': round(edge, 4), 'close_time': m.get('close_time'),
                        'volume': m.get('volume_fp'), 'match': match,
                        'warning': 'UNVERIFIED: check contract settlement, live ask, and fees.'})
        diagnostics['stat_counts'] = dict(stats)
        diagnostics['player_name_matches'] = name_hits
        out['scanner']['usable_projections'] = usable
        out['scanner']['stats_identified'] = sum(stats.values())
        out['scanner']['review'].sort(key=lambda x: x['edge_after_buffer'], reverse=True)
        unique = {}
        for c in out['scanner']['review']:
            unique.setdefault(c['ticker'], c)
        out['scanner']['review'] = list(unique.values())[:20]
        out['scanner']['status'] = 'review_candidates' if unique else 'no_qualified_matches'
        out['scanner']['diagnostic'] = (
            'No usable probabilities' if usable == 0 else
            'Market IDs did not map to recognizable stats' if not stats else
            'No player names overlap in sampled Kalshi contracts' if name_hits == 0 else
            'Player names overlap but exact stat/threshold/direction did not match' if not out['scanner']['matches_checked'] else
            'Exact labels matched, but no candidate cleared time/price/edge filters' if not unique else
            'Review candidates identified; settlement terms not verified')
        out['scanner']['debug'] = diagnostics
        print('DIAGNOSTICS (field names/IDs only; no secret or projections):', json.dumps(diagnostics, sort_keys=True))
    Path('data/latest.json').write_text(json.dumps(out, separators=(',', ':')), encoding='utf-8')
    print('Kalshi:', out['kalshi']['status'], len(markets), 'BettingPros:', out['bettingpros']['status'])
    print('Props:', out['scanner']['scanned_props'], 'usable:', out['scanner']['usable_projections'],
          'stats identified:', out['scanner']['stats_identified'], 'matched:', out['scanner']['matches_checked'],
          'candidates:', len(out['scanner']['review']))
    print('Diagnostic:', out['scanner'].get('diagnostic', 'not configured'))

if __name__ == '__main__': main()
