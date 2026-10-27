"""Extract allowlisted work evidence from paired native tools; never execute log text."""
import hashlib
import time
import json
import os
import re
import shlex
import subprocess


def _value(text, key):
    """Read a literal JSON value from a JS object without evaluating JavaScript."""
    match = re.search(r'(?:["\']?' + re.escape(key) + r'["\']?)\s*:\s*', text)
    if match:
        try:return json.JSONDecoder().raw_decode(text[match.end():])[0]
        except ValueError:pass


_REPOS={};_REPOS_AT=0
_ROOTS={}
def worktree_root(path):
    if not os.path.isdir(path):return path
    if path not in _ROOTS:
        try:
            result=subprocess.run(['git','-C',path,'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=3)
            _ROOTS[path]=os.path.realpath(result.stdout.strip()) if result.returncode==0 else path
        except (OSError,subprocess.SubprocessError):_ROOTS[path]=path
    return _ROOTS[path]

def object_repository(repo):
    """Find objects for a deleted worktree using an exact origin match, never HEAD."""
    from . import collect
    from .work import git_context
    global _REPOS_AT
    if not _REPOS or time.monotonic()-_REPOS_AT>300:
        _REPOS.clear()
        for path in collect.find_repos([], [os.path.join(collect.HOME,'Projects')]):
            identity=git_context(path).get('repo')
            if identity:_REPOS.setdefault(identity,[]).append(path)
        _REPOS_AT=time.monotonic()
    return _REPOS.get(repo,[])


def _shell(command, cwd, native_cwd=None, native_repo='', scope_only=False, repo_context=None):
    from .work import git_context
    if isinstance(command,list) and len(command)==3 and command[1] in ('-c','-lc') and os.path.basename(command[0]) in ('bash','zsh','sh'):command=command[2]
    if not isinstance(command,str):return None
    has_intent=any(word in command for word in ('commit','checkout','switch','pr create')) and '<<' not in command
    if not has_intent and not scope_only:return None
    if not has_intent:
        if repo_context is not None:
            recorded=repo_context(cwd)
            return dict(kinds=[],cwd=cwd,scope_cwd=recorded.get('scope_cwd') or cwd,object_cwd=cwd,repo=recorded['repo']) if recorded.get('repo') and recorded['repo']==native_repo else None
        repo=git_context(cwd).get('repo','')
        return dict(kinds=[],cwd=cwd,scope_cwd=worktree_root(cwd),object_cwd=cwd,repo=repo) if repo and repo==native_repo else None
    try:
        lexer=shlex.shlex(command,posix=True,punctuation_chars=';&|\n');lexer.whitespace=' \t\r';lexer.whitespace_split=True
        tokens=list(lexer)
        if any('||' in t for t in tokens):return None
        tokens=[';' if re.fullmatch(r'[;\n&|]+',t) else t for t in tokens]
    except ValueError:return None
    targets=[cwd];kinds=set()
    for i,t in enumerate(tokens):
        if i and tokens[i-1] not in (';', '&&', '||', '&'):continue
        if t=='cd' and i+1<len(tokens):targets.append(os.path.abspath(os.path.join(cwd or '',tokens[i+1])))
        if t not in ('git','gh'):continue
        j=i+1
        if t=='git' and j+1<len(tokens) and tokens[j]=='-C':targets.append(os.path.abspath(os.path.join(cwd or '',tokens[j+1])));j+=2
        if t=='git' and j<len(tokens) and tokens[j] in ('checkout','switch'):kinds.add('checkout')
        if t=='git' and j<len(tokens) and tokens[j]=='commit' and '--dry-run' not in tokens:kinds.add('commit')
        if t=='gh' and tokens[j:j+2]==['pr','create'] and '--dry-run' not in tokens:kinds.add('pr_create')
    # A leading cd / git -C overrides the invocation cwd. Multiple targets are unsafe.
    explicit=set(targets[1:]);target=next(iter(explicit)) if len(explicit)==1 else cwd
    if len(explicit)>1 or not kinds:return None
    if repo_context is not None:
        recorded=repo_context(target)
        if not recorded.get('repo') or recorded['repo']!=native_repo:return None
        # Recorded origin/scope is historical evidence. Current configuration is
        # consulted only to locate immutable Git objects, never to assign scope.
        objects=target
        if 'commit' in kinds and git_context(target).get('repo')!=recorded['repo']:
            paths=object_repository(recorded['repo'])
            objects=paths[0] if len(paths)==1 else ''
        return dict(kinds=sorted(kinds),cwd=target,scope_cwd=recorded.get('scope_cwd') or target,object_cwd=objects,repo=recorded['repo'])
    cross_worktree=False
    if native_cwd and os.path.realpath(target)!=os.path.realpath(native_cwd):
        def root(path):
            try:
                result=subprocess.run(['git','-C',path,'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=3)
                return os.path.realpath(result.stdout.strip()) if result.returncode==0 else None
            except (OSError,subprocess.SubprocessError):return None
        target_root,native_root=root(target),root(native_cwd)
        if not target_root:return None
        cross_worktree=target_root!=native_root
    repo=git_context(target).get('repo','')
    objects=target
    if not repo and native_cwd and target==native_cwd and native_repo and not os.path.exists(target):
        paths=object_repository(native_repo)
        if len(paths)==1:repo=native_repo;objects=paths[0]
    if not repo or cross_worktree and repo!=native_repo:return None
    return dict(kinds=sorted(kinds),cwd=target,scope_cwd=worktree_root(target),object_cwd=objects,repo=repo)



def invocation(name, arguments, state, repo_context=None):
    """Reduce a shell invocation to safe intent, path and repository metadata."""
    cwd=state.get('_native_cwd') or state.get('cwd') or ''
    if name.split('.')[-1] in ('Bash','exec_command','shell'):
        if isinstance(arguments,str):
            try:arguments=json.loads(arguments)
            except ValueError:return None
        if not isinstance(arguments,dict):return None
        return _shell(arguments.get('command',arguments.get('cmd')),arguments.get('workdir') or cwd,cwd,state.get('repo',''),bool(arguments.get('workdir')),repo_context)
    if name.split('.')[-1]=='write_stdin':
        if isinstance(arguments,str):
            try:arguments=json.loads(arguments)
            except ValueError:return None
        if isinstance(arguments,dict):return state.get('_shells',{}).get(str(arguments.get('session_id')))
    if name.split('.')[-1]=='wait':
        if isinstance(arguments,str):
            try:arguments=json.loads(arguments)
            except ValueError:return None
        if isinstance(arguments,dict):return state.get('_cells',{}).get(str(arguments.get('cell_id')))
    if name.split('.')[-1]=='exec' and isinstance(arguments,str):
        if len(re.findall(r'\btools\.\w+\s*\(',arguments))!=1:return None
        intents=[]
        for match in re.finditer(r'\btools\.(exec_command|write_stdin)\s*\(\s*\{',arguments):
            fragment=arguments[match.end():]
            if match[1]=='write_stdin':
                intent=state.get('_shells',{}).get(str(_value(fragment,'session_id')))
            else:
                intent=_shell(_value(fragment,'cmd'),_value(fragment,'workdir') or cwd,cwd,state.get('repo',''),bool(_value(fragment,'workdir')),repo_context)
            if intent:intents.append(intent)
        if len(intents)==1:return intents[0]
        # Without individual result correlation, several commands are ambiguous.
    return None


def _texts(value, depth=0):
    if depth>8:return []
    if isinstance(value,str):
        if re.search(r'(?:Process exited with code|Exit code:)\s*[1-9][0-9]*',value):return []
        try:return _texts(json.loads(value),depth+1)
        except ValueError:
            nested=[];structured=False
            for match in re.finditer(r'(?m)^[ \t]*[\{\[]',value):
                try:item=json.JSONDecoder().raw_decode(value[match.start():].lstrip())[0]
                except ValueError:continue
                if isinstance(item,dict) and any(k in item for k in ('output','content','exit_code')):
                    structured=True;nested.extend(_texts(item,depth+1))
            return nested if structured else [value]
    if isinstance(value,list):return [t for item in value for t in _texts(item,depth+1)]
    if isinstance(value,dict):
        if value.get('is_error') or value.get('exit_code') not in (None,0) or value.get('session_id') and value.get('exit_code') is None:return []
        return [t for k in ('output','stdout','text','content') if k in value for t in _texts(value[k],depth+1)]
    return []


def result_objects(value,depth=0):
    if depth>8:return []
    if isinstance(value,str):
        try:return result_objects(json.loads(value),depth+1)
        except ValueError:
            found=[]
            for match in re.finditer(r'(?m)^[ \t]*[\{\[]',value):
                try:item=json.JSONDecoder().raw_decode(value[match.start():].lstrip())[0]
                except ValueError:continue
                found.extend(result_objects(item,depth+1))
            return found
    if isinstance(value,list):return [x for item in value for x in result_objects(item,depth+1)]
    if isinstance(value,dict):
        if 'session_id' in value or 'exit_code' in value:return [value]
        return [x for k in ('content','text','output') if k in value for x in result_objects(value[k],depth+1)]
    return []


def commit_record(repo,cwd,sha,ts):
    from . import collect
    try:
        result=subprocess.run(['git','-C',cwd,'show','--format=%ct','--numstat','--no-renames',sha],capture_output=True,text=True,timeout=5)
        lines=result.stdout.splitlines();stamp=int(lines[0]);added=removed=files=0
        if result.returncode:return None
        for line in lines[1:]:
            fields=line.split('\t',2)
            if len(fields)!=3:continue
            a,r,path=fields
            if collect.SKIP_FILE.search(path) or collect.is_doc(path):continue
            files+=1
            if a.isdigit() and r.isdigit():added+=int(a);removed+=int(r)
        return dict(t='work_commit',uid=repo+':'+sha,ts=stamp,repo=repo,sha=sha,added_code=added,removed_code=removed,files=files,provenance='git_object')
    except (OSError,ValueError,subprocess.SubprocessError):return None


def _sha(cwd, short):
    try:
        p=subprocess.run(['git','-C',cwd,'rev-parse','--verify',short+'^{commit}'],capture_output=True,text=True,timeout=3)
        sha=p.stdout.strip()
        return sha if p.returncode==0 and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',sha) else ''
    except (OSError,subprocess.SubprocessError):return ''


def observe(tool, record, payload, state, ts, repo_context=None):
    """Pair tool IDs across incremental tails; checkpoint only sanitized intent."""
    from .work import pr_identity,scope_context,context_event
    metadata=payload.get('internal_chat_message_metadata_passthrough') or {}
    created=metadata.get('create_time') if isinstance(metadata,dict) else None
    if tool=='codex' and (ts<state.get('started',0) or isinstance(created,(int,float)) and created<state.get('started',0)-5):return []
    pending=state.setdefault('_tools',{});calls=[];results=[];context_rows=[]
    if tool=='claude':
        content=(record.get('message') or {}).get('content')
        if isinstance(content,list):
            for item in content:
                if not isinstance(item,dict):continue
                if item.get('type')=='tool_use':calls.append((item.get('id'),item.get('name',''),item.get('input')))
                if item.get('type')=='tool_result' and not item.get('is_error'):results.append((item.get('tool_use_id'),item.get('content')))
    elif record.get('type')=='response_item':
        if payload.get('type') in ('function_call','custom_tool_call'):calls.append((payload.get('call_id'),payload.get('name',''),payload.get('arguments',payload.get('input'))))
        if payload.get('type') in ('function_call_output','custom_tool_call_output'):results.append((payload.get('call_id'),payload.get('output')))
    for ident,name,args in calls:
        hint=invocation(name,args,state,repo_context)
        if hint and ident and hint['repo']==state.get('repo'):
            if tool=='codex' and hint.get('scope_cwd',hint['cwd'])!=state.get('cwd'):
                state.update(cwd=hint.get('scope_cwd',hint['cwd']),branch='',head_sha='')
                scope_context(state,ts)
                rows_context=context_event(tool,state['session'],ts,state['cwd'],repo=state['repo'],scope_id=state['_scope_id'],task_id=state.get('task_id',''),provenance='tool_workdir')
                context_rows.append(rows_context)
            hint.update(scope_id=state.get('_scope_id',''),session=state['session'],task_id=state.get('task_id',''))
            pending[ident]=hint
    while len(pending)>128:pending.pop(next(iter(pending)))
    rows=list(context_rows)
    for ident,output in results:
        hint=pending.pop(ident,None)
        if not hint or not hint.get('scope_id') or hint['repo']!=state.get('repo') or hint['scope_id']!=state.get('_scope_id'):continue
        # Follow asynchronous exec sessions using numeric IDs, never raw command text.
        for wrapper in result_objects(output):
            if wrapper.get('session_id') and wrapper.get('exit_code') is None:
                shells=state.setdefault('_shells',{});shells[str(wrapper['session_id'])]=hint
                while len(shells)>128:shells.pop(next(iter(shells)))
        if isinstance(output,str):
            match=re.search(r'Script running with cell ID ([A-Za-z0-9_-]+)',output)
            if match:
                cells=state.setdefault('_cells',{});cells[match[1]]=hint
                while len(cells)>128:cells.pop(next(iter(cells)))
        for text in _texts(output):
            if 'checkout' in hint['kinds']:
                branches=set(re.findall(r"^(?:Switched to (?:a new )?branch|Already on) '([\w./-]{1,256})'\s*$",text,re.M))
                if len(branches)==1:
                    branch=next(iter(branches))
                    if branch!=state.get('branch'):
                        state.update(branch=branch,head_sha='')
                        hint['scope_id']=scope_context(state,ts)
                        rows.append(context_event(tool,state['session'],ts,state.get('cwd'),repo=state['repo'],branch=branch,scope_id=state['_scope_id'],task_id=state.get('task_id',''),provenance='tool_checkout'))
            facts=[]
            if 'commit' in hint['kinds']:
                for match in re.finditer(r'^\[[^\]\n]+\s([0-9a-f]{7,64})\]\s',text,re.M):
                    sha=_sha(hint.get('object_cwd',hint['cwd']),match[1])
                    if sha:
                        facts.append(('tool_commit',sha,''))
                        commit=commit_record(hint['repo'],hint.get('object_cwd',hint['cwd']),sha,ts)
                        if commit:rows.append(commit)
            if 'pr_create' in hint['kinds']:
                for line in text.splitlines():
                    url=line.strip();identity=pr_identity(url)
                    if identity and identity[0]==hint['repo']:facts.append(('tool_pr_create','','https://'+identity[0]+'/pull/'+str(identity[1])))
            for provenance,sha,url in facts:
                row=dict(t='work_evidence',capture_version=5,ts=ts,tool=tool,session=hint['session'],repo=hint['repo'],scope_id=hint['scope_id'],task_id=hint['task_id'],sha=sha,pr_url=url,provenance=provenance)
                row['uid']='evidence:'+hashlib.sha256(json.dumps([tool,hint['session'],ident,provenance,sha,url],sort_keys=True).encode()).hexdigest()
                rows.append(row)
    return rows
