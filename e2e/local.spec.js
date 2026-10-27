const {test, expect} = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const views = ['usage', 'history', 'work', 'costs', 'accounts', 'capture', 'machine'];

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
  for (const view of views) {
    await test.step(`Open ${view} and check keyboard/accessible content`, async () => {
      const button = page.locator(`[data-view="${view}"]`);
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
  await page.locator('[data-view="history"]').click();
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
