const {test,expect}=require('@playwright/test');
const AxeBuilder=require('@axe-core/playwright').default;
const nav=(page,d)=>page.locator(`.piq-primary-nav a[href="#${d}"]:visible, .piq-shell__bar a[href="#${d}"]:visible`);
// @covers US-001-AC1 @covers US-001-AC2 @covers US-001-AC3 @covers US-003-AC1 @covers US-003-AC3 @covers US-003-AC4
test('project spending carries scope to a full session and browser Back restores it',async({page})=>{
 await page.goto('/');
 await expect(page.locator('#overview-project-rows')).toContainText('Synthetic product');
 await expect(page.locator('#money-kpis')).toContainText('Rack-rate value');
 await page.locator('[data-overview-project]').first().click();
 await expect(page.locator('#view-costs')).toBeVisible();
 await expect(page.locator('#title-costs')).toHaveText('Synthetic product');
 await expect(page.locator('#view-work')).toBeVisible();
 await expect(page.locator('#project')).toHaveValue('quality:app');
 const projectURL=page.url();
 await expect(page.locator('#cost-session-table')).toContainText('Preserve agent history');
 await page.locator('[data-cost-session]').first().click();
 await expect(page.locator('#history-reader')).toBeVisible();
 await expect(page.locator('#history-events')).toContainText('SYNTHETIC trace');
 await expect(page.locator('#history-cost-summary')).toContainText('Observed tokens');
 await expect(page.locator('#history-cost-summary')).toContainText('127');
 await expect(page.locator('#history-metrics')).toContainText('largest_request_total_tokens');
 expect(page.url()).toContain('#session');
 const sessionURL=page.url();
 await page.reload();
 await expect(page.locator('#history-events')).toContainText('SYNTHETIC trace');
 expect(page.url()).toBe(sessionURL);
 await page.goBack();
 await expect(page.locator('#view-costs')).toBeVisible();
 expect(page.url()).toBe(projectURL);
 await expect(page.locator('#project')).toHaveValue('quality:app');
});
// @covers US-001-AC1 @covers US-001-AC2 @covers US-003-AC2
test('overview projects and accounts agree on canonical token usage and scope survives switching',async({page,request})=>{
 await page.goto('/');
 const money=await (await request.get('/api/money')).json();
 expect(money.available).toBe(true);
 await expect(page.locator('#money-gaps')).toBeVisible();
 const costs=await (await request.get('/api/costs')).json();
 expect(costs.totals.tokens).toBe(127);
 await page.locator('#project').selectOption('quality:app');
 await page.locator('#machine').selectOption('quality-host');
 await nav(page,'accounts').click();
 await expect(page.locator('#account-table')).toContainText('127 tokens');
 await expect(page.locator('#project')).toHaveValue('quality:app');
 await expect(page.locator('#machine')).toHaveValue('quality-host');
 await nav(page,'projects').click();
 await expect(page.locator('#cost-kpis')).toContainText('127');
 await page.reload();
 await expect(page.locator('#project')).toHaveValue('quality:app');
 await expect(page.locator('#machine')).toHaveValue('quality-host');
});
// @covers US-002-AC1 @covers US-002-AC2 @covers US-002-AC3 @covers US-002-AC4
test('accounts prioritize observed capacity and distinguish quota resets from charges',async({page})=>{
 await page.goto('/#accounts');
 await expect(page.locator('#account-capacity')).toContainText('75% remaining');
 await expect(page.locator('#account-capacity')).toContainText('Resets');
 await expect(page.locator('#account-capacity')).toContainText('Observed');
 await expect(page.locator('#subscription-table')).toContainText('No subscription charges recorded');
 await expect(page.locator('#account-capacity')).not.toContainText('$0');
 const result=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa']).analyze();
 expect(result.violations,JSON.stringify(result.violations,null,2)).toEqual([]);
});
// @covers US-001-AC4
test('a failed refresh retains the last complete project results and offers recovery',async({page})=>{
 await page.goto('/#projects');
 await expect(page.locator('#cost-project-list')).toContainText('Synthetic product');
 await page.route('**/api/costs?*',r=>r.fulfill({status:503,body:'Unavailable'}));
 await page.getByRole('button',{name:'Refresh data',exact:true}).click();
 await expect(page.locator('#cost-status')).toContainText('Previous completed results are retained');
 await expect(page.locator('#cost-project-list')).toContainText('Synthetic product');
});
// @covers US-001-AC1 @covers US-003-AC1
test('custom whole-hour dates are validated and shareable',async({page})=>{
 await page.goto('/');
 await page.getByText('Custom dates',{exact:true}).click();
 await page.locator('#scope-start').fill('2026-10-29T13:00');
 await page.locator('#scope-end').fill('2026-10-29T12:00');
 await page.getByRole('button',{name:'Apply dates',exact:true}).click();
 await expect(page.locator('#date-error')).toContainText('end after the start');
 await page.locator('#scope-end').fill('2026-10-29T15:00');
 await page.getByRole('button',{name:'Apply dates',exact:true}).click();
 expect(new URL(page.url()).searchParams.has('start')).toBe(true);
 await nav(page,'sessions').click();
 expect(new URL(page.url()).searchParams.has('end')).toBe(true);
});
// @covers US-001-AC1 @covers US-002-AC1 @covers US-003-AC1
test('primary navigation and all reporting views reflow at narrow widths',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(const width of [320,720,1024,1366]){
  await page.setViewportSize({width,height:950});
  await page.goto('/');
  for(const d of ['overview','projects','sessions','accounts']){
   await nav(page,d).click();
   await expect(nav(page,d)).toHaveAttribute('aria-current','page');
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  }
  await page.getByRole('button',{name:'System',exact:true}).click();
  await expect(page.locator('#view-capture')).toBeVisible();
  await expect(page.locator('#view-machine')).toBeVisible();
  expect(await page.locator('main').count()).toBe(1);
 }
 expect(errors).toEqual([]);
});
// @covers US-001-AC4
 test('Databricks denial stays explicit and never silently switches backend',async({page})=>{
 await page.route('**/api/effectiveness?*',r=>r.fulfill({status:403,body:'Denied'}));
 await page.goto('/databricks#accounts');
 await expect(page.locator('#value-summary')).toContainText('403');
 await expect(page.locator('#backend-label')).toHaveText('Databricks · admitted corpus');
 expect(new URL(page.url()).pathname).toBe('/databricks');
});

// @covers US-001-AC1 @covers US-001-AC2
 test('historical custom totals exclude both outside boundaries even in daily buckets',async({page})=>{
 const first=Date.UTC(2026,9,28,10)/3600000,end=(first+2)*3600;
 await page.route('**/api/data?*',async route=>{
  const response=await route.fetch(),data=await response.json(),cols=data.llm_cols;
  const seed=Object.fromEntries(cols.map((key,i)=>[key,data.llm[0][i]]));
  data.llm=[first-1,first,first+1,first+2,first+24].map(hour=>{
   const row={...seed,hour,fresh_in:10,cache_read:0,cache_write:0,out:0,calls:1,value_low_usd:1,value_high_usd:1,priced_calls:1,unpriced_calls:0};
   return cols.map(key=>row[key]);
  });
  await route.fulfill({response,json:data});
 });
 await page.goto('/?range=All&bucket=day&start='+first*3600+'&end='+end+'#overview');
 await expect(page.locator('#kpis .kpi').first().locator('.v')).toHaveText('20');
 await expect(page.locator('#kpis .kpi').nth(1).locator('.v')).toHaveText('$2.00');
 await expect(page.locator('#tbl tr').nth(1).locator('td').nth(1)).toHaveText('20');
 await expect(page.locator('#l-tokens')).toContainText('20');
 const heat=await page.locator('#heat [title]').evaluateAll(nodes=>nodes.reduce((sum,node)=>sum+Number(node.title.split(' — ')[1]||0),0));
 expect(heat).toBe(20);
});
// @covers US-001-AC1 @covers US-001-AC4
 test('refresh updates overview projects even when scope and publication are unchanged',async({page})=>{
 await page.goto('/');
 await expect(page.locator('#overview-project-rows')).toContainText('Synthetic product');
 await page.route('**/api/costs?*',async route=>{
  const response=await route.fetch(),data=await response.json();
  data.projects=data.projects.map(row=>({...row,name:'Refreshed project',tokens:999}));
  await route.fulfill({response,json:data});
 });
 await page.getByRole('button',{name:'Refresh data',exact:true}).click();
 await expect(page.locator('#overview-project-rows')).toContainText('Refreshed project');
 await expect(page.locator('#overview-project-rows')).toContainText('999');
});
// @covers US-001-AC1 @covers US-003-AC3
 test('Back restores overview controls and a bare URL restores the current rolling window',async({page})=>{
 const now=Date.now();await page.clock.setFixedTime(new Date(now));
 await page.goto('/');
 await expect(page.locator('#overview-project-rows')).toContainText('Synthetic product');
 await page.locator('#project').selectOption('quality:app');
 await page.locator('#project').selectOption('@unassigned');
 await page.goBack();
 await expect(page.locator('#project')).toHaveValue('quality:app');
 await expect(page.locator('#overview-project-rows')).toContainText('Synthetic product');
 await page.evaluate(()=>history.replaceState(null,'','/#overview'));
 await page.getByText('Custom dates',{exact:true}).click();
 await page.locator('#scope-start').fill('2026-10-28T10:00');
 await page.locator('#scope-end').fill('2026-10-28T12:00');
 await page.getByRole('button',{name:'Apply dates',exact:true}).click();
 await page.goBack();
 await expect(page.locator('#project')).toHaveValue('*');
 const scope=await page.evaluate(()=>Object.fromEntries(scopeQuery()));
 expect(Number(scope.end)).toBe((Math.floor(now/3600000)+1)*3600);
 expect(Number(scope.end)-Number(scope.start)).toBe(168*3600);
});
// @covers US-001-AC1
 test('clearing a pinned custom range resets the rolling window to now',async({page})=>{
 const now=Date.now();await page.clock.setFixedTime(new Date(now));
 const first=Date.UTC(2026,9,28,10)/3600/1000;
 await page.goto('/?range=All&start='+first*3600+'&end='+(first+2)*3600+'&anchor='+(first+1)+'#overview');
 await page.getByRole('button',{name:'Clear filters',exact:true}).click();
 const scope=await page.evaluate(()=>Object.fromEntries(scopeQuery()));
 expect(Number(scope.end)).toBe((Math.floor(now/3600000)+1)*3600);
 expect(Number(scope.end)-Number(scope.start)).toBe(168*3600);
 expect(new URL(page.url()).searchParams.get('range')).toBe('7d');
});

// @covers US-001-AC1 @covers US-003-AC3
 test('Back synchronizes all overview presentation controls',async({page})=>{
 await page.goto('/?group=model&measure=out&bucket=day&dim=tool#overview');
 await expect(page.locator('#kpis .kpi')).toHaveCount(4);
 await page.getByText('More filters',{exact:true}).click();
 await page.locator('#bucket').getByRole('button',{name:'Hour',exact:true}).click();
 await page.locator('#group').selectOption('machine');
 await page.locator('#measure').selectOption('io');
 await page.locator('#tabdim').getByRole('button',{name:'Folders',exact:true}).click();
 await page.goBack();await expect(page.locator('#tabdim .on')).toHaveText('Agent');
 await page.goBack();await expect(page.locator('#measure')).toHaveValue('out');
 await page.goBack();await expect(page.locator('#group')).toHaveValue('model');
 await page.goBack();await expect(page.locator('#bucket .on')).toHaveText('Day');
});
// @covers US-001-AC1 @covers US-003-AC3
 test('Back synchronizes custom date inputs and clearing filters clears the form',async({page})=>{
 await page.goto('/');
 await expect(page.locator('#kpis .kpi')).toHaveCount(4);
 await page.getByText('Custom dates',{exact:true}).click();
 for(const day of ['28','29']){
  await page.locator('#scope-start').fill('2026-10-'+day+'T10:00');
  await page.locator('#scope-end').fill('2026-10-'+day+'T12:00');
  await page.getByRole('button',{name:'Apply dates',exact:true}).click();
 }
 await page.goBack();
 await expect(page.locator('#scope-start')).toHaveValue('2026-10-28T10:00');
 await expect(page.locator('#scope-end')).toHaveValue('2026-10-28T12:00');
 await expect(page.locator('#custom-range')).toHaveAttribute('open','');
 await page.getByRole('button',{name:'Clear filters',exact:true}).click();
 await expect(page.locator('#scope-start')).toHaveValue('');
 await expect(page.locator('#scope-end')).toHaveValue('');
 await expect(page.locator('#custom-range')).not.toHaveAttribute('open','');
});
// @covers US-001-AC1
 test('empty fractional and invalid anchors restore the current rolling interval',async({page})=>{
 const now=Date.now();await page.clock.setFixedTime(new Date(now));
 for(const anchor of ['', '12.5', '-1', 'invalid']){
  await page.goto('/?anchor='+encodeURIComponent(anchor)+'#overview');
  await expect(page.locator('#kpis .kpi')).toHaveCount(4);
  const scope=await page.evaluate(()=>Object.fromEntries(scopeQuery()));
  expect(Number(scope.end)).toBe((Math.floor(now/3600000)+1)*3600);
  expect(Number(scope.end)-Number(scope.start)).toBe(168*3600);
 }
});
