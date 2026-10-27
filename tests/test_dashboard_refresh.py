"""Hidden/background dashboards must not repeatedly rebuild heavy reports."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "JavaScript runtime unavailable")
class DashboardRefresh(unittest.TestCase):
    def test_history_load_and_background_tick_request_only_relevant_data(self):
        page = Path(__file__).resolve().parents[1] / "erg/static/index.html"
        code = r"""
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes=new Map(),requests=[],timers=[];
const document={hidden:false,querySelector:s=>{if(!nodes.has(s))nodes.set(s,{value:'',style:{},replaceChildren(){},setAttribute(){}});return nodes.get(s)},querySelectorAll:()=>[],addEventListener(){}};
const context={document,location:{hash:'#history'},history:{replaceState(){}},window:{scrollTo(){}},addEventListener(){},setInterval:fn=>timers.push(fn),setTimeout(){},clearTimeout(){},fetch:url=>{requests.push(url);return new Promise(()=>{})},Date,URLSearchParams,console};
vm.runInNewContext(script,context);
const initial=[...requests];document.hidden=true;timers.forEach(fn=>fn());const hidden=[...requests];
document.hidden=false;timers.forEach(fn=>fn());const visible=[...requests];
console.log(JSON.stringify({initial,hidden,visible}));
"""
        result = subprocess.run(
            ["node", "-e", code, str(page)], capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["initial"][0], "/api/history/discovery")
        self.assertTrue(data["initial"][1].startswith("/api/history/sessions?"))
        self.assertEqual(data["initial"][2:], ["/api/capture/status", "/api/delivery/status"])
        self.assertFalse(
            any(
                p in ("/api/history/status", "/api/history/reconciliation") for p in data["visible"]
            )
        )
        self.assertEqual(data["initial"], data["hidden"])
        self.assertEqual(data["visible"][-1], "/api/capture/status")
        self.assertFalse(
            any("/api/data" in p or "/api/effectiveness" in p for p in data["visible"])
        )

    def test_history_browsing_survives_pending_and_failed_health_requests(self):
        page = Path(__file__).resolve().parents[1] / "erg/static/index.html"
        code = r"""
(async()=>{
const fs=require('node:fs'),vm=require('node:vm'),nodes=new Map(),requests=[],pending=[];
const document={hidden:false,querySelector:s=>{if(!nodes.has(s))nodes.set(s,{value:'',style:{},replaceChildren(){},setAttribute(){}});return nodes.get(s)},querySelectorAll:()=>[],addEventListener(){}};
const context=vm.createContext({document,location:{hash:'#history'},history:{replaceState(){}},window:{scrollTo(){}},addEventListener(){},setInterval(){},setTimeout(){},clearTimeout(){},Date,URLSearchParams,console,fetch:url=>{requests.push(url);return new Promise((resolve,reject)=>pending.push({url,resolve,reject}))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').match(/<script>([\s\S]*?)<\/script>/)[1],context);
const tick=()=>new Promise(resolve=>setImmediate(resolve)),answer=(p,value)=>p.resolve({ok:true,json:async()=>value});
const sessions={sessions:[],more:false,next_cursor:null,through_seq:7,project_revision:1,project_catalog:[],folder_catalog:[],observed_tools:[]};
answer(pending.find(p=>p.url.startsWith('/api/history/sessions?')),sessions);await tick();await tick();
const renderedWhileHealthPending=nodes.get('#history-sessions').innerHTML.includes('No archived sessions match');
nodes.get('#history-audit').onclick();pending.find(p=>p.url==='/api/history/status').reject(new Error('health failed'));await tick();await tick();
const retryEnabled=!nodes.get('#history-audit').disabled,errorVisible=nodes.get('#history-audit-status').textContent.includes('health failed');
nodes.get('#history-audit').onclick();answer(pending.filter(p=>p.url==='/api/history/status').at(-1),{enabled:false});await tick();await tick();
const audit=pending.find(p=>p.url==='/api/history/reconciliation');
vm.runInContext('loadHistory()',context);await tick();
answer(pending.filter(p=>p.url==='/api/history/discovery').at(-1),{available:false});await tick();await tick();
answer(audit,{proof_scope:'late audit'});answer(pending.find(p=>p.url==='/api/history/discovery'),{available:true,observed_at:1,counts:{},current_state:'current'});await tick();await tick();
console.log(JSON.stringify({renderedWhileHealthPending,retryEnabled,errorVisible,health:nodes.get('#history-health').textContent,detail:nodes.get('#history-coverage').textContent,sessionError:nodes.get('#history-list-status').textContent,auditRequests:requests.filter(p=>p==='/api/history/reconciliation').length}));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run(["node", "-e", code, str(page)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["renderedWhileHealthPending"])
        self.assertTrue(data["retryEnabled"])
        self.assertTrue(data["errorVisible"])
        self.assertEqual(data["health"], "Discovery completeness has not been checked.")
        self.assertNotIn("late audit", data["detail"])
        self.assertEqual(data["auditRequests"], 1)

    def scenario(self, view, mode, steps):
        page = Path(__file__).resolve().parents[1] / "erg/static/index.html"
        code = r"""
const fs=require('node:fs'),vm=require('node:vm');
(async()=>{
 const html=fs.readFileSync(process.argv[1],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
 const nodes=new Map(),requests=[];let now=Date.now();
 const document={hidden:false,querySelector:s=>{if(!nodes.has(s))nodes.set(s,{value:'',style:{},replaceChildren(){},setAttribute(){}});return nodes.get(s)},querySelectorAll:()=>[],addEventListener(){}};
 const dataset={me:[],llm_cols:[],llm:[],edit_cols:[],edits:[],prompt_cols:[],prompts:[],commit_cols:[],commits:[],metric_cols:[],metrics:[],note_cols:[],notes:[],session_cols:['tool','session','last_ts','project','usage_status'],sessions:[['codex','s',1000,'p','observed']],source_cols:[],sources:[],generated:1,backend:'sqlite'};
 const report={available:true,generated:1,sessions:[],accounts:[],prs:[],commits:[],subscription_periods:[],quota_latest:[],quota_counts:{},valuation:{priced_calls:0,unpriced_calls:0,rate_card_dates:[]}};
 const fetch=url=>{requests.push(url);if(url.startsWith('/api/effectiveness')){if(process.argv[3]==='hold')return new Promise(()=>{});const view=new URLSearchParams(url.split('?')[1]).get('view');const shape=view==='accounts'?{available:true,generated:1,valuation:report.valuation,account_registry:[],subscription_periods:[],quota_counts:{},quota_latest:[]}:view==='capture'?{available:true,generated:1,sessions:[],subscription_period_count:0}:{available:true,generated:1,prs:[],commits:[],commit_attributed_calls:0,total_calls:0,attributed_calls:0,pre_pr_calls:0,unattributed_calls:0};return Promise.resolve({ok:process.argv[3]!=='error',status:503,json:async()=>shape})}if(url.startsWith('/api/data'))return Promise.resolve({ok:true,json:async()=>dataset});return new Promise(()=>{})};
 const context=vm.createContext({document,location:{hash:'#'+process.argv[2]},history:{replaceState(){}},window:{scrollTo(){}},addEventListener(){},setInterval(){},setTimeout(){},clearTimeout(){},fetch,Date:class extends Date{static now(){return now}},URLSearchParams,console});
 vm.runInContext(script,context);
 // Suppress chart rendering only; actual loading, work/account rendering,
 // session rendering and navigation still execute against the served source.
 vm.runInContext('render=()=>{};buildControls=()=>{};renderCapture=()=>{};',context);
 const tick=()=>new Promise(resolve=>setImmediate(resolve));await tick();await tick();
 const initial=[...requests];
STEPS
})().catch(error=>{console.error(error);process.exitCode=1});
""".replace("STEPS", steps)
        result = subprocess.run(
            ["node", "-e", code, str(page), view, mode], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_work_and_accounts_skip_usage_and_reuse_report_across_navigation(self):
        for view in ("work", "accounts"):
            data = self.scenario(
                view,
                "ready",
                r"""
vm.runInContext("showView('accounts');showView('work');",context);await tick();
const navigated=[...requests],accountText=nodes.get('#value-summary').textContent,workText=nodes.get('#pr-summary').textContent;
now+=900001;vm.runInContext("showView('accounts');",context);await tick();await tick();const expired=[...requests];
nodes.get('#refresh-report').onclick();await tick();await tick();
console.log(JSON.stringify({initial,navigated,refreshed:requests,expired,accountText,workText}));
""",
            )
            self.assertEqual(len(data["initial"]), 4)
            self.assertEqual(data["initial"][3], "/api/delivery/status")
            self.assertEqual(data["initial"][2], "/api/reporting/status")
            self.assertTrue(data["initial"][0].startswith("/api/effectiveness?view=" + view))
            self.assertEqual(data["initial"][1], "/api/capture/status")
            if view == "work":
                self.assertIn("&start=", data["initial"][0])
            self.assertEqual(
                sum(p.startswith("/api/effectiveness?view=accounts") for p in data["navigated"]), 1
            )
            self.assertEqual(
                sum(p.startswith("/api/effectiveness?view=work") for p in data["navigated"]), 1
            )
            self.assertEqual(
                sum(p.startswith("/api/effectiveness?view=accounts") for p in data["expired"]), 2
            )
            self.assertTrue(
                data["refreshed"][-1].startswith("/api/effectiveness?view=accounts&start=")
            )
            self.assertTrue(data["refreshed"][-1].endswith("&refresh=1"))
            self.assertFalse(any("/api/data" in p for p in data["refreshed"]))
            self.assertIn("Observed token value", data["accountText"])
            self.assertIn("calls attributed", data["workText"])

    def test_capture_defers_full_metrics_and_coalesces_inflight_work_loads(self):
        data = self.scenario(
            "capture",
            "hold",
            r"""
vm.runInContext('sessionSection({h0:0});',context);
const table=nodes.get('#session-table').innerHTML;
nodes.get('#load-capture-metrics').onclick();nodes.get('#load-capture-metrics').onclick();
vm.runInContext("showView('accounts');",context);await tick();
console.log(JSON.stringify({initial,afterClick:requests,table,disabled:nodes.get('#load-capture-metrics').disabled}));
""",
        )
        self.assertEqual(
            data["initial"], ["/api/data?days=0", "/api/capture/status", "/api/delivery/status"]
        )
        self.assertEqual(data["afterClick"].count("/api/effectiveness?view=capture"), 1)
        self.assertEqual(
            sum(p.startswith("/api/effectiveness?view=accounts") for p in data["afterClick"]), 1
        )
        self.assertEqual(data["table"].count("On demand"), 3)
        self.assertTrue(data["disabled"])

    def test_accounts_errors_are_visible_and_allow_retry_without_loading_usage(self):
        data = self.scenario(
            "accounts",
            "error",
            r"""
console.log(JSON.stringify({initial,error:nodes.get('#value-summary').textContent,disabled:nodes.get('#refresh-report').disabled}));
""",
        )
        self.assertTrue(data["initial"][0].startswith("/api/effectiveness?view=accounts&start="))
        self.assertEqual(data["initial"][1], "/api/capture/status")
        self.assertIn("503", data["error"])
        self.assertFalse(data["disabled"])
