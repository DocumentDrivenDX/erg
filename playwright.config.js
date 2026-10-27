const {defineConfig} = require('@playwright/test');
const python = process.env.QUALITY_PYTHON || '.venv/bin/python';
module.exports = defineConfig({
  testDir: './e2e', workers: 1, retries: 0, timeout: 30000,
  reporter: [['list'], ['junit', {outputFile: 'reports/browser-junit.xml'}],
    ['html', {outputFolder: 'reports/browser', open: 'never'}]],
  use: {baseURL: 'http://127.0.0.1:8877', video: 'on', trace: 'retain-on-failure',
    screenshot: 'only-on-failure', browserName: 'chromium'},
  projects: ['light', 'dark'].map(colorScheme => ({name: colorScheme, use: {colorScheme}})),
  webServer: [
    {command: `${python} -B scripts/serve-quality-fixture.py 8877`, url: 'http://127.0.0.1:8877/healthz', reuseExistingServer: false},
    {command: `${python} -B scripts/serve-quality-fixture.py 8878`, url: 'http://127.0.0.1:8878/healthz', reuseExistingServer: false, env: {ERG_FIXTURE_EMPTY: '1'}},
  ],
});
