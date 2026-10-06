const { chromium } = require('playwright');
const SP = process.argv[2];
const URL = 'http://127.0.0.1:8601/';
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  page.on('pageerror', e => console.log('PAGEERROR', e.message));
  await page.goto(URL);
  await page.getByLabel('이름 (사용·검토 기록용)').waitFor({ timeout: 30000 });
  const nav = await page.locator('[data-testid="stSidebarNav"]').innerText();
  console.log('no login form:', !(await page.getByText('접속 토큰').count()), '| nav:', JSON.stringify(nav));
  await page.screenshot({ path: SP + '/shots/01-landing.png' });
  await page.getByLabel('이름 (사용·검토 기록용)').fill('kim');
  await page.getByLabel('이름 (사용·검토 기록용)').press('Enter');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: SP + '/shots/02-consultant.png', fullPage: true });
  await browser.close();
})();
