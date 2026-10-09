import { chromium } from 'playwright';
const BASE = 'http://localhost:8092';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
for (const [route, name] of [['/live','live'],['/harness','harness'],['/market-lab','marketlab'],['/control','control'],['/data-platform','data'],['/about','about']]) {
  await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded' }).catch(() => {});
  await page.waitForTimeout(3500);
  const title = await page.evaluate(() => document.title);
  const navs = await page.evaluate(() => Array.from(document.querySelectorAll('nav a')).map(a => a.textContent?.trim()).join(' | '));
  const body = await page.evaluate(() => (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 160));
  console.log(`[${route}] title=${title}\n   nav: ${navs}\n   body: ${body}\n`);
  await page.screenshot({ path: `/tmp/arena_${name}.png` });
}
await browser.close();
