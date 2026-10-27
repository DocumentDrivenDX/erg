"""Bounded read-only PR markers must preserve REST refresh evidence and unknowns."""
import json
import subprocess
import unittest
from unittest.mock import patch
from erg import work
import test_effectiveness as fixtures

REPO='github.com/acme/r'

class GithubMetadataBatch(unittest.TestCase):
 def payload(self,numbers):
  return dict(data=dict(repository={'p%d'%i:dict(number=n,url='https://'+REPO+'/pull/'+str(n),title='PR '+str(n),updatedAt='2026-10-22T00:00:00Z') for i,n in enumerate(numbers)}))
 def test_batch_uses_one_read_only_query_and_verifies_requested_identities(self):
  with patch.object(work,'gh_json',return_value=self.payload([8,2])) as remote:
   rows=work.pull_heads(REPO,[8,2])
  self.assertEqual([r['number'] for r in rows],[8,2]);self.assertEqual(remote.call_count,1)
  args,kw=remote.call_args;self.assertEqual(args,(REPO,'graphql'))
  fields=kw['fields'];self.assertEqual((fields['owner'],fields['name']),('acme','r'))
  self.assertTrue(fields['query'].startswith('query('));self.assertNotIn('mutation',fields['query'])
  self.assertIn('p0:pullRequest(number:8)',fields['query'])
  self.assertNotIn('files',fields['query']);self.assertNotIn('commits',fields['query'])
 def test_single_and_empty_batches_keep_small_rest_path_and_limits_are_checked(self):
  with patch.object(work,'gh_json',return_value=dict(number=1)) as remote:
   self.assertEqual(work.pull_heads(REPO,[]),[]);remote.assert_not_called()
   self.assertEqual(work.pull_heads(REPO,[1]),[dict(number=1)])
   self.assertEqual(remote.call_args.args,(REPO,'repos/acme/r/pulls/1'))
   for numbers in ([True,2],[0,2],[1,1],['1',2],list(range(1,102)),[1,2147483648]):
    with self.assertRaises(ValueError):work.pull_heads(REPO,numbers)
   self.assertEqual(remote.call_count,1)
 def test_partial_null_foreign_or_malformed_batch_is_discarded_before_rest_fallback(self):
  good=self.payload([1,2]);bad=[]
  for change in ('missing','foreign','number','updated'):
   value=json.loads(json.dumps(good));node=value['data']['repository']['p1']
   if change=='missing':value['data']['repository']['p1']=None
   elif change=='foreign':node['url']='https://github.com/foreign/r/pull/2'
   elif change=='number':node['number']=3
   else:node['updatedAt']=None
   bad.append(value)
  bad += [dict(data=[]),dict(errors=[dict(type='UNSUPPORTED')],data=good['data']),None]
  for value in bad:
   with self.subTest(value=value),patch.object(work,'gh_json',side_effect=[value,dict(number=1),dict(number=2)]) as remote:
    self.assertEqual(work.pull_heads(REPO,[1,2]),[dict(number=1),dict(number=2)])
    self.assertEqual(remote.call_count,3)
 def test_auth_rate_and_server_errors_do_not_fan_out_into_rest_requests(self):
  for code in (401,403,429,500,503):
   with self.subTest(code=code),patch.object(work,'gh_json',side_effect=work.MetadataFetchError(code)) as remote:
    with self.assertRaises(work.MetadataFetchError):work.pull_heads(REPO,[1,2])
    self.assertEqual(remote.call_count,1)
  for kind in ('FORBIDDEN','UNAUTHORIZED','RATE_LIMITED'):
   with patch.object(work,'gh_json',return_value=dict(errors=[dict(type=kind)])) as remote:
    with self.assertRaises(work.MetadataFetchError):work.pull_heads(REPO,[1,2])
    self.assertEqual(remote.call_count,1)
 def test_transport_fields_are_literal_and_failures_do_not_expose_response_text(self):
  result=subprocess.CompletedProcess([],1,stdout=json.dumps(dict(errors=[dict(type='RATE_LIMITED',message='PRIVATE')])),stderr='PRIVATE')
  with patch.object(work.subprocess,'run',return_value=result) as run:
   with self.assertRaises(work.MetadataFetchError) as error:work.gh_json(REPO,'graphql',fields=dict(query='query { viewer { login } }',owner='@literal'))
  self.assertEqual(error.exception.status,429);self.assertNotIn('PRIVATE',str(error.exception))
  argv=run.call_args.args[0];self.assertIn('owner=@literal',argv);self.assertIn('-f',argv);self.assertNotIn('-F',argv)

class BatchedRefresh(unittest.TestCase):
 setUp=fixtures.Effectiveness.setUp
 ingest=fixtures.Effectiveness.ingest
 pr=fixtures.Effectiveness.pr
 def test_unchanged_known_prs_outside_list_page_use_one_batch_without_detail_reads(self):
  self.pr();other='https://'+REPO+'/pull/2';self.pr(other)
  with self.db:self.db.execute('UPDATE pull_requests SET remote_updated_ts=?',(1760000000,))
  marker='2025-10-09T08:53:20Z'
  batch=dict(data=dict(repository=dict(p0=dict(number=2,url=other,title='PR 2',updatedAt=marker),p1=dict(number=1,url=fixtures.URL,title='PR 1',updatedAt=marker))))
  # Verify the timestamp fixture agrees with the real parser.
  self.assertEqual(work.collect.parse_ts(marker),1760000000)
  def fetch(repo,endpoint,paginate=False,fields=None):
   if '/pulls?' in endpoint:return []
   if endpoint=='graphql':return batch
   self.fail('Unchanged PR unexpectedly fetched details: '+endpoint)
  before=self.db.execute('SELECT count(*) FROM pr_commits').fetchone()[0]
  with patch.object(work,'gh_json',side_effect=fetch) as remote:result=work.refresh(self.db,[REPO],limit=1)
  self.assertEqual(result['errors'],[]);self.assertEqual(remote.call_count,2)
  self.assertEqual(self.db.execute('SELECT count(*) FROM pr_commits').fetchone()[0],before)

 def test_changed_batch_markers_still_fetch_membership_and_final_diff(self):
  self.pr();other='https://'+REPO+'/pull/2';self.pr(other)
  batch=GithubMetadataBatch().payload([2,1])
  def fetch(repo,endpoint,paginate=False,fields=None):
   if '/pulls?' in endpoint:return []
   if endpoint=='graphql':return batch
   if endpoint.endswith('/commits'):return [[dict(sha='new-head')]]
   if endpoint.endswith('/files'):return [[dict(filename='code.py',additions=7,deletions=2),dict(filename='README.md',additions=100,deletions=20)]]
   number=int(endpoint.rsplit('/',1)[1])
   return dict(number=number,title='updated',state='closed',created_at='2026-10-20T00:00:00Z',closed_at='2026-10-22T00:00:00Z',merged_at='2026-10-22T00:00:00Z',head=dict(ref='feature',sha='new-head'),changed_files=2 if number==1 else 3)
  with patch.object(work,'gh_json',side_effect=fetch) as remote:result=work.refresh(self.db,[REPO],limit=1)
  self.assertEqual(result['errors'],[]);self.assertEqual(result['prs'],2)
  self.assertEqual(self.db.execute('SELECT number,added_code,removed_code,files_complete FROM pull_requests ORDER BY number').fetchall(),[(1,7,2,1),(2,None,None,0)])
  self.assertEqual(self.db.execute("SELECT count(*) FROM pr_commits WHERE sha='new-head'").fetchone()[0],2)
  self.assertEqual(remote.call_count,8)  # list, batch, and three detailed reads per changed PR.
