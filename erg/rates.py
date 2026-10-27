"""Dated standard API-equivalent value, distinct from historical charges."""
import bisect
import hashlib
import json
import os
from pathlib import Path
import re


def compile_ledger(path):
    """Read only the documented scalar/inline-tier subset of the draft ledger.

    Unsupported entries remain unpriced. This is deliberately not a YAML loader.
    """
    raw=Path(path).read_text();entries=[];aliases={};active=False;entry=None
    for line in raw.splitlines():
        if line=='entries:':active=True;continue
        if active and line and not line[0].isspace() and not line.startswith('#'):break
        if not active:
            m=re.match(r'  ([\w.-]+): \[([^]]+)\]',line)
            if m:
                for alias in m[2].split(','):aliases[alias.strip()]=m[1]
            continue
        m=re.match(r'  - model: ([\w.-]+)',line)
        if m:entry=dict(model=m[1],tiers=[]);entries.append(entry);continue
        if entry is None:continue
        m=re.match(r'    (channel|as_of|source|input|output|cache_read|cache_write_5m|cache_write_1h|service_tier): (.+)',line)
        if m:
            key,value=m.groups();value=value.split(' #',1)[0].strip().strip("'\"")
            if key in ('input','output','cache_read','cache_write_5m','cache_write_1h'):
                try:value=float(value)
                except ValueError:continue
            entry[key]=value
        if 'kind: long_context' in line:
            tier={key:float(value) for key,value in re.findall(r'\b(min_prompt_tokens|input|output|cache_read|cache_write_5m|cache_write_1h|cache_write)\s*:\s*([0-9.]+)',line)}
            if 'min_prompt_tokens' in tier:entry['tiers'].append(tier)
    entries=[e for e in entries if e.get('channel') in ('openai-api','anthropic-api','openrouter') and not e.get('service_tier') and isinstance(e.get('input'),float) and isinstance(e.get('output'),float)]
    return dict(version=1,source_file='catalog/prices.yaml',source_sha256=hashlib.sha256(raw.encode()).hexdigest(),aliases=aliases,entries=entries)


def _load():
    override=os.environ.get('ERG_PRICE_LEDGER')
    if not override:
        from . import collect
        shared=Path(collect.STATE_DIR)/'rate-catalog'/'current.json'
        if shared.is_file():return json.loads(shared.read_text())
    # A checkout and a wheel use the same bundled snapshot. Authored ledgers
    # are selected explicitly, never by an adjacent directory in site-packages.
    path=Path(override) if override else None
    if path is not None:
        card=json.loads(path.read_text()) if path.suffix=='.json' else compile_ledger(path)
        return card
    return json.loads((Path(__file__).parent/'rate_card.json').read_text())


def load(db=None):
    card=_load()
    if db is not None:card['_db']=db
    return card


def parent_models(call,card):
    db=card.get('_db')
    if db is None:return []
    key=tuple(call.get(k) for k in ('machine','tool','session'))
    cache=card.setdefault('_parent_models',{})
    if key not in cache:
        rows=db.execute("SELECT DISTINCT r.model FROM sessions s JOIN call_winner_rows r ON r.machine=s.machine AND r.tool=s.tool AND r.session=s.parent_session WHERE s.machine=? AND s.tool=? AND s.session=? AND r.model IS NOT NULL AND r.model<>'codex-auto-review'",key)
        cache[key]=sorted({r[0] for r in rows})
    return cache[key]


def value(call,card):
    if call.get('model')=='codex-auto-review' and card.get('reviewer_valuation')=='observed_parent_model_range.v1':
        models=parent_models(call,card);values=[value(dict(call,model=m),card) for m in models]
        scope='own_parent_session'
        if not values or any(v is None for v in values):
            db=card.get('_db')
            if db is None:return None
            if '_parent_pool' not in card:
                card['_parent_pool']=[r[0] for r in db.execute("SELECT DISTINCT p.model FROM sessions s JOIN call_winner_rows p ON p.machine=s.machine AND p.tool=s.tool AND p.session=s.parent_session WHERE p.model IS NOT NULL AND p.model<>'codex-auto-review' AND EXISTS(SELECT 1 FROM call_winner_rows c WHERE c.machine=s.machine AND c.tool=s.tool AND c.session=s.session AND c.model='codex-auto-review')")]
            models=card['_parent_pool'];values=[value(dict(call,model=m),card) for m in models];values=[v for v in values if v is not None]
            if not values:return None
            scope='observed_parent_model_pool_parent_unavailable'
        low=min(v['low_usd'] for v in values);high=max(v['high_usd'] for v in values)
        return dict(low_usd=low,high_usd=high,usd=low if low==high else None,as_of=max(v['as_of'] for v in values),source=('Parent-session comparison: ' if scope=='own_parent_session' else 'Parent unavailable; observed parent-model pool: ')+', '.join(models),basis='observed_parent_model_range',comparison_models=models,comparison_scope=scope)

    model=card.get('aliases',{}).get(call.get('model'),call.get('model'))
    if '_prices' not in card:
        grouped={}
        for entry in card['entries']:grouped.setdefault(entry['model'],[]).append(entry)
        compiled={}
        for name,entries in grouped.items():
            entry=max(entries,key=lambda e:e.get('as_of',''))
            tiers=entry.get('tiers',[])
            thresholds=sorted({tier['min_prompt_tokens'] for tier in tiers})
            prices=[dict(entry)]
            for threshold in thresholds:
                price=dict(entry)
                # Preserve authored order, including overlapping partial tiers.
                for tier in tiers:
                    if tier['min_prompt_tokens']<=threshold:
                        price.update(tier)
                        if 'cache_write' in tier:price['cache_write_5m']=tier['cache_write'];price['cache_write_1h']=tier['cache_write']
                prices.append(price)
            compiled[name]=(thresholds,prices,entry)
        card['_prices']=compiled
    selected=card['_prices'].get(model)
    if selected is None and isinstance(model,str) and model.startswith('system.ai.'):
        suffix=model[len('system.ai.'):];normalize=lambda v:re.sub('[^a-z0-9]','',v.lower())
        candidates=[name for name in card['_prices'] if normalize(name)==normalize(suffix)]
        if len(candidates)==1:selected=card['_prices'][candidates[0]]
    if selected is None:return None
    thresholds,prices,e=selected
    input_tokens=sum(call.get(k) or 0 for k in ('fresh_in','cache_read','cache_write'))
    # A tier starts strictly above its threshold, including fractional counts.
    price=prices[bisect.bisect_left(thresholds,input_tokens)]
    fresh,read,write,out=[call.get(k) or 0 for k in ('fresh_in','cache_read','cache_write','out')]
    if any(x<0 for x in (fresh,read,write,out)) or read and 'cache_read' not in price or write and 'cache_write_5m' not in price:return None
    fixed=fresh*price['input']+read*price.get('cache_read',0)+out*price['output']
    low=price.get('cache_write_5m',0);high=price.get('cache_write_1h',low)
    five=call.get('cache_write_5m');hour=call.get('cache_write_1h')
    if five is not None and hour is not None and five+hour==write:
        amount=(fixed+five*low+hour*high)/1e6;lower=upper=amount
    else:lower=(fixed+write*min(low,high))/1e6;upper=(fixed+write*max(low,high))/1e6
    return dict(low_usd=lower,high_usd=upper,usd=lower if lower==upper else None,as_of=e['as_of'],source=e['source'],basis='estimated_standard_api_equivalent')


def empty():return dict(priced_calls=0,unpriced_calls=0,known_value_low_usd=0.0,known_value_high_usd=0.0,rate_card_dates=set(),rate_sources=set())
def add(summary,valuation,weight=1):
    if valuation:
        summary['priced_calls']+=weight;summary['known_value_low_usd']+=valuation['low_usd']*weight;summary['known_value_high_usd']+=valuation['high_usd']*weight;summary['rate_card_dates'].add(valuation['as_of']);summary['rate_sources'].add(valuation['source'])
    else:summary['unpriced_calls']+=weight

def finish(summary):
    summary=dict(summary);summary['rate_card_dates']=sorted(summary['rate_card_dates']);summary['rate_sources']=sorted(summary['rate_sources'])
    complete=summary['priced_calls']>0 and not summary['unpriced_calls']
    summary['api_equivalent_low_usd']=summary['known_value_low_usd'] if complete else None
    summary['api_equivalent_high_usd']=summary['known_value_high_usd'] if complete else None
    summary['api_equivalent_usd']=summary['api_equivalent_low_usd'] if complete and summary['api_equivalent_low_usd']==summary['api_equivalent_high_usd'] else None
    summary['coverage']='priced_observations' if complete else 'partial' if summary['priced_calls'] else 'unknown'
    summary['basis']='estimated_standard_api_equivalent';summary['actual_billed_usd']=None
    return summary
