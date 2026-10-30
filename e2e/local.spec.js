const {test, expect} = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const views = [['overview','usage'],['projects','costs'],['sessions','history'],['accounts','accounts'],['system','capture']];

test.beforeEach(async ({page}) => {
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
});

test('populated local dashboard navigates every view with accessible controls', async ({page}) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#gen')).not.toHaveText('');
  const data = await (await page.request.get('/api/data?days=0')).json();
  expect(data.llm.length).toBeGreaterThan(0);
  for (const [destination,view] of views) {
    await test.step(`Open ${view} and check keyboard/accessible content`, async () => {
      const button = destination==='system'?page.getByRole('button',{name:'System',exact:true}):page.locator(`.piq-primary-nav:visible a[href="#${destination}"]`);
      await button.focus(); await page.keyboard.press('Enter');
      await expect(button).toHaveAttribute('aria-current', 'page');
      await expect(page.locator(`#view-${view}`)).toBeVisible();
      await expect(page.locator(`#title-${view}`)).toBeVisible();
      const result = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa']).analyze();
      expect(result.violations, JSON.stringify(result.violations, null, 2)).toEqual([]);
    });
  }
  expect(errors).toEqual([]);
  await page.screenshot({path: 'reports/dashboard-populated.png', fullPage: true});
});

test('filter and refresh use the selected local scope', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#machine option[value="quality-host"]')).toBeAttached();
  const response = page.waitForResponse(r => r.url().includes('/api/data') && r.status() === 200);
  await page.locator('#machine').selectOption('quality-host');
  await page.getByRole('button', {name: 'Refresh data', exact: true}).click();
  await response;
  await expect(page.locator('#machine')).toHaveValue('quality-host');
  await expect(page.locator('#report-warnings')).not.toContainText('Check reporter connectivity');
});

test('empty installation reports honestly and retains no history bodies', async ({page}) => {
  await page.goto('http://127.0.0.1:8878/');
  const data = await (await page.request.get('http://127.0.0.1:8878/api/data')).json();
  expect(data.llm).toEqual([]);
  await page.locator('.piq-primary-nav:visible a[href="#sessions"]').click();
  await expect(page.locator('#view-history')).toBeVisible();
  const status = await (await page.request.get('http://127.0.0.1:8878/api/history/status')).json();
  expect(status.enabled).toBe(false);
  const result = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa']).analyze();
  expect(result.violations).toEqual([]);
  await page.screenshot({path: 'reports/dashboard-empty.png', fullPage: true});
});

test('report failures display a useful error instead of stale successful data', async ({page}) => {
  await page.route('**/api/data**', route => route.fulfill({status:503, body:'Unavailable'}));
  await page.goto('/');
  await expect(page.locator('#report-warnings')).toContainText('Check reporter connectivity');
});

test('project mutation rejects cross-origin requests', async ({request}) => {
  const response = await request.post('/api/projects', {headers:{Origin:'https://untrusted.invalid'}, data:{}});
  expect(response.status()).toBe(403);
});


test('cloud history distinguishes delivered records from local collector evidence', async ({page}) => {
  const inventory = {available:false, storage_role:'databricks_cache', current_state:'not_collector', reason:'Source discovery runs on enrolled collectors. This Databricks cache verifies delivered records, not collector files.'};
  await page.route('**/api/history/discovery?report_backend=databricks', route => route.fulfill({json:inventory}));
  await page.route('**/api/history/status?report_backend=databricks', route => route.fulfill({json:{enabled:null, events:42, source_inventory:inventory}}));
  await page.route('**/api/history/reconciliation?report_backend=databricks', route => route.fulfill({json:{counts:{archived:1}, proof_scope:'Delivered cache records only'}}));
  await page.goto('/databricks#history');
  await expect(page.locator('#history-health')).toHaveText(/not collector files/);
  await page.getByRole('button',{name:'System',exact:true}).click();
  await expect(page.locator('#history-capture-title')).toHaveText('Delivered history');
  await expect(page.locator('#history-local-evidence')).toBeVisible();
  await expect(page.locator('#history-local-evidence')).toHaveAttribute('href','/#history');
  await page.getByText('Coverage evidence and gaps',{exact:true}).click();
  await page.getByRole('button',{name:'Reconcile reporting cache',exact:true}).click();
  await expect(page.locator('#history-health')).toContainText('42 retained events in the Databricks reporting cache');
  await expect(page.locator('#history-health')).not.toContainText('not enrolled');
  await expect(page.locator('#history-audit-status')).toContainText('reconciliation completed');
});


test('recent history preserves opaque pagination and refreshes changed metadata', async ({page}) => {
  const requests=[];let changed=false;
  const row=(session,title,ts)=>({machine:'quality-host',tool:'pi',session,title,project:'fixture',events:1,expired_events:0,first_ts:ts,last_ts:ts});
  const response=(sessions,more)=>({sessions,more,next_cursor:more?'opaque-recent-cursor':null,through_seq:7,project_revision:1,project_catalog:[],folder_catalog:[],observed_tools:['pi'],matching_sessions:2,order:'recent'});
  await page.route('**/api/history/discovery', route=>route.fulfill({json:{available:false}}));
  await page.route('**/api/history/sessions?*', route=>{
    const query=new URL(route.request().url()).searchParams;
    requests.push({order:query.get('order'),after:query.get('after'),upper:query.get('through_seq')});
    if(query.get('after')==='opaque-recent-cursor'){
      if(!changed){changed=true;return route.fulfill({status:409,body:'History changed'});}
      return route.fulfill({json:response([row('older','Older imported work',100)],false)});
    }
    return route.fulfill({json:response([row('newest','Recent live work',1900)],true)});
  });
  await page.goto('/#history');
  await expect(page.locator('#history-sessions')).toContainText('Recent live work');
  await page.getByRole('button',{name:'More sessions',exact:true}).click();
  await expect(page.locator('#history-list-status')).toContainText('History changed; refreshed the latest sessions');
  await expect(page.locator('#history-sessions tr')).toHaveCount(2);
  await page.getByRole('button',{name:'More sessions',exact:true}).click();
  await expect(page.locator('#history-sessions')).toContainText('Older imported work');
  await expect(page.locator('#history-list-status')).toContainText('Showing 2 of 2 sessions · most recent activity first');
  await expect(page.locator('#history-more')).toBeHidden();
  expect(requests.every(r=>r.order==='recent')).toBe(true);
  expect(requests.filter(r=>r.after==='opaque-recent-cursor').every(r=>r.upper==='7')).toBe(true);
});
