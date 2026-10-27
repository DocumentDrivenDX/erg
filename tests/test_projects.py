import copy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
from erg import history,project_reports as reports,projects,store


class ProjectAccounting(unittest.TestCase):
    def setUp(self):
        self.db=store.initialize_db(sqlite3.connect(':memory:'));self.addCleanup(self.db.close)
        self.config=dict(version=1,revision=0,projects=[dict(project_key='p',name='Product'),dict(project_key='q',name='Other')],bindings=[],aliases=[])
        self.call=dict(machine='host',tool='pi',session='s',ts=100)
    def rule(self,**extra):
        return dict(dict(binding_id='r',priority=10,selectors={'repository':'github.com/a/r'},allocations=[dict(project_key='p',units=1000000)],valid_from=0),**extra)
    def configure(self,rules):
        self.config['bindings']=rules;projects.apply(self.db,self.config);return projects.snapshot(self.db)
    def usage(self,uid='a',ts=100,**values):
        row=dict(t='llm',uid=uid,ts=ts,tool='pi',session='s',project='folder-hint',model='priced',fresh_in=10,cache_read=0,cache_write=0,out=5)
        row.update(values);store.ingest(self.db,[json.dumps(row)],machine='host')
    def context(self,ts=50,cwd='/work/app',repo='github.com/a/r'):
        with self.db:projects.observe_context(self.db,'host','pi','s',ts,'c:'+str(ts),cwd,repo)
    def card(self):
        return dict(aliases={},entries=[dict(model='priced',input=1.0,output=2.0,as_of='2026-01-01',source='fixture')])
    def report(self,**kw):
        with patch.object(reports.rates,'load',return_value=self.card()):return reports.report(self.db,0,300,**kw)
    def test_rename_alias_and_replay_preserve_identity_without_touching_usage(self):
        config=self.configure([self.rule()]);self.usage();self.context()
        first=self.report();config['projects'][0]['name']='Renamed';config['aliases']=[dict(authority='upstream',external_id='42',project_key='p')]
        projects.apply(self.db,config);current=projects.snapshot(self.db)
        self.assertFalse(projects.apply(self.db,current)['changed'])
        second=self.report();self.assertEqual(first['totals'],second['totals']);self.assertEqual(second['projects'][0]['name'],'Renamed')
        self.assertEqual(self.db.execute('select project from llm').fetchone()[0],'folder-hint')
    def test_split_partial_and_unknown_prices_conserve_request_and_value(self):
        self.configure([self.rule(allocations=[dict(project_key='p',units=600000),dict(project_key='q',units=200000)])]);self.context();self.usage();self.usage('b',model='unpriced')
        data=self.report();self.assertEqual(sum(r['tokens'] for r in data['projects']),data['totals']['tokens'])
        self.assertEqual(sum(r['request_equivalents'] for r in data['projects']),2)
        self.assertAlmostEqual(sum(r['known_value_low_usd'] for r in data['projects']),.000020)
        self.assertEqual(data['totals']['unpriced_equivalents'],1);self.assertIsNone(data['totals']['actual_billed_usd'])
        self.assertEqual({r['project_key']:r['request_equivalents'] for r in data['projects']},{'p':1.2,'q':.4,'@unassigned':.4})
    def test_conflicts_and_explicit_priority_have_no_hidden_specificity(self):
        config=projects.validate(dict(self.config,bindings=[self.rule(),self.rule(binding_id='second',allocations=[dict(project_key='q',units=1000000)])]))
        self.assertEqual(projects.Resolver(config).resolve(self.call,{'repo':'github.com/a/r'})['state'],'ambiguous')
        config['bindings'][1]['priority']=11
        self.assertEqual(projects.Resolver(config).resolve(self.call,{'repo':'github.com/a/r'})['allocations'][0]['project_key'],'q')
    def test_directory_component_boundary_and_temporal_scope(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}},valid_from=100,valid_to=200)])
        self.context(50,'/work/app2');self.usage('a',100);self.context(150,'/work/app/sub');self.usage('b',150);self.usage('c',200)
        data=self.report();self.assertEqual({r['project_key']:r['distinct_calls'] for r in data['projects']},{'p':1,'@unassigned':2})
    def test_session_rule_does_not_require_directory_or_repo_and_filter_uses_weights(self):
        self.configure([self.rule(selectors={'session':{'machine':'host','tool':'pi','id':'s'}},allocations=[dict(project_key='p',units=600000)])]);self.usage()
        data=self.report(project_key='p');self.assertEqual(data['totals']['request_equivalents'],.6);self.assertEqual(data['totals']['tokens'],9)
    def test_late_context_is_not_backdated_and_changed_worktree_clears_path(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}})])
        self.usage('before',50);self.context(100);self.usage('during',110)
        row=dict(t='work_context',uid='ctx',tool='pi',session='s',ts=120,repo='github.com/b/r',worktree=hashlib.sha256(b'/different').hexdigest()[:16])
        store.ingest(self.db,[json.dumps(row)],machine='host');self.usage('after',130)
        self.assertEqual({r['project_key']:r['distinct_calls'] for r in self.report()['projects']},{'p':1,'@unassigned':2})
    def test_stale_invalid_or_destructive_config_is_rejected_atomically(self):
        config=self.configure([self.rule()]);before=projects.snapshot(self.db)
        for bad in (dict(self.config,projects=[dict(p,name='Stale change') for p in self.config['projects']]),dict(config,projects=[]),dict(config,bindings=[self.rule(selectors={'title':'guess'})]),dict(config,bindings=[self.rule(allocations=[dict(project_key='p',units=1000001)])])):
            with self.assertRaises(ValueError):projects.apply(self.db,bad)
            self.assertEqual(projects.snapshot(self.db),before)
    def test_archive_preserves_historical_allocation_and_rejects_later_usage(self):
        self.config['projects'][0].update(state='archived',archived_at=150)
        self.configure([self.rule()]);self.context();self.usage('a',100);self.usage('b',200)
        self.assertEqual({r['project_key']:r['distinct_calls'] for r in self.report()['projects']},{'p':1,'@unassigned':1})
    def test_retained_context_index_and_expiration_do_not_import_source_bodies(self):
        with self.db:history.put(self.db,'codex','s','folder',100,'turn_context',dict(type='turn_context',payload=dict(cwd='/work/app',model='m')),'header',machine='host')
        self.assertEqual(self.db.execute('select cwd from project_contexts').fetchone()[0],'/work/app')
        with self.db:self.db.execute('delete from project_contexts')
        self.assertEqual(history.index_project_contexts(self.db)['examined'],1)
        self.assertEqual(self.db.execute('select count(*) from history_events').fetchone()[0],1)
    def test_filters_do_not_treat_unknown_costs_as_zero(self):
        self.configure([]);self.usage()
        data=self.report(machine='other');self.assertEqual(data['totals']['distinct_calls'],0)
        data=self.report();self.assertIsNone(data['totals']['allocated_subscription_usd']);self.assertEqual(data['totals']['reported_equivalents'],0)
        with self.assertRaises(ValueError):reports.report(self.db,300,100)

    def test_reserved_and_malformed_targets_do_not_enter_catalog(self):
        for bad in (dict(self.config,version=True),dict(self.config,projects=[dict(project_key='@unassigned',name='Bad')]),dict(self.config,bindings=[self.rule(allocations=[dict(project_key=[],units=1)])])):
            with self.assertRaises(ValueError):projects.validate(bad)
    def test_report_preserves_caller_transaction_and_releases_own_read_snapshot(self):
        self.configure([]);self.usage();self.report();self.assertFalse(self.db.in_transaction)
        self.db.execute('BEGIN');self.report();self.assertTrue(self.db.in_transaction);self.db.rollback()

    def test_retry_of_applied_configuration_is_idempotent_even_with_old_revision(self):
        original=projects.validate(dict(self.config,bindings=[self.rule()]))
        projects.apply(self.db,original)
        self.assertFalse(projects.apply(self.db,original)['changed'])
        self.assertEqual(self.db.execute('select count(*) from project_config_history').fetchone()[0],1)
    def test_grouping_does_not_reintroduce_gateway_or_local_mirrors(self):
        self.configure([self.rule(selectors={'session':{'machine':'host','tool':'pi','id':'s'}})])
        self.usage(request_id='shared',response_id='response')
        store.ingest_gateway(self.db,[dict(uid='g',ts=100,tool='gateway',session='g',model='priced',fresh_in=10,out=5,request_id='shared')],machine='gateway')
        store.ingest(self.db,[json.dumps(dict(t='llm',uid='local:i',ts=100,tool='local',session='inference:i',model='priced',fresh_in=10,out=5,response_id='response'))],machine='host')
        data=self.report();self.assertEqual(data['totals']['distinct_calls'],1);self.assertEqual(data['totals']['tokens'],15)
        self.assertEqual(data['projects'][0]['project_key'],'p')
    def test_synthetic_positive_usage_is_not_costed_or_allocated(self):
        self.configure([self.rule(selectors={'session':{'machine':'host','tool':'pi','id':'s'}})])
        self.usage(usage_kind='synthetic_history')
        self.assertEqual(self.report()['totals']['distinct_calls'],0)
