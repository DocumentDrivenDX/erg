import io,json,tempfile,time,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
from erg import codex_billing as b,accounting,store,account_reports,report_views

class CodexBilling(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name)
  (self.home/'auth.json').write_text(json.dumps(dict(auth_mode='chatgpt',tokens=dict(access_token='SECRET',account_id='PRIVATE'))));b.CACHE.clear()
 def documents(self):
  return {'/wham/usage':dict(account_id='PRIVATE',email='owner@example.invalid',plan_type='future-plan',rate_limit=dict(primary_window=dict(used_percent=20,limit_window_seconds=600,reset_at=2000))),
   '/wham/rate-limit-reset-credits':dict(available_count=2),
   '/wham/rate-limit-reset-credits/history':dict(events=[dict(id='reset-private:used',kind='used',occurred_at='1970-01-01T00:10:00Z')],next_cursor=None),
   '/accounts/check/v4-2023-04-27':dict(accounts={'PRIVATE':dict(entitlement=dict(has_active_subscription=True,billing_currency='USD'))}),
   '/checkout_pricing_config/configs/USD':dict(currency_config=dict(symbol_code='USD',minor_unit_exponent=2,**{'future-plan':dict(month=dict(amount=123.45))})),
   '/subscriptions?account_id=PRIVATE':dict(payment_method='NEVER_PERSIST',invoices=[])}
 def read(self,docs=None):
  documents=self.documents() if docs is None else docs;seen=[]
  class Response(io.BytesIO):pass
  class Opener:
   def open(self,request,timeout):
    assert request.full_url.startswith(b.BASE)
    path=request.full_url[len(b.BASE):];seen.append(request)
    value=documents[path]
    if isinstance(value,Exception):raise value
    return Response(json.dumps(value).encode())
  with patch.object(b.urllib.request,'build_opener',return_value=Opener()),patch.object(b.time,'time',return_value=1000):r=b.read(self.home,'PRIVATE')
  return r,seen
 def test_dynamic_price_units_reset_history_and_secret_exclusion(self):
  r,seen=self.read();rows=b.events(self.home,r);text=json.dumps(rows)
  self.assertEqual(rows[0]['monthly_usd'],123.45);self.assertEqual(rows[0]['plan'],'future-plan')
  self.assertTrue(any(e.get('occurred_at')==600 and e['event']=='reset_credit_used' for e in rows))
  for secret in ('SECRET','PRIVATE','NEVER_PERSIST','owner@example.invalid'):self.assertNotIn(secret,text)
  self.assertTrue(all(q.get_method()=='GET' and q.full_url.startswith(b.BASE+'/') for q in seen))
  self.assertFalse(any(e['t']=='subscription_charge' for e in rows))
 def test_http_failure_is_explicit_and_resets_still_capture(self):
  d=self.documents();d['/checkout_pricing_config/configs/USD']=urllib.error.HTTPError('unused',403,'SECRET',{},None)
  r,_=self.read(d);self.assertEqual(r['endpoints']['pricing'],'http_403');self.assertNotIn('SECRET',json.dumps(r));rows=b.events(self.home,r)
  self.assertNotIn('monthly_usd',rows[0]);self.assertTrue(any(e['event']=='reset_credit_used' for e in rows if e['t']=='quota'))
 def test_identity_mismatch_never_reads_other_billing_endpoints(self):
  d=self.documents();d['/wham/usage']['account_id']='OTHER';r,seen=self.read(d)
  self.assertEqual(r['status'],'account_mismatch');self.assertEqual(len(seen),1);self.assertEqual(b.events(self.home,r),[])
 def test_redirect_and_mutation_paths_rejected(self):
  self.assertIsNone(b.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
  for path in ('https://evil.invalid','/wham/rate-limit-reset-credits/consume','/subscriptions/update','/wham/rate-limit-reset-credits/history/consume','/subscriptions?account_id=a&action=buy'):
   self.assertFalse(b.allowed(path),path)
 def test_probe_cache_prevents_repeat_http_and_rotation_invalidates(self):
  r,first=self.read();again,second=self.read();self.assertTrue(first);self.assertEqual(second,[]);self.assertEqual(r,again)
  (self.home/'auth.json').write_text(json.dumps(dict(tokens=dict(access_token='ROTATED',account_id='PRIVATE'))))
  self.assertTrue(self.read()[1])
 def test_reset_history_joins_reports_and_replay_deduplicates(self):
  db=store.connect(str(self.home/'db'));self.addCleanup(db.close);r,_=self.read();rows=b.events(self.home,r)
  for _ in range(2):store.ingest(db,map(json.dumps,rows),machine='host')
  report=account_reports.report(db);self.assertEqual(report['quota_counts']['reset_credit_used'],1)
  self.assertEqual(len(report['reset_history']),1);self.assertEqual(report['reset_history'][0]['occurred_at'],600)
  self.assertIn('billing_status',report_views.project(report,'accounts')['account_registry'][0])
 def test_account_price_does_not_establish_session_payer(self):
  db=store.connect(str(self.home/'db'));self.addCleanup(db.close);r,_=self.read();store.ingest(db,map(json.dumps,b.events(self.home,r)),machine='host')
  resolve,_,_=accounting.resolver(db);self.assertFalse(resolve('codex','',1000)['billing_evidence']);self.assertIsNone(resolve('codex','session',1000))

 def test_mirrored_provider_reset_events_count_once(self):
  db=store.connect(str(self.home/'db'));self.addCleanup(db.close);r,_=self.read();rows=b.events(self.home,r)
  for machine in ('first','mirror'):store.ingest(db,map(json.dumps,rows),machine=machine)
  report=account_reports.report(db);self.assertEqual(report['quota_counts']['reset_credit_used'],1);self.assertEqual(len(report['reset_history']),1)
 def test_collector_preserves_first_observed_reset_time_on_new_poll(self):
  from erg import account_probe,collect,account_probe_context
  db=store.connect(str(self.home/'db'));self.addCleanup(db.close);r,_=self.read()
  response=dict(account=dict(type='chatgpt',planType='future-plan'),limits=dict(accountId='PRIVATE'),_complete=True,billing=r)
  with patch.dict('os.environ',{'CODEX_HOME':str(self.home)}),patch.object(collect,'native_roots',return_value=[]),patch.object(account_probe_context,'snapshot',return_value=None),patch.object(account_probe,'read',return_value=response):
   account_probe.poll(db);response['billing']=dict(r,ts=2000);account_probe.poll(db)
  row=db.execute("SELECT count(*),min(ts),max(ts) FROM quota_events WHERE provenance='codex_reset_history_api'").fetchone()
  self.assertEqual(tuple(row),(1,1000,1000))
