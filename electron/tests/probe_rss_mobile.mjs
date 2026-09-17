/**
 * RSS 信息流移动端探针：按手机尺寸走真实用户路径（登录 → 底部导航进 RSS → 点文章看正文）
 *
 * 断言：
 *  1. 底部导航「RSS信息流」入口在手机屏内可见可点（曾因 center 对齐被裁到屏幕外）
 *  2. 页面无横向溢出；三栏塌缩为单栏（订阅源树不出现在 DOM 主栏）
 *  3. 文章列表占满宽度 → 点击文章 → 正文态出现「返回列表」→ 可返回
 *  4. 触控目标尺寸统计（<32px 计数）
 *
 * 用法：node electron/tests/probe_rss_mobile.mjs
 */
import { chromium, devices } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const SIZES = ['iPhone 13', 'Pixel 7', 'iPhone SE'];

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});

for (const name of SIZES) {
  const ctx = await browser.newContext({ ...devices[name] });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 120)));

  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  if ((await page.locator('input[type=password]').count()) > 0) {
    await page.locator('input').nth(0).fill('admin');
    await page.locator('input[type=password]').first().fill('admin123');
    const btns = page.locator('button');
    for (let i = 0; i < (await btns.count()); i++) {
      const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
      if (/登录/.test(t)) { await btns.nth(i).click(); break; }
    }
    await page.locator('input[type=password]').first().waitFor({ state: 'detached', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(3000);
  }
  const later = page.locator('button', { hasText: '稍后再答' });
  if (await later.count()) { await later.first().click().catch(() => {}); await page.waitForTimeout(800); }
  await page.waitForTimeout(2000);

  // ① 底部导航入口可见性
  const nav = await page.evaluate(() => {
    const d = document.querySelector('.bottom-dock');
    if (!d) return { exists: false };
    const rss = [...d.querySelectorAll('button')].find((b) => (b.innerText || '').includes('RSS'));
    if (!rss) return { exists: true, rss: null };
    const r = rss.getBoundingClientRect();
    return {
      exists: true,
      rss: { l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) },
      inViewport: r.left >= -1 && r.right <= window.innerWidth + 1 && r.width > 0,
      inner: (() => { const i = d.querySelector('.bottom-dock-inner'); const ir = i.getBoundingClientRect(); return { l: Math.round(ir.left), r: Math.round(ir.right), sw: i.scrollWidth, cw: i.clientWidth }; })(),
    };
  });
  console.log(`\n===== ${name} =====`);
  console.log(`  底部导航 RSS 入口: ${nav.rss ? `x=${nav.rss.l}..${nav.rss.r} ${nav.inViewport ? '✓ 屏内可见' : '✗ 屏外'}` : '未找到'}` +
    (nav.inner ? ` | dock 内容 ${nav.inner.sw}px / 可视 ${nav.inner.cw}px` : ''));

  // ② 进入 RSS 页
  const clicked = await page.evaluate(() => {
    const d = document.querySelector('.bottom-dock');
    const rss = [...d.querySelectorAll('button')].find((b) => (b.innerText || '').includes('RSS'));
    if (!rss) return null;
    rss.scrollIntoView({ inline: 'center', block: 'nearest' });
    const r = rss.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  });
  if (clicked) await page.mouse.click(clicked.x, clicked.y);
  await page.waitForSelector('.news-panel', { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(6000);

  const listView = await page.evaluate(() => {
    const vw = window.innerWidth;
    const q = (s) => document.querySelector(s);
    const box = (s) => { const e = q(s); if (!e) return null; const r = e.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; };
    const escaping = [];
    document.querySelectorAll('.news-frame *').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > vw + 1) escaping.push({ cls: String(el.className).slice(0, 36), right: Math.round(r.right) });
    });
    const small = [];
    document.querySelectorAll('.news-frame button, .news-frame a').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && (r.height < 28 || r.width < 28)) small.push({ t: (el.textContent || '').trim().slice(0, 8), w: Math.round(r.width), h: Math.round(r.height) });
    });
    return {
      vw,
      docScrollW: document.documentElement.scrollWidth,
      leftPanel: box('.news-left-panel'),
      centerPanel: box('.news-center-panel'),
      rightPanel: box('.news-right-panel'),
      articleCount: document.querySelectorAll('.news-article-item').length,
      hasSourceBtn: [...document.querySelectorAll('.news-toolbar button')].some((b) => (b.innerText || '').includes('订阅源')),
      escaping: escaping.slice(0, 4),
      smallCount: small.length,
      smallSample: small.slice(0, 3),
    };
  });
  console.log(`  列表态: 文档 ${listView.docScrollW}px ${listView.docScrollW > listView.vw + 1 ? '✗ 横向溢出' : '✓'}` +
    ` | 订阅源左栏=${listView.leftPanel ? '仍在 ✗' : '已塌缩 ✓'} | 列表宽=${listView.centerPanel?.w} | 文章 ${listView.articleCount} 篇` +
    ` | 订阅源按钮=${listView.hasSourceBtn ? '✓' : '✗'}`);
  if (listView.escaping.length) listView.escaping.forEach((e) => console.log(`    ✗ 溢出 [${e.cls}] right=${e.right}`));
  console.log(`  小触控目标(<28px): ${listView.smallCount} 个 ${JSON.stringify(listView.smallSample)}`);

  // ③ 点第一篇文章 → 正文态
  const firstArticle = page.locator('.news-article-item').first();
  if (await firstArticle.count()) {
    await firstArticle.click({ timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(3500);
    const detail = await page.evaluate(() => {
      const back = [...document.querySelectorAll('.news-right-panel button')].find((b) => (b.innerText || '').includes('返回列表'));
      const rp = document.querySelector('.news-right-panel');
      return {
        detailShown: !!rp,
        backBtn: !!back,
        centerStillThere: !!document.querySelector('.news-center-panel'),
        title: (document.querySelector('.news-right-panel h4')?.textContent || '').slice(0, 30),
        rightW: rp ? Math.round(rp.getBoundingClientRect().width) : 0,
        vw: window.innerWidth,
      };
    });
    console.log(`  正文态: 正文面板=${detail.detailShown ? '✓' : '✗'} 宽=${detail.rightW}/${detail.vw} | 列表已让位=${detail.centerStillThere ? '✗ 仍在' : '✓'}` +
      ` | 返回按钮=${detail.backBtn ? '✓' : '✗'} | 标题「${detail.title}」`);
    if (detail.backBtn) {
      await page.locator('.news-right-panel button', { hasText: '返回列表' }).first().click({ timeout: 8000 }).catch(() => {});
      await page.waitForTimeout(2500);
      const backOk = await page.evaluate(() => ({
        center: !!document.querySelector('.news-center-panel'),
        detail: !!document.querySelector('.news-right-panel'),
      }));
      console.log(`  返回列表: 列表=${backOk.center ? '✓' : '✗'} 正文=${backOk.detail ? '✗ 仍在' : '✓ 已关闭'}`);
    }
    // 订阅源抽屉
    const srcBtn = page.locator('.news-toolbar button', { hasText: '订阅源' });
    if (await srcBtn.count()) {
      await srcBtn.first().click({ timeout: 8000 }).catch(() => {});
      await page.waitForTimeout(1500);
      const drawer = await page.evaluate(() => {
        const d = document.querySelector('.ant-drawer-content');
        const r = d?.getBoundingClientRect();
        return { open: !!d, w: r ? Math.round(r.width) : 0, tree: !!document.querySelector('.ant-drawer .ant-tree') };
      });
      console.log(`  订阅源抽屉: 打开=${drawer.open ? '✓' : '✗'} 宽=${drawer.w} 含源树=${drawer.tree ? '✓' : '✗'}`);
      await page.keyboard.press('Escape').catch(() => {});
      await page.waitForTimeout(800);
    }
  } else {
    console.log('  列表无文章，跳过正文态检查');
  }

  if (errors.length) console.log('  页面错误:', errors.join(' | '));
  await page.screenshot({ path: `/tmp/qm_rss_m_${listView.vw}.png` });
  await ctx.close();
}
await browser.close();
