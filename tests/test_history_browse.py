"""Indexed browsing agrees with physical metadata under invalidation and races."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from erg import history,history_browse,projects,store
import test_projects as fixtures


class BrowseIndex(unittest.TestCase):
    rule=fixtures.ProjectAccounting.rule
    configure=fixtures.ProjectAccounting.configure
    context=fixtures.ProjectAccounting.context

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=str(Path(temp.name)/'ledger.db');self.db=store.connect(self.path);self.addCleanup(self.db.close)
        self.config=dict(version=1,revision=0,projects=[dict(project_key='p',name='Product'),dict(project_key='q',name='Other')],bindings=[],aliases=[])

    def put(self,uid,ts=100,tool='pi',session='s'):
        with self.db:history.put(self.db,tool,session,'folder-hint',ts,'message',{'text':'private'},uid,machine='host')

    def report(self,**filters):
        return history.scoped_sessions(self.db,dict(start=0,end=7200,**filters))

    def drain(self):
        for unused in range(10):
            if not history_browse.run(self.path,max_hours=4)['pending_hours']:return
        self.fail('Browse projection did not drain')

    def equal_raw(self,**filters):
        indexed=self.report(**filters)
        # Force exact raw fallback without changing source data or policy.
        with patch.object(history_browse,'VERSION',-1):raw=self.report(**filters)
        indexed.pop('browse_index');raw.pop('browse_index');self.assertEqual(indexed,raw)
        return indexed

    def test_tiles_preserve_temporal_allocation_splits_and_identity_filters(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}},valid_from=100,valid_to=150,allocations=[dict(project_key='p',units=600000),dict(project_key='q',units=200000)])])
        self.context(50)
        for ts in (99,100,101,149,150,151,3700):self.put(str(ts),ts=ts)
        self.context(151,'/work/other');self.put('other',tool='codex',session='other')
        self.drain()
        self.assertEqual(self.report()['browse_index'],dict(indexed_hours=2,raw_hours=0))
        for f in ({},{'project_key':'p'},{'project_key':'@unassigned'},{'tool':'pi','session':'s'},{'tools':'codex,pi'},{'machine':'absent'}):self.equal_raw(**f)
        self.assertEqual(self.report(project_key='p')['sessions'][0]['events'],3)
        folder=self.report(project_key='p')['sessions'][0]['folder_keys'][0]
        self.equal_raw(folder_key=folder)

    def test_partial_hours_and_old_pagination_boundaries_fall_back_exactly(self):
        self.put('first');upper=self.db.execute('SELECT max(seq) FROM history_events').fetchone()[0];self.put('later',ts=120);self.drain()
        data=history.scoped_sessions(self.db,dict(start=110,end=200));self.assertEqual(data['sessions'][0]['events'],1);self.assertEqual(data['browse_index']['raw_hours'],1)
        data=history.scoped_sessions(self.db,dict(start=0,end=3600),through_seq=upper)
        self.assertEqual(data['sessions'][0]['events'],1);self.assertEqual(data['browse_index']['raw_hours'],1)

    def test_append_expire_move_delete_and_link_changes_invalidate(self):
        self.put('a');self.drain();self.put('b');self.assertEqual(self.report()['browse_index']['raw_hours'],1);self.drain();self.equal_raw()
        with self.db:self.db.execute('UPDATE history_events SET expired_at=300,body=NULL WHERE ts=100')
        self.assertEqual(self.report()['browse_index']['raw_hours'],1);self.drain();self.assertEqual(self.report()['sessions'][0]['expired_events'],2)
        with self.db:self.db.execute('UPDATE history_events SET ts=3700 WHERE event_id=(SELECT event_id FROM history_events LIMIT 1)')
        self.drain();self.equal_raw()
        self.put('local',tool='local',session='inference:x');self.drain()
        with self.db:self.db.execute("INSERT INTO history_links VALUES('inference:x','pi','s','r',100)")
        self.assertEqual(self.report()['browse_index']['indexed_hours'],0);self.drain();self.equal_raw();self.assertEqual(len(self.report()['sessions']),1)
        with self.db:self.db.execute("DELETE FROM history_events WHERE tool='local'")
        self.drain();self.equal_raw()

    def test_late_context_and_policy_changes_invalidate_existing_tiles(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}})])
        self.put('one');self.drain();self.assertEqual(self.report(project_key='p')['sessions'],[])
        self.context(50);self.assertEqual(self.report()['browse_index']['raw_hours'],1);self.drain();self.assertEqual(len(self.report(project_key='p')['sessions']),1)
        cfg=projects.snapshot(self.db);cfg['bindings'][0]['allocations']=[dict(project_key='q',units=1000000)];projects.apply(self.db,cfg)
        self.assertEqual(self.report()['browse_index']['raw_hours'],1);self.drain();self.equal_raw(project_key='q')

    def test_rebuild_never_clears_concurrent_changes(self):
        self.put('first');original=history_browse.build
        def changed(source,hour,revision):
            result=original(source,hour,revision);self.put('concurrent',ts=110);return result
        with patch.object(history_browse,'build',changed):out=history_browse.run(self.path)
        self.assertEqual(out['processed'],0);self.assertEqual(out['pending_hours'],1)
        self.drain();self.assertEqual(self.report()['sessions'][0]['events'],2)

    def test_first_build_checks_context_and_link_races_without_existing_dependencies(self):
        self.put('first');original=history_browse.build
        for kind in ('context','link'):
            def changed(source,hour,revision):
                result=original(source,hour,revision)
                if kind=='context':self.context(50)
                else:
                    with self.db:self.db.execute("INSERT INTO history_links VALUES('new','pi','s','r',100)")
                return result
            with patch.object(history_browse,'build',changed):out=history_browse.run(self.path)
            self.assertEqual(out['processed'],0)
        self.drain();self.equal_raw()

    def test_body_free_reads_and_idle_worker_do_not_write(self):
        self.put('first');self.drain();before=self.db.execute('PRAGMA data_version').fetchone()[0]
        for unused in range(3):self.assertEqual(history_browse.run(self.path)['processed'],0)
        self.assertEqual(self.db.execute('PRAGMA data_version').fetchone()[0],before)
        self.db.set_authorizer(lambda a,t,c,*args:sqlite3.SQLITE_DENY if a==sqlite3.SQLITE_READ and t=='history_events' and c=='body' else sqlite3.SQLITE_OK)
        self.equal_raw()
        self.db.set_authorizer(None)

    def test_new_context_does_not_dirty_earlier_hours_and_catchup_is_fair(self):
        self.put('old',ts=100);self.put('new',ts=3700);self.drain()
        self.context(3701)
        self.assertEqual([r[0] for r in self.db.execute('SELECT hour FROM history_browse_dirty')],[1])
        # New hot-hour writes must not starve the oldest outstanding tile.
        with self.db:
            self.db.execute('INSERT INTO history_browse_dirty VALUES(0,1)')
            self.db.execute("INSERT OR REPLACE INTO history_config VALUES('browse_turn_v1','0')")
        for n in range(3):
            self.put('hot'+str(n),ts=3800+n);history_browse.run(self.path,max_hours=1)
        self.assertIsNone(self.db.execute('SELECT 1 FROM history_browse_dirty WHERE hour=0').fetchone())

    def test_replace_context_preserves_displaced_identity_invalidation_and_noop_is_idle(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}})])
        self.context(50);self.put('first');self.drain()
        with self.db:self.db.execute("INSERT OR REPLACE INTO project_contexts VALUES('host','pi','s','c:50',50,'/work/app','github.com/a/r')")
        self.assertEqual(history_browse.status(self.db)['pending_hours'],0)
        with self.db:self.db.execute("INSERT OR REPLACE INTO project_contexts VALUES('host','pi','s','c:50',3700,'/work/other','github.com/a/r')")
        self.assertEqual(history_browse.status(self.db)['pending_hours'],1)
        self.drain();self.equal_raw();self.assertEqual(self.report(project_key='p')['sessions'],[])

    def test_filtered_folder_exemplar_does_not_leak_another_sessions_context(self):
        self.configure([self.rule(selectors={'directory':{'machine':'host','root':'/work/app'}})])
        self.context(50)
        with self.db:
            projects.observe_context(self.db,'host','codex','other',50,'other','/work/app','github.com/other/repo')
            history.put(self.db,'codex','other','different-hint',100,'message',{'text':'other'},'other',machine='host')
        self.put('pi');self.drain()
        self.equal_raw(tool='pi',session='s');self.equal_raw(tool='codex')
        self.assertEqual(self.report(tool='pi')['folder_catalog'][0]['repository'],'github.com/a/r')

    def test_build_loads_only_hour_identities_and_linked_context(self):
        self.put('old',ts=100);self.put('new',ts=3700,session='later')
        self.put('server',tool='local',session='inference:x')
        with self.db:self.db.execute("INSERT INTO history_links VALUES('inference:x','codex','linked','r',100)")
        original=history_browse.project_scope.Scope;seen=[]
        def scoped(db,end,**kwargs):
            seen.append(kwargs.get('identities'));return original(db,end,**kwargs)
        with patch.object(history_browse.project_scope,'Scope',scoped):
            history_browse.build(self.db,0,projects.snapshot(self.db)['revision'])
        self.assertEqual(seen,[{('host','pi','s'),('host','local','inference:x'),('host','codex','linked')}])
