const appearance=matchMedia('(prefers-color-scheme: dark)');document.documentElement.dataset.appearance=appearance.matches?'dark':'light';appearance.addEventListener('change',e=>{document.documentElement.dataset.appearance=e.matches?'dark':'light'});
/* Erg navigation and reporting presentation. Reports retain their original owners. */
window.ErgUI=(()=>{
 const q=s=>document.querySelector(s), all=s=>[...document.querySelectorAll(s)];
 const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const canonical={usage:'overview',costs:'projects',history:'sessions',work:'projects',capture:'system',machine:'system'};
 const internal={overview:'usage',projects:'costs',project:'costs',sessions:'history',session:'history',accounts:'accounts',system:'capture'};
 let frame='',restoring=false,selected=null,sessionRequest=0,workScope='',costScope='',moneySignature='';
 function mount(){
  const old=q('body > header');old.id='scope-controls';
  old.querySelector('h1').remove();
  const scope=document.createElement('section');scope.id='scope-region';scope.setAttribute('aria-label','Shared report scope');
  while(old.firstChild)scope.append(old.firstChild);old.replaceWith(scope);
  q('.view-nav').remove();const oldMain=q('body > main'),content=document.createElement('div');while(oldMain.firstChild)content.append(oldMain.firstChild);oldMain.replaceWith(content);content.id='report-content';
  const host=document.createElement('div');host.id='dashboard-shell';content.before(host);
  const payload=document.createElement('div');payload.id='dashboard-content';payload.append(scope,content);
  function layout(){
   const next=innerWidth<840?'top-nav':'side-nav';if(frame===next)return;frame=next;
   host.innerHTML=window.ergShells[next];q('#erg-content').append(payload);if(typeof window.ergReady!=='undefined')paintNav();
  }
  layout();addEventListener('resize',layout);
  // Scope and system evidence are separate from primary reporting.
  const system=q('#view-capture');system.insertAdjacentHTML('afterbegin','<nav class="system-sections" aria-label="System sections"><a href="#capture-sources">Capture</a><a href="#sync-summary">Delivery</a><a href="#report-lag">Reporting</a><a href="#title-machine">Machine</a></nav>');
  system.querySelector('.view-heading').id='capture-sources';
  const cards=[q('#sync-summary').closest('.card'),q('#history-health').closest('.card')];for(const card of cards)system.append(card);
  const machine=q('#view-machine');machine.classList.remove('view');machine.hidden=false;system.append(machine);
  const work=q('#view-work');work.classList.remove('view');work.hidden=false;q('#view-costs').append(work);
  q('#title-work').textContent='Work produced';q('#title-costs').textContent='Projects';q('#title-history').textContent='Sessions';q('#title-accounts').textContent='Accounts';q('#title-capture').textContent='System';q('#title-usage').textContent='Overview';
  q('#view-accounts .view-heading').insertAdjacentHTML('afterend','<section id="account-capacity" aria-label="Current account availability"></section>');
  q('#money-kpis').insertAdjacentHTML('afterend','<p id="money-status" role="status"></p>');
  q('#money-note').outerHTML='<details class="money-explanation"><summary>Cost basis and evidence</summary><p id="money-note" class="sub" role="status">Loading cost accounting…</p></details>';
  q('#money-overview').insertAdjacentHTML('beforeend','<details id="money-gaps" hidden><summary></summary><div></div></details>');
  const health=q('.health-strip');system.prepend(health);
  const reportInfo=document.createElement('details');reportInfo.id='report-evidence';reportInfo.innerHTML='<summary>Reporting freshness and corpus</summary>';
  [q('#gen'),q('#report-lag'),q('#cloud-report-status'),q('#sync-health')].forEach(e=>reportInfo.append(e));
  const meta=document.createElement('div');meta.className='report-meta';meta.innerHTML='<strong id="backend-label"></strong><span id="publication-summary" role="status"></span>';meta.append(q('#report-backend'),reportInfo);scope.before(meta);const headings=document.createElement('div');headings.id='page-headings';meta.after(headings);for(const key of ['usage','costs','history','accounts','capture']){const h=q('#title-'+key).closest('.view-heading');h.dataset.heading=key;headings.append(h)}
  q('#backend-label').textContent=location.pathname==='/databricks'?'Databricks · admitted corpus':'Local · this ledger';
  // Folder selection is a refinement, after the three primary scope controls.
  q('.filter-body').prepend(q('#folder').closest('.ctl'));
  scope.insertAdjacentHTML('beforeend','<button id="clear-scope" type="button">Clear filters</button><details id="custom-range"><summary>Custom dates</summary><form id="date-scope"><label>From<input id="scope-start" type="datetime-local" step="3600" required></label><label>Until (exclusive)<input id="scope-end" type="datetime-local" step="3600" required></label><button>Apply dates</button><p id="date-error" role="status"></p></form></details>');
  q('#history-reader').insertAdjacentHTML('afterbegin','<nav class="breadcrumbs" aria-label="Session ancestry"><button id="session-back" type="button">Back to sessions</button></nav>');
  q('#history-cost-summary').insertAdjacentHTML('afterend','<div id="session-expense" role="status"></div>');
  q('#history-reader').insertAdjacentHTML('beforeend','<section id="session-work" aria-label="Session work evidence"></section>');
  q('#history-filters').insertAdjacentHTML('beforeend','<label>Search loaded titles or folders<input id="history-search" type="search" placeholder="Filter the loaded session list"></label>');
  q('#history-search').addEventListener('input',()=>{const term=q('#history-search').value.toLowerCase();all('#history-sessions tr').slice(1).forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(term))});
  document.addEventListener('click',event=>{
   const dest=event.target.closest('[data-destination], .piq-primary-nav a, .piq-shell__bar a');
   if(dest){const key=dest.dataset.destination||dest.hash?.slice(1);if(internal[key]){event.preventDefault();navigate(key)}}
  });
  q('#session-back').onclick=()=>{selected=null;showView('history')};
  q('#clear-scope').onclick=()=>{Object.assign(S,{range:'7d',start:null,end:null,project:'*',folder:'*',machine:'*',requester:'all',toolsAll:true,kindsAll:true});scopeChanged()};
  q('#date-scope').onsubmit=e=>{e.preventDefault();const start=Date.parse(q('#scope-start').value)/1000,end=Date.parse(q('#scope-end').value)/1000;if(!Number.isFinite(start)||!Number.isFinite(end)||start>=end||start%3600||end%3600){q('#date-error').textContent='Choose whole-hour boundaries, with the end after the start.';return}q('#date-error').textContent='';S.start=start;S.end=end;S.anchor=Math.floor((end-1)/3600);S.range='All';scopeChanged()};
  new MutationObserver(()=>{q('#publication-summary').textContent=q('#gen').textContent.split(' · ').find(s=>/published|as of/i.test(s))||q('#gen').textContent.split(' · ')[0]}).observe(q('#gen'),{childList:true,characterData:true,subtree:true});
  new MutationObserver(styleControls).observe(content,{childList:true,subtree:true});styleControls();
 }
 function styleControls(){
  all('#dashboard-content [style*="overflow"]').forEach(e=>{e.tabIndex=0});
  all('#dashboard-content button:not(.piq-btn)').forEach(b=>b.classList.add('piq-btn','piq-btn--secondary','piq-btn--sm'));
  all('#dashboard-content select:not(.piq-field__input),#dashboard-content input:not(.piq-field__input)').forEach(e=>e.classList.add('piq-field__input'));
 }
 function paintNav(){
  const dest=canonical[typeof activeView==='undefined'?'usage':activeView]||activeView;
  all('#dashboard-shell a[href^="#"]').forEach(a=>{if(internal[a.hash.slice(1)]){if(a.hash.slice(1)===dest)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')}});
  const sys=q('[data-destination="system"]');if(sys){if(dest==='system')sys.setAttribute('aria-current','page');else sys.removeAttribute('aria-current')}
 }
 function state(){const obj={};for(const k of ['range','anchor','project','folder','machine','requester','measure','group','bucket','metric','dim','start','end'])if(S[k]!=null)obj[k]=S[k];if(!S.toolsAll)obj.tools=[...S.tools].join(',');if(!S.kindsAll)obj.kinds=[...S.kinds].join(',');return obj}
 function save(replace=false){
  if(restoring)return;const u=new URL(location.href);u.search='';for(const [k,v]of Object.entries(state()))u.searchParams.set(k,v);
  u.hash=selected?'session':(canonical[activeView]||activeView);
  if(selected){u.searchParams.set('session',selected.session);u.searchParams.set('tool',selected.tool);if(selected.machine)u.searchParams.set('session_machine',selected.machine)}
  if(u.href!==location.href)history[replace?'replaceState':'pushState'](null,'',u);
  const backend=q('#report-backend');backend.href=(location.pathname==='/databricks'?'/':'/databricks')+u.search+u.hash;
 }
 function read(){
  const p=new URLSearchParams(location.search);for(const k of ['range','project','folder','machine','requester','measure','group','bucket','metric','dim'])if(p.has(k))S[k]=p.get(k);
  if(!RANGES.hasOwnProperty(S.range))S.range='7d';if(!MEASURES[S.measure])S.measure='nocache';if(!GROUPS[S.group])S.group='folder';if(!['hour','day'].includes(S.bucket))S.bucket='hour';
  for(const k of ['anchor','start','end']){const v=Number(p.get(k));if(p.has(k)&&Number.isFinite(v)&&v>=0&&v<=(k==='anchor'?Math.floor(Date.now()/3600000):Math.floor(Date.now()/1000)+86400))S[k]=v;else if(k!=='anchor')S[k]=null}
  if(S.start!=null&&S.end!=null&&S.start>=S.end){S.start=null;S.end=null}
  for(const k of ['tools','kinds']){S[k+'All']=!p.has(k);if(p.has(k))S[k]=new Set(p.get(k).split(',').filter(Boolean))}
  return internal[location.hash.slice(1)]||({usage:'usage',costs:'costs',history:'history',work:'costs',capture:'capture',machine:'capture'}[location.hash.slice(1)])||'usage';
 }
 function openFromProject(row){restoring=true;showView('history');restoring=false;openHistory(row)}
 function navigate(dest){selected=null;showView(internal[dest]||dest)}
 function view(key){document.body.dataset.reportView=key;all('[data-heading]').forEach(h=>h.hidden=h.dataset.heading!==key);
  if(key!=='history')selected=null;
  const detail=key==='costs'&&S.project!=='*';q('#title-costs').textContent=detail?projectLabel(S.project):'Projects';
  q('#view-work').hidden=key!=='costs';
  if(key==='history'){q('#history-reader').hidden=!selected;if(!selected)q('#history-search').value='';for(const child of q('#view-history').children)if(child!==q('#history-reader'))child.hidden=!!selected}
  q('#money-overview').hidden=!['usage','costs','accounts'].includes(key);
  paintNav();save();
  const heading=key==='history'&&selected?q('#history-reader-title'):q('#title-'+key);if(heading&&!restoring){heading.tabIndex=-1;heading.focus({preventScroll:true})}
  if(key==='costs'){const scope=scopeQuery().toString();workScope=scope;loadEffectiveness(false,'work')}
  if(key==='usage')overviewProjects();
 }
 async function overviewProjects(){
  if(!q('#overview-projects'))q('#view-usage').insertAdjacentHTML('afterbegin','<section id="overview-projects" class="card"><div class="chart-heading"><h2>Projects at a glance</h2><button id="compare-projects" type="button">Compare projects</button></div><div id="overview-project-rows"></div></section>');
  q('#compare-projects').onclick=()=>showView('costs');
  const scope=scopeQuery().toString();if(scope===costScope)return;costScope=scope;
  try{const r=await apiFetch('/api/costs?'+scope);if(!r.ok)throw Error('Project overview unavailable');const d=await r.json();if(scope!==scopeQuery().toString())return;requireCurrentPublication(d);const rows=d.projects||[];q('#overview-project-rows').innerHTML=rows.length?rows.slice(0,5).map((r,i)=>'<div class="allocation-row"><button class="allocation-name" data-overview-project="'+i+'">'+escape(r.name)+'</button><div><small>Tokens</small>'+fmt(r.tokens)+'</div><div><small>Rack value · est.</small>'+escape(costValue(r))+'</div><div><small>Effective expense</small>'+escape(effectiveCost(r))+'</div></div>').join(''):'<p>No usage in this scope. Try a wider range or clear filters.</p>';all('[data-overview-project]').forEach(b=>b.onclick=()=>{S.project=rows[+b.dataset.overviewProject].project_key;S.folder='*';showView('costs')})}catch(e){costScope='';q('#overview-project-rows').textContent=e.message}
 }
 function money(data){
  q('#money-status').textContent=data.available?'':(data.reason||'Cost reporting unavailable');
  const t=data.totals||{},gaps=Object.entries(t.missing_reasons||{}),details=q('#money-gaps');details.hidden=!gaps.length;
  details.querySelector('summary').textContent=gaps.length+' cost evidence '+(gaps.length===1?'gap':'gaps')+' · expand for affected usage';
  details.querySelector('div').innerHTML=gaps.map(([reason,n])=>'<p>'+fmt(n)+' requests · '+escape(reason.replaceAll('_',' '))+'</p>').join('');
  const basis=location.pathname==='/databricks'?'Databricks':'Local';moneySignature=basis+' · allocation '+scopeCatalog.revision;
 }
 function published(id,data){let e=q('#'+id+'-publication');if(!e){e=document.createElement('p');e.id=id+'-publication';e.className='scope-note';q('#view-'+id).prepend(e)}const dates=scopeQuery();e.textContent='Completed results: '+(S.project==='*'?'All human projects':projectLabel(S.project))+' · '+(S.machine==='*'?'all machines':S.machine)+' · '+new Date(Number(dates.get('start'))*1000).toLocaleString()+' to '+new Date(Number(dates.get('end'))*1000).toLocaleString()+' · allocation '+(data.project_revision??data.revision??scopeCatalog.revision)}
 function projects(data){published('costs',data);q('#title-costs').textContent=S.project==='*'?'Projects':projectLabel(S.project);if(activeView==='costs')q('#view-work').hidden=false}
 function work(data){published('work',data);
  if(activeView==='costs'&&workScope!==scopeQuery().toString())return;
  q('#title-work').textContent='Work produced';
 }
 function accounts(data){published('accounts',data);
  const registry=data.account_registry||data.accounts||[],byKey=new Map(registry.map(a=>[a.account_key,a]));
  const grouped=new Map();for(const row of data.quota_latest||[]){if(!row.account_key)continue;const key=row.account_key;const list=grouped.get(key)||[];list.push(row);grouped.set(key,list)}
  q('#account-capacity').innerHTML='<h2>Current availability</h2><p class="sub">Account-wide observations; independent of the usage date range below.</p><div class="capacity-grid">'+(grouped.size?[...grouped].map(([key,windows])=>{const a=byKey.get(key)||{};const latest=new Map();for(const w of windows){const id=w.window+'|'+(w.window_minutes||'');if(!latest.has(id)||(w.ts||0)>(latest.get(id).ts||0))latest.set(id,w)}return '<article class="card"><h3>'+escape(a.provider||windows[0].tool)+' · '+escape(a.email_hint||key.slice(0,12))+'</h3><p>'+escape(a.plan||'Plan not exposed')+'</p>'+[...latest.values()].map(w=>'<div class="quota-window"><strong>'+escape(w.window)+((w.resets_at&&w.resets_at<Date.now()/1000)?' · previous window; current availability unknown':w.used_percent==null?' · availability not exposed':' · '+Math.max(0,100-w.used_percent).toFixed(0)+'% remaining')+'</strong>'+(w.used_percent!=null&&!(w.resets_at&&w.resets_at<Date.now()/1000)?'<progress max="100" value="'+Math.max(0,100-w.used_percent)+'" aria-label="'+escape(w.window)+' remaining"></progress>':'')+'<span>'+(w.resets_at?'Resets '+escape(new Date(w.resets_at*1000).toLocaleString()):'Reset time not exposed')+'</span><small>'+(w.ts?'Observed '+escape(new Date(w.ts*1000).toLocaleString()):'Observation time unavailable')+'</small></div>').join('')+'</article>'}).join(''):'<p>No account-linked quota observations. Usage remains available below; inspect account evidence for unsupported associations.</p>')+'</div>';
 }
 function session(row,append){
  if(append)return;selected=row;sessionRequest++;q('#session-expense').replaceChildren();
  if(!row.title){const titles=new URLSearchParams({scope:'1',tool:row.tool,session:row.session,start:'0',end:String(Math.floor(Date.now()/1000)+1),limit:'1',order:'recent'});if(row.machine)titles.set('machine',row.machine);apiFetch('/api/history/sessions?'+titles).then(r=>r.json()).then(d=>{if(selected!==row)return;const item=d.sessions?.[0];if(item?.title){row.title=item.title;q('#history-reader-title').textContent=row.title+' · '+row.tool+' · '+row.session}}).catch(()=>{})}q('#session-work').textContent='Loading verified work…';
  for(const child of q('#view-history').children)if(child!==q('#history-reader'))child.hidden=true;q('#history-reader').hidden=false;save();
  const request=sessionRequest,params=new URLSearchParams({start:'0',end:String(Math.floor(Date.now()/1000)+1),tool:row.tool,session:row.session});if(row.machine)params.set('machine',row.machine);
  apiFetch('/api/money?'+params).then(r=>{if(!r.ok)throw Error('Expense evidence unavailable');return r.json()}).then(d=>{if(request!==sessionRequest)return;const t=d.totals;q('#session-expense').innerHTML='';q('#session-expense').insertAdjacentHTML('beforeend','<div class="kpi"><div class="l">Full-session effective expense</div><div class="v">'+escape(effectiveCost(t))+'</div><div class="s">Whole billing-period allocation; separate from rack value.</div></div>')}).catch(e=>{if(request===sessionRequest)q('#session-work').textContent=e.message});
  apiFetch('/api/effectiveness?view=work&'+params).then(r=>{if(!r.ok)throw Error('Work evidence unavailable');return r.json()}).then(d=>{if(request!==sessionRequest)return;q('#session-work').innerHTML='<h3>Verified work</h3>'+((d.commits||[]).length?'<ul>'+(d.commits||[]).map(c=>'<li><code>'+escape(c.sha)+'</code> · '+escape(c.repo)+'</li>').join('')+'</ul>':'<p>No verified commit match for this session. Uncertain matches remain unassigned.</p>')+((d.prs||[]).length?'<ul>'+d.prs.map(p=>'<li>'+escape(p.title||'Untitled PR')+' · '+escape(p.state||'State not exposed')+'</li>').join('')+'</ul>':'')}).catch(e=>{if(request===sessionRequest)q('#session-work').textContent=e.message});
 }
 function metrics(data,row){
  if(selected?.session!==row.session||selected?.tool!==row.tool)return;const m=data.metrics;if(!m)return;
  q('#history-cost-summary').insertAdjacentHTML('afterbegin',[['Observed tokens',m.total_tokens==null?'Not exposed':fmt(m.total_tokens)],['Largest observed request',m.largest_request_total_tokens==null?'Not exposed':fmt(m.largest_request_total_tokens)]].map(([label,value])=>'<div class="kpi"><div class="l">'+escape(label)+'</div><div class="v">'+escape(value)+'</div></div>').join(''));
 }
 function initialize(){
  window.ergReady=true;const original=new URL(location.href);restoring=true;const key=read();showView(key);restoring=false;
  const p=original.searchParams;if(original.hash==='#session'&&p.get('session')&&p.get('tool'))openHistory({tool:p.get('tool'),session:p.get('session'),machine:p.get('session_machine')||'',title:''});else save(true);
  addEventListener('popstate',()=>{selected=null;restoring=true;const key=read();showView(key);restoring=false;const p=new URLSearchParams(location.search);if(location.hash==='#session'&&p.get('session')&&p.get('tool')){restoring=true;openHistory({tool:p.get('tool'),session:p.get('session'),machine:p.get('session_machine')||'',title:''});restoring=false}});
 }
 return {mount,view,openFromProject,read,save,initialize,money,projects,accounts,work,session,metrics,overviewProjects};
})();
