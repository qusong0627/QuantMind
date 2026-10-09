/**
 * 宽度/样式体检：行情回测与关于「太窄」、实况模型卡观感（2026-09-23 用户反馈）。
 *
 * 量三件事，都是为了不靠肉眼猜：
 *  1) 每个页面的 `.page` 容器**实际多宽 vs 上级给的可视宽**（差额就是被 max-width 吃掉的部分）；
 *  2) 这条 max-width 是**哪张样式表**定的，以及 `qm-arena-overrides.css` 在它之前还是之后
 *     —— 同特异性覆盖成不成立，取决于后者；这条决定了改法（提特异性 or 直接同分靠顺序）。
 *  3) 实况模型卡的计算样式（宽/字号/配色/内边距）。
 *
 * 2026-09-23 起它还兼任**改造后的验收**（不只是体检）：末尾按 results 出通过率并置退出码，
 * 断的是三件事 —— 行情回测/关于用满上级宽度、模型卡铺满整行、权益是等宽大数字。
 * 「设置 → 关于」要连点两下（先侧栏设置、再面板按钮），量到的才是它在设置卡片里的真实宽度。
 *
 * 运行：PROBE_BASE=http://localhost:3080 node electron/tests/probe_arena_width_audit.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const W = Number(process.env.PROBE_W || 1920);
const H = Number(process.env.PROBE_H || 1080);

const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: W, height: H } });
const click = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    const t = ((await items.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (re.test(t)) { await items.nth(i).click({ timeout: 5000 }).catch(() => {}); return true; }
  }
  return false;
};
const dismiss = async () => {
  for (let r = 0; r < 8; r++) {
    const n = await page.evaluate(() => Array.from(document.querySelectorAll('.ant-modal-wrap')).filter((m) => getComputedStyle(m).display !== 'none').length);
    if (!n) return;
    if (!(await click(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button'))) break;
    await page.waitForTimeout(700);
  }
};

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass: !!pass });
  console.log(`  ${pass ? '✅' : '❌'} ${name}${detail ? ` —— ${detail}` : ''}`);
};

/** 页面容器体检：自己多宽 / 上级多宽 / max-width 从哪来 */
const auditPage = (sel) =>
  page.evaluate((s) => {
    const el = document.querySelector(s);
    if (!el) return { found: false };
    const parent = el.parentElement;
    const cs = getComputedStyle(el);
    // 找出定义 max-width 的那条规则在哪个样式表、第几条
    const origin = [];
    for (let i = 0; i < document.styleSheets.length; i++) {
      let rules;
      try { rules = document.styleSheets[i].cssRules; } catch { continue; }
      for (const r of rules) {
        if (!r.selectorText || !r.style) continue;
        if (!r.style.maxWidth) continue;
        try { if (el.matches(r.selectorText)) origin.push(`sheet#${i} ${r.selectorText} → ${r.style.maxWidth}`); } catch { /* 无效选择器 */ }
      }
    }
    const r = el.getBoundingClientRect();
    return {
      found: true,
      w: Math.round(r.width),
      parentW: parent ? Math.round(parent.getBoundingClientRect().width) : null,
      parentCls: parent ? String(parent.className).slice(0, 40) : null,
      maxWidth: cs.maxWidth,
      margin: `${cs.marginLeft}/${cs.marginRight}`,
      padding: cs.padding,
      origin,
      sheetCount: document.styleSheets.length,
    };
  }, sel);

/** 覆盖层是否排在页面样式之后（同特异性时靠后者赢） */
const sheetOrder = () =>
  page.evaluate(() => {
    const find = (needle) => {
      for (let i = 0; i < document.styleSheets.length; i++) {
        let rules;
        try { rules = document.styleSheets[i].cssRules; } catch { continue; }
        for (const r of rules) {
          if (r.selectorText && r.selectorText.includes(needle)) return i;
        }
      }
      return -1;
    };
    return {
      aboutCss: find('.about-page'),
      labCss: find('.lab-page'),
      overrides: find('.control-grid-3 .mk-table'),
      total: document.styleSheets.length,
    };
  });

try {
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const inputs = page.locator('input:visible');
  if ((await inputs.count()) >= 2) {
    await inputs.nth(0).fill('admin'); await inputs.nth(1).fill('admin123');
    await click(/登录/); await page.waitForTimeout(6000);
  }
  await dismiss();
  await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(8000);
  await dismiss();

  console.log(`视口 ${W}×${H}`);
  const contentW = await page.evaluate(() => {
    const el = document.querySelector('.flex-1.overflow-hidden.relative') || document.querySelector('.flex-1.min-h-0.flex');
    return el ? Math.round(el.getBoundingClientRect().width) : null;
  });
  console.log(`交易台内容区可视宽 ≈ ${contentW}px`);
  console.log('样式表顺序：', JSON.stringify(await sheetOrder()));

  // 盘中实况（模型卡就在这一页；2026-10-09 由「实况」改名）
  await dismiss();
  await click(/^盘中实况$/);
  await page.waitForTimeout(14000);
  await dismiss();
  const cards = await page.evaluate(() => {
    const sec = document.querySelector('.qm-arena-root .model-cards-section');
    if (!sec) return null;
    const list = Array.from(sec.querySelectorAll('.model-card-mini'));
    return {
      sectionW: Math.round(sec.getBoundingClientRect().width),
      gap: getComputedStyle(sec).gap,
      count: list.length,
      cards: list.map((c) => {
        const cs = getComputedStyle(c);
        const name = c.querySelector('.model-name');
        const bal = c.querySelector('.model-balance');
        const pnl = c.querySelector('.model-pnl');
        const tok = c.querySelector('.model-tokens');
        const logo = c.querySelector('.model-logo');
        const rr = c.getBoundingClientRect();
        const cs2 = (el) => (el ? { fs: getComputedStyle(el).fontSize, fw: getComputedStyle(el).fontWeight, color: getComputedStyle(el).color } : null);
        return {
          w: Math.round(rr.width), h: Math.round(rr.height),
          // 相对卡区的行号（同一行的卡 top 相同）：卡在窄栏里会换行，断「铺满整行」要按行算
          row: Math.round((rr.top - sec.getBoundingClientRect().top) / 10),
          pad: cs.padding, gap: cs.gap, border: cs.border, bg: cs.backgroundColor,
          maxWidth: cs.maxWidth,
          name: name?.innerText, nameStyle: cs2(name),
          balance: bal?.innerText, balStyle: bal ? { ...cs2(bal), ff: getComputedStyle(bal).fontFamily, num: getComputedStyle(bal).fontVariantNumeric } : null,
          pnl: pnl?.innerText, pnlCls: pnl?.className, pnlStyle: pnl ? { ...cs2(pnl), border: getComputedStyle(pnl).border } : null,
          tokens: tok?.innerText ?? null,
          logoSize: logo ? getComputedStyle(logo).fontSize : null,
          logoBg: logo ? getComputedStyle(logo).backgroundColor : null,
          radius: cs.borderRadius, shadow: cs.boxShadow,
        };
      }),
    };
  });
  console.log('\n===== 实况 · 模型卡');
  if (cards) {
    console.log(`  卡区宽 ${cards.sectionW}px 间距 ${cards.gap} 卡数 ${cards.count}`);
    for (const c of cards.cards) {
      console.log(`  [${c.name}] ${c.w}×${c.h} 行${c.row} pad=${c.pad} max-width=${c.maxWidth} border=${c.border} radius=${c.radius} bg=${c.bg} shadow=${c.shadow}`);
      console.log(`      balance ${c.balance} ${JSON.stringify(c.balStyle)} / pnl ${c.pnl}(${c.pnlCls}) ${JSON.stringify(c.pnlStyle)} / tok ${c.tokens} / logo ${c.logoSize} bg=${c.logoBg}`);
    }
    // 每行的卡宽之和 + 间距 ≈ 卡区宽：说明卡铺满了整行（原版卡在 240px，右侧空一截）。
    // 按行算而不是整体算 —— 栏窄时 3 张卡会换行（1440 视口下卡区只有 604px），
    // 整体求和会把两行加一起，永远对不上。
    const rows = new Map();
    for (const c of cards.cards) rows.set(c.row, [...(rows.get(c.row) ?? []), c]);
    const rowSlack = [...rows.values()].map((row) => {
      const used = row.reduce((s, c) => s + c.w, 0) + Number.parseFloat(cards.gap) * (row.length - 1);
      return Math.round(used - cards.sectionW);
    });
    // 主题层（qm-arena-theme.css）整段限定在 min-width: 481px —— 小屏走页面自带的两列
    // 布局，卡片本就该是半宽/12px 字号，这里再断「铺满整行/18px」就是拿错尺子量
    if (W < 481) {
      console.log('  ⓘ 视口 < 481px：模型卡按页面自带的两列布局排，跳过主题层断言');
    } else {
      check(
        `模型卡铺满每行（${rows.size} 行）`,
        rowSlack.every((s) => Math.abs(s) <= 4),
        `各行差 ${rowSlack.join('/')}px（卡区 ${cards.sectionW}px）`,
      );
      check('模型卡不再被 max-width 卡在 240px', cards.cards.every((c) => c.maxWidth === 'none'), cards.cards.map((c) => c.maxWidth).join('/'));
      check(
        '权益是等宽大数字（Courier + tabular-nums + ≥16px）',
        cards.cards.every((c) => /Courier/i.test(c.balStyle?.ff ?? '') && /tabular-nums/.test(c.balStyle?.num ?? '') && Number.parseFloat(c.balStyle?.fs ?? '0') >= 16),
        cards.cards.map((c) => `${c.balStyle?.fs}/${c.balStyle?.ff?.split(',')[0]}`).join(' '),
      );
      check(
        '收益率是描边 pill（有可见边框）',
        cards.cards.every((c) => Number.parseFloat(c.pnlStyle?.border ?? '0px') >= 1),
        cards.cards.map((c) => c.pnlStyle?.border).join(' | '),
      );
      check(
        'logo 有品牌色底块（非透明）',
        cards.cards.every((c) => c.logoBg && c.logoBg !== 'rgba(0, 0, 0, 0)'),
        cards.cards.map((c) => c.logoBg).join(' | '),
      );
    }
  } else { console.log('  没找到 .model-cards-section'); check('实况页有模型卡区', false); }

  // 两块「整幅页面」的宽度体检。关于 2026-09-23 起在设置页里 —— 得先点设置再点关于，
  // 量到的才是它在设置卡片里的真实宽度。
  for (const [label, sel, wait, open] of [
    ['行情回测', '.qm-arena-root .lab-page', 12000, async () => click(/^行情回测$/)],
    ['设置 → 关于', '.qm-arena-root .about-page', 6000, async () => {
      await click(/^设置$/);
      await page.waitForTimeout(4000);
      await click(/^关于$/);
    }],
  ]) {
    await dismiss();
    await open();
    await page.waitForTimeout(wait);
    await dismiss();
    const a = await auditPage(sel);
    console.log(`\n===== ${label}`);
    if (!a.found) { console.log(`  没找到 ${sel}`); check(`${label} 页面渲染`, false); continue; }
    const slack = a.parentW - a.w;
    console.log(`  容器 ${a.w}px / 上级可视 ${a.parentW}px（${a.parentCls}） → 差 ${slack}px`);
    console.log(`  max-width=${a.maxWidth} margin=${a.margin} padding=${a.padding}`);
    for (const o of a.origin) console.log(`  ↳ max-width 来源：${o}`);
    check(`${label} 用满上级宽度（不被 max-width 留白）`, a.maxWidth === 'none', `max-width=${a.maxWidth}`);
    check(`${label} 实际宽度贴合上级（差 ≤ 4px）`, Math.abs(slack) <= 4, `差 ${slack}px`);
  }

  // 设置页内容区宽度（关于并进去之后要按这个宽度排）
  await dismiss();
  await click(/^设置$/);
  await page.waitForTimeout(6000);
  await dismiss();
  const settings = await page.evaluate(() => {
    const panel = document.querySelector('[class*="Settings"] , .custom-scrollbar');
    return {
      extraBtnLabels: Array.from(document.querySelectorAll('button')).map((b) => (b.innerText || '').trim()).filter((t) => /^(总控|数据|关于)$/.test(t)),
      panelW: panel ? Math.round(panel.getBoundingClientRect().width) : null,
    };
  });
  console.log(`\n===== 设置页\n  顶部按钮：${settings.extraBtnLabels.join('/') || '（无）'} 面板容器宽 ${settings.panelW}px`);
  check('设置页有总控/数据/关于三块', settings.extraBtnLabels.length >= 3, settings.extraBtnLabels.join('/') || '（无）');
} catch (e) {
  console.log('探针异常：', String(e).slice(0, 300));
  check('探针整体无异常', false, String(e).slice(0, 120));
}
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
await browser.close();
process.exit(failed.length ? 1 : 0);
