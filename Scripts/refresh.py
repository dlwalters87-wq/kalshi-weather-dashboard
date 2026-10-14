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
from urllib.parse import urlencode, urljoin, urlsplit, parse_qsl
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
BP = 'https://api.bettingpros.com/v3'
SPORTS = ('NBA', 'NFL', 'MLB', 'NHL')
UTC = dt.timezone.utc

MAX_KALSHI_PAGES = 10
MAX_BP_PAGES = 30
BUFFER = .03
MIN_EDGE = .05


def fetch(url, headers=None):
    for attempt in range(3):
        try:
            req = Request(url, headers={
                'User-Agent': 'KalshiResearchDashboard/6.0',
                **(headers or {})
            })
            with urlopen(req, timeout=35) as res:
                return json.load(res)
        except HTTPError as ex:
            if ex.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def safe_error(ex):
    # Never publish request headers, credentials, or response bodies.
    if isinstance(ex, HTTPError):
        return 'HTTP ' + str(ex.code)
    return type(ex).__name__


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(c for c in value if not unicodedata.combining(c))
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

    for value in (data.get(key), data.get('data'), data.get('results')):
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            nested = value.get(key) or value.get('items')
            if isinstance(nested, list):
                return [r for r in nested if isinstance(r, dict)]
    return []


ALIASES = {
    'points': ('points', 'pts'),
    'rebounds': ('rebounds', 'reb'),
    'assists': ('assists', 'ast'),
    'strikeouts': ('strikeouts', 'strikeout', 'pitcher ks'),
    'receptions': ('receptions', 'catches'),
    'passing yards': ('passing yards', 'pass yards'),
    'receiving yards': ('receiving yards', 'rec yards'),
    'rushing yards': ('rushing yards', 'rush yards'),
    'three pointers': (
        'three pointers', 'three point', '3 pointers', '3pt', 'threes'
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

SERIES = {
    'NBA': (
        'KXNBAPTS', 'KXNBAREB', 'KXNBAAST', 'KXNBA3PT'
    ),
    'NFL': (
        'KXNFLPASSYDS', 'KXNFLRECYDS', 'KXNFLRSHYDS', 'KXNFLREC'
    ),
    'MLB': (
        'KXMLBHIT', 'KXMLBTB', 'KXMLBSB', 'KXMLBKS'
    ),
    'NHL': (
        'KXNHLAST', 'KXNHLSAVES', 'KXNHLSAVE',
        'KXNHLGOAL', 'KXNHLANYGOAL'
    ),
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

    # Do not treat combined stats as one of their components.
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

    for key in ('market_name', 'market_label', 'stat_type', 'stat'):
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
    return any(re.search(p, label, re.I) for p in patterns)


def bp_next(current, endpoint, pagination):
    nxt = pagination.get('next')
    if not nxt:
        return None

    if not isinstance(nxt, str):
        raise ValueError('Unsupported pagination format')

    target = urlsplit(urljoin(current, nxt))

    if (
        target.scheme != 'https'
        or target.netloc != 'api.bettingpros.com'
        or target.path.rstrip('/') != '/v3/' + endpoint
    ):
        raise ValueError('Unexpected pagination destination')

    # Never copy credentials from pagination URLs.
    # Authentication is supplied through the header only.
    allowed = {
        'sport', 'page', 'limit', 'per_page',
        'market_id', 'event_id', 'date', 'season'
    }
    params = [
        (k, v)
        for k, v in parse_qsl(target.query)
        if k in allowed
    ]

    return BP + '/' + endpoint + '?' + urlencode(params)


def bp_pages(endpoint, sport, headers):
    url = BP + '/' + endpoint + '?' + urlencode({
        'sport': sport,
        'limit': 100
    })

    result = []
    seen = set()
    fingerprints = set()
    info = {
        'pages': 0,
        'items': 0,
        'more_pages': False
    }

    for _ in range(MAX_BP_PAGES):
        if url in seen:
            info['error'] = 'Repeated page URL'
            break

        seen.add(url)

        try:
            data = fetch(url, headers)
            batch = rows(data, endpoint)
            fingerprint = json.dumps(batch, sort_keys=True)

            if batch and fingerprint in fingerprints:
                info['error'] = 'Repeated page content'
                break

            fingerprints.add(fingerprint)
            result.extend(batch)
            info['pages'] += 1

            pagination = (
                data.get('_pagination', {})
                if isinstance(data, dict)
                else {}
            )
            if not isinstance(pagination, dict):
                pagination = {}

            for field in ('total_pages', 'total_items', 'limit'):
                value = number(pagination.get(field))
                if value is not None:
                    info[field] = int(value)

            info['more_pages'] = bool(pagination.get('next'))
            url = bp_next(url, endpoint, pagination)

            if not url:
                break

        except Exception as ex:
            info['error'] = safe_error(ex)
            info['more_pages'] = True
            break

    info['items'] = len(result)

    if (
        'total_items' in info
        and info['items'] < info['total_items']
    ):
        info['more_pages'] = True

    return result, info


def scheduled_date(m):
    # Use the explicit date in settlement rules.
    # A contract's closing deadline is not the game date.
    rules = (
        str(m.get('rules_primary') or '')
        + ' '
        + str(m.get('rules_secondary') or '')
    )

    found = re.findall(
        r'originally scheduled for\s+'
        r'([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})',
        rules,
        re.I
    )

    dates = set()

    for month, day, year in found:
        for fmt in ('%b %d %Y', '%B %d %Y'):
            try:
                value = dt.datetime.strptime(
                    month + ' ' + day + ' ' + year,
                    fmt
                ).date()
                dates.add(value)
                break
            except ValueError:
                pass

    return next(iter(dates)) if len(dates) == 1 else None


def main():
    now = dt.datetime.now(UTC)
    today = now.astimezone(
        ZoneInfo('America/New_York')
    ).date()

    rejected = Counter()
    stats = Counter()
    unknown = Counter()

    debug = {
        'sports': {},
        'series': {},
        'time_examples': []
    }

    errors = []
    props = []
    mapping = {}
    markets = []

    key = os.getenv('BETTINGPROS_API_KEY', '').strip()
    success = 0

    if key:
        headers = {'x-api-key': key}

        for sport in SPORTS:
            found, pinfo = bp_pages('props', sport, headers)
            definitions, minfo = bp_pages(
                'markets', sport, headers
            )

            debug['sports'][sport] = {
                'props': pinfo,
                'markets': minfo
            }

            if pinfo['pages']:
                success += 1

            props.extend((sport, p) for p in found)

            for definition in definitions:
                mid = definition.get('id')
                if mid is None:
                    mid = definition.get('market_id')

                if mid is not None:
                    mapping[(sport, str(mid))] = market_label(
                        definition, sport
                    )

            for endpoint, info in (
                ('props', pinfo),
                ('markets', minfo)
            ):
                if info.get('error'):
                    errors.append(
                        sport + ' ' + endpoint + ': '
                        + info['error']
                    )

    incomplete = False

    for sport, tickers in SERIES.items():
        for ticker in tickers:
            cursor = None
            seen = set()

            info = {
                'pages': 0,
                'items': 0,
                'more_pages': False
            }
            debug['series'][ticker] = info

            try:
                for _ in range(MAX_KALSHI_PAGES):
                    params = {
                        'series_ticker': ticker,
                        'status': 'open',
                        'limit': 1000,
                        'mve_filter': 'exclude'
                    }
                    if cursor:
                        params['cursor'] = cursor

                    data = fetch(
                        KALSHI + '/markets?' + urlencode(params)
                    )
                    batch = rows(data, 'markets')
                    markets.extend((sport, m) for m in batch)

                    info['pages'] += 1
                    info['items'] += len(batch)

                    cursor = (
                        data.get('cursor')
                        if isinstance(data, dict)
                        else None
                    )
                    info['more_pages'] = bool(cursor)

                    if not cursor:
                        break

                    if cursor in seen:
                        raise ValueError('Repeated cursor')

                    seen.add(cursor)

            except Exception as ex:
                info['error'] = safe_error(ex)
                info['more_pages'] = True

            incomplete = incomplete or info['more_pages']

    markets = list({
        m['ticker']: (sport, m)
        for sport, m in markets
        if m.get('ticker')
    }.values())

    indexed = {}
    for sport, m in markets:
        indexed.setdefault(sport, []).append((
            m,
            market_text(m),
            norm(market_text(m))
        ))

    scanner = {
        'status': 'insufficient_data',
        'review': [],
        'scanned_props': len(props),
        'usable_projections': 0,
        'stats_identified': 0,
        'matches_checked': 0
    }

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
            unknown[
                sport + ': '
                + (label[:80] or '(unmapped market ID)')
            ] += 1
            rejected['prop_unknown_stat'] += 1
            continue

        scanner['stats_identified'] += 1
        stats[stat] += 1

        participant = p.get('participant')
        player = (
            norm(participant.get('name'))
            if isinstance(participant, dict)
            else ''
        )

        over = p.get('over')
        line = (
            number(over.get('line'))
            if isinstance(over, dict)
            else None
        )

        if len(player.split()) < 2:
            rejected['prop_missing_name'] += 1
            continue

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
            for m, raw, text in indexed.get(sport, [])
            if pattern.search(text)
        ]

        if not candidates:
            rejected['prop_no_player_overlap'] += 1

        for m, raw in candidates:
            if stat_type(raw) != stat:
                rejected['contract_stat_mismatch'] += 1
                continue

            if not threshold_matches(raw, line):
                rejected['contract_threshold_mismatch'] += 1
                continue

            scanner['matches_checked'] += 1
            game_date = scheduled_date(m)

            if len(debug['time_examples']) < 8:
                debug['time_examples'].append({
                    'ticker': m.get('ticker'),
                    'scheduled_date': str(game_date),
                    'close_time': m.get('close_time'),
                    'expected_expiration_time': m.get(
                        'expected_expiration_time'
                    )
                })

            if game_date is None:
                rejected[
                    'contract_scheduled_date_unverified'
                ] += 1
                continue

            if game_date != today:
                rejected['contract_not_scheduled_today'] += 1
                continue

            try:
                close = dt.datetime.fromisoformat(
                    str(m.get('close_time')).replace(
                        'Z', '+00:00'
                    )
                )
                if close.tzinfo is None or close <= now:
                    raise ValueError('Closed or invalid deadline')
            except (TypeError, ValueError):
                rejected[
                    'contract_closed_or_invalid_deadline'
                ] += 1
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
                    rejected['side_edge_below_requirement'] += 1
                    continue

                review.append({
                    'ticker': m.get('ticker'),
                    'title': m.get('title'),
                    'side': direction,
                    'ask': round(price, 4),
                    'model_probability': round(p_side, 4),
                    'edge_after_buffer': round(edge, 4),
                    'close_time': m.get('close_time'),
                    'scheduled_game_date': game_date.isoformat(),
                    'volume': m.get('volume_fp'),
                    'match': {
                        'player': player,
                        'stat': stat,
                        'line': line,
                        'threshold': math.floor(line) + 1
                    },
                    'warning': (
                        'UNVERIFIED cross-provider game identity/date '
                        'and period. Check rules, live price and fees.'
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
        'unknown_stat_labels': dict(unknown.most_common(25))
    })
    scanner['debug'] = debug

    summary = (
        'Research candidates require event/period verification.'
        if unique
        else 'No candidate passed this scan.'
    )

    if not key:
        summary = 'BETTINGPROS_API_KEY is not configured.'

    scanner['diagnostic'] = (
        summary
        + ' Rejections: '
        + json.dumps(dict(rejected), sort_keys=True)
    )

    fields = (
        'ticker', 'event_ticker', 'title', 'subtitle',
        'yes_sub_title', 'yes_bid_dollars', 'yes_ask_dollars',
        'no_bid_dollars', 'no_ask_dollars', 'volume_fp',
        'close_time', 'expected_expiration_time'
    )

    out = {
        'updated_utc': now.isoformat(),
        'version': 6,
        'kalshi': {
            'status': 'partial' if incomplete else 'ok',
            'market_count': len(markets),
            'more_pages': incomplete,
            'scope': 'Selected single-game player-prop series',
            'markets': [
                {k: m.get(k) for k in fields}
                for _, m in markets
            ]
        },
        'bettingpros': {
            'status': (
                'connected' if success
                else 'connection_error' if key
                else 'not_configured'
            ),
            'successful_queries': success,
            'market_labels': len(mapping),
            'errors': errors
        },
        'scanner': scanner
    }

    Path('data').mkdir(exist_ok=True)
    Path('data/latest.json').write_text(
        json.dumps(out, separators=(',', ':')),
        encoding='utf-8'
    )

    print(
        'Kalshi:', len(markets),
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
