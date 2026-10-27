"""Content-free input/context capture and evidence-based PR effectiveness."""
import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit
from . import collect
from . import accounting,rates,routes

EVENTS={
 'work_context':'uid ts tool session project repo branch head_sha task_id worktree provenance scope_id capture_version',
 'work_link':'uid ts tool session pr_url task_id start_ts end_ts provenance active',
 'pull_request':'uid ts repo number url state created_ts closed_ts merged_ts branch head_sha added_code removed_code files_complete remote_updated_ts title',
 'pr_commit':'uid ts pr_url sha',
 'work_commit':'uid ts repo sha added_code removed_code files provenance',
 'work_evidence':'uid ts tool session repo scope_id task_id sha pr_url provenance capture_version',
}
EVENTS.update(accounting.EVENTS)

TABLES={'work_context':'work_contexts','work_link':'work_links','pull_request':'pull_requests','pr_commit':'pr_commits','work_evidence':'work_evidence','work_commit':'work_commit_evidence'}
TABLES.update(accounting.TABLES)
NUMERIC=accounting.NUMERIC | set('files capture_version ts number created_ts closed_ts merged_ts added_code removed_code files_complete start_ts end_ts active remote_updated_ts'.split())
_CACHE={}


def repository(value):
    if not isinstance(value,str) or not value:return ''
    value=re.sub(r'^git@([^:]+):',r'https://\1/',value)
    if '://' not in value:value='https://'+value
    p=urlsplit(value);path=p.path.strip('/');path=path[:-4] if path.endswith('.git') else path;parts=path.split('/')
    if len(parts)!=2 or any(not re.fullmatch(r'[\w.-]+',x) or x in ('.','..') for x in parts):return ''
    return (p.hostname or '').lower()+'/'+('/'.join(parts)).lower() if p.hostname else ''


def pr_identity(url):
    if not isinstance(url,str):return None
    p=urlsplit(url);m=re.fullmatch(r'/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?',p.path)
    if p.scheme!='https' or not m or p.username or p.password or p.query or p.fragment:return None
    repo=repository('https://'+p.netloc+'/'+m[1]+'/'+m[2])
    return (repo,int(m[3])) if repo else None


def git_context(cwd, live=False):
    if not cwd or not os.path.isdir(cwd):return {}
    key=(cwd,live)
    if key in _CACHE and time.monotonic()-_CACHE[key][0]<30:return _CACHE[key][1]
    def git(*args):
        try:return subprocess.run(['git','-C',cwd,*args],capture_output=True,text=True,timeout=3).stdout.strip()
        except (OSError,subprocess.TimeoutExpired):return ''
    result={'repo':repository(git('remote','get-url','origin'))}
    if live:result.update(branch=git('branch','--show-current'),head_sha=git('rev-parse','HEAD'))
    _CACHE[key]=(time.monotonic(),result);return result


def user_text(text):
    if not isinstance(text,str):return ''
    for marker in ('## My request for Codex:','## My request:'):
        if marker in text:return text.split(marker,1)[1].strip()
    if text.startswith(('<system-reminder','<task-notification','<environment_context','<permissions','<command-','<local-command','<turn_aborted', '# AGENTS.md instructions','[Request interrupted','Caveat:','Host notice','Retained')):return ''
    return text


def task_id(text):
    m=re.search(r'(?mi)^\s*(?:Task-ID|Bead-ID|Feature-ID|Work-Item)\s*:\s*([A-Za-z0-9][A-Za-z0-9_.:/-]{0,127})\s*$',text)
    if not m:m=re.search(r'(?mi)^\s*(?:execute(?: the)? bead|ddx try)\s+(ddx-[a-z0-9]+)\b',text)
    return m[1] if m else ''


def context_event(tool,session,ts,cwd,**fields):
    d=dict(t='work_context',ts=ts,tool=tool,session=session,project=collect.project_of(cwd),
           repo='',branch='',head_sha='',task_id='',provenance='native_git',
           worktree=hashlib.sha256((cwd or '').encode()).hexdigest()[:16],capture_version=3)
    d.update(fields)
    d['uid']='ctx:'+hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest()
    return d


def scope_context(state,ts):
    key=json.dumps([state['session'],state.get('cwd'),state.get('repo'),state.get('branch'),state.get('task_id')])
    if state.get('_scope_key')!=key:
        state['_scope_key']=key
        state['_scope_id']=hashlib.sha256((key+str(ts)).encode()).hexdigest()
    return state['_scope_id']


def initialize(db):
    for kind,fields in EVENTS.items():
        db.execute('CREATE TABLE IF NOT EXISTS '+TABLES[kind]+'(machine TEXT,'+','.join(c+(' INT' if c in NUMERIC else ' TEXT') for c in fields.split())+',PRIMARY KEY(machine,uid))')
        have={r[1] for r in db.execute('PRAGMA table_info('+TABLES[kind]+')')}
        for c in fields.split():
            if c not in have:db.execute('ALTER TABLE '+TABLES[kind]+' ADD COLUMN '+c+(' INT' if c in NUMERIC else ' TEXT'))
    db.execute('CREATE TABLE IF NOT EXISTS native_offsets(path TEXT PRIMARY KEY,inode TEXT,offset INT,line INT,context TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS work_lookups(repo TEXT,sha TEXT,checked_ts INT,PRIMARY KEY(repo,sha))')
    db.execute('CREATE TABLE IF NOT EXISTS work_lookup_gaps(repo TEXT,sha TEXT,checked_ts INT,status INT,reason TEXT,PRIMARY KEY(repo,sha))')
    db.execute('CREATE INDEX IF NOT EXISTS work_context_session ON work_contexts(tool,session,ts)')
    # Historical witnesses are scoped to machine and checkout. Keep this narrow:
    # no transcript bodies or duplicated context payloads in the index.
    db.execute('CREATE INDEX IF NOT EXISTS work_context_witness ON work_contexts(machine,tool,session,worktree,ts DESC) WHERE capture_version>=3')


def record_events(tool,d,state,path,line,fallback_ts=0,include_metadata=True,include_prompts=True,repo_context=None):
    """Parse one native record; no database writes and never execute logged text."""
    rows=[];recorded_repository=repo_context;repo_context=repo_context or git_context
    payload=d.get('payload') if tool=='codex' else d
    if include_metadata and isinstance(payload,dict):rows.extend(routes.native(tool,d,payload,collect.parse_ts(d.get('timestamp')),state))
    cost=accounting.reported_cost(d,fallback_ts) if include_metadata and tool=='claude' else None
    if cost:rows.append(cost)
    ts=collect.parse_ts(d.get('timestamp'))
    if not ts:return rows
    kind=d.get('type');p=d.get('payload') if tool=='codex' else d
    if not isinstance(p,dict):return rows
    if tool=='codex' and kind=='session_meta':
        g=p.get('git') or {};state['kind']='subagent' if isinstance(p.get('source'),dict) or p.get('parent_thread_id') or p.get('thread_source','user')!='user' else 'main'
        state['started']=ts
        state.update(session=p.get('id') or p.get('session_id'),_native_cwd=p.get('cwd'),cwd=p.get('cwd'),repo=repository(g.get('repository_url','')),branch=g.get('branch',''),head_sha=g.get('commit_hash',''))
        state['_recorded_native_repo']=repository(g.get('repository_url',''));state['_recorded_native_cwd']=p.get('cwd')
    elif tool=='claude' and kind in ('user','assistant'):
        side=d.get('isSidechain') or '/subagents/' in str(path)
        state.update(session=(d.get('agentId') or Path(path).stem) if side else d.get('sessionId') or Path(path).stem,
                     cwd=d.get('cwd') or state.get('cwd'),branch=d.get('gitBranch') or state.get('branch',''),head_sha='')
        state['repo']=repo_context(state.get('cwd')).get('repo','')
    elif tool=='codex' and kind=='turn_context':
        if p.get('cwd') and state.get('_native_cwd',state.get('cwd'))!=p['cwd']:
            state.update(cwd=p['cwd'],repo=repo_context(p['cwd']).get('repo',''),branch='',head_sha='')
        state['_native_cwd']=p.get('cwd') or state.get('_native_cwd') or state.get('cwd');state['model']=p.get('model','')
    if not state.get('session'):return rows
    if include_metadata:rows.extend(accounting.observe(tool,d,p,state,ts,str(path)))
    if not state.get('repo'):state['repo']=repo_context(state.get('cwd')).get('repo','')
    scope_context(state,ts)
    ctx={k:state.get(k,'') for k in ('repo','branch','head_sha','task_id')}
    ctx['scope_id']=state['_scope_id']
    if (tool=='codex' and kind in ('session_meta','turn_context')) or (tool=='claude' and kind in ('user','assistant')):
        signature=json.dumps([state['session'],state.get('cwd'),ctx],sort_keys=True)
        if state.get('_signature')!=signature:
            rows.append(context_event(tool,state['session'],ts,state.get('cwd'),**ctx));state['_signature']=signature
    from . import work_tools
    def historical_scope(cwd):
        proof=recorded_repository(cwd)
        if proof.get('repo') or proof.get('ambiguous'):return proof
        # A retained Codex header is itself a repository witness, even when no
        # independent native context rows were previously materialized.
        if (tool=='codex' and cwd==state.get('_recorded_native_cwd') and state.get('_recorded_native_repo')
            and state['_recorded_native_repo']==state.get('repo')):
            return dict(repo=state['_recorded_native_repo'])
        return proof
    rows.extend(work_tools.observe(tool,d,p,state,ts,repo_context=historical_scope if recorded_repository is not None else None))
    texts=[];uid='';origin='unknown';provenance='native_user_message';source_uid=''
    if tool=='claude' and kind=='user' and not d.get('isMeta') and not d.get('isSidechain') and '/subagents/' not in str(path):
        msg=d.get('message') or {};content=msg.get('content')
        if isinstance(content,str):texts=[content]
        elif isinstance(content,list) and content and all(isinstance(x,dict) and x.get('type')=='text' for x in content):texts=['\n'.join(x.get('text','') for x in content)]
        uid='c:'+str(d.get('uuid') or state['session']+':line:'+str(line-1))
    elif tool=='codex' and kind=='response_item' and p.get('type')=='message' and p.get('role')=='user':
        metadata=p.get('internal_chat_message_metadata_passthrough') or {}
        kinds=metadata.get('content_item_kinds',[]) if isinstance(metadata,dict) else []
        if not any(isinstance(k,str) and k.startswith('user.') for k in kinds):return rows
        created=metadata.get('create_time')
        if isinstance(created,(int,float)) and created<state.get('started',0)-5:return rows
        texts=[c.get('text','') for c in p.get('content',[]) if isinstance(c,dict)]
        origin='automation' if state.get('kind')=='subagent' else 'human'
        provenance='codex_user_text';source_uid=p.get('id') or ''
    elif tool=='codex' and kind=='event_msg' and p.get('type')=='user_message':
        # CLI event provenance identifies an input, but does not establish a human actor.
        texts=[p.get('message','')];provenance='codex_user_event';source_uid=p.get('message_id') or ''
    for text in texts:
        text=user_text(text)
        if not text:continue
        ident=uid or 'x:%s:%s:%s'%(state['session'],d.get('ordinal',line-1),hashlib.sha256(text.encode()).hexdigest()[:12])
        if include_prompts:rows.append(dict(t='prompt',uid=ident,ts=ts,tool=tool,session=state['session'],project=collect.project_of(state.get('cwd')),
                         model=state.get('model',''),chars=len(text),words=len(text.split()),est_tokens=(len(text)+3)//4,origin=origin,provenance=provenance,source_uid=source_uid))
        task=task_id(text)
        if task:
            if state.get('task_id')!=task:
                state['_scope_key']=''  # The next record starts a new task scope.
                state['_scope_id']=hashlib.sha256((state['_scope_id']+task+str(ts)).encode()).hexdigest()
            state['task_id']=task;ctx['task_id']=task;ctx['scope_id']=state['_scope_id']
            state['_scope_key']=json.dumps([state['session'],state.get('cwd'),state.get('repo'),state.get('branch'),task])
            rows.append(context_event(tool,state['session'],ts,state.get('cwd'),**ctx))
    return rows


def capture_file(db,tool,path,full=False):
    """Parse outside the writer lock; atomically ingest only if progress is unchanged."""
    from . import store
    stat=os.stat(path);inode='%s:%s'%(stat.st_dev,stat.st_ino)
    old=db.execute('SELECT inode,offset,line,context FROM native_offsets WHERE path=?',(str(path),)).fetchone()
    if old and old[0]==inode and stat.st_size==old[1]:return 0
    archive=str(path).endswith('.zst')
    if not archive and old and old[0]==inode and stat.st_size>=old[1]:offset,line,state=old[1],old[2],json.loads(old[3])
    else:offset,line,state=0,0,{}
    rows=[];consumed=0
    with collect.transcript(str(path)) as stream:
        if not archive:stream.seek(offset)
        while full or archive or consumed<4000:
            start=0 if archive else stream.tell();raw=stream.readline()
            if not raw or not raw.endswith(b'\n'):
                if not archive:stream.seek(start)
                break
            line+=1;consumed+=1
            try:d=json.loads(raw)
            except ValueError:continue
            rows.extend(record_events(tool,d,state,path,line,stat.st_mtime))
        offset=stat.st_size if archive else stream.tell()

    current_stat=os.stat(path)
    if (current_stat.st_dev,current_stat.st_ino)!=(stat.st_dev,stat.st_ino):return 0
    if not archive and current_stat.st_size<offset:return 0
    if archive and (current_stat.st_size,current_stat.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):return 0
    with db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT inode,offset,line,context FROM native_offsets WHERE path=?',(str(path),)).fetchone()
        if current!=old:return 0  # Another writer/reset won; retry from durable progress.
        store.ingest(db,map(json.dumps,rows),machine=collect.machine_name(),transaction=False)
        db.execute('INSERT OR REPLACE INTO native_offsets VALUES(?,?,?,?,?)',(str(path),inode,offset,line,json.dumps(state)))
    return len(rows)


def scan(db,full=False,since=None,paths=None,metadata=True):
    cutoff=0 if full else max(since or 0,time.time()-60)
    count=errors=0
    if metadata:accounting.capture_metadata(db)
    if metadata and (full or time.time()-getattr(capture_file,'_creator_scan',0)>300):
        accounting.capture_codex_creators(db);capture_file._creator_scan=time.time()
    if paths is None:
        paths=[]
        for tool,roots in [('claude',collect.claude_roots()),('codex',collect.native_roots('codex'))]:
            def mtime(path):
                try:return os.path.getmtime(path)
                except OSError:return 0
            paths.extend((tool,p) for p in sorted(collect.log_paths(roots,cutoff),key=mtime,reverse=True))
    for tool,path in paths:
        if tool not in ('claude','codex'):continue
        if path.endswith('.zst') and not full:continue
        try:count+=capture_file(db,tool,path,full)
        except (OSError,ValueError,sqlite3.Error):errors+=1
        if not full and count>16000:break
    return {'events':count,'errors':errors}


class MetadataFetchError(RuntimeError):
    def __init__(self,status=None,reason='request_failed'):
        self.status=status;self.reason=reason
        super().__init__('PR metadata '+reason+(' (HTTP '+str(status)+')' if status else ''))


def graphql_error_status(body):
    errors=body.get('errors') if isinstance(body,dict) else None
    kinds={e.get('type') for e in errors if isinstance(e,dict) and isinstance(e.get('type'),str)} if isinstance(errors,list) else set()
    return next((code for kind,code in (('RATE_LIMITED',429),('UNAUTHORIZED',401),('FORBIDDEN',403)) if kind in kinds),None)


def gh_json(repo,endpoint,paginate=False,fields=None):
    host=repo.split('/')[0];cmd=['gh','api','--hostname',host,endpoint]
    if paginate:cmd+=['--paginate','--slurp']
    if fields:
        for name,value in fields.items():cmd+=['-f',name+'='+value]
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=120)
    if result.returncode:
        # Preserve actionable categories, never CLI stderr, response bodies or secrets.
        try:body=json.loads(result.stdout)
        except ValueError:body={}
        if isinstance(body,list) and len(body)==1:body=body[0]  # gh --paginate --slurp error envelope
        if not isinstance(body,dict):body={}
        code=str(body.get('status',''))
        if not code.isdigit():
            match=re.search(r'\bHTTP ([1-5][0-9]{2})\b',result.stderr or '')
            code=match[1] if match else ''
        status=int(code) if code else graphql_error_status(body) if endpoint=='graphql' else None
        reason='commit_not_found' if status==422 and str(body.get('message','')).startswith('No commit found for SHA:') else {
            401:'authentication_failed',403:'access_denied_or_rate_limited',404:'not_found',429:'rate_limited'}.get(status,'remote_error' if status and status>=500 else 'request_failed')
        raise MetadataFetchError(status,reason)
    return json.loads(result.stdout)


def pull_heads(repo,numbers):
    """Read bounded PR update markers together; detailed evidence still uses REST.

    GraphQL aliases avoid one CLI startup per old known PR outside the recent
    list page. Partial/missing/foreign results are never accepted as evidence.
    """
    if not numbers:return []
    if len(numbers)>100 or any(isinstance(n,bool) or not isinstance(n,int) or not 1<=n<=2147483647 for n in numbers) or len(set(numbers))!=len(numbers):raise ValueError('invalid PR metadata batch')
    owner,name=repo.split('/')[1:]
    def rest():return [gh_json(repo,'repos/%s/%s/pulls/%s'%(owner,name,n)) for n in numbers]
    if len(numbers)==1:return rest()
    selections=' '.join('p%d:pullRequest(number:%d){number url title updatedAt}'%(i,n) for i,n in enumerate(numbers))
    query='query($owner:String!,$name:String!){repository(owner:$owner,name:$name){'+selections+'}}'
    try:
        result=gh_json(repo,'graphql',fields=dict(query=query,owner=owner,name=name))
        if not isinstance(result,dict) or result.get('errors'):raise MetadataFetchError(graphql_error_status(result),'graphql_unavailable')
        payload=result.get('data')
        data=payload.get('repository') if isinstance(payload,dict) else None
        if not isinstance(data,dict):raise MetadataFetchError(None,'graphql_unavailable')
        rows=[]
        for i,number in enumerate(numbers):
            node=data.get('p%d'%i)
            if not isinstance(node,dict) or (not isinstance(node.get('number'),int) or isinstance(node.get('number'),bool) or node['number']!=number) or pr_identity(node.get('url'))!=(repo,number) or not isinstance(node.get('title'),str) or not collect.parse_ts(node.get('updatedAt')):raise MetadataFetchError(None,'graphql_unavailable')
            rows.append(dict(number=number,title=node['title'],updated_at=node['updatedAt']))
        return rows
    except MetadataFetchError as error:
        # Preserve REST-only hosts/tokens without retrying auth/rate/server errors.
        if error.status in (401,403,429) or error.status and error.status>=500:raise
        return rest()


def evidence_pulls(db,repo,limit):
    """Bounded read-only discovery, restricted to the configured repository."""
    owner,name=repo.split('/')[1:];numbers=set();errors=[]
    facts=db.execute('SELECT sha,pr_url,MAX(ts) FROM work_evidence WHERE repo=? GROUP BY sha,pr_url ORDER BY MAX(ts) DESC',(repo,)).fetchall()
    remaining=limit
    for sha,url,_ in facts:
        identity=pr_identity(url)
        if identity and identity[0]==repo:numbers.add(identity[1])
        if not sha or remaining<=0:continue
        checked=db.execute('SELECT checked_ts FROM work_lookups WHERE repo=? AND sha=?',(repo,sha)).fetchone()
        if checked and time.time()-checked[0]<3600:continue
        gap=db.execute('SELECT checked_ts FROM work_lookup_gaps WHERE repo=? AND sha=?',(repo,sha)).fetchone()
        if gap and time.time()-gap[0]<3600:continue
        remaining-=1
        try:
            pages=gh_json(repo,'repos/%s/%s/commits/%s/pulls'%(owner,name,sha),True)
            for page in pages:
                for pr in page:
                    identity=pr_identity(pr.get('html_url'))
                    if identity and identity[0]==repo:numbers.add(identity[1])
            with db:
                db.execute('INSERT OR REPLACE INTO work_lookups VALUES(?,?,?)',(repo,sha,int(time.time())))
                db.execute('DELETE FROM work_lookup_gaps WHERE repo=? AND sha=?',(repo,sha))
        except MetadataFetchError as error:
            # Repository visibility was checked by refresh's PR listing. An absent
            # commit is no evidence of PR membership, and is retried after an hour.
            if error.status==404 or error.reason=='commit_not_found':
                with db:db.execute('INSERT OR REPLACE INTO work_lookup_gaps VALUES(?,?,?,?,?)',(repo,sha,int(time.time()),error.status,error.reason))
            else:errors.append(repo+': '+str(error))
        except (RuntimeError,subprocess.SubprocessError,ValueError,KeyError) as error:errors.append(repo+': '+str(error))
    # Previously discovered PRs need updates too; keep force-push/closure evidence current.
    for (url,) in db.execute('SELECT url FROM pull_requests WHERE repo=?',(repo,)):
        identity=pr_identity(url)
        if identity:numbers.add(identity[1])
    return sorted(numbers,reverse=True)[:max(limit,100)],errors


def refresh(db,repos,limit=50):
    from . import store
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:raise ValueError('PR limit must be between 1 and 100')
    rows=[];errors=[];unavailable=[]
    for repo in repos:
        repo=repository(repo)
        if not repo:raise ValueError('invalid repository identity')
        owner,name=repo.split('/')[1:]
        try:
            listed=gh_json(repo,'repos/%s/%s/pulls?state=all&sort=updated&direction=desc&per_page=100'%(owner,name))
            numbers,lookup_errors=evidence_pulls(db,repo,limit);errors.extend(lookup_errors)
            # The larger metadata page avoids one CLI process per known PR,
            # while retaining exactly the requested recent/evidence-linked scope.
            wanted=set(numbers)
            pulls=[pr for i,pr in enumerate(listed) if i<limit or pr['number'] in wanted]
            unavailable.extend(dict(repo=repo,sha=r[0],checked_ts=r[1],status=r[2],reason=r[3]) for r in db.execute('SELECT sha,checked_ts,status,reason FROM work_lookup_gaps WHERE repo=?',(repo,)))
            seen={pr['number'] for pr in pulls}
            pulls.extend(pull_heads(repo,[number for number in numbers if number not in seen]))
            for pr in pulls:
                number=pr['number'];url='https://'+repo+'/pull/'+str(number);ts=int(time.time())
                updated=collect.parse_ts(pr.get('updated_at'))
                cursor=db.execute('SELECT * FROM pull_requests WHERE uid=? ORDER BY ts DESC LIMIT 1',(url,));values=cursor.fetchone()
                current=dict(zip([c[0] for c in cursor.description],values)) if values else None
                if updated and current and current['remote_updated_ts']==updated:
                    if pr.get('title') and current.get('title')!=pr['title']:
                        current.pop('machine',None);current.update(t='pull_request',title=pr['title']);rows.append(current)
                    continue
                commits=gh_json(repo,'repos/%s/%s/pulls/%s/commits'%(owner,name,number),True)
                for page in commits:
                    for commit in page:rows.append(dict(t='pr_commit',uid=url+':'+commit['sha'],ts=ts,pr_url=url,sha=commit['sha']))
                # Files reflect the final PR diff, never the sum of intermediate edits.
                pages=gh_json(repo,'repos/%s/%s/pulls/%s/files'%(owner,name,number),True);files=[x for page in pages for x in page]
                detail=gh_json(repo,'repos/%s/%s/pulls/%s'%(owner,name,number))
                complete=len(files)==detail.get('changed_files')
                code=[f for f in files if not collect.SKIP_FILE.search(f['filename']) and not collect.is_doc(f['filename'])]
                rows.append(dict(t='pull_request',uid=url,ts=ts,repo=repo,number=number,url=url,title=detail.get('title') or pr.get('title'),state='merged' if detail.get('merged_at') else detail['state'],
                    created_ts=collect.parse_ts(detail.get('created_at')),closed_ts=collect.parse_ts(detail.get('closed_at')),
                    merged_ts=collect.parse_ts(detail.get('merged_at')),branch=detail['head']['ref'],head_sha=detail['head']['sha'],
                    added_code=sum(f['additions'] for f in code) if complete else None,removed_code=sum(f['deletions'] for f in code) if complete else None,files_complete=int(complete),remote_updated_ts=updated))
        except (RuntimeError,subprocess.SubprocessError,ValueError,KeyError) as error:errors.append(repo+': '+str(error))
    store.ingest(db,map(json.dumps,rows),machine=collect.machine_name())
    return dict(prs=sum(r['t']=='pull_request' for r in rows),errors=errors,unavailable=unavailable,scope='Recent and evidence-linked PRs in configured repositories; commit lookups bounded per refresh; missing metadata stays unknown')



def refresh_configuration():
    path=Path(collect.HOME)/'.config'/'erg'/'prs.json'
    try:value=json.loads(path.read_text())
    except FileNotFoundError:return None
    if not isinstance(value,dict) or not isinstance(value.get('repos'),list) or not all(repository(r) for r in value['repos']):raise ValueError('invalid PR refresh configuration')
    limit=value.get('limit',10)
    if not isinstance(limit,int) or not 1<=limit<=100:raise ValueError('invalid PR limit')
    return value

def bind(db,tool,session,url,task='',start=0,end=0):
    from . import store
    identity=pr_identity(url)
    if not identity:raise ValueError('expected an HTTPS pull request URL')
    url='https://'+identity[0]+'/pull/'+str(identity[1])
    if start<0 or end<0 or end and end<start:raise ValueError('invalid attribution interval')
    row=dict(t='work_link',uid=hashlib.sha256(json.dumps([tool,session,url,task,start,end]).encode()).hexdigest(),ts=int(time.time()),tool=tool,session=session,pr_url=url,task_id=task,start_ts=start,end_ts=end,provenance='operator',active=1)
    store.ingest(db,[json.dumps(row)],machine=collect.machine_name())



def unlink(db,tool,session,url):
    from . import store
    identity=pr_identity(url)
    if not identity:raise ValueError('invalid PR URL')
    url='https://'+identity[0]+'/pull/'+str(identity[1])
    cursor=db.execute('SELECT * FROM work_links WHERE tool=? AND session=? AND pr_url=?',(tool,session,url))
    names=[r[0] for r in cursor.description];rows=[dict(zip(names,r),t='work_link') for r in cursor]
    for row in rows:row['active']=0;row['ts']=int(time.time())
    for row in rows:store.ingest(db,[json.dumps(row)],machine=row.pop('machine'))
    return len(rows)

def prepare_attribution(db,tick=None):
    """Prepare recorded-evidence interval resolution once per report batch."""
    conflict_sessions=set(db.execute('SELECT machine,tool,session FROM history_work_conflicts')) if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_work_conflicts'").fetchone() else set()
    def objects(table):
        if conflict_sessions and table in ('work_contexts','work_evidence'):
            cur=db.execute('SELECT r.* FROM '+table+' r WHERE NOT EXISTS(SELECT 1 FROM history_work_conflicts c WHERE c.machine=r.machine AND c.tool=r.tool AND c.session=r.session)')
        else:cur=db.execute('SELECT * FROM '+table)
        names=[d[0] for d in cur.description]
        try:
            for values in cur:
                if tick:tick()
                yield dict(zip(names,values))
        finally:cur.close()
    prs={}
    for pr in objects('pull_requests'):
        if pr['url'] not in prs or pr['ts']>prs[pr['url']]['ts']:prs[pr['url']]=pr
    commits={}
    for r in objects('pr_commits'):
        if r['pr_url'] in prs and r['ts']==prs[r['pr_url']]['ts']:commits.setdefault(r['pr_url'],set()).add(r['sha'])
    contexts={};links={}
    for r in objects('work_contexts'):contexts.setdefault((r['machine'],r['tool'],r['session']),[]).append(r)
    for key in contexts:
        versions={}
        for r in contexts[key]:versions[r['ts']]=max(versions.get(r['ts'],0),r.get('capture_version') or 0)
        contexts[key]=[r for r in contexts[key] if (r.get('capture_version') or 0)==versions[r['ts']]]
        contexts[key].sort(key=lambda r:(r['ts'],r['uid']))
    for r in objects('work_links'):
        if r.get('active')==0:continue
        links.setdefault((r['tool'],r['session']),[]).append(r)
    parents={(r['tool'],r['session']):r['parent_session'] for r in objects('sessions') if r.get('parent_session')}
    by_branch={}
    for url,p in prs.items():by_branch.setdefault((p['repo'],p['branch']),[]).append(url)
    times={k:[r['ts'] for r in values] for k,values in contexts.items()}
    groups={}
    for key,values in contexts.items():
        for r in values:groups.setdefault((key,r['ts']),[]).append(r)
    strong={};commit_prs={};facts_by_url={}
    for url,shas in commits.items():
        for sha in shas:commit_prs.setdefault((prs.get(url,{}).get('repo'),sha),set()).add(url)
    facts=[f for f in objects('work_evidence') if (f.get('capture_version') or 0)>=5]
    anchors={}
    for fact in facts:
        if (fact.get('capture_version') or 0)<5:continue
        if fact['sha']:
            anchors.setdefault((fact['machine'],fact['tool'],fact['session'],fact['scope_id']),{}).setdefault(fact['ts'],set()).add((fact['repo'],fact['sha']))
        urls=commit_prs.get((fact['repo'],fact['sha']),set()) if fact['sha'] else {fact['pr_url']}
        key=(fact['machine'],fact['tool'],fact['session'],fact['scope_id'])
        for url in urls:
            p=prs.get(url)
            if not p or p['repo']!=fact['repo']:continue
            end=p['merged_ts'] or p['closed_ts']
            if end and fact['ts']>end:continue
            strong.setdefault(key,{}).setdefault(url,set()).add(fact['provenance'])
            facts_by_url.setdefault(url,[]).append({k:fact[k] for k in ('machine','tool','session','ts','scope_id','sha','pr_url','provenance')})
    # Resolve each recorded context group once, rather than rebuilding its
    # conflict set and task selection for every call and input in that interval.
    context_groups={}
    for (key,stamp),same in groups.items():
        ctx=max(same,key=lambda r:(bool(r.get('scope_id')),bool(r.get('task_id'))))
        if len({(r['repo'],r['branch'],r['head_sha']) for r in same})>1:
            context_groups[(key,stamp)]=(None,'ambiguous');continue
        if not ctx['task_id']:
            ctx=dict(ctx,task_id=next((r['task_id'] for r in same if r['task_id']),''))
        context_groups[(key,stamp)]=(ctx,None)
    def context_at(key,ts):
        i=bisect.bisect_right(times.get(key,[]),ts)-1
        if i<0:return None,'missing_context'
        return context_groups[(key,contexts[key][i]['ts'])]
    def resolve(machine,tool,session,ts):
        key=(machine,tool,session)
        explicit=[r for r in links.get((tool,session),[]) if (not r['start_ts'] or ts>=r['start_ts']) and (not r['end_ts'] or ts<=r['end_ts'])]
        urls={r['pr_url'] for r in explicit}
        if len(urls)==1:return next(iter(urls)),'operator',{r['task_id'] for r in explicit if r['task_id']}
        if len(urls)>1:return None,'ambiguous',set()
        parent=parents.get((tool,session));seen={session}
        while parent and parent not in seen:
            seen.add(parent)
            inherited=[r for r in links.get((tool,parent),[]) if (not r['start_ts'] or ts>=r['start_ts']) and (not r['end_ts'] or ts<=r['end_ts'])]
            choices={r['pr_url'] for r in inherited}
            if len(choices)==1:return next(iter(choices)),'parent_operator',{r['task_id'] for r in inherited if r['task_id']}
            if len(choices)>1:return None,'ambiguous',set()
            parent=parents.get((tool,parent))
        if key in conflict_sessions:return None,'retained_work_source_conflict',set()
        ctx,error=context_at(key,ts)
        if error:return None,error,set()
        evidence=strong.get((machine,tool,session,ctx.get('scope_id')), {})
        # Collect all candidates before applying time bounds so a multi-PR work
        # segment cannot become spuriously unique after one PR is closed.
        if len(evidence)>1:return None,'ambiguous',set()
        if evidence:
            url=next(iter(evidence));p=prs[url];end=p['merged_ts'] or p['closed_ts']
            if not end or ts<=end:
                return url,'+'.join(sorted(evidence[url])),{ctx['task_id']} if ctx['task_id'] else set()
        candidates=[]
        for url in by_branch.get((ctx['repo'],ctx['branch']),[]):
            p=prs[url];end=p['merged_ts'] or p['closed_ts']
            if ctx['head_sha'] in commits.get(url,set()) and p['created_ts'] and ts>=p['created_ts'] and (not end or ts<=end):candidates.append(url)
        if len(candidates)==1:return candidates[0],'verified_branch_commit',{ctx['task_id']} if ctx['task_id'] else set()
        return None,'ambiguous' if candidates else 'unverified_context',set()
    anchor_times={k:sorted(v) for k,v in anchors.items()}
    def resolve_commit(machine,tool,session,ts):
        if (machine,tool,session) in conflict_sessions:return None,'retained_work_source_conflict'
        ctx,error=context_at((machine,tool,session),ts)
        if error:return None,error
        key=(machine,tool,session,ctx.get('scope_id'));times_for_scope=anchor_times.get(key,[])
        index=bisect.bisect_left(times_for_scope,ts)
        if index>=len(times_for_scope):return None,'no_later_commit_evidence'
        choices=anchors[key][times_for_scope[index]]
        return (next(iter(choices)),'tool_commit_interval') if len(choices)==1 else (None,'ambiguous_commit_batch')
    from . import work_intervals
    resolve_at=work_intervals.resolver(resolve,resolve_commit,contexts,links,parents,by_branch,prs,strong,anchor_times)
    return resolve_at,prs,commits,contexts,facts,commit_prs,facts_by_url,objects


def effectiveness(db,view='full',selection=None):
    if view not in ('full','work'):raise ValueError('invalid effectiveness calculation view')
    full=view=='full'
    from . import project_scope,report_scope,projects
    if selection is not None and not isinstance(selection,report_scope.Selection):raise ValueError('Validated report selection required')
    if selection is not None and full:raise ValueError('Scoped metrics require the work view')
    hierarchy=project_scope.Scope(db,selection.values['end'] if selection else 2**63-1)
    if selection and selection.values['project_key'] and selection.values['project_key']!='@unassigned' and selection.values['project_key'] not in hierarchy.catalog:raise ValueError('Unknown human project')
    resolve_at,prs,commits,contexts,facts,commit_prs,facts_by_url,objects=prepare_attribution(db)
    card=dict(rates.load(),_db=db)
    resolve_account,account_root,identities=accounting.resolver(db) if full else (None,None,[])
    session_results={};commit_results={};commit_unknown={};global_value=rates.empty();resolve_route=routes.route_resolver(db) if full else None
    from . import session_usage
    def session_row(tool,session):
        key=(tool,session)
        if key not in session_results:session_results[key]=session_usage.new_row(tool,session)
        return session_results[key]
    if full:
        for native in objects('report_sessions'):
            session_usage.add_native(session_row(native['tool'],native['session']),native)
    for commit in objects('work_commit_evidence'):
        key=(commit['repo'],commit['sha'])
        commit_results[key]=dict(commit,calls=0,total_tokens=0,out=0,sessions=set(),valuation=rates.empty(),linked_sessions=set(),prs=sorted(commit_prs.get(key,set())))
    for f in facts:
        if f['sha'] and (f['repo'],f['sha']) in commit_results:commit_results[(f['repo'],f['sha'])]['linked_sessions'].add((f['tool'],f['session']))
    from . import account_reports
    periods=account_reports.periods(db,account_root) if full else {}
    from . import titles
    session_titles={(r['machine'],r['tool'],r['session']):r['title'] for r in titles.rows(db)}
    result={};unassigned={};unassigned_sessions={};total=0;reported_call_count=0;reported_call_value=0.0;observed_tools=set()
    def row(url):
        if url not in result:
            p=prs.get(url,{})
            result[url]=dict(url=url,repo=p.get('repo') or (pr_identity(url) or ('',0))[0],number=p.get('number') or (pr_identity(url) or ('',0))[1],
                title=p.get('title'),state=p.get('state','unknown'),metadata_asof=p.get('ts'),calls=0,pre_pr_calls=0,fresh_in=0,cache_read=0,cache_write=0,out=0,reasoning=0,
                sessions=set(),tasks=set(),evidence=set(),inputs={},added_code=p.get('added_code') if p.get('state')=='merged' else None,
                removed_code=p.get('removed_code') if p.get('state')=='merged' else None,valuation=rates.empty(),cost_usd=None,cost_provenance='unknown')
        return result[url]
    for url in prs:row(url)
    cols='machine tool session ts fresh_in cache_read cache_write out reasoning model cache_write_5m cache_write_1h reported_cost_usd uid project kind requester'.split()
    from . import report_sqlite
    call_sql=report_sqlite.calls_select(cols,db=db) if isinstance(db,sqlite3.Connection) else 'SELECT '+','.join(cols)+' FROM calls'
    for values in db.execute(call_sql):
        call=dict(zip(cols,values));machine,tool,session,ts,fresh,read,write,out,reasoning=values[:9]
        observed_tools.add(tool)
        if selection:
            units,allocation=selection.allocation(hierarchy,call)
            if not units:continue
        else:units=projects.UNITS;allocation=hierarchy.resolve(call)
        weight=units/projects.UNITS
        attribution,commit_match=resolve_at(machine,tool,session,ts)
        ck,why=commit_match
        if full:
            sr=session_row(tool,session)
            valuation,account=session_usage.add_call(sr,call,card,resolve_account,resolve_route)
            rates.add(global_value,valuation)
            account_reports.allocate(periods,ts,valuation,account)
        else:
            # Price canonical requests before project weighting. Work needs the
            # selected total too, including usage without a commit/PR match.
            valuation=rates.value(call,card);rates.add(global_value,valuation,weight)
        tokens=sum(call[name] or 0 for name in ('fresh_in','cache_read','cache_write','out'))*weight
        if ck in commit_results:
            cr=commit_results[ck];cr['calls']+=weight;cr['total_tokens']+=tokens;cr['out']+=(out or 0)*weight;cr['sessions'].add((tool,session));rates.add(cr['valuation'],valuation,weight)
        else:
            if ck is not None:why='missing_commit_metadata'
            commit_unknown[why]=commit_unknown.get(why,0)+weight
        reported=call.get('reported_cost_usd')
        if isinstance(reported,(int,float)) and reported>=0:
            import math
            if math.isfinite(reported):reported_call_count+=weight;reported_call_value+=reported*weight
        total+=weight;url,evidence,tasks=attribution
        if not url:
            unassigned[evidence]=unassigned.get(evidence,0)+weight
            key=(machine,tool,session)
            if key not in unassigned_sessions:unassigned_sessions[key]=dict(machine=machine,tool=tool,session=session,calls=0,reasons=set())
            unassigned_sessions[key]['calls']+=weight;unassigned_sessions[key]['reasons'].add(evidence)
            continue
        r=row(url);r['calls']+=weight;rates.add(r['valuation'],valuation,weight)
        if prs.get(url,{}).get('created_ts') and ts<prs[url]['created_ts']:r['pre_pr_calls']+=weight
        r['sessions'].add((machine,tool,session));r['tasks'].update(tasks);r['evidence'].add(evidence)
        for name,value in zip(['fresh_in','cache_read','cache_write','out','reasoning'],[fresh,read,write,out,reasoning]):r[name]+=(value or 0)*weight
    for p in objects('input_messages'):
        if selection:
            units,_=selection.allocation(hierarchy,p)
            if not units:continue
        else:units=projects.UNITS
        p=dict(p,allocation_weight=units/projects.UNITS)
        url,evidence,tasks=resolve_at(p['machine'],p['tool'],p['session'],p['ts'])[0]
        if url:
            r=row(url);r['tasks'].update(tasks);r['evidence'].add(evidence)
            key=(p['tool'],p.get('source_uid') or p['uid']);old=r['inputs'].get(key)
            if old is None or old.get('origin') not in ('human','automation'):r['inputs'][key]=p
    for r in result.values():
        inputs=list(r.pop('inputs').values());sessions=r.pop('sessions');r['sessions']=len(sessions);r['session_refs']=[dict(machine=m,tool=t,session=s,title=session_titles.get((m,t,s))) for m,t,s in sorted(sessions)]
        r['evidence_refs']=[f for f in facts_by_url.get(r['url'],[]) if (f['machine'],f['tool'],f['session']) in sessions][:100]
        r['tasks']=sorted(r['tasks']);r['evidence']=sorted(r['evidence']);r['total_tokens']=r['fresh_in']+r['cache_read']+r['cache_write']+r['out']
        r['observed_inputs']=sum(p['allocation_weight'] for p in inputs) if inputs else None
        r['observed_input_events']=len(inputs)
        r['known_human_inputs']=sum(p['allocation_weight'] for p in inputs if p.get('origin')=='human')
        r['unknown_origin_inputs']=sum(p['allocation_weight'] for p in inputs if p.get('origin') not in ('human','automation'))
        r['human_inputs']=r['known_human_inputs'] if inputs and not r['unknown_origin_inputs'] else None
        r['input_sessions_observed']=len({(p['machine'],p['tool'],p['session']) for p in inputs})
        r['input_coverage']='partial' if r['input_sessions_observed']<r['sessions'] or r['unknown_origin_inputs'] else 'observed_only' if inputs else 'unknown'
        r['accepted_code_per_million_tokens']=r['added_code']*1000000/r['total_tokens'] if r['added_code'] is not None and r['total_tokens'] else None
        r['valuation']=rates.finish(r['valuation'])
        r['tokens_per_dollar']=None;r['usage_status']='partial' if r['calls'] else 'unknown';r['attribution']='allocated_observations_only' if r['calls'] else 'unknown'
    missing=sorted(unassigned_sessions.values(),key=lambda r:-r['calls'])
    for item in missing:item['reasons']=sorted(item['reasons'])
    task_contexts=[dict(task_id=c['task_id'],tool=c['tool'],session=c['session'],repo=c['repo'],branch=c['branch']) for values in contexts.values() for c in values if c.get('task_id')]
    attribution_unknown_reasons=session_usage.finish(db,session_results,resolve_account,account_root,identities) if full else {}
    for cr in commit_results.values():
        cr['sessions']=sorted(cr['sessions']);cr['linked_sessions']=sorted(cr['linked_sessions']);cr['valuation']=rates.finish(cr['valuation']);cr['actual_billed_usd']=None
    account_report=account_reports.finish(db,account_root,identities,global_value,periods) if full else dict(definition_version='erg.work-effectiveness.v3',cost_provenance='unknown',generated=int(time.time()),valuation=rates.finish(global_value))
    return dict(project_catalog=hierarchy.config['projects'],folder_catalog=list(hierarchy.folders.values()),project_revision=hierarchy.config['revision'],observed_tools=sorted(observed_tools),report_scope=selection.values if selection else None,reported_call_count=reported_call_count,reported_call_value=reported_call_value,input_count_basis='Allocated input equivalents; physical input events retained separately',attribution_unknown_reasons=attribution_unknown_reasons,commits=sorted(commit_results.values(),key=lambda r:(-r['calls'],-r['ts'])),commit_unattributed_reasons=commit_unknown,commit_attributed_calls=sum(r['calls'] for r in commit_results.values()),sessions=sorted(session_results.values(),key=lambda r:-(r['last_ts'] or 0)),unattributed_sessions=missing[:100],task_contexts=task_contexts[:100],prs=sorted(result.values(),key=lambda r:(-r['calls'],r['url'])),
        pre_pr_calls=sum(r['pre_pr_calls'] for r in result.values()),total_calls=total,attributed_calls=total-sum(unassigned.values()),unattributed_calls=sum(unassigned.values()),unattributed_reasons=unassigned,
        automatic_evidence_count=len(facts),
        unclassified_codex_role_records=db.execute("SELECT COUNT(*) FROM prompts WHERE tool='codex' AND COALESCE(provenance,'') NOT IN ('codex_user_text','codex_user_event')").fetchone()[0],definitions='Accepted = merged. Added code is final non-generated, non-document PR diff; it is not AI authorship or retained-code measurement.',
        limitations=['Successful commit/PR creation evidence can link a task/repository/branch segment before PR creation. Conflicting segments, absent evidence and unmatched subagents remain unknown.',
                     'Observed inputs are lower bounds. Only explicit user-input events count; replayed history and injected context do not. Human origin is an interface proxy, not proof of a physical actor.',
                     'Reference token value and native reported estimates are separate from billed spend. Whole-PR diff lines are independent of the selected usage window/allocation; no feature count or AI authorship is inferred.'],**account_report)


INPUTS_SELECT = """SELECT * FROM (SELECT p.*,ROW_NUMBER() OVER(
 PARTITION BY tool,COALESCE(NULLIF(source_uid,''),uid)
 ORDER BY CASE WHEN origin IN ('human','automation') THEN 1 ELSE 0 END DESC,ts,machine,session) AS input_rank
 FROM prompts p WHERE tool<>'codex' OR provenance IN ('codex_user_text','codex_user_event')) WHERE input_rank=1"""
