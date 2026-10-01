const { chromium } = require('playwright');
const fs = require('fs');
const SP = process.argv[2];
const tokens = JSON.parse(fs.readFileSync(SP + '/env/tokens.json'));
const URL = 'http://127.0.0.1:8601/';
const log = (...a) => console.log(...a);
async function login(page, who) {
  await page.goto(URL);
  await page.getByLabel('이름(계정)').fill(who);
  await page.getByLabel('접속 토큰').fill(tokens[who]);
  await page.getByRole('button', { name: '로그인' }).click();
  await page.getByText('로그인됨').first().waitFor({ timeout: 20000 });
}
const main = p => p.locator('[data-testid="stMain"]');
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  const kim = await ctx.newPage();
  kim.on('pageerror', e => log('PAGEERROR', e.message));
  await login(kim, 'kim');
  for (const path of ['verify', 'admin', 'review']) {
    await kim.goto(URL + path);
    await kim.getByLabel('접속 토큰').waitFor({ timeout: 20000 });
    log(`reload of /${path} asks for login again`);
    await kim.getByLabel('이름(계정)').fill('kim');
    await kim.getByLabel('접속 토큰').fill(tokens['kim']);
    await kim.getByRole('button', { name: '로그인' }).click();
    await kim.getByText('로그인됨').first().waitFor({ timeout: 20000 });
    await kim.waitForTimeout(1500);
    const t = await main(kim).innerText();
    log(`consultant at /${path}:`, t.includes('검색 경로와 근거 추적') || t.includes('사용량 관리') && t.includes('미확정 비용') || t.includes('질문 검토') ? 'PRIVILEGED CONTENT VISIBLE' : 'no privileged content', '|', t.slice(0, 60).replace(/\n/g, ' '));
  }
  // verifier: free trace, frozen run, comparison
  const lee = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  lee.on('pageerror', e => log('PAGEERROR', e.message));
  await login(lee, 'lee');
  await lee.getByRole('link', { name: '검증' }).click();
  await lee.getByText('검색 경로와 근거 추적').waitFor({ timeout: 20000 });
  await lee.getByRole('textbox', { name: '질문' }).fill('하자보수 기간은 얼마인가요?');
  await lee.getByRole('textbox', { name: '질문' }).press('Enter');
  await lee.locator('[data-testid="stMultiSelect"]').first().click();
  await lee.getByRole('option').first().click();
  await lee.keyboard.press('Escape');
  await lee.getByRole('button', { name: '검색만 실행 (무료)' }).click();
  await lee.getByText(/실행 vr-/).first().waitFor({ timeout: 20000 });
  await lee.screenshot({ path: SP + '/shots/11-verifier-trace.png', fullPage: true });
  await lee.getByLabel('근거 최대 개수').fill('1');
  await lee.getByLabel('근거 최대 개수').press('Enter');
  await lee.waitForTimeout(800);
  await lee.getByRole('button', { name: '검색만 실행 (무료)' }).click();
  await lee.waitForTimeout(2000);
  await lee.getByRole('tab', { name: '실행 비교' }).click();
  await lee.waitForTimeout(1000);
  const ct = await main(lee).innerText();
  log('run comparison shows config diff:', ct.includes('limits'));
  await lee.screenshot({ path: SP + '/shots/12-verifier-compare.png', fullPage: true });
  // owner: admin page and cap exhaustion
  const owner = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  owner.on('pageerror', e => log('PAGEERROR', e.message));
  await login(owner, 'owner');
  await owner.getByRole('link', { name: '사용량 관리' }).click();
  await owner.getByText('미확정 비용').first().waitFor({ timeout: 20000 });
  await owner.getByRole('tab', { name: '외부 사용 조정' }).click();
  const panel = owner.getByRole('tabpanel', { name: '외부 사용 조정' });
  await panel.getByLabel('조정 키(중복 방지)').fill('browser-cap-test');
  await panel.getByLabel('금액(USD, 음수는 정정)').fill('16');
  await panel.getByLabel('증빙').fill('browser test: synthetic exhaustion');
  await panel.getByLabel('사유').fill('cap exhaustion scenario');
  await panel.getByRole('button', { name: '조정 기록' }).click();
  await owner.getByText('기록했습니다.').waitFor({ timeout: 10000 });
  await owner.screenshot({ path: SP + '/shots/13-admin.png', fullPage: true });
  // kim: cap exhausted — warning visible, paid blocked, free browsing works
  await kim.getByText(/운영 한도에 도달/).first().waitFor({ timeout: 8000 });
  log('cap warning visible to consultant');
  await kim.getByText('선택', { exact: true }).nth(0).click();
  await kim.getByRole('textbox', { name: '질문' }).waitFor();
  await kim.waitForTimeout(500);
  await kim.getByRole('textbox', { name: '질문' }).fill('하자보수 기간은 얼마인가요?');
  await kim.getByRole('button', { name: /근거 기반 답변 받기/ }).click();
  await kim.getByText(/사용 한도로 차단/).first().waitFor({ timeout: 20000 });
  log('paid answer blocked at cap');
  await kim.getByRole('button', { name: '원문 파일 받기' }).first().waitFor();
  const [download] = await Promise.all([kim.waitForEvent('download'), kim.getByRole('button', { name: '원문 파일 받기' }).first().click()]);
  log('original download at cap:', download.suggestedFilename());
  await kim.screenshot({ path: SP + '/shots/14-cap-blocked.png', fullPage: true });
  // refresh/polling produce no calls: read attempt count via owner admin after waiting
  await browser.close();
})();
