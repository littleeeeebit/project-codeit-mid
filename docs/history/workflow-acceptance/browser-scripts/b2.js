const { chromium } = require('playwright');
const fs = require('fs');
const SP = process.argv[2];
const URL = 'http://127.0.0.1:8601/';
const log = (...a) => console.log(...a);
async function login(page, who) {  // no login: type the attribution name in the sidebar
  await page.goto(URL);
  const box = page.getByLabel('이름 (사용·검토 기록용)');
  await box.waitFor({ timeout: 30000 });
  await box.fill(who);
  await box.press('Enter');
  await page.waitForTimeout(1200);
}
async function pending(page) {
  const t = await page.locator('section[data-testid="stSidebar"]').innerText();
  const m = t.match(/진행 중 예약 \$([0-9.,]+)/); return m ? m[1] : null;
}
(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const kim = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  const lee = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  for (const p of [kim, lee]) p.on('pageerror', e => log('PAGEERROR', e.message));
  await login(kim, 'kim'); await login(lee, 'lee');
  // 1. select A and ask; observe running state and disabled submit
  await kim.getByText('선택', { exact: true }).first().click();
  await kim.getByRole('textbox', { name: '질문' }).waitFor();
  await kim.getByRole('textbox', { name: '질문' }).fill('하자보수 기간은 얼마인가요?');
  await kim.getByRole('button', { name: /근거 기반 답변 받기/ }).click();
  await kim.getByText(/처리 중|대기 중/).first().waitFor({ timeout: 10000 });
  const disabled = await kim.getByRole('button', { name: /근거 기반 답변 받기/ }).isDisabled();
  log('submit disabled while running:', disabled);
  await kim.screenshot({ path: SP + '/shots/03-running.png', fullPage: true });
  // 2. other session's strip shows the reservation within the polling interval
  const t0 = Date.now(); let seen = null;
  while (Date.now() - t0 < 4000) { seen = await pending(lee); if (seen && seen !== '0.0000') break; await lee.waitForTimeout(250); }
  log('lee sees pending', seen, 'after', Date.now() - t0, 'ms');
  // 3. change scope mid-request: deselect A, select D
  await kim.getByRole('button', { name: '선택 해제' }).first().click();
  await kim.waitForTimeout(800);
  await kim.getByText('선택', { exact: true }).nth(2).click();
  await kim.waitForTimeout(6000);  // the old request finishes meanwhile
  const body = await kim.locator('section.main, [data-testid="stMain"]').first().innerText();
  log('stale answer attached after scope change:', body.includes('가짜 제공자 응답'));
  await kim.screenshot({ path: SP + '/shots/04-scope-changed.png', fullPage: true });
  // history shows it as past request
  await kim.getByText('내 최근 요청').click();
  await kim.waitForTimeout(500);
  await kim.screenshot({ path: SP + '/shots/05-history.png', fullPage: true });
  // 4. ask on D and open evidence
  await kim.getByRole('textbox', { name: '질문' }).fill('좌석 예약 시스템은 무엇을 구축하나요?');
  await kim.getByRole('button', { name: /근거 기반 답변 받기/ }).click();
  await kim.getByText('가짜 제공자 응답').first().waitFor({ timeout: 20000 });
  await kim.getByRole('button', { name: '근거 E1' }).first().click();
  await kim.getByText('근거 E1 ·').first().waitFor({ timeout: 10000 }).catch(() => {});
  await kim.screenshot({ path: SP + '/shots/06-answer-evidence.png', fullPage: true });
  const after = await pending(lee);
  log('lee pending after settlement', after);
  // narrow layout
  await kim.setViewportSize({ width: 420, height: 900 });
  await kim.waitForTimeout(1000);
  await kim.screenshot({ path: SP + '/shots/07-narrow.png', fullPage: true });
  await browser.close();
})();
