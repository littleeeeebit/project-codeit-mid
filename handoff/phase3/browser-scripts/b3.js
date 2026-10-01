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
  const kim = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  kim.on('pageerror', e => log('PAGEERROR', e.message));
  await login(kim, 'kim');
  // compare A and D
  await kim.getByText('선택', { exact: true }).nth(0).click();
  await kim.waitForTimeout(500);
  await kim.getByText('선택', { exact: true }).nth(2).click();
  await kim.getByRole('button', { name: /비교 답변 받기/ }).waitFor();
  await kim.getByRole('textbox', { name: '질문' }).fill('두 사업의 시스템 구축 내용을 비교해 주세요.');
  await kim.getByRole('button', { name: /비교 답변 받기/ }).click();
  await kim.getByText('가짜 제공자 응답').first().waitFor({ timeout: 20000 });
  const txt = await main(kim).innerText();
  log('compare: doc1 claim', txt.includes('문서 1'), 'doc2 claim', txt.includes('문서 2'));
  await kim.screenshot({ path: SP + '/shots/08-compare.png', fullPage: true });
  // metadata comparison (free)
  await kim.getByText('기본 정보 비교 (무료)').click();
  await kim.getByRole('button', { name: /기본 정보 보기/ }).click();
  await kim.waitForTimeout(3000); await kim.screenshot({ path: SP + '/shots/dbg-meta.png', fullPage: true });
  await kim.getByText('CSV 기본 정보입니다').first().waitFor({ timeout: 10000 });
  const mtxt = await main(kim).innerText();
  log('metadata states: conflict', mtxt.includes('충돌(원문 확인 필요)'), 'unknown', mtxt.includes('값 없음'));
  await kim.screenshot({ path: SP + '/shots/09-metadata.png', fullPage: true });
  // quarantined E alone
  for (const b of await kim.getByRole('button', { name: '선택 해제' }).all()) { await kim.getByRole('button', { name: '선택 해제' }).first().click(); await kim.waitForTimeout(700); }
  await kim.getByText('선택', { exact: true }).nth(3).click();
  await kim.waitForTimeout(800);
  const etxt = await main(kim).innerText();
  log('quarantined explained:', /변환|수집하지 못|색인에 포함/.test(etxt));
  await kim.screenshot({ path: SP + '/shots/dbg-e.png', fullPage: true });
  await kim.getByText('요구사항 목록 (무료)').click();
  await kim.getByRole('button', { name: /요구사항 목록 보기/ }).click();
  await kim.getByText(/원문 미수집|수집하지 못해/).first().waitFor({ timeout: 10000 });
  log('inventory on quarantined -> ingestion_unavailable shown');
  await kim.screenshot({ path: SP + '/shots/10-quarantined.png', fullPage: true });
  // direct URL to verifier page as consultant
  await kim.goto(URL + 'verify');
  await kim.waitForTimeout(2500);
  const vtxt = await main(kim).innerText();
  log('consultant at /verify sees verifier content:', vtxt.includes('검색 경로와 근거 추적'));
  await kim.goto(URL + 'admin');
  await kim.waitForTimeout(2500);
  log('consultant at /admin sees admin content:', (await main(kim).innerText()).includes('사용량 관리'));
  await browser.close();
})();
