"""Exercise real dashboard scope and valuation code without a browser or network."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "JavaScript runtime unavailable")
class UniversalDashboard(unittest.TestCase):
    def run_ui(self, steps, view="usage"):
        page = Path(__file__).resolve().parents[1] / "erg/static/index.html"
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
(async()=>{
 const html=fs.readFileSync(process.argv[1],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
 const nodes=new Map(),requests=[],held=[],posts=[];let hold=false,revision=1;
 const document={hidden:false,querySelector:s=>{if(!nodes.has(s))nodes.set(s,{value:'',style:{},replaceChildren(){},setAttribute(){}});return nodes.get(s)},querySelectorAll:()=>[],addEventListener(){}};
 const catalog=[{project_key:'p',name:'Human project',state:'active'},{project_key:'q',name:'Other',state:'active'}];
 const folders=[{folder_key:'folder:f',name:'/work/app',kind:'folder',machine:'host',project_keys:['p']},{folder_key:'folder:g',name:'/work/other',kind:'folder',machine:'host',project_keys:['q']}];
 const valuation={priced_calls:1,unpriced_calls:1,known_value_low_usd:1,known_value_high_usd:2,rate_card_dates:['fixture']};
 const dataset={me:[],llm_cols:['hour','machine','tool','project','model','kind','fresh_in','cache_read','cache_write','out','calls','requester','folder','value_low_usd','value_high_usd','priced_calls','unpriced_calls'],llm:[[472222,'host','pi','p','m','main',10,0,0,5,1,'','folder:f',1,2,1,0],[472222,'host','pi','q','unknown','main',20,0,0,5,1,'','folder:g',0,0,0,1]],edit_cols:[],edits:[],prompt_cols:[],prompts:[],commit_cols:[],commits:[],metric_cols:[],metrics:[],note_cols:[],notes:[],session_cols:[],sessions:[],source_cols:[],sources:[],generated:1700000000,backend:'sqlite',machines:['host'],project_catalog:catalog,folder_catalog:folders};
 const work={available:true,generated:1700000000,prs:[],commits:[],commit_attributed_calls:0,total_calls:2,attributed_calls:0,pre_pr_calls:0,unattributed_calls:2,valuation,project_catalog:catalog,folder_catalog:folders,observed_tools:['pi']};
 const total={tokens:15,request_equivalents:1,priced_equivalents:1,unpriced_equivalents:0,known_value_low_usd:1,known_value_high_usd:2,reported_equivalents:0,known_reported_usd:0,price_coverage:1};
 const costs={available:true,generated:1700000000,revision:1,catalog,folder_catalog:folders,observed_tools:['pi'],totals:total,projects:[{...total,project_key:'p',name:'Human project'}],folders:[{...total,project_key:'p',folder_key:'folder:f',name:'/work/app',kind:'folder',machine:'host',session_count:1}],sessions:[{...total,tool:'pi',session:'s',title:'Session <title>'}],session_count:1,warnings:[],binding_count:1,unassigned_reasons:{}};
 const response=value=>({ok:true,json:async()=>JSON.parse(JSON.stringify(value))});
 const fetch=(url,options)=>{requests.push(url);if(url==='/api/projects'&&options?.method==='POST'){
 const body=JSON.parse(options.body);posts.push(body);
 if(body.action==='create'){catalog.push({project_key:body.project_key,name:body.name,state:'active'});costs.revision=++revision;return Promise.resolve(response({available:true,applied:true,revision,catalog}))}
 const mutation={...body,binding_id:body.binding_id||'local:preview',revision};delete mutation.apply;
 if(!body.apply)return Promise.resolve(response({available:true,revision,catalog,mutation,preview:{before:costs.projects,after:[{...total,project_key:body.project_key}],unassigned_reasons:{}}}));
 costs.revision=++revision;costs.projects=[{...total,project_key:body.project_key,name:catalog.find(p=>p.project_key===body.project_key).name}];folders[0].project_keys=[body.project_key];return Promise.resolve(response({available:true,applied:true,revision,catalog,mutation}));
 }if(url.startsWith('/api/data'))return Promise.resolve(response(dataset));if(url.startsWith('/api/effectiveness')){if(hold)return new Promise(resolve=>held.push({url,resolve}));return Promise.resolve(response(work))}if(url.startsWith('/api/costs'))return Promise.resolve(response(costs));if(url.startsWith('/api/history/status'))return Promise.resolve(response({enabled:true,enrolled_at:1,events:1,source_states:{},projects:[]}));if(url.startsWith('/api/history/reconciliation'))return Promise.resolve(response({counts:{},proof_scope:'fixture'}));if(url.startsWith('/api/history/sessions'))return Promise.resolve(response({sessions:[],more:false,next_cursor:null,through_seq:1,project_revision:1,project_catalog:catalog,folder_catalog:folders,observed_tools:['pi','codex','claude']}));return new Promise(()=>{})};
 const context=vm.createContext({document,location:{hash:'#'+process.argv[2]},history:{replaceState(){}},window:{scrollTo(){}},addEventListener(){},setInterval(){},setTimeout(){},clearTimeout(){},fetch,Date,URLSearchParams,console});
 vm.runInContext(script,context);const actualRender=vm.runInContext('render',context);vm.runInContext('render=()=>{};renderCapture=()=>{};',context);
 const tick=()=>new Promise(resolve=>setImmediate(resolve));await tick();await tick();
STEPS
})().catch(error=>{console.error(error);process.exitCode=1});
""".replace("STEPS", steps)
        result = subprocess.run(
            ["node", "-e", script, str(page), view], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_usage_work_and_costs_share_project_folder_time_and_machine(self):
        result = self.run_ui(r"""
vm.runInContext("S.project='p';S.folder='folder:f';S.machine='host';S.range='3d';scopeChanged();table({h0:0},MEASURES.all[1]);",context);
const breakdown=nodes.get('#tbl').innerHTML;
vm.runInContext("showView('work');",context);await tick();await tick();
const workCards=nodes.get('#work-kpis').innerHTML;
vm.runInContext("showView('costs');",context);await tick();await tick();
console.log(JSON.stringify({requests,breakdown,workCards,rollup:nodes.get('#cost-project-list').innerHTML,breadcrumbs:nodes.get('#cost-breadcrumbs').innerHTML,sessions:nodes.get('#cost-session-table').innerHTML}));
""")
        from urllib.parse import parse_qs, urlsplit

        work = next(p for p in result["requests"] if p.startswith("/api/effectiveness"))
        costs = next(p for p in result["requests"] if p.startswith("/api/costs"))
        work_params = parse_qs(urlsplit(work).query)
        work_params.pop("view")
        self.assertEqual(work_params, parse_qs(urlsplit(costs).query))
        self.assertEqual(work_params["project_key"], ["p"])
        self.assertEqual(work_params["folder_key"], ["folder:f"])
        self.assertEqual(work_params["machine"], ["host"])
        self.assertIn("Rack-rate value · est.", result["breakdown"])
        self.assertIn("Effective expense · allocated", result["breakdown"])
        self.assertIn("/work/app", result["breakdown"])
        self.assertNotIn("Other</td>", result["breakdown"])
        self.assertIn("Token value · est.", result["workCards"])
        self.assertIn("50% priced", result["workCards"])
        self.assertIn("/work/app", result["rollup"])
        self.assertIn("Human project", result["breadcrumbs"])
        self.assertIn("Session &lt;title&gt;", result["sessions"])

    def test_old_work_response_cannot_replace_new_scope(self):
        result = self.run_ui(r"""
hold=true;vm.runInContext("showView('work');",context);await tick();
vm.runInContext("S.project='p';scopeChanged();",context);await tick();
const latest=held.find(r=>new URLSearchParams(r.url.split('?')[1]).get('project_key')==='p');
latest.resolve(response({...work,total_calls:77,valuation:{...valuation,known_value_low_usd:7,known_value_high_usd:7}}));await tick();await tick();
const newer=nodes.get('#work-kpis').innerHTML;
held.find(r=>r!==latest).resolve(response({...work,total_calls:999}));await tick();await tick();
console.log(JSON.stringify({newer,final:nodes.get('#work-kpis').innerHTML,requests}));
""")
        self.assertEqual(result["newer"], result["final"])
        self.assertIn("77 request equivalents", result["final"])
        self.assertNotIn("999 request equivalents", result["final"])

    def test_create_preview_and_apply_invalidates_all_project_views(self):
        result = self.run_ui(r"""
vm.runInContext("showView('costs');",context);await tick();await tick();
document.querySelector('#project-name').value='New human project';await nodes.get('#project-create').onsubmit({preventDefault(){}});await tick();
const created=posts[0].project_key;nodes.get('#binding-folder').value='folder:f';nodes.get('#binding-project').value=created;
await nodes.get('#folder-bind').onsubmit({preventDefault(){}});await tick();const previewText=nodes.get('#binding-preview-text').textContent;
const beforeApply=posts.length;await nodes.get('#binding-apply').onclick();await tick();
console.log(JSON.stringify({posts,beforeApply,previewText,status:nodes.get('#project-admin-status').textContent,breadcrumbs:nodes.get('#cost-breadcrumbs').innerHTML,previewHidden:nodes.get('#binding-preview').hidden}));
""")
        self.assertEqual(result["beforeApply"], 2)
        self.assertEqual(len(result["posts"]), 3)
        create, preview, apply = result["posts"]
        self.assertEqual(create["action"], "create")
        self.assertTrue(create["apply"])
        self.assertFalse(preview["apply"])
        self.assertTrue(apply["apply"])
        self.assertEqual(preview["project_key"], create["project_key"])
        self.assertEqual(apply["selectors"], preview["selectors"])
        self.assertEqual(apply["selectors"]["directory"], {"machine": "host", "root": "/work/app"})
        self.assertIn("New human project", result["previewText"])
        self.assertIn("retained", result["previewText"])
        self.assertIn("Usage, Work, Costs and Session history", result["status"])
        self.assertTrue(result["previewHidden"])
        self.assertIn("New human project", result["breadcrumbs"])

    def test_history_first_load_discovers_agents_without_fetching_usage(self):
        result = self.run_ui(
            r"""
await tick();await tick();console.log(JSON.stringify({requests,choices:nodes.get('#toolchips').innerHTML}));
""",
            view="history",
        )
        self.assertFalse(any(p.startswith("/api/data") for p in result["requests"]))
        for tool in ("pi", "codex", "claude"):
            self.assertIn('data-scope-tool="' + tool + '"', result["choices"])

    def test_warming_and_reallocation_do_not_recursively_refresh_or_show_zero(self):
        result = self.run_ui(r"""
context.render=actualRender;vm.runInContext('buildControls=()=>{};',context);
dataset.project_revision=1;dataset.rollup_status={enabled:true,ready:false,stale_policy:true};
vm.runInContext("updateScope({revision:2,catalog:[]});",context);const before=requests.length;
await vm.runInContext("load(true)",context);const usageRequests=requests.slice(before).filter(p=>p.startsWith('/api/data')).length;
costs.rollup_status={enabled:true,ready:true,stale_policy:true};const beforeCosts=requests.length;
await vm.runInContext("loadCosts()",context);
console.log(JSON.stringify({usageRequests,costRequests:requests.slice(beforeCosts).filter(p=>p.startsWith('/api/costs')).length,kpi:nodes.get('#kpis').innerHTML,status:nodes.get('#cost-status').textContent,projectRows:nodes.get('#cost-project-list').innerHTML}));
""")
        self.assertEqual(result["usageRequests"], 1)
        self.assertEqual(result["costRequests"], 1)
        self.assertIn("updating", result["kpi"])
        self.assertIn("updating", result["status"])
        self.assertEqual(result["projectRows"], "")

    def test_old_publication_cannot_overwrite_work_after_newer_costs(self):
        result = self.run_ui(r"""
hold=true;vm.runInContext("showView('work');",context);await tick();
vm.runInContext("updateScope({rollup_status:{updated_at:2}});",context);
held[0].resolve(response({...work,total_calls:999,rollup_status:{updated_at:1}}));await tick();await tick();
console.log(JSON.stringify({cards:nodes.get('#work-kpis').innerHTML,summary:nodes.get('#pr-summary').textContent}));
""")
        self.assertNotIn("999", result["cards"])
        self.assertIn("newer reporting snapshot", result["summary"])

    def test_usage_reload_follows_newer_shared_publication(self):
        result = self.run_ui(r"""
vm.runInContext("D.rollup_status={updated_at:1};updateScope({rollup_status:{updated_at:2}});",context);
dataset.rollup_status={updated_at:2};const before=requests.length;
vm.runInContext("showView('usage');",context);await tick();await tick();
console.log(JSON.stringify({requests:requests.slice(before),publication:vm.runInContext('D.rollup_status.updated_at',context)}));
""")
        self.assertTrue(any(p.startswith("/api/data") for p in result["requests"]))
        self.assertEqual(result["publication"], 2)

    def test_views_label_the_same_publication_despite_different_request_times(self):
        result = self.run_ui(r"""
work.generated=10;costs.generated=20;work.rollup_status={updated_at:1700000000,pending_hours:2};costs.rollup_status={...work.rollup_status};
vm.runInContext("showView('work');",context);await tick();await tick();const workStamp=nodes.get('#gen').textContent;
vm.runInContext("showView('costs');",context);await tick();await tick();console.log(JSON.stringify({workStamp,costStamp:nodes.get('#gen').textContent}));
""")
        self.assertEqual(result["workStamp"], result["costStamp"])
        self.assertIn("Report published", result["workStamp"])
        self.assertIn("2 hours updating", result["workStamp"])

    def test_bound_account_usage_is_visible_without_registry_metadata(self):
        result = self.run_ui(r"""
Object.assign(work,{account_registry:[],account_usage:[{account_key:'bound',provider:'gateway',calls:3,tokens:99,valuation}],subscription_periods:[],quota_counts:{},quota_latest:[]});
vm.runInContext("showView('accounts');",context);await tick();await tick();console.log(JSON.stringify({table:nodes.get('#account-table').innerHTML,summary:nodes.get('#value-summary').textContent}));
""")
        self.assertIn("gateway", result["table"])
        self.assertIn("99 tokens", result["table"])
        self.assertIn("3 calls", result["table"])
        self.assertIn("Session-time attribution", result["table"])

    def test_live_lag_does_not_replace_the_rendered_publication(self):
        result = self.run_ui(r"""
vm.runInContext("renderReportingLag({updated_at:9999999999,recent_usage_source_at:Date.now()/1000-600,pending_hours:5,capture:{sampled_at:Date.now()/1000,usage_pending:4,work_pending:300}})",context);
console.log(JSON.stringify({lag:nodes.get('#report-lag').textContent,publication:vm.runInContext('reportPublication',context)}));
""")
        self.assertIn("10m old", result["lag"])
        self.assertIn("Work pending 300", result["lag"])
        self.assertIn("New report available", result["lag"])
        self.assertNotEqual(result["publication"], 9999999999)

    def test_effective_expense_groups_use_same_project_shares_and_normalized_folders(self):
        result = self.run_ui(r"""
vm.runInContext(`moneyScope=scopeQuery().toString();moneyData={available:true,totals:{effective_cost_usd:40},rows:[{kind:'folder',key:JSON.stringify(['p','folder:f']),known_effective_cost_usd:24,effective_cost_calls:.6,missing_effective_cost_calls:0},{kind:'folder',key:JSON.stringify(['q','folder:f']),known_effective_cost_usd:16,effective_cost_calls:.4,missing_effective_cost_calls:0}]};`,context);
console.log(JSON.stringify(vm.runInContext("({folder:groupExpense('folder','folder:f'),total:groupExpense('none',undefined)})",context)));
""")
        self.assertEqual(result["folder"]["effective_cost_usd"], 40)
        self.assertEqual(result["folder"]["effective_cost_calls"], 1)
        self.assertEqual(result["total"]["effective_cost_usd"], 40)
