"""Quota summaries match retained source history, including replay and aliases."""
import sqlite3
import unittest
from unittest.mock import patch
from erg import store,accounting,account_reports,quota_projection


class QuotaSummary(unittest.TestCase):
    def setUp(self):
        self.db=store.initialize_db(sqlite3.connect(':memory:'));self.addCleanup(self.db.close)

    def put(self,uid='a',machine='host',**extra):
        row={k:None if k in accounting.NUMERIC else '' for k in accounting.EVENTS['quota'].split()}
        row.update(uid=uid,ts=100,tool='pi',session='s',provider='p',window='weekly',limit_id='usage',event='snapshot',provenance='fixture')
        row.update(extra);cols=['machine']+list(row)
        with self.db:self.db.execute('INSERT OR REPLACE INTO quota_events('+','.join(cols)+') VALUES('+','.join('?' for c in cols)+')',[machine]+list(row.values()))

    def drain(self):
        for unused in range(200):
            if not quota_projection.run(self.db,limit=3,budget=1)['pending']:return
        self.fail('quota projection did not drain')

    def parity(self,root=lambda k:k):
        self.assertTrue(quota_projection.ready(self.db))
        self.assertEqual(quota_projection.summary(self.db,root),account_reports.raw_quota_summary(self.db,root))

    def test_counts_latest_resets_and_invalid_history_preserve_source_semantics(self):
        self.put('a',account_key='account',used_percent=10)
        self.put('b',ts=200,account_key='account',event='window_exhausted',used_percent=100)
        self.put('c',ts=150,account_key='account',window='reset_history',occurred_at=120,event='reset_credit_used')
        self.put('d',window='reset_history',event='reset_credit_granted')
        self.assertIsNone(quota_projection.summary(self.db,lambda k:k));self.drain();self.parity()
        value=quota_projection.summary(self.db,lambda k:k)
        self.assertEqual(value['quota_counts']['snapshot'],1);self.assertEqual(value['quota_counts']['reset_credit_granted'],0)
        self.assertEqual(value['quota_latest'][0]['uid'],'b');self.assertEqual(len(value['reset_history']),1)

    def test_cross_machine_uid_deduplication_replace_delete_and_counter_corrections(self):
        self.put('same',ts=100);self.put('same',machine='mirror',ts=200,event='limit_reached');self.drain();self.parity()
        self.assertEqual(quota_projection.summary(self.db,lambda k:k)['quota_counts']['limit_reached'],1)
        self.put('same',ts=300,event='reset_observed',account_key='changed');self.drain();self.parity()
        with self.db:self.db.execute("DELETE FROM quota_events WHERE machine='host'")
        self.assertFalse(quota_projection.ready(self.db));self.drain();self.parity()
        with self.db:self.db.execute('DELETE FROM quota_events')
        self.drain();self.parity();self.assertFalse(any(quota_projection.summary(self.db,lambda k:k)['quota_counts'].values()))

    def test_aliases_and_equal_timestamp_order_preserve_exact_reference_results(self):
        self.put('early',ts=0,account_key='a')
        self.put('other',ts=100,account_key='b')
        self.put('later',ts=100,account_key='a')
        self.put('unknown',ts=100,session='unknown')
        self.drain();self.parity();self.parity(lambda k:'merged' if k in ('a','b') else k)
        result=quota_projection.summary(self.db,lambda k:'merged' if k in ('a','b') else k)
        self.assertEqual(result['quota_latest'][0]['uid'],'other')
        # An exact replacement changes row order in the source; ties must follow it.
        self.put('other',ts=100,account_key='b');self.drain();self.parity(lambda k:'merged' if k in ('a','b') else k)

    def test_raw_fallback_when_partial_or_uninitialized_and_no_idle_writes(self):
        for n in range(8):self.put(str(n),ts=n,event='snapshot')
        self.assertGreater(quota_projection.run(self.db,limit=1,budget=1)['pending'],0)
        self.assertIsNone(quota_projection.summary(self.db,lambda k:k));self.drain();before=self.db.total_changes
        for unused in range(3):self.assertEqual(quota_projection.run(self.db)['processed'],0)
        self.assertEqual(self.db.total_changes,before)
        with self.db:self.db.execute("DELETE FROM history_config WHERE key='quota_summary_seed_v1'")
        self.assertIsNone(quota_projection.summary(self.db,lambda k:k))

    def test_transaction_rollback_keeps_projection_and_source_consistent(self):
        self.put();self.drain();before=quota_projection.summary(self.db,lambda k:k)
        self.db.execute('BEGIN');self.db.execute("UPDATE quota_events SET event='limit_reached'");self.db.rollback()
        self.assertTrue(quota_projection.ready(self.db));self.assertEqual(quota_projection.summary(self.db,lambda k:k),before)
        self.db.execute('BEGIN');self.db.execute('DELETE FROM quota_events')
        with self.assertRaises(ValueError):quota_projection.run(self.db)
        self.db.rollback();self.parity()

    def test_ready_summary_never_scans_source_history_in_python(self):
        self.put();self.drain()
        with patch.object(accounting,'rows',side_effect=AssertionError('full history read')):
            value=account_reports.finish(self.db,lambda k:k,[],{'priced_calls':0,'unpriced_calls':0,'known_value_low_usd':0,'known_value_high_usd':0,'rate_card_dates':set(),'rate_sources':set()}, {})
        self.assertEqual(value['quota_counts']['snapshot'],1)

    def test_existing_metadata_bootstrap_and_top_200_null_windows_match_reference(self):
        for event in ('insert','update','delete'):
            self.db.execute('DROP TRIGGER quota_summary_'+event)
        with self.db:self.db.execute("DELETE FROM history_config WHERE key='quota_summary_seed_v1'")
        for n in range(205):self.put(str(n),ts=n,session=str(n),window=None if n==0 else 'weekly')
        with self.db:quota_projection.initialize(self.db)
        self.assertEqual(quota_projection.status(self.db)['pending'],205);self.drain();self.parity()
        self.assertEqual(len(quota_projection.summary(self.db,lambda k:k)['quota_latest']),200)
        self.assertEqual(self.db.execute('SELECT count(*) FROM quota_events').fetchone()[0],205)

    def test_compaction_requalifies_source_order_and_stable_primary_keys(self):
        for n in range(12):self.put(str(n),session=str(n),ts=100)
        with self.db:self.db.execute("DELETE FROM quota_events WHERE uid IN ('0','1','2','3','4','5','6','7','8','9')")
        self.drain();self.parity();self.db.execute('VACUUM')
        self.assertIsNone(quota_projection.summary(self.db,lambda k:k))
        self.drain();self.parity()
        self.put('after-vacuum',session='after',ts=100);self.drain();self.parity()
