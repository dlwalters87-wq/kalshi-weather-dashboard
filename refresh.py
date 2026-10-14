import datetime as dt
import json, os, re, math
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KALSHI='https://external-api.kalshi.com/trade-api/v2'
BP='https://api.bettingpros.com/v3'

def get(url,headers=None):
    req=Request(url,headers={'User-Agent':'PersonalMarketResearch/2.0',**(headers or {})})
    with urlopen(req,timeout=35) as r:return json.load(r)

def norm(s):return re.sub(r'[^a-z0-9]+',' ',str(s or '').lower()).strip()

def probability(x):
    try:
        p=float(x)
        return p if 0<p<1 else None
    except (ValueError,TypeError):return None

def num(x):
    try:return float(x)
    except (TypeError,ValueError):return None

def match(prop,market,sport):
    # Conservative matching: same sport, full player name, over threshold and metric.
    # Never map a generic team/game market to a player prop.
    participant=norm((prop.get('participant') or {}).get('name'))
    if len(participant.split())<2:return None
    text=norm(' '.join(str(market.get(k) or '') for k in ('title','subtitle','ticker')))
    if participant not in text:return None
    ticker=str(market.get('ticker') or '').upper()
    if sport not in ticker and sport.lower() not in text:return None
    over=prop.get('over') or {}; line=num(over.get('line'))
    if line is None:return None
    # Kalshi binary threshold is usually an integer; half-point sportsbook over x.5
    # maps to >= ceil(x+epsilon), but only if that exact threshold is explicit.
    threshold=math.floor(line)+1
    if not re.search(r'(?<!\d)'+str(threshold)+r'(?!\d)',text):return None
    # Require unambiguous statistic type on both sides.
    labels={'points':('points','pts'),'rebounds':('rebounds','reb'),'assists':('assists','ast'),
            'strikeouts':('strikeouts','strikeout','ks'),'yards':('yards','yds'),
            'receptions':('receptions','catches'),'threes':('threes','3 pointers','3pt')}
    # Market ID alone does not identify stat, so use any supplied market labels.
    bptext=norm(' '.join(str(prop.get(k) or '') for k in ('market_name','market','stat','stat_type','market_label')))
    common=[name for name,aliases in labels.items() if any(a in bptext for a in aliases) and any(a in text for a in aliases)]
    if len(common)!=1:return None
    # Do not guess whether the Kalshi YES side means over or under.
    if not re.search(r'\b(at least|or more|more than|over|\+)\b',text):return None
    return {'stat':common[0],'line':line,'threshold':threshold,'player':participant}

def main():
    Path('data').mkdir(exist_ok=True)
    out={'updated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
         'kalshi':{'status':'unavailable','markets':[]},
         'bettingpros':{'status':'not_configured'},
         'scanner':{'status':'no_verified_matches','ranked':[],'review':[],'scanned_props':0,'matches_checked':0}}
    markets=[]
    try:
        cursor=None
        for _ in range(5):
            args={'status':'open','limit':1000,'mve_filter':'exclude'}
            if cursor:args['cursor']=cursor
            data=get(KALSHI+'/markets?'+urlencode(args))
            markets+=data.get('markets',[]);cursor=data.get('cursor')
            if not cursor:break
        fields=('ticker','title','subtitle','event_ticker','yes_bid_dollars','yes_ask_dollars','no_bid_dollars','no_ask_dollars','volume_fp','close_time')
        out['kalshi']={'status':'ok','market_count':len(markets),'more_pages':bool(cursor),
                       'markets':[{k:m.get(k) for k in fields} for m in markets]}
    except Exception as e:out['kalshi']['error']=str(e)[:120]
    key=os.getenv('BETTINGPROS_API_KEY','').strip()
    if key:
        props=[];errors=[];sports=['NBA','NFL','MLB','NHL']
        for sport in sports:
            try:
                data=get(BP+'/props?'+urlencode({'sport':sport,'limit':20}),{'x-api-key':key})
                if not isinstance(data,dict):continue
                props += [(sport,p) for p in data.get('props',[]) if isinstance(p,dict)]
            except Exception as e:errors.append(sport+': '+str(e)[:75])
        out['bettingpros']={'status':'connected' if len(errors)<len(sports) else 'connection_error',
                            'sports_queried':sports,'successful_queries':len(sports)-len(errors),
                            'errors':errors}
        out['scanner']['scanned_props']=len(props)
        now=dt.datetime.now(dt.timezone.utc)
        for sport,p in props:
            proj=p.get('projection') or {}
            side=str(proj.get('recommended_side') or '').lower()
            prob=probability(proj.get('probability'))
            if side not in ('over','under') or prob is None:continue
            # Probability is for recommended_side, not always for OVER.
            yesprob=prob if side=='over' else 1-prob
            for m in markets:
                info=match(p,m,sport)
                if not info:continue
                out['scanner']['matches_checked']+=1
                try:close=dt.datetime.fromisoformat(str(m['close_time']).replace('Z','+00:00'))
                except Exception:continue
                if close<=now or close>now+dt.timedelta(hours=30):continue
                yes=num(m.get('yes_ask_dollars'));no=num(m.get('no_ask_dollars'))
                if yes is None or no is None or not (0<yes<1 and 0<no<1):continue
                candidates=[('YES',yes,yesprob),('NO',no,1-yesprob)]
                side2,ask,p2=max(candidates,key=lambda x:x[2]-x[1])
                # 3c fee/slippage buffer plus 5c minimum model edge; deliberately conservative.
                edge=p2-ask-0.03
                if edge<=0.05:continue
                out['scanner']['review'].append({'ticker':m.get('ticker'),'title':m.get('title'),
                    'side':side2,'ask':round(ask,3),'model_probability':round(p2,3),
                    'edge_after_buffer':round(edge,3),'close_time':m.get('close_time'),
                    'volume':m.get('volume_fp'),'match':info,
                    'warning':'CANDIDATE ONLY: verify Kalshi YES definition and settlement rules; API market-label match is not settlement verification.'})
        out['scanner']['review'].sort(key=lambda x:x['edge_after_buffer'],reverse=True)
        out['scanner']['review']=out['scanner']['review'][:20]
        out['scanner']['status']='review_candidates' if out['scanner']['review'] else 'no_verified_matches'
    Path('data/latest.json').write_text(json.dumps(out,separators=(',',':')),encoding='utf-8')
    print('Kalshi',out['kalshi']['status'],len(markets),'BettingPros',out['bettingpros']['status'])
    print('Props',out['scanner']['scanned_props'],'review candidates',len(out['scanner']['review']))

if __name__=='__main__':main()
