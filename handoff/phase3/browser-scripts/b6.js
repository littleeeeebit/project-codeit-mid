const { chromium } = require('playwright');
const fs = require('fs');
const SP = process.argv[2];
const tokens = JSON.parse(fs.readFileSync(SP + '/env/tokens.json'));
const log = (...a) => console.log(...a);
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const pages = [];
  for (let i = 1; i <= 6; i++) {
    const p = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    p.on('pageerror', e => log('PAGEERROR', i, e.message));
    await p.goto('http://127.0.0.1:8601/');
    await p.getByLabel('이름(계정)').fill('m' + i);
    await p.getByLabel('접속 토큰').fill(tokens['m' + i]);
    await p.getByRole('button', { name: '로그인' }).click();
    await p.getByText('로그인됨').first().waitFor({ timeout: 30000 });
    await p.getByText('선택', { exact: true }).nth(i % 2 ? 0 : 2).click();
    await p.getByRole('textbox', { name: '질문' }).waitFor();
    await p.waitForTimeout(400);
    await p.getByRole('textbox', { name: '질문' }).fill(i % 2 ? '하자보수 기간은 얼마인가요?' : '좌석 예약 시스템은 무엇을 구축하나요?');
    pages.push(p);
  }
  const t0 = Date.now();
  await Promise.all(pages.map(p => p.getByRole('button', { name: /근거 기반 답변 받기/ }).click()));
  // while running, every strip shows a nonzero reservation
  const strips = await Promise.all(pages.map(async p => {
    const s0 = Date.now();
    while (Date.now() - s0 < 8000) {
      const v = (await p.locator('section[data-testid="stSidebar"]').innerText()).match(/진행 중 예약 \$([0-9.]+)/)?.[1];
      if (v && v !== '0.0000') return `${v}@${Date.now() - t0}ms`;
      await p.waitForTimeout(200);
    }
    return 'none';
  }));
  log('first nonzero shared reservation seen per session:', strips.join(', '));
  await Promise.all(pages.map(p => p.getByText('가짜 제공자 응답').first().waitFor({ timeout: 40000 })));
  log('all six answered in', Date.now() - t0, 'ms (fake delay 4 s each)');
  const own = await Promise.all(pages.map(async (p, i) => {
    const t = await p.locator('[data-testid="stMain"]').innerText();
    return (i + 1) % 2 ? t.includes('하자보수') : t.includes('좌석');
  }));
  log('each session shows its own answer:', own.join(','));
  await pages[0].screenshot({ path: SP + '/shots/16-six-sessions-m1.png', fullPage: false });
  // refresh every page and keep them polling for a while: no new paid call may appear
  for (const p of pages) await p.reload();
  await pages[0].waitForTimeout(8000);
  await browser.close();
})();
