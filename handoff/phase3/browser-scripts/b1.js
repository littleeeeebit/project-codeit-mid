const { chromium } = require('playwright');
const fs = require('fs');
const SP = process.argv[2];
const tokens = JSON.parse(fs.readFileSync(SP + '/env/tokens.json'));
const URL = 'http://127.0.0.1:8601/';
async function login(page, who) {
  await page.goto(URL);
  await page.getByLabel('이름(계정)').fill(who);
  await page.getByLabel('접속 토큰').fill(tokens[who]);
  await page.getByRole('button', { name: '로그인' }).click();
  await page.getByText('로그인됨').first().waitFor({ timeout: 20000 });
}
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  page.on('pageerror', e => console.log('PAGEERROR', e.message));
  await page.goto(URL);
  await page.getByLabel('접속 토큰').waitFor();
  await page.screenshot({ path: SP + '/shots/01-login.png' });
  // wrong token: generic failure
  await page.getByLabel('이름(계정)').fill('kim');
  await page.getByLabel('접속 토큰').fill('wrong');
  await page.getByRole('button', { name: '로그인' }).click();
  await page.getByText('로그인에 실패했습니다').waitFor();
  console.log('generic failure shown');
  await login(page, 'kim');
  const nav = await page.locator('[data-testid="stSidebarNav"]').innerText().catch(() => '(no nav)');
  console.log('kim nav:', JSON.stringify(nav));
  await page.screenshot({ path: SP + '/shots/02-consultant.png', fullPage: true });
  await browser.close();
})();
