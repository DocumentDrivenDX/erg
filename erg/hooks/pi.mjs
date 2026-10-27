// Content-free snapshots use Pi's native entry IDs, shared with transcript capture.
import {mkdirSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync, unlinkSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {homedir} from 'node:os';
import {createHash, randomUUID} from 'node:crypto';

export function install(pi, options = {}) {
  const state = options.stateDir || process.env.ERG_STATE_DIR || join(homedir(), '.local', 'share', 'erg');
  let contentEnabled=false;let enrolledAt=0;
  try { const config=JSON.parse(readFileSync(join(state,'capture.json'),'utf8'));contentEnabled=config.content===true;enrolledAt=config.enrolled_at || 0; } catch {}
  const contentSeen=new Set();
  const writer = randomUUID(); // Concurrent/resumed Pi processes cannot overwrite each other.
  let warned = false;
  const submitted = [];
  const origins = new Map();
  const routes = [];
  const routeSignatures = new Map();
  const fingerprint = text => createHash('sha256').update(text).digest('hex');
  pi.on('input', event => {
    submitted.push({hash:fingerprint(event.text || ''),origin:event.source==='interactive'?'human':'unknown'});
  });
  const capture = (_event, ctx) => {
    let temp;
    try {
      const manager = ctx.sessionManager;
      const header = manager.getHeader();
      const id = manager.getSessionId();
      if (!id || !header) return;
      const rows = [{type:'session', id, timestamp:header.timestamp, cwd:manager.getCwd(), version:header.version}];
      // Observe the selected runtime route now; never stamp old messages with it.
      const model=ctx.model;
      if (model?.provider || model?.baseUrl) {
        const route={provider:model.provider || ''};
        if (typeof model.baseUrl==='string') {
          try {
            const url=new URL(model.baseUrl);
            if (['http:','https:'].includes(url.protocol) && !url.username && !url.password) {
              route.base_url=url.origin+(/^\/(?:api\/|ai-gateway\/|anthropic\/|codex\/|openai\/)*v\d+\/?$/.test(url.pathname)?url.pathname:'');
              const host=process.env.DATABRICKS_HOST;
              if (host && new URL(host).host===url.host && process.env.DATABRICKS_CONFIG_PROFILE) route.profile=process.env.DATABRICKS_CONFIG_PROFILE;
            }
          } catch {}
        }
        if (typeof model.region==='string') route.region=model.region;
        const key=id+JSON.stringify(route);
        if (routeSignatures.get(id)!==key) { routes.push({type:'erg_route',sessionId:id,timestamp:new Date().toISOString(),route}); routeSignatures.set(id,key); }
      }
      rows.push(...routes.filter(r=>r.sessionId===id));
      for (const entry of manager.getEntries()) {
        const m = entry.message;
        if (entry.type !== 'message' || !entry.id) continue;
        if (m?.role === 'user') {
          const text = typeof m.content==='string'?m.content:(m.content||[]).filter(b=>b.type==='text').map(b=>b.text||'').join('\n');
          if (!origins.has(entry.id)) {
            const i=submitted.findIndex(x=>x.hash===fingerprint(text));
            origins.set(entry.id,i<0?'unknown':submitted.splice(i,1)[0].origin);
          }
          const chars=Array.from(text).length;
          rows.push({type:'message',id:entry.id,timestamp:entry.timestamp,message:{role:'user',...(m.timestamp===0?{usageProvenance:'synthetic_history'}:{}),inputStats:{
            chars,words:text.trim()?text.trim().split(/\s+/u).length:0,est_tokens:Math.ceil(chars/4),
            origin:origins.get(entry.id),provenance:'pi_lifecycle'}}});
          continue;
        }
        if (m?.role==='toolResult') continue;
        if (m?.role !== 'assistant' || !m.usage) continue;
        const usage = {};
        for (const key of ['input', 'output', 'cacheRead', 'cacheWrite', 'reasoning', 'totalTokens']) {
          if (typeof m.usage[key] === 'number' && Number.isFinite(m.usage[key]) && m.usage[key] >= 0) usage[key] = m.usage[key];
        }
        if (typeof m.usage.cost?.total==='number' && Number.isFinite(m.usage.cost.total) && m.usage.cost.total>=0) usage.cost={total:m.usage.cost.total};
        if (!Object.keys(usage).length) continue;
        const message = {role:'assistant', usage};
        if (m.timestamp===0) message.usageProvenance='synthetic_history';
        for (const key of ['model','provider','responseId','requestId']) {
          if (typeof m[key] === 'string') message[key] = m[key];
        }
        rows.push({type:'message', id:entry.id, timestamp:entry.timestamp, message});
      }
      if (contentEnabled) {
        const batch=[rows[0]],keys=[];
        for (const entry of manager.getEntries()) {
          const m=entry.message,key=id+':'+entry.id;
          if (entry.type!=='message' || !entry.id || !m || contentSeen.has(key)) continue;
          const time=typeof entry.timestamp==='number'?entry.timestamp:Date.parse(entry.timestamp);
          const started=typeof header.timestamp==='number'?header.timestamp:Date.parse(header.timestamp);
          if ((time>1e11?time/1000:time)<enrolledAt && (started>1e11?started/1000:started)<enrolledAt) {contentSeen.add(key);continue;}
          const message={};
          for (const name of ['role','content','usage','provider','model','responseId','requestId','toolCallId','toolName','isError','stopReason']) {
            if (m[name]!==undefined) message[name]=m[name];
          }
          if (m.timestamp===0) message.usageProvenance='synthetic_history';
          batch.push({type:'message',id:entry.id,parentId:entry.parentId,timestamp:entry.timestamp,message});keys.push(key);
        }
        if (keys.length) {
          const outbox=join(state,'pi-content-outbox');mkdirSync(outbox,{recursive:true,mode:0o700});
          const path=join(outbox,writer+'-'+randomUUID()+'.jsonl');temp=path+'.tmp';
          const fd=openSync(temp,'wx',0o600);
          try {writeFileSync(fd,batch.map(r=>JSON.stringify(r)).join('\n')+'\n');fsyncSync(fd);} finally {closeSync(fd);}
          renameSync(temp,path);temp=undefined;
          const directory=openSync(outbox,'r');try {fsyncSync(directory);} finally {closeSync(directory);}
          keys.forEach(k=>contentSeen.add(k));
        }
      }
      const dir = join(state, 'pi-snapshots');
      mkdirSync(dir, {recursive:true, mode:0o700});
      const path = join(dir, createHash('sha256').update(id).digest('hex')+'-'+writer+'.jsonl');
      const text = rows.map(row=>JSON.stringify(row)).join('\n')+'\n';
      try { if (readFileSync(path, 'utf8') === text) return; } catch (error) { if (error.code !== 'ENOENT') throw error; }
      temp = path+'.'+randomUUID()+'.tmp';
      const fd = openSync(temp, 'wx', 0o600);
      try { writeFileSync(fd, text); fsyncSync(fd); } finally { closeSync(fd); }
      renameSync(temp, path); temp = undefined;
      const directory = openSync(dir, 'r');
      try { fsyncSync(directory); } finally { closeSync(directory); }
      warned = false;
    } catch (_error) {
      if (temp) { try { unlinkSync(temp); } catch {} }
      // Capture errors must be visible without disrupting the user's agent.
      if (!warned) ctx.ui?.notify?.('erg could not save local usage; check capture service status.', 'warning');
      warned = true;
    }
  };
  // message_end fires before Pi assigns its persisted entry ID. These boundaries
  // run afterward, so snapshots and native transcripts retain identical identities.
  for (const event of ['before_agent_start','session_start','turn_end','agent_end','tool_execution_start',
                        'session_before_switch','session_switch','session_before_compact','session_shutdown']) {
    pi.on(event, capture);
  }
}
export default install;
