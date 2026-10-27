"""Worker-local preparation reuse must preserve pinned reporting evidence."""
import contextlib
import json
import os
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch
from tests import test_project_rollups as fixtures
from erg import store,project_rollups,project_reports,report_evidence,report_sqlite,report_scope,quota_projection,monetary

class Preparation(unittest.TestCase):
    setUp=fixtures.DurableProjectRollups.setUp
    add=fixtures.DurableProjectRollups.add
    canonical=fixtures.DurableProjectRollups.canonical
    def event(self,**row):store.ingest(self.db,[json.dumps(row)],machine='host')
    def publish(self,cache):
        self.canonical();project_rollups.rebuild(self.path,preparation_cache=cache)
        return json.loads(project_rollups.state(self.db)['evidence_metadata'])
    def test_call_only_batches_reuse_preparation_after_source_connections_close(self):
        cache=report_evidence.PreparationCache();self.add();self.publish(cache)
        self.add('two',ts=3702)
        with patch('erg.work.prepare_attribution',side_effect=AssertionError('Repeated work scan')),patch('erg.accounting.resolver',side_effect=AssertionError('Repeated account scan')),patch('erg.account_reports.quota_summary',side_effect=AssertionError('Repeated quota scan')),patch('erg.titles.rows',side_effect=AssertionError('Repeated title scan')):
            self.publish(cache)
        for component in ('work','account','quota','title'):self.assertGreater(cache.stats()['hits'].get(component,0),0)
        self.assertEqual(cache.stats()['components'],4)
        actual=project_rollups.costs(self.db);expected=project_reports.report(self.db,canonical_select=lambda cols:report_sqlite.calls_select(cols))
        self.assertEqual(actual['totals']['tokens'],expected['totals']['tokens'])
        self.assertEqual(actual['totals']['known_value_low_usd'],expected['totals']['known_value_low_usd'])
    def test_late_billing_reset_and_title_changes_refresh_cached_metadata(self):
        cache=report_evidence.PreparationCache();self.add();self.publish(cache)
        self.event(t='identity',uid='owner',ts=3600,tool='pi',session='s',account_key='account',provider='fixture',provenance='native_auth')
        self.event(t='subscription_charge',uid='fee',ts=3601,provider='fixture',account_key='account',period_start=3000,period_end=5000,usd=20)
        self.event(t='quota',uid='reset',ts=3700,tool='pi',session='s',account_key='account',window='reset_history',occurred_at=3690,event='reset_credit_used')
        with self.db:self.db.execute("INSERT INTO session_titles VALUES('host','pi','s','New title','fixture',20,3701)")
        metadata=self.publish(cache)
        self.assertEqual(metadata['subscription_periods'][0]['subscription_spend_usd'],20)
        self.assertEqual(metadata['quota_counts']['reset_credit_used'],1)
        self.assertEqual(metadata['session_titles'][0]['title'],'New title')
        self.assertAlmostEqual(monetary.report(self.db,report_scope.Selection({}))['totals']['effective_cost_usd'],20)
        self.event(t='subscription_charge',uid='fee',ts=3602,provider='fixture',account_key='account',period_start=3000,period_end=5000,usd=30)
        self.assertEqual(self.publish(cache)['subscription_periods'][0]['subscription_spend_usd'],30)
        self.assertAlmostEqual(monetary.report(self.db,report_scope.Selection({}))['totals']['effective_cost_usd'],30)
        self.assertGreater(cache.stats()['misses']['account'],1)
    def test_quota_raw_to_ready_and_replaced_reset_invalidate_cached_summary(self):
        cache=report_evidence.PreparationCache();self.add();self.publish(cache)
        self.event(t='quota',uid='reset',ts=3700,tool='pi',session='s',window='reset_history',occurred_at=3690,event='reset_credit_used')
        self.assertEqual(self.publish(cache)['quota_counts']['reset_credit_used'],1)
        while quota_projection.run(self.db,limit=10000,budget=1)['pending']:pass
        self.assertEqual(self.publish(cache)['quota_counts']['reset_credit_used'],1)
        self.event(t='quota',uid='reset',ts=3701,tool='pi',session='s',window='reset_history',occurred_at=3690,event='reset_credit_granted')
        metadata=self.publish(cache)
        self.assertEqual(metadata['quota_counts']['reset_credit_used'],0)
        self.assertEqual(metadata['quota_counts']['reset_credit_granted'],1)
        self.assertGreaterEqual(cache.stats()['misses']['quota'],3)
    def test_period_output_mutation_cannot_change_cached_inputs(self):
        cache=report_evidence.PreparationCache()
        self.event(t='subscription_charge',uid='fee',ts=3600,provider='fixture',account_key='account',period_start=3000,period_end=5000,usd=20)
        first=report_evidence.prepare(self.db,cache=cache);next(iter(first[2].values()))['subscription_spend_usd']=999
        second=report_evidence.prepare(self.db,cache=cache)
        self.assertEqual(second[3]['subscription_periods'][0]['subscription_spend_usd'],20)
    def test_cache_is_invalidated_for_other_databases_and_schema_changes(self):
        cache=report_evidence.PreparationCache()
        self.event(t='identity',uid='owner',ts=3600,tool='pi',session='s',account_key='account',provider='fixture')
        report_evidence.prepare(self.db,cache=cache)
        with contextlib.closing(store.connect(str(Path(self.temp.name)/'other.db'))) as other:
            with other:project_rollups.initialize(other)
            self.assertEqual(report_evidence.prepare(other,cache=cache)[3]['account_registry'],[])
        self.assertEqual(len(report_evidence.prepare(self.db,cache=cache)[3]['account_registry']),1)
        previous=cache.stats()['misses']['account']
        with self.db:self.db.execute('CREATE INDEX fixture_schema_change ON quota_events(limit_id)')
        report_evidence.prepare(self.db,cache=cache)
        self.assertEqual(cache.stats()['misses']['account'],previous+1)
        self.assertLessEqual(cache.stats()['components'],4)

    def test_work_scope_and_commit_evidence_refresh_closed_connection_resolvers(self):
        cache=report_evidence.PreparationCache();self.add();self.publish(cache)
        sha='c'*40
        self.event(t='work_context',uid='ctx',ts=3600,tool='pi',session='s',repo='github.com/a/app',scope_id='one',capture_version=5)
        self.event(t='work_evidence',uid='fact',ts=4000,tool='pi',session='s',repo='github.com/a/app',scope_id='one',sha=sha,capture_version=5,provenance='tool_commit')
        self.event(t='work_commit',uid='object',ts=4000,repo='github.com/a/app',sha=sha,added_code=12,provenance='git_object')
        self.publish(cache)
        self.assertEqual(report_evidence.report(self.db,'work',report_scope.Selection({}))['commit_attributed_calls'],1)
        self.event(t='work_context',uid='ctx',ts=3600,tool='pi',session='s',repo='github.com/a/app',scope_id='other',capture_version=5)
        self.publish(cache)
        self.assertEqual(report_evidence.report(self.db,'work',report_scope.Selection({}))['commit_attributed_calls'],0)
    def test_missing_change_clock_uses_fresh_preparation(self):
        cache=report_evidence.PreparationCache();report_evidence.prepare(self.db,cache=cache)
        with self.db:self.db.execute("DELETE FROM project_rollup_state WHERE key='preparation_generation'")
        self.event(t='identity',uid='new',ts=3600,tool='pi',session='s',account_key='account',provider='fixture')
        self.assertEqual(len(report_evidence.prepare(self.db,cache=cache)[3]['account_registry']),1)

    def test_replacing_ledger_with_same_clock_values_does_not_reuse_old_accounts(self):
        cache=report_evidence.PreparationCache();replacement=str(Path(self.temp.name)/'replacement.db')
        with contextlib.closing(sqlite3.connect(replacement)) as clone:self.db.backup(clone)
        self.event(t='identity',uid='owner',ts=3600,tool='pi',session='s',account_key='first',provider='fixture')
        self.assertEqual(report_evidence.prepare(self.db,cache=cache)[3]['account_registry'][0]['account_key'],'first')
        with contextlib.closing(store.connect(replacement,initialize=False)) as clone:
            store.ingest(clone,[json.dumps(dict(t='identity',uid='owner',ts=3600,tool='pi',session='s',account_key='second',provider='fixture'))],machine='host')
            self.assertEqual(project_rollups.state(clone)['evidence_account_serial'],project_rollups.state(self.db)['evidence_account_serial'])
        self.db.close();os.replace(replacement,self.path)
        with contextlib.closing(store.open_readonly(self.path)) as db:
            self.assertEqual(report_evidence.prepare(db,cache=cache)[3]['account_registry'][0]['account_key'],'second')
