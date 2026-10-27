"""Human projects inherit temporal folders; every metric uses the same shares."""
import json
import unittest
from unittest.mock import patch
from erg import project_dataset,project_reports,project_scope,projects,report_scope,store
import test_projects as fixtures


class UniversalProjects(unittest.TestCase):
    setUp=fixtures.ProjectAccounting.setUp
    rule=fixtures.ProjectAccounting.rule
    configure=fixtures.ProjectAccounting.configure
    context=fixtures.ProjectAccounting.context
    usage=fixtures.ProjectAccounting.usage
    card=fixtures.ProjectAccounting.card

    def build(self):
        with patch.object(project_dataset.rates,'load',return_value=self.card()):return project_dataset.build(self.db)

    def objects(self,data,kind):
        names=data[{'edits':'edit','prompts':'prompt','commits':'commit'}.get(kind,kind)+'_cols']
        return [dict(zip(names,row)) for row in data[kind]]

    def test_native_agent_project_binding_is_machine_and_agent_scoped(self):
        self.configure([self.rule(selectors={'agent_project':{'machine':'host','tool':'pi','id':'folder-hint'}})])
        self.usage();scope=project_scope.Scope(self.db,300)
        row=dict(self.call,project='folder-hint')
        result=scope.resolve(row);self.assertEqual(result['allocations'],[dict(project_key='p',units=projects.UNITS)])
        for change in (dict(machine='other'),dict(tool='codex'),dict(project='other')):
            self.assertEqual(scope.resolve(dict(row,**change))['allocations'],[dict(project_key='@unassigned',units=projects.UNITS)])
        self.assertEqual(self.db.execute('SELECT project FROM llm').fetchone()[0],'folder-hint')

    def test_folder_identity_joins_agents_but_never_same_basename_elsewhere(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}})])
        self.context(cwd='/work/app/sub');scope=project_scope.Scope(self.db,300)
        with self.db:
            projects.observe_context(self.db,'host','codex','other',50,'other','/work/app/sub')
            projects.observe_context(self.db,'different','pi','s',50,'remote','/work/app/sub')
        scope=project_scope.Scope(self.db,300)
        a=scope.resolve(dict(self.call,project='app'))
        b=scope.resolve(dict(self.call,tool='codex',session='other',project='app'))
        remote=scope.resolve(dict(self.call,machine='different',project='app'))
        self.assertEqual(a['folder']['folder_key'],b['folder']['folder_key'])
        self.assertEqual(a['folder']['name'],'/work/app')
        self.assertNotEqual(a['folder']['folder_key'],remote['folder']['folder_key'])
        self.assertEqual(remote['allocations'][0]['project_key'],'@unassigned')

    def test_session_folder_moves_and_late_context_never_relabel_prior_usage(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}})])
        self.usage('before',50);self.context(100);self.usage('during',110)
        self.context(150,'/work/other');self.usage('after',160)
        rows=self.objects(self.build(),'llm')
        self.assertEqual(sum(r['calls'] for r in rows if r['project']=='p'),1)
        self.assertEqual(sum(r['calls'] for r in rows if r['project']=='@unassigned'),2)
        self.assertEqual(len({r['folder'] for r in rows}),3)

    def test_usage_inputs_edits_commits_and_costs_conserve_the_same_split(self):
        self.configure([self.rule(allocations=[dict(project_key='p',units=600000),dict(project_key='q',units=200000)])]);self.context();self.usage()
        store.ingest(self.db,[json.dumps(row) for row in [
            dict(t='prompt',uid='input',ts=100,tool='pi',session='s',project='folder-hint',chars=40,words=8,est_tokens=10),
            dict(t='edit',uid='edit',ts=100,tool='pi',session='s',project='folder-hint',doc=0,added=10,removed=5),
            dict(t='commit',uid='commit',ts=100,repo='github.com/a/r',project='folder-hint',agent='pi',added_code=10,removed_code=5,added_doc=0,removed_doc=0,files=1,doc_files=0)]],machine='host')
        data=self.build()
        for kind,metric in (('llm','calls'),('prompts','n'),('edits','n'),('commits','n')):
            totals={}
            for row in self.objects(data,kind):totals[row['project']]=totals.get(row['project'],0)+row[metric]
            self.assertEqual(totals,{'p':.6,'q':.2,'@unassigned':.2})
        with patch.object(project_reports.rates,'load',return_value=self.card()):cost=project_reports.report(self.db,0,300)
        rows=self.objects(data,'llm')
        for project in cost['projects']:
            selected=[r for r in rows if r['project']==project['project_key']]
            self.assertAlmostEqual(project['tokens'],sum(sum(r[n] for n in ('fresh_in','cache_read','cache_write','out')) for r in selected))
            self.assertAlmostEqual(project['known_value_low_usd'],sum(r['value_low_usd'] for r in selected))
        self.assertEqual(sum(r['session_count'] for r in cost['folders']),3)  # distinct counts across shares are not additive
        folder=cost['folders'][0]['folder_key']
        with patch.object(project_reports.rates,'load',return_value=self.card()):filtered=project_reports.report(self.db,0,300,project_key='p',folder_key=folder)
        self.assertEqual(filtered['totals']['request_equivalents'],.6)

    def test_value_is_computed_per_request_before_hourly_aggregation(self):
        self.configure([]);self.usage('a',fresh_in=150000,out=0);self.usage('b',fresh_in=150000,out=0)
        card=self.card();card['entries'][0]['tiers']=[dict(min_prompt_tokens=200000,input=2.0)]
        with patch.object(project_dataset.rates,'load',return_value=card):data=project_dataset.build(self.db)
        rows=self.objects(data,'llm');self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0]['value_low_usd'],.3)
        self.assertEqual(rows[0]['priced_calls'],2)
        self.assertEqual(rows[0]['reported_calls'],0)

    def test_legacy_heuristic_flag_does_not_override_canonical_request_identity(self):
        self.configure([]);self.usage()
        with self.db:self.db.execute('UPDATE llm SET dup=1')
        data=self.build();self.assertEqual(sum(r['calls'] for r in self.objects(data,'llm')),1)
        with patch.object(project_reports.rates,'load',return_value=self.card()):cost=project_reports.report(self.db,0,300)
        self.assertEqual(cost['totals']['request_equivalents'],1)

    def test_missing_folder_filter_is_an_evidence_bucket_not_a_shared_identity(self):
        self.configure([]);scope=project_scope.Scope(self.db,300)
        a=scope.resolve(dict(self.call,project=''));b=scope.resolve(dict(self.call,session='different',project=''))
        self.assertNotEqual(a['folder']['folder_key'],b['folder']['folder_key'])
        self.assertEqual(scope.weight(dict(self.call,project=''),folder_key='@unobserved')[0],projects.UNITS)
        self.assertEqual(scope.weight(dict(self.call,project='observed-agent-label'),folder_key='@unobserved')[0],0)

    def test_common_selection_keeps_weights_and_rejects_invalid_filters(self):
        self.configure([self.rule(selectors={'session':{'machine':'host','tool':'pi','id':'s'}},allocations=[dict(project_key='p',units=600000)])])
        scope=project_scope.Scope(self.db,300)
        select=report_scope.Selection(dict(start=50,end=150,project_key='p',machine='host',tools='pi,codex',requester='me'),identities=['operator'])
        row=dict(self.call,requester='operator');self.assertEqual(select.allocation(scope,row)[0],600000)
        for change in (dict(ts=150),dict(machine='elsewhere'),dict(tool='claude'),dict(requester='other')):
            self.assertEqual(select.allocation(scope,dict(row,**change))[0],0)
        for bad in (dict(start=True),dict(end=0),dict(unknown='x'),dict(tools='pi,,codex')):
            with self.assertRaises(ValueError):report_scope.Selection(bad)
