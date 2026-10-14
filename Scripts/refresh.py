import datetime as dt
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
BP = 'https://api.bettingpros.com/v3'
SPORTS = ('NBA', 'NFL', 'MLB', 'NHL')
UTC = dt.timezone.utc

MAX_KALSHI_PAGES = 100
BP_PROP_LIMIT = 100
BUFFER = .03
MIN_EDGE = .05


def fetch(url, headers=None):
    for attempt in range(3):
        try:
            req = Request(url, headers={
                'User-Agent': 'KalshiResearchDashboard/5.0',
                **(headers or {})
            })
            with urlopen(req, timeout=35) as res:
                return json.load(res)
        except HTTPError as ex:
            if ex.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def safe_error(ex):
    # Never publish headers, credentials, or response bodies.
    if isinstance(ex, HTTPError):
        return 'HTTP ' + str(ex.code)
    return type(ex).__name__


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(
        c for c in value
        if not unicodedata.combining(c)
    )
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def rows(data, key):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]

    if not isinstance(data, dict):
        return []

    for value in (
        data.get(key),
        data.get('data'),
        data.get('results')
    ):
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]

        if isinstance(value, dict):
            nested = value.get(key) or value.get('items')
            if isinstance(nested, list):
                return [
                    r for r in nested
                    if isinstance(r, dict)
                ]
    return []


ALIASES = {
    'points': ('points', 'pts'),
    'rebounds': ('rebounds', 'reb'),
    'assists': ('assists', 'ast'),
    'strikeouts': (
        'strikeouts', 'strikeout', 'pitcher ks'
    ),
    'receptions': ('receptions', 'catches'),
    'passing yards': ('passing yards', 'pass yards'),
    'receiving yards': ('receiving yards', 'rec yards'),
    'rushing yards': ('rushing yards', 'rush yards'),
    'three pointers': (
        'three pointers', 'three point',
        '3 pointers', '3pt', 'threes'
    ),
    'hits': ('hits',),
    'home runs': ('home runs', 'homeruns'),
    'total bases': ('total bases',),
    'stolen bases': ('stolen bases',),
    'goals': ('goals',),
    'saves': ('saves',),
    'shots on goal': ('shots on goal',),
}

BP_CODES = {
    'MLB': {
        'h': 'hits',
        'tb': 'total bases',
        'sb': 'stolen bases',
        'hr': 'home runs'
    },
    'NBA': {
        'pts': 'points',
        'reb': 'rebounds',
        'ast': 'assists'
    },
    'NHL': {
        'sv': 'saves',
        'sog': 'shots on goal'
    },
}


def market_label(m, sport=None):
    for key in (
        'stat', 'name', 'label', 'title',
        'display_name', 'slug', 'key'
    ):
        value = m.get(key)
        if isinstance(value, str) and value.strip():
            return BP_CODES.get(sport, {}).get(
                norm(value), value.strip()
            )
    return ''


def stat_type(value):
    text = norm(value)

    # Reject combined hits/runs/RBIs as a hits-only market.
    if 'hits' in text and ('runs' in text or 'rbis' in text):
        return None

    matches = [
        stat for stat, aliases in ALIASES.items()
        if any(
            re.search(r'\b' + re.escape(a) + r'\b', text)
            for a in aliases
        )
    ]

    return matches[0] if len(matches) == 1 else None


def prop_label(p, mapping, sport):
    values = [
        mapping.get((sport, str(p.get('market_id'))), '')
    ]

    for key in (
        'market_name', 'market_label', 'stat_type', 'stat'
    ):
        value = p.get(key)
        if isinstance(value, str):
            values.append(
                BP_CODES.get(sport, {}).get(norm(value), value)
            )

    market = p.get('market')
    if isinstance(market, dict):
        values.append(market_label(market, sport))
    elif isinstance(market, str):
        values.append(market)

    return ' '.join(values).strip()


def market_text(m):
    return ' '.join(
        str(m.get(k) or '')
        for k in ('title', 'subtitle', 'yes_sub_title')
    )


def threshold_matches(label, line):
    threshold = math.floor(line) + 1

    if re.search(
        r'\b(between|exactly|under|fewer|less than)\b',
        norm(label)
    ):
        return False

    n = re.escape(str(threshold))
    half = re.escape(str(float(line)))

    patterns = (
        r'(?<![\d.])' + n + r'\s*\+',
        r'\bat least\s+' + n + r'(?![\d.])',
        r'(?<![\d.])' + n + r'\s+or more\b',
        r'\b(?:over|more than)\s+' + half + r'(?![\d.])',
    )

    return any(
        re.search(pattern, label, re.I)
        for pattern in patterns
    )


def main():
    now = dt.datetime.now(UTC)
    rejected = Counter()
    stats = Counter()
    unknown = Counter()

    debug = {
        'rejections': {},
        'sports': {},
        'unknown_stat_labels': {}
    }

    out = {
        'updated_utc': now.isoformat(),
        'kalshi': {
            'status': 'unavailable',
            'markets': [],
            'market_count': 0
        },
        'bettingpros': {
            'status': 'not_configured'
        },
        'scanner': {
            'status': 'insufficient_data',
            'review': [],
            'scanned_props': 0,
            'usable_projections': 0,
            'stats_identified': 0,
            'matches_checked': 0
        }
    }

    scanner = out['scanner']
    markets = []
    cursor = None
    pages = 0
    kalshi_error = None
    seen_cursors = set()

    try:
        for _ in range(MAX_KALSHI_PAGES):
            params = {
                'status': 'open',
                'limit': 1000,
                'mve_filter': 'exclude'
            }
            if cursor:
                params['cursor'] = cursor

            data = fetch(
                KALSHI + '/markets?' + urlencode(params)
            )
            markets.extend(rows(data, 'markets'))
            pages += 1

            cursor = (
                data.get('cursor')
                if isinstance(data, dict)
                else None
            )

            if not cursor:
                break

            if cursor in seen_cursors:
                kalshi_error = 'Repeated pagination cursor'
                break

            seen_cursors.add(cursor)

    except Exception as ex:
        kalshi_error = safe_error(ex)

    markets = list({
        m['ticker']: m
        for m in markets
        if m.get('ticker')
    }.values())

    fields = (
        'ticker', 'event_ticker', 'title', 'subtitle',
        'yes_sub_title', 'yes_bid_dollars', 'yes_ask_dollars',
        'no_bid_dollars', 'no_ask_dollars',
        'volume_fp', 'close_time'
    )

    out['kalshi'] = {
        'status': (
            'partial' if markets and kalshi_error
            else 'ok' if markets
            else 'unavailable'
        ),
        'market_count': len(markets),
        'pages_loaded': pages,
        'more_pages': bool(cursor) or bool(kalshi_error),
        'markets': [
            {k: m.get(k) for k in fields}
            for m in markets
        ]
    }

    if kalshi_error:
        out['kalshi']['error'] = kalshi_error

    # Normalize market text once for the entire scan.
    indexed = [
        (m, market_text(m), norm(market_text(m)))
        for m in markets
    ]

    props, mapping, errors = [], {}, []
    key = os.getenv('BETTINGPROS_API_KEY', '').strip()
    success = 0

    if key:
        headers = {'x-api-key': key}

        for sport in SPORTS:
            info = {
                'props_loaded': 0,
                'markets_loaded': 0
            }
            debug['sports'][sport] = info

            for endpoint, limit in (
                ('props', BP_PROP_LIMIT),
                ('markets', 200)
            ):
                try:
                    data = fetch(
                        BP + '/' + endpoint + '?' + urlencode({
                            'sport': sport,
                            'limit': limit
                        }),
                        headers
                    )

                    found = rows(data, endpoint)
                    info[endpoint + '_loaded'] = len(found)

                    # Record field names without exposing raw payloads.
                    # BettingPros coverage remains sampled until its
                    # pagination scheme is verified.
                    pagination = (
                        data.get('_pagination')
                        if isinstance(data, dict)
                        else None
                    )
                    info[endpoint + '_pagination_fields'] = (
                        sorted(pagination)
                        if isinstance(pagination, dict)
                        else []
                    )

                    if endpoint == 'props':
                        success += 1
                        props.extend((sport, p) for p in found)
                    else:
                        for m in found:
                            mid = (
                                m.get('id')
                                if m.get('id') is not None
                                else m.get('market_id')
                            )
                            if mid is not None:
                                mapping[(sport, str(mid))] = (
                                    market_label(m, sport)
                                )

                except Exception as ex:
                    errors.append(
                        sport + ' ' + endpoint + ': '
                        + safe_error(ex)
                    )

        out['bettingpros'] = {
            'status': (
                'connected'
                if success
                else 'connection_error'
            ),
            'successful_queries': success,
            'market_labels': len(mapping),
            'errors': errors
        }

    scanner['scanned_props'] = len(props)
    name_hits = 0
    review = []

    for sport, p in props:
        projection = p.get('projection')

        if not isinstance(projection, dict):
            rejected['prop_missing_projection'] += 1
            continue

        side = str(
            projection.get('recommended_side') or ''
        ).lower()
        probability = number(projection.get('probability'))

        if (
            side not in ('over', 'under')
            or probability is None
            or not 0 < probability < 1
        ):
            rejected['prop_invalid_probability_or_side'] += 1
            continue

        scanner['usable_projections'] += 1
        label = prop_label(p, mapping, sport)
        stat = stat_type(label)

        if not stat:
            rejected['prop_unknown_stat'] += 1
            unknown[
                sport + ': '
                + (label[:80] or '(unmapped market ID)')
            ] += 1
            continue

        stats[stat] += 1
        scanner['stats_identified'] += 1

        participant = p.get('participant')
        player = (
            norm(participant.get('name'))
            if isinstance(participant, dict)
            else ''
        )

        if len(player.split()) < 2:
            rejected['prop_missing_player_name'] += 1
            continue

        over = p.get('over')
        line = (
            number(over.get('line'))
            if isinstance(over, dict)
            else None
        )

        if (
            line is None
            or abs(line - (math.floor(line) + .5)) > .001
        ):
            rejected['prop_missing_or_non_half_line'] += 1
            continue

        pattern = re.compile(
            r'\b' + re.escape(player) + r'\b'
        )
        candidates = [
            (m, raw)
            for m, raw, text in indexed
            if pattern.search(text)
        ]

        name_hits += len(candidates)

        if not candidates:
            rejected['prop_no_player_overlap'] += 1

        for m, raw in candidates:
            if stat_type(raw) != stat:
                rejected['contract_stat_mismatch'] += 1
                continue

            if not threshold_matches(raw, line):
                rejected[
                    'contract_threshold_or_direction_mismatch'
                ] += 1
                continue

            scanner['matches_checked'] += 1

            try:
                close = dt.datetime.fromisoformat(
                    str(m.get('close_time')).replace(
                        'Z', '+00:00'
                    )
                )
                if close.tzinfo is None:
                    raise ValueError('Missing timezone')
            except (TypeError, ValueError):
                rejected['contract_invalid_close_time'] += 1
                continue

            if close <= now:
                rejected['contract_already_closed'] += 1
                continue

            if close > now + dt.timedelta(hours=30):
                rejected['contract_beyond_30_hours'] += 1
                continue

            p_over = (
                probability
                if side == 'over'
                else 1 - probability
            )

            for direction, field, p_side in (
                ('YES', 'yes_ask_dollars', p_over),
                ('NO', 'no_ask_dollars', 1 - p_over)
            ):
                price = number(m.get(field))

                if price is None or not 0 < price < 1:
                    rejected[
                        'side_missing_or_invalid_ask'
                    ] += 1
                    continue

                edge = p_side - price - BUFFER

                if edge <= MIN_EDGE:
                    rejected[
                        'side_edge_not_above_5_percent_after_buffer'
                    ] += 1
                    continue

                review.append({
                    'ticker': m.get('ticker'),
                    'title': m.get('title'),
                    'side': direction,
                    'ask': round(price, 4),
                    'model_probability': round(p_side, 4),
                    'edge_after_buffer': round(edge, 4),
                    'close_time': m.get('close_time'),
                    'volume': m.get('volume_fp'),
                    'match': {
                        'player': player,
                        'stat': stat,
                        'line': line,
                        'threshold': math.floor(line) + 1
                    },
                    'warning': (
                        'UNVERIFIED: event identity/date, period, '
                        'settlement, live ask and fees require review.'
                    )
                })

    review.sort(
        key=lambda x: x['edge_after_buffer'],
        reverse=True
    )

    unique = {}
    for candidate in review:
        unique.setdefault(candidate['ticker'], candidate)

    scanner['review'] = list(unique.values())[:20]
    scanner['status'] = (
        'review_candidates'
        if unique
        else 'no_qualified_matches'
    )

    debug.update({
        'rejections': dict(rejected),
        'stat_counts': dict(stats),
        'unknown_stat_labels': dict(unknown.most_common(20)),
        'player_name_matches': name_hits,
        'kalshi_pages': pages
    })
    scanner['debug'] = debug

    summary = (
        'Research candidates found; event and settlement NOT verified.'
        if unique else
        'Label matches found, but none passed time/price/edge filters.'
        if scanner['matches_checked'] else
        'No exact player/stat/threshold label matches found.'
    )

    if not key:
        summary = 'BETTINGPROS_API_KEY is not configured.'

    scanner['diagnostic'] = (
        summary
        + ' Rejection counts: '
        + json.dumps(dict(rejected), sort_keys=True)
    )

    Path('data').mkdir(exist_ok=True)
    Path('data/latest.json').write_text(
        json.dumps(out, separators=(',', ':')),
        encoding='utf-8'
    )

    print(
        'Kalshi:', out['kalshi']['status'],
        len(markets),
        'pages:', pages,
        'more pages:', out['kalshi']['more_pages'],
        'BettingPros:', out['bettingpros']['status']
    )
    print(
        'Props:', len(props),
        'usable:', scanner['usable_projections'],
        'stats:', scanner['stats_identified'],
        'matched:', scanner['matches_checked'],
        'candidates:', len(scanner['review'])
    )
    print('DIAGNOSTICS:', json.dumps(debug, sort_keys=True))
    print('Diagnostic:', scanner['diagnostic'])


if __name__ == '__main__':
    main()
