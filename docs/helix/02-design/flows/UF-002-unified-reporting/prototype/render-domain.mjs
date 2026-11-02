import {readFileSync,writeFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const sdk=process.env.ERG_UX_BUILDER_ROOT;
if(!sdk)throw Error('Set ERG_UX_BUILDER_ROOT to the UX Builder 0.14.0 checkout');
const require=createRequire(sdk+'/package.json');
const {createElement:h}=await import(pathToFileURL(require.resolve('react')));
const {renderToStaticMarkup:render}=await import(pathToFileURL(require.resolve('react-dom/server')));
const ui=await import(pathToFileURL(sdk+'/packages/ui/dist/index.js'));
const P=await import(pathToFileURL(sdk+'/packages/ui/dist/patterns.js'));
const root=new URL('../',import.meta.url).pathname.replace(/\/$/,'');
const bp=JSON.parse(readFileSync(root+'/app.blueprint.json'));
const link=(label,to,size)=>h('a',{href:`${to}.${size}.html`},label);
const note=(title,body)=>h(ui.Notice,{title,tone:'info'},body);
const projects=[{id:'foundation',name:'Capture foundation',tokens:'12.0M',rack:'$84.00',expense:'$30.00',output:'2 verified commits'},{id:'console',name:'Operator console',tokens:'4.0M',rack:'$24.00',expense:'$12.00',output:'1 merged PR'},{id:'unassigned',name:'Unassigned',tokens:'1.0M',rack:'$6.00',expense:'Awaiting payer evidence',output:'No verified match'}];
const sessions=[{id:'demo-pi-001',title:'Preserve agent history',agent:'Pi · gateway',tokens:'8.0M',rack:'$56.00',expense:'$20.00'},{id:'demo-codex-002',title:'Reconcile capture checkpoints',agent:'Codex · subscription',tokens:'4.0M',rack:'$28.00',expense:'$10.00'}];
const table=(caption,columns,rows)=>h(ui.DataTable,{caption,columns,rows});
const details=(title,text)=>h('details',null,h('summary',null,title),h('p',null,text));
for(const size of bp.supports.sizeClasses){
 const projectTable=()=>table('Synthetic project usage and cost',[
 {key:'name',label:'Human project',render:r=>r.id==='foundation'?link(r.name,'project',size):r.name},{key:'tokens',label:'Tokens'},{key:'rack',label:'Rack value · est.'},{key:'expense',label:'Effective expense'},{key:'output',label:'Verified output'}],projects);
 const sessionTable=()=>table('Synthetic captured sessions',[
 {key:'title',label:'Session',render:r=>r.id==='demo-pi-001'?link(r.title,'session',size):r.title},{key:'agent',label:'Agent / route'},{key:'tokens',label:'Tokens'},{key:'rack',label:'Rack value · est.'},{key:'expense',label:'Effective expense'}],sessions);
 const common=()=>note('Local · synthetic', 'Last 7 days · all projects · this machine · allocation A. Static scope for this navigation prototype.');
 const tiles=[['Observed tokens','17.0M'],['Rack-rate value · estimate','$114.00'],['Effective expense · known portion','$42.00'],['Verified output','2 commits · 1 merged PR']].map(([label,value])=>h(P.StatTile,{key:label,label,value}));
 const gaps=()=>details('1 evidence gap · 1.0M tokens affected','The Unassigned cohort has a model rate but no supported inference payer. Rack-rate value is $6.00. Effective expense is unavailable until payer and billing evidence are associated; these tokens remain in total usage.');
 const work=()=>table('Synthetic verified work',[{key:'name',label:'Work item'},{key:'state',label:'Evidence'},{key:'value',label:'Attribution'}],[{id:'c1',name:'a1b2c3d · Preserve Pi checkpoints',state:'Verified commit; no PR required',value:'One linked session'},{id:'c2',name:'e4f5a6b · Add capture reconciliation',state:'Verified commit',value:'One linked session'},{id:'pr1',name:'Improve operator navigation',state:'Merged PR · synthetic',value:'One project; no invented feature count'}]);
 for(const screen of bp.screens){
  const actions=h(P.Cluster,null,...screen.actions.filter(a=>!a.to.startsWith('state:')).map(a=>h(ui.Button,{key:a.id},a.label)));
  let body;
  if(screen.id==='overview')body=h(P.Dashboard,{title:'Overview',tiles,actions,filters:common()},h(P.Stack,null,h('h2',null,'Where usage and expense went'),projectTable(),gaps()));
  if(screen.id==='projects')body=h(P.Stack,null,common(),h(P.CollectionList,{title:'Projects',actions,columns:[{key:'name',label:'Human project',render:r=>r.id==='foundation'?link(r.name,'project',size):r.name},{key:'tokens',label:'Tokens'},{key:'rack',label:'Rack value · est.'},{key:'expense',label:'Effective expense'},{key:'output',label:'Verified output'}],rows:projects,caption:'Synthetic human projects'}),gaps());
  if(screen.id==='project')body=h(P.RecordDetail,{title:'Capture foundation',actions,fields:[{label:'Tokens',value:'12.0M'},{label:'Rack-rate value · estimate',value:'$84.00'},{label:'Effective allocated expense',value:'$30.00'},{label:'Allocation',value:'Revision A · all usage assigned'}]},h(P.Stack,null,common(),h('h2',null,'Folders and agent projects'),h('p',null,'erg / capture · 12.0M tokens · $84.00 rack value · $30.00 effective expense'),h('h2',null,'Sessions'),sessionTable(),h('h2',null,'Work produced'),work(),details('How expense is allocated','Synthetic account period: $100 subscription charge; whole-period observed subscription rack value: $280. This project contributes $28 subscription rack value, allocating $10; $20 reported gateway charges bring effective expense to $30. This is policy allocation, not a provider line-item charge. Incomplete period coverage changes the allocation.')));
  if(screen.id==='sessions')body=h(P.Stack,null,common(),h(P.CollectionList,{title:'Sessions',actions,columns:[{key:'title',label:'Session title',render:r=>r.id==='demo-pi-001'?link(r.title,'session',size):r.title},{key:'id',label:'Session ID'},{key:'agent',label:'Agent / route'},{key:'tokens',label:'Tokens'},{key:'rack',label:'Rack value · est.'},{key:'expense',label:'Effective expense'}],rows:sessions,caption:'Synthetic session history'}));
  if(screen.id==='session')body=h(P.RecordDetail,{title:'Preserve agent history',actions,fields:[{label:'Session ID',value:'demo-pi-001'},{label:'Captured time',value:'Illustrative day · 09:00–09:30 local'},{label:'Project',value:'Capture foundation → erg / capture'},{label:'Usage / peak context',value:'8.0M cumulative tokens · 96K largest observed request'},{label:'Rack-rate value · estimate',value:'$56.00'},{label:'Effective expense',value:'$20.00 · allocated; synthetic'}]},h(P.Stack,null,common(),details('Route and payer evidence','Synthetic session-time route: gateway workspace and region. A separate payer binding supports this example allocation. Native owner identity does not prove payer.'),h('h2',null,'Session timeline'),table('Synthetic trace',[{key:'time',label:'Time'},{key:'role',label:'Kind'},{key:'text',label:'Content'}],[{id:'i1',time:'09:00',role:'Human input',text:'Preserve sessions before source logs are pruned.'},{id:'i2',time:'09:02',role:'Tool activity',text:'Inspect checkpoint behavior in the repository.'},{id:'i3',time:'09:30',role:'Agent response',text:'Checkpoint changes and verification complete (illustrative).'}]),h('h2',null,'Linked work'),work()));
  if(screen.id==='accounts')body=h(P.Stack,null,common(),h(P.CollectionList,{title:'Accounts',columns:[{key:'name',label:'Account / plan'},{key:'remaining',label:'Current availability'},{key:'reset',label:'Next reset'},{key:'charge',label:'Recorded charge'},{key:'value',label:'Selected-period value / expense'}],rows:[{id:'sub',name:'Example subscription · synthetic',remaining:'5-hour: 60% left · weekly: 85% left',reset:'In 2h · weekly in 4d',charge:'$100 / billing month · illustrative receipt',value:'$28 rack · $10 allocated'},{id:'gateway',name:'Example gateway · synthetic',remaining:'Provider quota not exposed',reset:'No reset evidence',charge:'Metered usage separately reported',value:'$80 rack · $32 reported'}],caption:'Synthetic accounts and availability'}),note('Current capacity','Quota observations are 1 minute old in this scenario. Scheduled quota resets do not establish purchased resets. No purchase or reset can be triggered here.'),details('Subscription charges and reset history','Synthetic billing month: $100. No purchased-reset receipt in this scenario. An observed weekly reset is availability evidence, not a cash charge.'),gaps());
  if(screen.id==='system')body=h(P.ReportViewer,{title:'System',applied:[{label:'Source',value:'Local · synthetic'}],parameters:h('p',null,'Diagnostics use the current machine. Read-only navigation prototype.'),results:table('Synthetic service health',[{key:'name',label:'Component'},{key:'state',label:'Status'},{key:'freshness',label:'Progress / freshness'}],[{id:'capture',name:'Capture',state:'Running · synthetic',freshness:'Claude / Codex / Pi observed'},{id:'delivery',name:'Delivery',state:'Waiting for receipt · synthetic',freshness:'12 accepted; 3 not yet verified in Delta'},{id:'reporting',name:'Reporting',state:'Ready · synthetic',freshness:'Through 09:30; allocation revision A'},{id:'machine',name:'Machine correlations',state:'Available in full product',freshness:'CPU, memory and lagged activity: secondary diagnostics'}])});
  const nav=bp.destinations.filter(d=>d.id!=='system').map(d=>({...d,href:`${d.screen}.${size}.html`}));
  const app=render(h(P.AppShell,{nav,current:screen.id==='project'?'projects':screen.id==='session'?'sessions':screen.id,brand:h('strong',null,'erg'),actions:link('System','system',size)},h(P.Page,null,body)));
  const file=`${root}/prototype/${screen.id}.${size}.html`;
  let html=readFileSync(file,'utf8');
  const start=html.indexOf('<div class="piq-shell');
  const end=html.indexOf('<script>(() => { const go =',start);
  if(start<0||end<0)throw Error('Unexpected prototype frame');
  html=html.slice(0,start)+app+'\n'+html.slice(end);
  writeFileSync(file,html);
 }
}
console.log('Domain content rendered with SDK components on 28 ideal-state pages');
