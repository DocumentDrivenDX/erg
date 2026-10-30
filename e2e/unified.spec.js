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
