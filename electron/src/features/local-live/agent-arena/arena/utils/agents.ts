/** agent 签名的展示名映射（展示层唯一入口）。
 *
 *  签名是目录名/接口参数（`market-research`），所有取数调用必须原样传签名；
 *  只有渲染才换中文。2026-09-12 P2：混合对话流此前直接渲染 `r.name`，
 *  `market-research` 在「全部模型」视图裸露英文——旧的「缺了才补中文卡」
 *  兜底因 overview 早已含该行而永不触发。
 */
const DISPLAY_NAMES: Record<string, string> = {
  'market-research': '市场研究',
};

export function displayAgentName(name: string | null | undefined): string {
  const key = String(name ?? '');
  return DISPLAY_NAMES[key] ?? key;
}

export interface Performer {
  name: string;
  ret: number;
}

/** 涨幅榜的最高/最低（Live 顶栏 chip）。展示名映射放在函数内——调用点直接渲染
 *  `r.name` 时，下跌行情里 0% 的研究线常排最高，页面会裸露英文签名
 *  （2026-09-12 审查 LOW）。返回 null 表示无收益数据，页面照旧渲染「—」。 */
export function rankPerformers(
  rows: { name: string; ret: number | null | undefined }[],
): { highest: Performer | null; lowest: Performer | null } {
  const list = rows
    .map((r) => ({ name: displayAgentName(r.name), ret: r.ret }))
    .filter((p): p is Performer => p.ret != null)
    .sort((a, b) => b.ret - a.ret);
  return { highest: list[0] ?? null, lowest: list[list.length - 1] ?? null };
}
