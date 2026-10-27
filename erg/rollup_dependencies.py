"""Couple hours whose canonical request/input/commit winner can move.

These are mutation dependencies, not general session links. Snapshot serials let
publication retire old edges without losing a new cross-hour mutation.
"""

def initialize(db):
    fresh=not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_rollup_dependencies'").fetchone()
    db.execute('CREATE TABLE IF NOT EXISTS project_rollup_dependencies(a INT,b INT,serial INT,PRIMARY KEY(a,b))')
    db.execute('CREATE INDEX IF NOT EXISTS project_rollup_dependency_b ON project_rollup_dependencies(b,a)')
    db.execute('CREATE TABLE IF NOT EXISTS project_rollup_removed(key TEXT PRIMARY KEY,hour INT)')
    db.execute('CREATE INDEX IF NOT EXISTS project_rollup_removed_hour ON project_rollup_removed(hour)')
    if fresh:
        if db.execute("SELECT 1 FROM project_rollup_state WHERE key='signature' AND length(value)>0").fetchone():
            db.execute("INSERT OR REPLACE INTO project_rollup_state VALUES('dependency_bootstrap','1')")
        # Existing debt predates mutation tracking: publish it once as a cohort.
        hours=[r[0] for r in db.execute('SELECT hour FROM project_rollup_dirty ORDER BY hour')]
        db.executemany('INSERT INTO project_rollup_dependencies VALUES(?,?,1)',zip(hours,hours[1:]))
    def edge(stamp,select):
        h='('+stamp+')/3600'
        return ('INSERT INTO project_rollup_dependencies SELECT min('+h+',peer),max('+h+',peer),1 FROM ('+select+') WHERE '+stamp+' IS NOT NULL AND peer IS NOT NULL AND '+h+'<>peer '
                'ON CONFLICT(a,b) DO UPDATE SET serial=serial+1; ')
    def queue_removed():
        return 'INSERT INTO project_rollup_dirty SELECT hour,1 FROM project_rollup_removed WHERE key=NEW.key ON CONFLICT(hour) DO UPDATE SET serial=serial+1; '
    def peers(alias):
        return (edge(alias+'.ts',"SELECT ts/3600 peer FROM call_winner_rows WHERE "+alias+".response_id IS NOT NULL AND "+alias+".response_id<>'' AND machine="+alias+".machine AND response_id="+alias+".response_id AND model="+alias+".model")+
                edge(alias+'.ts',"SELECT ts/3600 peer FROM call_winner_rows WHERE "+alias+".request_id IS NOT NULL AND "+alias+".request_id<>'' AND request_id="+alias+".request_id AND model="+alias+".model"))
    db.execute('CREATE INDEX IF NOT EXISTS llm_rollup_dependency_uid ON llm(tool,uid,ts)')
    triggers={
      'winner_delete':('AFTER DELETE ON call_winner_rows',"INSERT OR REPLACE INTO project_rollup_removed SELECT OLD.key,OLD.ts/3600 WHERE OLD.ts IS NOT NULL; "+peers('OLD')),
      'winner_insert':('AFTER INSERT ON call_winner_rows',edge('NEW.ts','SELECT hour peer FROM project_rollup_removed WHERE key=NEW.key')+queue_removed()+'DELETE FROM project_rollup_removed WHERE key=NEW.key; '+peers('NEW')),
      'winner_update':('AFTER UPDATE ON call_winner_rows',edge('NEW.ts','SELECT OLD.ts/3600 peer')+peers('OLD')+peers('NEW'))}
    for table in ('llm','edits','prompts','commits'):
        for event in ('INSERT','UPDATE','DELETE'):
            aliases=('NEW',) if event=='INSERT' else ('OLD',) if event=='DELETE' else ('OLD','NEW')
            body=''
            if event=='INSERT':body+=edge('NEW.ts','SELECT ts/3600 peer FROM '+table+' WHERE machine=NEW.machine AND uid=NEW.uid')
            if event=='UPDATE':body+=edge('NEW.ts','SELECT OLD.ts/3600 peer')
            if table=='prompts' and event=='INSERT':
                body+=edge('NEW.ts',"SELECT p.ts/3600 peer FROM prompts p JOIN prompts prior ON prior.machine=NEW.machine AND prior.uid=NEW.uid WHERE p.tool=prior.tool AND COALESCE(NULLIF(p.source_uid,''),p.uid)=COALESCE(NULLIF(prior.source_uid,''),prior.uid)")
            for alias in aliases:
                if table=='llm':body+=edge(alias+'.ts','SELECT ts/3600 peer FROM llm WHERE tool='+alias+'.tool AND uid='+alias+'.uid')
                if table=='commits':body+=edge(alias+'.ts','SELECT ts/3600 peer FROM commits WHERE uid='+alias+'.uid')
                if table=='prompts':
                    body+=edge(alias+'.ts',"SELECT ts/3600 peer FROM prompts WHERE tool="+alias+".tool AND COALESCE(NULLIF(source_uid,''),uid)=COALESCE(NULLIF("+alias+".source_uid,''),"+alias+'.uid)')
            if not body:continue
            # No-op writes must not grow dependency serials.
            cols=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
            when=''
            if event=='INSERT':when=' WHEN NOT EXISTS(SELECT 1 FROM '+table+' p WHERE p.machine=NEW.machine AND p.uid=NEW.uid AND '+' AND '.join('p.'+c+' IS NEW.'+c for c in cols)+')'
            if event=='UPDATE':when=' WHEN '+' OR '.join('OLD.'+c+' IS NOT NEW.'+c for c in cols)
            triggers[table+'_'+event.lower()]=(('BEFORE INSERT' if event=='INSERT' else 'AFTER '+event)+' ON '+table+when,body)
    for name,(event,body) in triggers.items():
        name='erg_rollup_dependency_'+name
        from .project_rollups import instrument_dirty
        body=instrument_dirty(body,name)
        sql='CREATE TRIGGER '+name+' '+event+' BEGIN '+body+'END'
        old=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        if old and old[0]!=sql:db.execute('DROP TRIGGER '+name);old=None
        if not old:db.execute(sql)


def cohort(db,hours):
    found=set(hours);todo=list(hours)
    while todo:
        h=todo.pop()
        for a,b in db.execute('SELECT a,b FROM project_rollup_dependencies WHERE a=? OR b=?',(h,h)):
            for peer in (a,b):
                if peer not in found:found.add(peer);todo.append(peer)
    return found


def cleanup(db,limit=256):
    with db:
        db.execute('DELETE FROM project_rollup_removed WHERE key IN (SELECT r.key FROM project_rollup_removed r LEFT JOIN project_rollup_dirty d ON d.hour=r.hour WHERE d.hour IS NULL LIMIT ?)',(limit,))
