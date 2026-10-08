/** 分析文本四段式解析：总体总结 / 分析链路 / 推理论证 / 交易决策。
 *  统一处理两套标记：
 *    A) 【总体总结】【分析链路】【推理论证】【决策】
 *    B) ①/②/③/④ + 小节名（flash 实测输出风格）
 *  无任何标记时回退：摘要=首段，推理=去掉已调用行与决策 JSON 的正文。
 *  各段互不重叠（摘要不会在推理里重复出现）。 */
export interface DecItem {
  action: string; // buy / sell / hold / watch
  code?: string;
  name?: string;
  pct?: number;
  stop_loss?: number;
  take_profit?: number;
  move_stop?: number;
  invalidation?: string;
  confidence?: number;
  risk_amount?: number;
  reason?: string;
}

export interface ParsedAnalysis {
  summary: string;
  chain: string;
  reasoning: string;
  decisions: DecItem[];
}

/** 括号匹配提取文本中所有 JSON 对象 */
function extractJsonBlocks(text: string): unknown[] {
  const out: unknown[] = [];
  let i = 0;
  while (i < text.length) {
    const s = text.indexOf('{', i);
    if (s < 0) break;
    let depth = 0;
    let inStr = false;
    let j = s;
    for (; j < text.length; j++) {
      const c = text[j];
      if (inStr) {
        if (c === '\\') j++;
        else if (c === '"') inStr = false;
      } else if (c === '"') inStr = true;
      else if (c === '{') depth++;
      else if (c === '}') {
        depth--;
        if (depth === 0) {
          j++;
          break;
        }
      }
    }
    const raw = text.slice(s, j);
    try {
      out.push(JSON.parse(raw));
    } catch {
      /* 忽略不可解析片段 */
    }
    i = j;
  }
  return out;
}

function toDecItems(blocks: unknown[]): DecItem[] {
  const items: DecItem[] = [];
  for (const b of blocks) {
    if (Array.isArray(b)) {
      for (const it of b) {
        if (it && typeof it === 'object' && (it as DecItem).action) items.push(it as DecItem);
      }
    } else if (b && typeof b === 'object') {
      const rec = b as Record<string, unknown>;
      const arr = rec.decisions ?? rec.actions ?? rec.trades;
      if (Array.isArray(arr)) {
        for (const it of arr) {
          if (it && typeof it === 'object' && (it as DecItem).action) items.push(it as DecItem);
        }
      } else if (rec.action) {
        items.push(rec as unknown as DecItem);
      }
    }
  }
  return items.slice(0, 8);
}

const clean = (x: string) => x.replace(/^[①②③④]?\s*/, '').replace(/[①②③④]\s*$/, '').trim();

export function parseAnalysis(thought?: string | null): ParsedAnalysis {
  const t = (thought ?? '').trim();
  const out: ParsedAnalysis = { summary: '', chain: '', reasoning: t, decisions: [] };
  if (!t) return out;

  const idxOf = (re: RegExp) => {
    const m = t.match(re);
    return m && m.index != null ? m.index : -1;
  };
  const hasBra = /【总体总结】|【分析链路】|【推理论证】|【决策】/.test(t);
  const i1 = idxOf(/①|【总体总结】/);
  const i2 = idxOf(/②|【分析链路】/);
  const i3 = idxOf(/③|【推理论证】/);
  const i4 = idxOf(/④|【决策】/);
  const hasNum = [i1, i2, i3, i4].some((i) => i >= 0);

  if (hasNum) {
    // 编号/【】位置分隔，各段互不重叠
    const seg = (from: number, to: number) =>
      from >= 0 ? clean(t.slice(from + 1, to >= 0 ? to : undefined)) : '';
    out.summary = i1 >= 0 ? seg(i1, i2 >= 0 ? i2 : i3 >= 0 ? i3 : i4) : '';
    out.chain = seg(i2, i3);
    const reasonRaw = seg(i3, i4);
    out.reasoning = reasonRaw || t;
    const decRaw = i4 >= 0 ? t.slice(i4 + 1) : '';
    out.decisions = toDecItems(extractJsonBlocks(hasBra ? decRaw : decRaw || t));
    if (!out.summary && i1 < 0 && i2 > 0) {
      // 无非标题①直接②开头：②前（如有）即摘要
      const pre2 = clean(t.slice(0, i2));
      out.summary = pre2;
    }
    out.summary = out.summary || (t.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '');
    if (!hasBra && !out.chain && i2 >= 0 && i3 > i2) {
      out.chain = clean(t.slice(i2 + 1, i3));
    }
  } else if (hasBra) {
    // 仅【】标记：按原逻辑
    const mH = t.match(/【总体总结】([\s\S]*?)(?=【分析链路】|【推理论证】|【决策】|$)/);
    const mC = t.match(/【分析链路】([\s\S]*?)(?=【推理论证】|【决策】|$)/);
    const mR = t.match(/【推理论证】([\s\S]*?)(?=【决策】|$)/);
    const mD = t.match(/【决策】([\s\S]*)$/);
    if (mH) out.summary = clean(mH[1]);
    if (mC) out.chain = clean(mC[1]);
    if (mR) out.reasoning = clean(mR[1]);
    out.decisions = toDecItems(extractJsonBlocks(mD ? mD[1] : t));
    if (!mH) {
      const endM = t.match(/[②③④]|^分析链路|^推理论证|^\s*决策|^【/m);
      const body = endM && endM.index != null ? t.slice(0, endM.index) : t;
      out.summary = clean(body);
    }
  } else {
    // 完全无标记：摘要=首段；推理=去掉已调用行与决策 JSON 尾巴全文
    const firstPar = t.split('\n\n')[0] ?? t;
    out.summary = clean(firstPar).slice(0, 260);
    const lines = t.split('\n').filter((l) => !/^\s*已调用/.test(l));
    out.reasoning = lines.join('\n').trim();
    out.decisions = toDecItems(extractJsonBlocks(t));
  }
  return out;
}