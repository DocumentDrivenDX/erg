"""Project edits are explicit metadata transactions, not trace changes."""
import contextlib
import io
import json
import tempfile
import threading
from pathlib import Path
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from erg import project_admin,projects,server,store
import test_projects as fixtures


class ProjectChanges(unittest.TestCase):
    setUp=fixtures.ProjectAccounting.setUp
    usage=fixtures.ProjectAccounting.usage
    context=fixtures.ProjectAccounting.context

    def create(self):return dict(action='create',project_key='local:ui',revision=0,name='Human project',apply=True)

    def test_project_creation_and_binding_retries_are_idempotent_and_trace_safe(self):
        self.usage();self.context();original=self.db.execute('SELECT * FROM llm').fetchall()
        first=project_admin.change(self.db,self.create());self.assertEqual(first['revision'],1)
        retry=project_admin.change(self.db,self.create());self.assertFalse(retry['changed']);self.assertEqual(retry['revision'],1)
        request=dict(action='bind',project_key='local:ui',binding_id='local:binding',revision=1,selectors=dict(directory=dict(machine='host',root='/work/app')),apply=False)
        changes=self.db.total_changes;preview=project_admin.change(self.db,request)
        self.assertEqual(self.db.total_changes,changes);self.assertEqual(preview['preview']['after'][0]['tokens'],15)
        self.assertEqual(projects.snapshot(self.db)['bindings'],[])
        applied=project_admin.change(self.db,dict(preview['mutation'],apply=True));self.assertEqual(applied['revision'],2)
        self.assertFalse(project_admin.change(self.db,dict(preview['mutation'],apply=True))['changed'])
        self.assertEqual(self.db.execute('SELECT * FROM llm').fetchall(),original)
        self.assertEqual(self.db.execute('SELECT count(*) FROM project_config_history').fetchone()[0],2)

    def test_stale_or_conflicting_changes_cannot_overwrite_metadata(self):
        project_admin.change(self.db,self.create());before=projects.snapshot(self.db)
        with self.assertRaises(projects.RevisionConflict):project_admin.change(self.db,dict(self.create(),project_key='local:other'))
        with self.assertRaises(projects.RevisionConflict):project_admin.change(self.db,dict(self.create(),name='Conflicting'))
        for bad in (dict(action='delete',revision=1),dict(self.create(),project_key='upstream:foreign'),dict(self.create(),apply='yes'),dict(self.create(),extra='unsupported')):
            with self.assertRaises(ValueError):project_admin.change(self.db,bad)
        self.assertEqual(projects.snapshot(self.db),before)

    def test_http_rejects_cross_origin_and_non_json_and_checks_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'erg.db');db=store.connect(path);db.close()
            created=[];ready=threading.Event();real=server.http.server.ThreadingHTTPServer
            def listener(address,handler):
                value=real(address,handler);created.append(value);ready.set();return value
            with patch.object(store,'DB_PATH',path),patch.object(server.http.server,'ThreadingHTTPServer',side_effect=listener),contextlib.redirect_stdout(io.StringIO()):
                runner=threading.Thread(target=server.serve,args=(0,),daemon=True);runner.start()
                try:
                    self.assertTrue(ready.wait(3));base='http://127.0.0.1:'+str(created[0].server_port)
                    def post(body,origin=None,content_type='application/json'):
                        headers={'Content-Type':content_type}
                        if origin:headers['Origin']=origin
                        request=urllib.request.Request(base+'/api/projects',data=json.dumps(body).encode(),headers=headers,method='POST')
                        with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)
                    for origin in ('https://evil.invalid','http://localhost:'+str(created[0].server_port)):
                        with self.assertRaises(urllib.error.HTTPError) as error:post(self.create(),origin)
                        self.assertEqual(error.exception.code,403)
                    with self.assertRaises(urllib.error.HTTPError) as error:post(self.create(),base,'text/plain')
                    self.assertEqual(error.exception.code,415)
                    result=post(self.create(),base);self.assertEqual(result['revision'],1)
                    self.assertFalse(post(self.create(),base)['changed'])
                    with self.assertRaises(urllib.error.HTTPError) as error:post(dict(self.create(),project_key='local:stale'),base)
                    self.assertEqual(error.exception.code,409)
                    with urllib.request.urlopen(base+'/api/projects',timeout=5) as response:catalog=json.load(response)
                    self.assertEqual(len(catalog['projects']),1);self.assertEqual(catalog['revision'],1)
                finally:
                    if created:created[0].shutdown();created[0].server_close()
                    runner.join(5)
