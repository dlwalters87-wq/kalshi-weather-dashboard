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
BUFFER = 0.03
MIN_EDGE = 0.05

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
    'KXNHLANYGOAL': ('NHL', 'goals')
}

ALIASES = {
    'points': 'points', 'pts': 'points',
    'rebounds': 'rebounds', 'reb': 'rebounds',
    'assists': 'assists', 'ast': 'assists',
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
    'goals': 'goals'
}

CODES = {
    'MLB': {
        'h': 'hits', 'tb': 'total bases', 'sb': 'stolen bases',
        'k': 'strikeouts', 'ks': 'strikeouts',
        'hr': 'home runs', 'hrs': 'home runs'
    },
    'NHL': {'sv': 'saves', 'sog': 'shots on goal'}
}

COLLEGE_SERIES = {
    'KXNCAAFGAME': ('NCAAF', 'moneyline'),
    'KXNCAAFSPREAD': ('NCAAF', 'spread'),
    'KXNCAAFTOTAL': ('NCAAF', 'total')
}

GAME_SERIES = {
    prefix + suffix: (sport, kind)
    for prefix, sport in [
        ('KXNFL', 'NFL'), ('KXNBA', 'NBA'),
        ('KXMLB', 'MLB'), ('KXNHL', 'NHL')
    ]
    for suffix, kind in [
        ('GAME', 'moneyline'),
        ('SPREAD', 'spread'),
        ('TOTAL', 'total')
    ]
}

KALSHI_BLOCKED = False


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return re.sub('[^a-z0-9]+', ' ', value.lower()).strip()


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
    for value in (data.get(key), data.get('data'), data.get('results')):
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = value.get(key) or value.get('items')
            if isinstance(nested, list):
                return [x for x in nested if isinstance(x, dict)]
    return []


def fetch(url, headers=None):
    global KALSHI_BLOCKED
    if url.startswith(KALSHI):
        if KALSHI_BLOCKED:
            raise RuntimeError('Kalshi scan paused after rate limit')
        time.sleep(0.35)

    request = Request(url, headers={
        'User-Agent': 'KalshiResearchDashboard/11.0',
        **(headers or {})
    })

    for attempt in range(2):
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)
        except HTTPError as ex:
            if not url.startswith(KALSHI) or ex.code != 429:
                raise
            delay = number(ex.headers.get('Retry-After')) or 5
            if attempt or delay > 30:
                KALSHI_BLOCKED = True
                raise
            time.sleep(max(5, delay))


def error_label(ex):
    # Do not publish raw response bodies, credentials or headers.
    if isinstance(ex, HTTPError):
        if ex.code == 429:
            try:
                message = json.loads(ex.read(4096)).get('message', '')
            except Exception:
                message = ''
            if message == 'Limit Exceeded':
                return 'daily_quota_exhausted'
            return 'rate_limited'
        return 'HTTP_' + str(ex.code)
    return type(ex).__name__


def timestamp(value, naive_utc=False):
    try:
        value = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if value.tzinfo is None:
            if not naive_utc:
                return None
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def week_bounds(now):
    day = now.astimezone(EASTERN).date()
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def in_week(day, now):
    monday, sunday = week_bounds(now)
    return day is not None and monday <= day <= sunday


def game_date(market):
    text = (
        str(market.get('rules_primary') or '') + ' '
        + str(market.get('rules_secondary') or '')
    )
    found = re.findall(
        r'originally scheduled for\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})',
        text, re.I
    )
    dates = set()
    for month, day, year in found:
        for fmt in ('%b %d %Y', '%B %d %Y'):
            try:
                dates.add(dt.datetime.strptime(
                    month + ' ' + day + ' ' + year, fmt
                ).date())
                break
            except ValueError:
                pass
    return next(iter(dates)) if len(dates) == 1 else None


def market_label(definition, sport):
    for key in ('stat', 'name', 'label', 'title', 'display_name', 'slug'):
        value = definition.get(key)
        if isinstance(value, str) and value.strip():
            value = norm(value)
            return CODES.get(sport, {}).get(
                value, ALIASES.get(value, value)
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

    allowed = {
        'sport', 'season', 'week', 'limit', 'page', 'date',
        'start', 'end', 'market_id', 'event_id',
        'include_events', 'include_markets'
    }
    params = {k: v for k, v in parse_qsl(target.query) if k in allowed}
    params.update({'include_events': 'true', 'include_markets': 'true'})
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
    return text if re.fullmatch('[A-Z]{2,4}', text) else None


def verify_event(event, market, sport, today, now):
    today = game_date(market)
    if not today or not in_week(today, now):
        return 'outside_current_week'
    if not event or str(event.get('sport', '')).upper() != sport:
        return 'event_missing_or_wrong_sport'
    if event.get('status') not in ('scheduled', 'not_started', 'created'):
        return 'event_not_pregame'

    start = timestamp(event.get('scheduled'), naive_utc=True)
    if start is None or start.astimezone(EASTERN).date() != today:
        return 'event_date_unverified_or_mismatch'
    if start <= now:
        return 'event_already_started'
    if game_date(market) != today:
        return 'contract_rules_date_mismatch'

    home = team_code(event, event.get('home'))
    away = team_code(event, event.get('visitor'))
    if not home or not away or home == away:
        return 'event_team_codes_missing'

    suffix = str(market.get('event_ticker') or '').split('-', 1)[-1]
    parsed = re.fullmatch(
        r'(\d{2}[A-Z]{3}\d{2})(\d{4})?([A-Z]+)', suffix
    )
    if not parsed:
        return 'contract_event_format_unverified'
    if parsed[1] != today.strftime('%y%b%d').upper():
        return 'contract_ticker_date_mismatch'
    if parsed[3] not in (away + home, home + away):
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
        return abs(floor - line) < 0.001
    if kind == 'greater_or_equal':
        return abs(floor - (math.floor(line) + 1)) < 0.001
    return False


def team_names(event, ident):
    names = set()
    for p in rows(event, 'participants'):
        if str(p.get('id')) != str(ident):
            continue
        team = p.get('team') or {}
        for value in (
            p.get('name'), team.get('city'), team.get('slug'),
            str(team.get('city') or '') + ' ' + str(p.get('name') or '')
        ):
            if value:
                names.add(norm(value).replace(' st ', ' state '))
    return names


def college_team_name(value):
    return re.sub(r'\bst\b', 'state', norm(value))


def college_event_check(event, market, today, now):
    # Shared full-game identity check for NFL and college football.
    today = game_date(market)
    if not today or not in_week(today, now):
        return 'outside_current_week'
    if event.get('sport') not in ('NCAAF', 'NFL'):
        return 'wrong_sport'

    expected = (
        'NCAAF'
        if str(market.get('event_ticker', '')).startswith('KXNCAAF')
        else 'NFL'
    )
    if event.get('sport') != expected:
        return 'wrong_sport'

    start = timestamp(event.get('scheduled'), naive_utc=True)
    if (
        event.get('status') not in ('scheduled', 'not_started', 'created')
        or start is None or start <= now
    ):
        return 'not_verified_pregame'
    if start.astimezone(EASTERN).date() != today or game_date(market) != today:
        return 'date_mismatch'

    pair = re.search(
        r'(?:in the|wins the) (.+?) vs\.? (.+?) '
        r'(?:college football|pro football|NFL) game originally',
        str(market.get('rules_primary') or ''), re.I
    )
    if not pair:
        return 'rules_team_pair_unparsed'

    a, b = map(college_team_name, pair.groups())
    home = {
        college_team_name(n) for n in team_names(event, event.get('home'))
    }
    away = {
        college_team_name(n) for n in team_names(event, event.get('visitor'))
    }
    if not ((a in home and b in away) or (a in away and b in home)):
        return 'team_names_mismatch'
    if a == b or (a in home and a in away) or (b in home and b in away):
        return 'ambiguous_team_names'

    close = timestamp(market.get('close_time'))
    return None if close and close > now else 'closed_or_missing_deadline'


def college_selection_yes(kind, selection, event, market, line):
    rules = str(market.get('rules_primary') or '')
    if kind == 'total':
        return (
            norm(selection.get('selection')) == 'over'
            and threshold_matches(market, line)
        )

    ident = selection.get('participant')
    if ident is None or str(ident) not in (
        str(event.get('home')), str(event.get('visitor'))
    ):
        return False

    names = {college_team_name(n) for n in team_names(event, ident)}
    if kind == 'moneyline':
        match = re.match(r'If (.+?) wins the ', rules)
        return bool(match and college_team_name(match[1]) in names)

    match = re.match(
        r'If (.+?) wins by more than ([0-9.]+) points in the ', rules
    )
    return bool(
        match and college_team_name(match[1]) in names
        and number(match[2]) == -line
        and threshold_matches(market, -line)
    )


def scan_football(markets, key, now):
    sports = ('NFL', 'NCAAF')
    infos, states, queues = {}, {}, {}
    all_events, review = {}, []
    calls, stop = 0, None
    budget = 32

    for sport in sports:
        relevant = [
            (stat, m) for sp, stat, m in markets
            if sp == sport and stat in ('moneyline', 'spread', 'total')
        ]
        infos[sport] = {
            'status': 'pending', 'requests': 0, 'pages': 0,
            'events': 0, 'eligible_games': 0,
            'games_with_offers': 0, 'games_with_comparisons': 0,
            'offers': 0, 'matches': 0, 'candidates': 0,
            'more_pages': False, 'rejections': {}
        }
        states[sport] = {
            'markets': relevant, 'definitions': {}, 'events': {},
            'candidates': {}, 'covered': set(), 'compared': set(),
            'rejected': Counter(), 'seen': set()
        }
        queues[sport] = []

        if relevant and key:
            queues[sport].append({
                'endpoint': 'markets', 'page': 1,
                'params': {
                    'sport': sport, 'market_category': 'game-odds'
                }
            })
        else:
            infos[sport]['status'] = (
                'missing_api_key' if not key else 'no_weekly_contracts'
            )

    def process_offer(sport, offer):
        state, info = states[sport], infos[sport]
        event_id = str(offer.get('event_id'))
        kind = state['definitions'].get(str(offer.get('market_id')))
        event = state['events'].get(event_id)
        candidates = state['candidates'].get(event_id, [])

        if (
            not kind or not event or not candidates
            or offer.get('active') is not True
            or offer.get('player_id') not in (None, '')
            or offer.get('team_id') not in (None, '')
        ):
            return

        state['covered'].add(event_id)

        for selection in rows(offer, 'selections'):
            if selection.get('active') is not True:
                continue
            for book in rows(selection, 'books'):
                if str(book.get('id')) != '0':
                    continue
                for quote in rows(book, 'lines'):
                    updated = timestamp(
                        quote.get('updated'), naive_utc=True
                    )
                    if (
                        quote.get('active') is not True
                        or quote.get('is_off') is not False
                        or quote.get('replaced') is True
                        or not updated
                        or not 0 <= (now - updated).total_seconds() <= 3600
                    ):
                        state['rejected']['inactive_or_stale_quote'] += 1
                        continue

                    probability = number(
                        (quote.get('metrics') or {}).get('cover_probability')
                    )
                    line = number(quote.get('line'))

                    if probability is None or not 0 < probability < 1:
                        state['rejected']['model_probability_missing'] += 1
                        continue
                    if kind != 'moneyline' and (
                        line is None
                        or abs(line - math.floor(line) - 0.5) > 0.001
                    ):
                        state['rejected']['non_half_point_line'] += 1
                        continue

                    for market_kind, market in candidates:
                        if (
                            market_kind != kind
                            or not college_selection_yes(
                                kind, selection, event, market, line
                            )
                        ):
                            continue

                        info['matches'] += 1
                        state['compared'].add(event_id)
                        market['_comparison'] = {
                            'yes_probability': probability,
                            'source': 'BettingPros consensus cover probability'
                        }

                        for side, field, prob in [
                            ('YES', 'yes_ask_dollars', probability),
                            ('NO', 'no_ask_dollars', 1 - probability)
                        ]:
                            ask = number(market.get(field))
                            if ask is None or not 0 < ask < 1:
                                continue
                            edge = prob - ask - BUFFER
                            if edge <= MIN_EDGE:
                                state['rejected']['edge_below_requirement'] += 1
                                continue

                            review.append({
                                'ticker': market['ticker'],
                                'title': market.get('title'),
                                'side': side,
                                'ask': round(ask, 4),
                                'model_probability': round(prob, 4),
                                'edge_after_buffer': round(edge, 4),
                                'close_time': market.get('close_time'),
                                'volume': market.get('volume_fp'),
                                'sport': sport,
                                'scheduled_game_date': (
                                    game_date(market).isoformat()
                                ),
                                'scheduled_start': timestamp(
                                    event.get('scheduled'), naive_utc=True
                                ).isoformat(),
                                'match': {
                                    'sport': sport, 'stat': kind,
                                    'line': line, 'period': 'game',
                                    'event_check': (
                                        'exact team names and date matched'
                                    )
                                },
                                'warning': (
                                    'Research only: verify rules, current '
                                    'prices, fees and liquidity.'
                                )
                            })

    while any(queues.values()) and calls < budget and not stop:
        for sport in sports:
            if not queues[sport] or calls >= budget or stop:
                continue

            job = queues[sport][0]
            endpoint = job['endpoint']
            state, info = states[sport], infos[sport]

            try:
                if calls:
                    time.sleep(BP_DELAY_SECONDS)
                calls += 1
                info['requests'] += 1

                data = fetch(
                    BP + '/' + endpoint + '?' + urlencode({
                        **job['params'], 'limit': 50, 'page': job['page']
                    }),
                    {'x-api-key': key}
                )
                batch = rows(data, endpoint)
                digest = hashlib.sha256(
                    json.dumps(batch, sort_keys=True).encode()
                ).hexdigest()
                marker = (
                    endpoint, json.dumps(job['params'], sort_keys=True), digest
                )
                if batch and marker in state['seen']:
                    raise ValueError('Repeated page')
                state['seen'].add(marker)
                info['pages'] += 1

                if endpoint == 'markets':
                    for d in batch:
                        kind = {
                            'moneyline': 'moneyline',
                            'spread': 'spread',
                            'against-the-spread': 'spread',
                            'total': 'total',
                            'over-under': 'total'
                        }.get(d.get('slug'))
                        if (
                            kind and d.get('period') == 'game'
                            and d.get('category') == 'game-odds'
                            and d.get('id') is not None
                        ):
                            state['definitions'][str(d['id'])] = kind

                elif endpoint == 'events':
                    for event in batch:
                        if event.get('id') is not None:
                            ident = str(event['id'])
                            state['events'][ident] = event
                            all_events[sport, ident] = event

                else:
                    info['offers'] += len(batch)
                    for offer in batch:
                        process_offer(sport, offer)

                if (data.get('_pagination') or {}).get('next'):
                    job['page'] += 1
                    continue

                queues[sport].pop(0)

                if endpoint == 'markets':
                    if not state['definitions']:
                        raise ValueError('No full-game definitions')
                    queues[sport].append({
                        'endpoint': 'events', 'page': 1,
                        'params': {
                            'sport': sport,
                            'start': week_bounds(now)[0].isoformat(),
                            'end': (
                                week_bounds(now)[1] + dt.timedelta(days=1)
                            ).isoformat()
                        }
                    })

                elif endpoint == 'events':
                    ordered = sorted(
                        state['events'].items(),
                        key=lambda item: str(item[1].get('scheduled') or '')
                    )
                    for ident, event in ordered:
                        candidates = []
                        for kind, market in state['markets']:
                            reason = college_event_check(
                                event, market, None, now
                            )
                            if reason:
                                state['rejected'][reason] += 1
                            else:
                                candidates.append((kind, market))
                        if candidates:
                            state['candidates'][ident] = candidates

                    ids = list(state['candidates'])
                    for offset in range(0, len(ids), 20):
                        queues[sport].append({
                            'endpoint': 'offers', 'page': 1,
                            'params': {
                                'sport': sport,
                                'event_id': ':'.join(ids[offset:offset + 20]),
                                'market_id': ':'.join(state['definitions']),
                                'live': 'false'
                            }
                        })

                if not queues[sport]:
                    info['status'] = 'scanned'

            except Exception as ex:
                label = error_label(ex)
                info.update({
                    'status': 'partial', 'error': label, 'more_pages': True
                })
                queues[sport].clear()
                if label in (
                    'daily_quota_exhausted', 'rate_limited',
                    'HTTP_401', 'HTTP_403'
                ):
                    stop = label

    for sport in sports:
        state, info = states[sport], infos[sport]
        if queues[sport]:
            info.update({
                'status': 'partial', 'more_pages': True,
                'error': stop or 'football_request_budget_reached'
            })

        info['events'] = len(state['events'])
        info['eligible_games'] = len(state['candidates'])
        info['games_with_offers'] = len(state['covered'])
        info['games_with_comparisons'] = len(state['compared'])
        info['candidates'] = len({
            r['ticker'] for r in review if r['sport'] == sport
        })
        info['rejections'] = dict(state['rejected'])

    return review, infos, all_events, calls, stop


def board_title(market):
    rules = str(market.get('rules_primary') or '')
    found = re.search(
        r'(?:in the|wins the) (.+?) game originally scheduled',
        rules, re.I
    )
    if found:
        return re.sub(
            r'\s+(?:college football|pro football|professional baseball|'
            r'pro baseball|NBA|NHL|basketball|hockey)$',
            '', found[1], flags=re.I
        )
    return str(market.get('event_ticker') or 'Game')


def build_board(markets, events, now):
    groups = {}
    today = now.astimezone(EASTERN).date()

    for sport, stat, market in markets:
        day = game_date(market)
        if not in_week(day, now) or day < today:
            continue

        suffix = str(market.get('event_ticker') or '').split('-', 1)[-1]
        key = sport + ':' + suffix
        group = groups.setdefault(key, {
            'id': key, 'sport': sport, 'date': day.isoformat(),
            'title': board_title(market), 'start': None,
            'schedule_status': 'time_unverified',
            'markets': [], '_sample': market
        })

        close = timestamp(market.get('close_time'))
        if not close or close <= now:
            continue

        group['markets'].append({
            'ticker': market['ticker'],
            'title': market.get('title'),
            'stat': stat,
            'yes': number(market.get('yes_ask_dollars')),
            'no': number(market.get('no_ask_dollars')),
            'comparison': market.get('_comparison')
        })

    result, hidden = [], 0
    event_list = list(events.items())

    for group in groups.values():
        market = group.pop('_sample')
        sport = group['sport']
        day = game_date(market)
        matched = []

        for (event_sport, _), event in event_list:
            if event_sport != sport:
                continue
            start = timestamp(event.get('scheduled'), naive_utc=True)
            if not start or start.astimezone(EASTERN).date() != day:
                continue

            probe = dict(event, status='scheduled')
            before_start = start - dt.timedelta(seconds=1)
            check = (
                college_event_check(probe, market, day, before_start)
                if sport in ('NCAAF', 'NFL')
                else verify_event(probe, market, sport, day, before_start)
            )
            if check is None:
                matched.append((start, event))

        starts = {start for start, _ in matched}
        if len(starts) == 1:
            start = next(iter(starts))
            if start <= now or any(
                e.get('status') not in ('scheduled', 'created', 'not_started')
                for _, e in matched
            ):
                continue
            group['start'] = start.isoformat()
            group['schedule_status'] = 'verified'
        else:
            parsed = re.fullmatch(
                r'(\d{2}[A-Z]{3}\d{2})(\d{4})([A-Z]+)',
                group['id'].split(':', 1)[1]
            )
            if parsed:
                try:
                    start = dt.datetime.combine(
                        day,
                        dt.time(int(parsed[2][:2]), int(parsed[2][2:])),
                        EASTERN
                    ).astimezone(UTC)
                    if start <= now:
                        continue
                    group['start'] = start.isoformat()
                    group['schedule_status'] = 'contract_time'
                except ValueError:
                    pass

            if not group['start'] and day == today:
                hidden += 1
                continue

        if group['markets']:
            group['markets'].sort(key=lambda x: (
                x['stat'] not in ('moneyline', 'spread', 'total'),
                x['stat'], x['title'] or ''
            ))
            result.append(group)

    result.sort(key=lambda g: (
        g['date'], g['start'] or 'Z', g['sport'], g['title']
    ))
    return {
        'games': result,
        'hidden_today_unverified': hidden,
        'note': (
            'Prices are snapshot asks. Future-date games may have '
            'unverified start times. Game odds comparisons cover NFL '
            'and NCAAF; player comparisons cover NFL, NBA, MLB and NHL.'
        )
    }


def main():
    now = dt.datetime.now(UTC)
    today = now.astimezone(EASTERN).date()
    debug = {
        'series': {}, 'sports': {},
        'request_budget': BP_REQUEST_BUDGET
    }
    rejected, unknown = Counter(), Counter()
    markets, errors = [], []

    SERIES.update(COLLEGE_SERIES)
    SERIES.update(GAME_SERIES)

    for series, (sport, stat) in SERIES.items():
        cursor, seen = None, set()
        info = {'items': 0, 'this_week': 0, 'more_pages': False}
        debug['series'][series] = info

        try:
            for _ in range(10):
                params = {
                    'series_ticker': series, 'status': 'open',
                    'limit': 1000, 'mve_filter': 'exclude'
                }
                if cursor:
                    params['cursor'] = cursor

                data = fetch(KALSHI + '/markets?' + urlencode(params))
                for m in rows(data, 'markets'):
                    info['items'] += 1
                    if in_week(game_date(m), now):
                        markets.append((sport, stat, m))
                        info['this_week'] += 1

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
        for sport, stat, m in markets if m.get('ticker')
    }.values())

    active_sports = sorted({sport for sport, _, _ in markets} - {'NCAAF'})
    props, definitions, events = [], {}, {}
    pending, seen_urls, seen_pages = {}, set(), set()
    key = os.getenv('BETTINGPROS_API_KEY', '').strip()

    (
        football_review, football_info, football_events,
        football_calls, football_stop
    ) = scan_football(markets, key, now)

    events.update(football_events)
    debug['football'] = football_info
    debug['college_football'] = football_info['NCAAF']

    for sport, info in football_info.items():
        if info.get('error'):
            errors.append(sport + ': ' + info['error'])

    for sport in active_sports:
        pending[sport] = BP + '/props?' + urlencode({
            'sport': sport, 'limit': 50,
            'start': week_bounds(now)[0].isoformat(),
            'end': (
                week_bounds(now)[1] + dt.timedelta(days=1)
            ).isoformat(),
            'include_events': 'true', 'include_markets': 'true'
        })
        debug['sports'][sport] = {
            'pages': 0, 'props': 0, 'more_pages': True
        }

    calls = football_calls
    stop_reason = 'missing_api_key' if not key else football_stop

    while pending and not stop_reason and calls < BP_REQUEST_BUDGET:
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
                        definitions[sport, str(definition['id'])] = definition
                for event in rows(data, 'events'):
                    if event.get('id') is not None:
                        events[sport, str(event['id'])] = event

                info['response_fields'] = sorted(data.keys())
                nxt = next_page(url, data.get('_pagination') or {})
                info['more_pages'] = bool(nxt)
                if nxt:
                    pending[sport] = nxt
                else:
                    del pending[sport]

            except Exception as ex:
                label = error_label(ex)
                if (
                    label == 'HTTP_400'
                    and info['pages'] == 0
                    and not info.get('retried_default_window')
                ):
                    info['retried_default_window'] = True
                    pending[sport] = BP + '/props?' + urlencode({
                        'sport': sport, 'limit': 50,
                        'include_events': 'true',
                        'include_markets': 'true'
                    })
                    continue

                info['error'] = label
                errors.append(sport + ': ' + label)
                del pending[sport]

                if label in (
                    'daily_quota_exhausted', 'rate_limited',
                    'HTTP_401', 'HTTP_403'
                ):
                    stop_reason = label
                    break

    if pending and not stop_reason:
        stop_reason = 'per_run_request_budget_reached'

    debug.update({
        'requests_used': calls, 'stop_reason': stop_reason,
        'event_records': len(events),
        'market_definitions': len(definitions)
    })

    scanner = {
        'status': 'insufficient_data', 'scanned_props': len(props),
        'usable_projections': 0, 'stats_identified': 0,
        'matches_checked': sum(
            i['matches'] for i in football_info.values()
        ),
        'review': []
    }
    review, event_examples = list(football_review), []
    supported = set(SERIES.values())

    for sport, p in props:
        projection = p.get('projection') or {}
        if not isinstance(projection, dict):
            continue

        side = str(projection.get('recommended_side') or '').lower()
        probability = number(projection.get('probability'))
        if (
            side not in ('over', 'under')
            or probability is None or not 0 < probability < 1
        ):
            rejected['projection_unusable'] += 1
            continue

        scanner['usable_projections'] += 1
        definition = definitions.get((sport, str(p.get('market_id'))), {})

        if definition.get('period') != 'game':
            reason = (
                'period_missing' if not definition.get('period')
                else 'period_not_full_game'
            )
            rejected[reason] += 1
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
            if isinstance(participant, dict) else ''
        )
        selected = p.get(side)
        line = (
            number(selected.get('line'))
            if isinstance(selected, dict) else None
        )

        if (
            len(player.split()) < 2 or line is None
            or abs(line - math.floor(line) - 0.5) > 0.001
        ):
            rejected['player_or_line_unusable'] += 1
            continue

        event = events.get((sport, str(p.get('event_id'))), {})
        pattern = re.compile(r'\b' + re.escape(player) + r'\b')
        candidates = [
            m for market_sport, market_stat, m in markets
            if market_sport == sport and market_stat == stat
            and pattern.search(norm(m.get('title')))
        ]

        if not candidates:
            rejected['no_player_stat_overlap'] += 1

        for m in candidates:
            if not threshold_matches(m, line):
                rejected['threshold_mismatch'] += 1
                continue

            scanner['matches_checked'] += 1
            reason = verify_event(event, m, sport, today, now)
            if reason:
                rejected[reason] += 1
                if len(event_examples) < 5:
                    event_examples.append({
                        'ticker': m.get('ticker'), 'reason': reason,
                        'event_fields': sorted(event.keys())
                    })
                continue

            close = timestamp(m.get('close_time'))
            if close is None or close <= now:
                rejected['closed_or_missing_deadline'] += 1
                continue

            p_over = probability if side == 'over' else 1 - probability
            m['_comparison'] = {
                'yes_probability': p_over,
                'source': 'BettingPros player projection'
            }

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
                    'scheduled_game_date': game_date(m).isoformat(),
                    'scheduled_start': timestamp(
                        event.get('scheduled'), naive_utc=True
                    ).isoformat(),
                    'sport': sport,
                    'match': {
                        'player': player, 'stat': stat, 'line': line,
                        'threshold': math.floor(line) + 1,
                        'period': 'game',
                        'event_check': 'date and team codes matched'
                    },
                    'warning': (
                        'Research only: verify settlement exceptions, '
                        'live liquidity, fees and projection freshness.'
                    )
                })

    review.sort(key=lambda x: x['edge_after_buffer'], reverse=True)
    unique = {}
    for candidate in review:
        unique.setdefault(candidate['ticker'], candidate)

    scanner['review'] = list(unique.values())[:20]
    scanner['status'] = (
        'review_candidates' if unique else 'no_qualified_matches'
    )

    debug.update({
        'rejections': dict(rejected),
        'unsupported_stats': dict(unknown.most_common(20)),
        'event_check_examples': event_examples
    })
    debug['period_counts'] = dict(Counter(
        str(d.get('period', '(missing)')) for d in definitions.values()
    ))
    debug['live_activity_flags'] = dict(Counter(
        str(d.get('is_live_activity', '(missing)'))
        for d in definitions.values()
    ))
    scanner['debug'] = debug

    incomplete = (
        bool(stop_reason) or bool(errors)
        or any(i['more_pages'] for i in football_info.values())
        or any(info['more_pages'] for info in debug['sports'].values())
    )

    summary = (
        'Research candidates found.'
        if unique else 'No candidate passed the verification checks.'
    )
    summary += ' Football coverage: ' + '; '.join(
        sport + ': ' + str(i['eligible_games']) + ' eligible games, '
        + str(i['games_with_offers']) + ' with odds, '
        + str(i['games_with_comparisons']) + ' with model comparisons, '
        + str(i['candidates']) + ' candidates (' + i['status'] + ')'
        for sport, i in football_info.items()
    ) + '.'

    if incomplete:
        summary += (
            ' BettingPros coverage is incomplete: '
            + str(stop_reason or 'request error') + '.'
        )
    if stop_reason == 'daily_quota_exhausted':
        reset = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        summary += ' Daily quota resets at ' + reset.isoformat() + '.'

    scanner['diagnostic'] = (
        summary + ' Rejections: '
        + json.dumps(dict(rejected), sort_keys=True)
    )

    fields = (
        'ticker', 'event_ticker', 'title',
        'yes_ask_dollars', 'no_ask_dollars',
        'volume_fp', 'close_time'
    )
    kalshi_incomplete = any(
        info['more_pages'] for info in debug['series'].values()
    )

    out = {
        'version': 11,
        'updated_utc': now.isoformat(),
        'week': {
            'start': week_bounds(now)[0].isoformat(),
            'end': week_bounds(now)[1].isoformat(),
            'timezone': 'America/New_York'
        },
        'board': build_board(markets, events, now),
        'kalshi': {
            'status': 'partial' if kalshi_incomplete else 'ok',
            'market_count': len(markets),
            'more_pages': kalshi_incomplete,
            'scope': (
                'Current Monday-Sunday week: game markets and player props; '
                'NFL and NCAAF game-odds comparisons prioritized'
            ),
            'markets': [
                {k: m.get(k) for k in fields} for _, _, m in markets
            ]
        },
        'bettingpros': {
            'status': (
                'not_configured' if not key
                else 'partial' if incomplete else 'connected'
            ),
            'successful_queries': sum(
                info['pages'] > 0 for info in debug['sports'].values()
            ) + sum(i['pages'] > 0 for i in football_info.values()),
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
