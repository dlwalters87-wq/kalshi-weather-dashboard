import datetime as dt
import hashlib
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
UTC = dt.timezone.utc
EASTERN = ZoneInfo('America/New_York')

BP_REQUEST_BUDGET = 40
BP_DELAY_SECONDS = 1.5
BUFFER = .03
MIN_EDGE = .05

SERIES = {
    'KXNBAPTS': ('NBA', 'points'),
    'KXNBAREB': ('NBA', 'rebounds'),
    'KXNBAAST': ('NBA', 'assists'),
    'KXNBA3PT': ('NBA', 'three pointers'),
    'KXNFLPASSYDS': ('NFL', 'passing yards'),
    'KXNFLRECYDS': ('NFL', 'receiving yards'),
    'KXNFLRSHYDS': ('NFL', 'rushing yards'),
    'KXNFLREC': ('NFL', 'receptions'),
    'KXMLBHIT': ('MLB', 'hits'),
    'KXMLBTB': ('MLB', 'total bases'),
    'KXMLBSB': ('MLB', 'stolen bases'),
    'KXMLBKS': ('MLB', 'strikeouts'),
    'KXNHLAST': ('NHL', 'assists'),
    'KXNHLSAVES': ('NHL', 'saves'),
    'KXNHLSAVE': ('NHL', 'saves'),
    'KXNHLGOAL': ('NHL', 'goals'),
    'KXNHLANYGOAL': ('NHL', 'goals'),
}

ALIASES = {
    'points': 'points',
    'pts': 'points',
    'rebounds': 'rebounds',
    'reb': 'rebounds',
    'assists': 'assists',
    'ast': 'assists',
    'receptions': 'receptions',
    'passing yards': 'passing yards',
    'pass yards': 'passing yards',
    'pass yds': 'passing yards',
    'receiving yards': 'receiving yards',
    'rec yards': 'receiving yards',
    'rec yds': 'receiving yards',
    'rushing yards': 'rushing yards',
    'rush yards': 'rushing yards',
    'rush yds': 'rushing yards',
    'three pointers': 'three pointers',
    '3pt': 'three pointers',
    'threes': 'three pointers',
    'hits': 'hits',
    'total bases': 'total bases',
    'stolen bases': 'stolen bases',
    'strikeouts': 'strikeouts',
    'saves': 'saves',
    'goals': 'goals',
}

CODES = {
    'MLB': {
        'h': 'hits',
        'tb': 'total bases',
        'sb': 'stolen bases',
        'k': 'strikeouts',
        'ks': 'strikeouts',
        'hr': 'home runs',
        'hrs': 'home runs'
    },
    'NHL': {
        'sv': 'saves',
        'sog': 'shots on goal'
    },
}


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(
        c for c in value
        if not unicodedata.combining(c)
    )
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def rows(data, key):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if not isinstance(data, dict):
        return []

    for value in (
        data.get(key), data.get('data'), data.get('results')
    ):
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            nested = value.get(key) or value.get('items')
            if isinstance(nested, list):
                return [
                    x for x in nested
                    if isinstance(x, dict)
                ]
    return []


def fetch(url, headers=None):
    request = Request(url, headers={
        'User-Agent': 'KalshiResearchDashboard/7.0',
        **(headers or {})
    })
    with urlopen(request, timeout=35) as response:
        return json.load(response)


def error_label(ex):
    # Never print raw responses, headers, or credentials.
    if isinstance(ex, HTTPError):
        if ex.code == 429:
            try:
                message = json.loads(
                    ex.read(4096)
                ).get('message', '')
            except Exception:
                message = ''

            if message == 'Limit Exceeded':
                return 'daily_quota_exhausted'

            return 'rate_limited'

        return 'HTTP_' + str(ex.code)

    return type(ex).__name__


def timestamp(value, naive_utc=False):
    try:
        value = dt.datetime.fromisoformat(
            str(value).replace('Z', '+00:00')
        )
        if value.tzinfo is None:
            if not naive_utc:
                return None

            # BettingPros scheduled examples use UTC
            # timestamps without an explicit offset.
            value = value.replace(tzinfo=UTC)

        return value.astimezone(UTC)

    except (ValueError, TypeError):
        return None


def game_date(market):
    text = (
        str(market.get('rules_primary') or '')
        + ' '
        + str(market.get('rules_secondary') or '')
    )

    found = re.findall(
        r'originally scheduled for\s+'
        r'([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})',
        text,
        re.I
    )

    dates = set()

    for month, day, year in found:
        for fmt in ('%b %d %Y', '%B %d %Y'):
            try:
                dates.add(
                    dt.datetime.strptime(
                        month + ' ' + day + ' ' + year,
                        fmt
                    ).date()
                )
                break
            except ValueError:
                pass

    return next(iter(dates)) if len(dates) == 1 else None


def market_label(definition, sport):
    for key in (
        'stat', 'name', 'label', 'title',
        'display_name', 'slug'
    ):
        value = definition.get(key)

        if isinstance(value, str) and value.strip():
            value = norm(value)
            return CODES.get(sport, {}).get(
                value,
                ALIASES.get(value, value)
            )

    return ''


def next_page(current, pagination):
    nxt = pagination.get('next')
    if not nxt:
        return None

    if not isinstance(nxt, str):
        raise ValueError('Unsupported next-page format')

    target = urlsplit(urljoin(current, nxt))

    if (
        target.scheme != 'https'
        or target.netloc != 'api.bettingpros.com'
        or target.path.rstrip('/') != '/v3/props'
    ):
        raise ValueError('Unexpected next-page destination')

    # Copy only known filters. Never copy credentials.
    allowed = {
        'sport', 'limit', 'page', 'date', 'start', 'end',
        'market_id', 'event_id',
        'include_events', 'include_markets'
    }

    params = {
        k: v
        for k, v in parse_qsl(target.query)
        if k in allowed
    }

    params.update({
        'include_events': 'true',
        'include_markets': 'true'
    })

    return BP + '/props?' + urlencode(params)


def team_code(event, team_id):
    for p in rows(event, 'participants'):
        if str(p.get('id')) == str(team_id):
            team = p.get('team')
            if isinstance(team, dict):
                abbreviation = team.get('abbreviation')
                if abbreviation:
                    return str(abbreviation).upper()

    text = str(team_id or '').upper()

    if re.fullmatch(r'[A-Z]{2,4}', text):
        return text

    return None


def verify_event(event, market, sport, today, now):
    if (
        not event
        or str(event.get('sport', '')).upper() != sport
    ):
        return 'event_missing_or_wrong_sport'

    if event.get('status') not in (
        'scheduled', 'not_started', 'created'
    ):
        return 'event_not_pregame'

    start = timestamp(
        event.get('scheduled'),
        naive_utc=True
    )

    if (
        start is None
        or start.astimezone(EASTERN).date() != today
    ):
        return 'event_date_unverified_or_not_today'

    if start <= now:
        return 'event_already_started'

    if game_date(market) != today:
        return 'contract_rules_date_mismatch'

    home = team_code(event, event.get('home'))
    away = team_code(event, event.get('visitor'))

    if not home or not away or home == away:
        return 'event_team_codes_missing'

    suffix = str(
        market.get('event_ticker') or ''
    ).split('-', 1)[-1]

    parsed = re.fullmatch(
        r'(\d{2}[A-Z]{3}\d{2})(\d{4})?([A-Z]+)',
        suffix
    )

    if not parsed:
        return 'contract_event_format_unverified'

    if parsed[1] != today.strftime('%y%b%d').upper():
        return 'contract_ticker_date_mismatch'

    if parsed[3] not in (
        away + home,
        home + away
    ):
        return 'event_team_pair_mismatch'

    if parsed[2]:
        if parsed[2] != start.astimezone(EASTERN).strftime('%H%M'):
            return 'event_start_time_mismatch'
    elif sport == 'MLB':
        return 'baseball_game_number_unverified'

    return None


def threshold_matches(market, line):
    floor = number(market.get('floor_strike'))

    if floor is None:
        return False

    kind = market.get('strike_type')

    if kind == 'greater':
        return abs(floor - line) < .001

    if kind == 'greater_or_equal':
        return abs(floor - (math.floor(line) + 1)) < .001

    return False


def main():
    now = dt.datetime.now(UTC)
    today = now.astimezone(EASTERN).date()

    debug = {
        'series': {},
        'sports': {},
        'request_budget': BP_REQUEST_BUDGET
    }

    rejected = Counter()
    unknown = Counter()
    markets = []
    errors = []

    # Load selected player-prop series, retaining today's games.
    for series, (sport, stat) in SERIES.items():
        cursor = None
        seen = set()

        info = {
            'items': 0,
            'today': 0,
            'more_pages': False
        }
        debug['series'][series] = info

        try:
            for _ in range(10):
                params = {
                    'series_ticker': series,
                    'status': 'open',
                    'limit': 1000,
                    'mve_filter': 'exclude'
                }

                if cursor:
                    params['cursor'] = cursor

                data = fetch(
                    KALSHI + '/markets?' + urlencode(params)
                )

                for m in rows(data, 'markets'):
                    info['items'] += 1

                    if game_date(m) == today:
                        markets.append((sport, stat, m))
                        info['today'] += 1

                cursor = data.get('cursor')
                info['more_pages'] = bool(cursor)

                if not cursor:
                    break

                if cursor in seen:
                    raise ValueError('Repeated cursor')

                seen.add(cursor)

        except Exception as ex:
            info['error'] = error_label(ex)
            info['more_pages'] = True

    markets = list({
        m['ticker']: (sport, stat, m)
        for sport, stat, m in markets
        if m.get('ticker')
    }.values())

    active_sports = sorted({
        sport for sport, _, _ in markets
    })

    props = []
    definitions = {}
    events = {}
    pending = {}
    seen_urls = set()
    seen_pages = set()

    key = os.getenv('BETTINGPROS_API_KEY', '').strip()

    for sport in active_sports:
        pending[sport] = BP + '/props?' + urlencode({
            'sport': sport,
            'limit': 50,
            'start': today.isoformat(),
            'end': (
                today + dt.timedelta(days=1)
            ).isoformat(),
            'include_events': 'true',
            'include_markets': 'true'
        })

        debug['sports'][sport] = {
            'pages': 0,
            'props': 0,
            'more_pages': True
        }

    calls = 0
    stop_reason = None

    if not key:
        stop_reason = 'missing_api_key'

    # Round-robin pages share the request budget across sports.
    while (
        pending
        and not stop_reason
        and calls < BP_REQUEST_BUDGET
    ):
        for sport in list(pending):
            if calls >= BP_REQUEST_BUDGET:
                break

            url = pending[sport]
            info = debug['sports'][sport]

            try:
                if url in seen_urls:
                    raise ValueError('Repeated pagination URL')

                seen_urls.add(url)

                if calls:
                    time.sleep(BP_DELAY_SECONDS)

                calls += 1
                data = fetch(url, {'x-api-key': key})
                batch = rows(data, 'props')

                digest = hashlib.sha256(
                    json.dumps(batch, sort_keys=True).encode()
                ).hexdigest()

                if batch and (sport, digest) in seen_pages:
                    raise ValueError('Repeated pagination content')

                seen_pages.add((sport, digest))
                props.extend((sport, p) for p in batch)

                info['pages'] += 1
                info['props'] += len(batch)

                for definition in rows(data, 'markets'):
                    if definition.get('id') is not None:
                        definitions[
                            (sport, str(definition['id']))
                        ] = definition

                for event in rows(data, 'events'):
                    if event.get('id') is not None:
                        events[
                            (sport, str(event['id']))
                        ] = event

                info['response_fields'] = sorted(data.keys())

                pagination = data.get('_pagination') or {}
                nxt = next_page(url, pagination)
                info['more_pages'] = bool(nxt)

                if nxt:
                    pending[sport] = nxt
                else:
                    del pending[sport]

            except Exception as ex:
                label = error_label(ex)
                info['error'] = label
                errors.append(sport + ': ' + label)
                del pending[sport]

                if label in (
                    'daily_quota_exhausted',
                    'rate_limited',
                    'HTTP_401',
                    'HTTP_403'
                ):
                    stop_reason = label
                    break

    if pending and not stop_reason:
        stop_reason = 'per_run_request_budget_reached'

    debug.update({
        'requests_used': calls,
        'stop_reason': stop_reason,
        'event_records': len(events),
        'market_definitions': len(definitions)
    })

    scanner = {
        'status': 'insufficient_data',
        'scanned_props': len(props),
        'usable_projections': 0,
        'stats_identified': 0,
        'matches_checked': 0,
        'review': []
    }

    review = []
    event_examples = []
    supported = set(SERIES.values())

    for sport, p in props:
        projection = p.get('projection') or {}

        if not isinstance(projection, dict):
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
            rejected['projection_unusable'] += 1
            continue

        scanner['usable_projections'] += 1

        definition = definitions.get(
            (sport, str(p.get('market_id'))),
            {}
        )

        if (
            definition.get('period') != 'game'
            or definition.get('is_live_activity') is True
        ):
            rejected['period_not_verified_full_game'] += 1
            continue

        stat = market_label(definition, sport)

        if (sport, stat) not in supported:
            unknown[sport + ': ' + stat[:60]] += 1
            rejected['stat_not_supported'] += 1
            continue

        scanner['stats_identified'] += 1

        participant = p.get('participant') or {}
        player = (
            norm(participant.get('name'))
            if isinstance(participant, dict)
            else ''
        )

        # Use the line associated with the recommended side.
        selected = p.get(side)
        line = (
            number(selected.get('line'))
            if isinstance(selected, dict)
            else None
        )

        if (
            len(player.split()) < 2
            or line is None
            or abs(line - math.floor(line) - .5) > .001
        ):
            rejected['player_or_line_unusable'] += 1
            continue

        event = events.get(
            (sport, str(p.get('event_id'))),
            {}
        )

        pattern = re.compile(
            r'\b' + re.escape(player) + r'\b'
        )

        candidates = [
            m for market_sport, market_stat, m in markets
            if market_sport == sport
            and market_stat == stat
            and pattern.search(norm(m.get('title')))
        ]

        if not candidates:
            rejected['no_player_stat_overlap'] += 1

        for m in candidates:
            if not threshold_matches(m, line):
                rejected['threshold_mismatch'] += 1
                continue

            scanner['matches_checked'] += 1
            reason = verify_event(
                event, m, sport, today, now
            )

            if reason:
                rejected[reason] += 1

                if len(event_examples) < 5:
                    event_examples.append({
                        'ticker': m.get('ticker'),
                        'reason': reason,
                        'event_fields': sorted(event.keys())
                    })

                continue

            close = timestamp(m.get('close_time'))

            if close is None or close <= now:
                rejected['closed_or_missing_deadline'] += 1
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
                    rejected['ask_unavailable'] += 1
                    continue

                edge = p_side - price - BUFFER

                if edge <= MIN_EDGE:
                    rejected['edge_below_requirement'] += 1
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
                    'scheduled_game_date': today.isoformat(),
                    'match': {
                        'player': player,
                        'stat': stat,
                        'line': line,
                        'threshold': math.floor(line) + 1,
                        'period': 'game',
                        'event_check': 'date and team codes matched'
                    },
                    'warning': (
                        'Research only: verify settlement exceptions, '
                        'live liquidity, fees and projection freshness.'
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
        'unsupported_stats': dict(unknown.most_common(20)),
        'event_check_examples': event_examples
    })
    scanner['debug'] = debug

    incomplete = (
        bool(stop_reason)
        or bool(errors)
        or any(
            info['more_pages']
            for info in debug['sports'].values()
        )
    )

    summary = (
        'Research candidates found.'
        if unique
        else 'No candidate passed the verification checks.'
    )

    if incomplete:
        summary += (
            ' BettingPros coverage is incomplete: '
            + str(stop_reason or 'request error')
            + '.'
        )

    if stop_reason == 'daily_quota_exhausted':
        reset = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)

        summary += (
            ' Daily quota resets at '
            + reset.isoformat()
            + '.'
        )

    scanner['diagnostic'] = (
        summary
        + ' Rejections: '
        + json.dumps(dict(rejected), sort_keys=True)
    )

    fields = (
        'ticker', 'event_ticker', 'title',
        'yes_ask_dollars', 'no_ask_dollars',
        'volume_fp', 'close_time'
    )

    kalshi_incomplete = any(
        info['more_pages']
        for info in debug['series'].values()
    )

    out = {
        'version': 7,
        'updated_utc': now.isoformat(),
        'kalshi': {
            'status': 'partial' if kalshi_incomplete else 'ok',
            'market_count': len(markets),
            'more_pages': kalshi_incomplete,
            'scope': (
                'Selected player props explicitly scheduled '
                'today in contract rules'
            ),
            'markets': [
                {k: m.get(k) for k in fields}
                for _, _, m in markets
            ]
        },
        'bettingpros': {
            'status': (
                'not_configured' if not key
                else 'partial' if incomplete
                else 'connected'
            ),
            'successful_queries': sum(
                info['pages'] > 0
                for info in debug['sports'].values()
            ),
            'market_labels': len(definitions),
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
        'BettingPros:', out['bettingpros']['status'],
        'API calls:', calls
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
