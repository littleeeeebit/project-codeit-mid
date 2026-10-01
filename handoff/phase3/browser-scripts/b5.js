const { chromium } = require('playwright');
const fs = require('fs'); const crypto = require('crypto');
const SP = process.argv[2];
const tokens = JSON.parse(fs.readFileSync(SP + '/env/tokens.json'));
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await (await browser.newContext({ acceptDownloads: true, viewport: { width: 1400, height: 1000 } })).newPage();
  await page.goto('http://127.0.0.1:8601/');
  const who = tokens['kim'] ? 'kim' : 'owner';
  await page.getByLabel('이름(계정)').fill(who);
  await page.getByLabel('접속 토큰').fill(tokens[who]);
  await page.getByRole('button', { name: '로그인' }).click();
  await page.getByText('로그인됨').first().waitFor({ timeout: 20000 });
  await page.getByText('선택', { exact: true }).nth(0).click();
  const [d] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '원문 파일 받기' }).first().click()]);
  const path = SP + '/dl.bin'; await d.saveAs(path);
  const got = crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex');
  const dir = SP + '/env/source/files/'; const orig = fs.readdirSync(dir).find(f => f.startsWith('기관A'));
  const want = crypto.createHash('sha256').update(fs.readFileSync(dir + orig)).digest('hex');
  console.log('download bytes equal managed original:', got === want, 'suggested name:', d.suggestedFilename());
  // keyboard: Tab focus reaches the search input
  await page.keyboard.press('Tab'); await page.keyboard.press('Tab');
  console.log('focused element tag:', await page.evaluate(() => document.activeElement && document.activeElement.tagName + ':' + (document.activeElement.getAttribute('aria-label') || document.activeElement.textContent.slice(0, 20))));
  await page.screenshot({ path: SP + '/shots/15-warnings-collapsed.png' });
  await browser.close();
})();
