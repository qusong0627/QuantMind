/** 新闻 agent 行式协议解析（纯函数，供 NewsProtocolView 与 vitest）。
 *
 *  六段新闻 agent 的输出是「每行一条、字段以 | 分隔」的填表协议（scripts/news_brief.py
 *  的 _system_for 定义），直接当散文渲染会变成一屏竖线：门卫的 35 行 `23 skip`、
 *  主编的 `H | 代码 | 名称 | 利好 | 1 | 短期 | 其他 | 标题 | 来源 | 备注` 都是表格数据。
 *  这里按 agent 逐段声明字段表，把每行拆成带标签的单元格；拆不出的行回落成散文。
 *
 *  字段表必须逐 agent 声明：同一个 token 在不同段含义不同（H 在持仓段 7 列、在主编段 10 列）。
 */

/** 单元格渲染方式：code=等宽代码名 / name=强调名 / num=数值 / sent=方向色 / long=整行块 / text=普通 */
export type FieldKind = 'code' | 'name' | 'num' | 'sent' | 'long' | 'text';

export interface ProtoField {
  k: string;
  kind?: FieldKind;
}

export interface ProtoRowSpec {
  token: string;
  label: string;
  /** 行样式 key（CSS .npx-row-<cls>） */
  cls: string;
  fields: ProtoField[];
}

export interface ProtoSpec {
  agent: string;
  rows: ProtoRowSpec[];
}

export interface ProtoCell {
  k: string;
  v: string;
  kind: FieldKind;
}

export interface ProtoRow {
  token: string;
  label: string;
  cls: string;
  cells: ProtoCell[];
  /** 多余的字段（声明之外）拼进第 last 个 long 单元格，避免静默丢数据 */
  raw: string;
}

/** 新闻门卫：黑名单式 `序号 方向`（默认全保留，只写例外） */
export interface GateParsed {
  skip: number[];
  macro: number[];
  holdings: number[];
  /** 非 `序号 方向` 的杂行（少见，保留原文） */
  plain: string[];
}

export interface ParsedProto {
  rows: ProtoRow[];
  /** 无法按协议解析的行（散文 / 表格外说明），原样展示 */
  plain: string[];
}

const F = (k: string, kind: FieldKind = 'text'): ProtoField => ({ k, kind });

/** 各段字段表（与 scripts/news_brief.py 的 _system_for 一一对应） */
export const PROTO_SPECS: Record<string, ProtoSpec> = {
  'news-macro': {
    agent: 'news-macro',
    rows: [
      { token: 'V', label: '观点', cls: 'view', fields: [F('观点', 'long'), F('偏向', 'sent')] },
      { token: 'D', label: '主线', cls: 'driver', fields: [F('主线', 'long'), F('传导', 'long')] },
      { token: 'R', label: '风险', cls: 'risk', fields: [F('风险', 'long'), F('严重度', 'num')] },
      { token: 'C', label: '置信度', cls: 'conf', fields: [F('置信度', 'num')] },
    ],
  },
  'news-micro': {
    agent: 'news-micro',
    rows: [
      {
        token: 'T',
        label: '主题',
        cls: 'topic',
        fields: [F('主题', 'name'), F('驱动逻辑', 'long'), F('强度', 'num'), F('催化', 'long')],
      },
      {
        token: 'E',
        label: '个股事件',
        cls: 'event',
        fields: [F('代码', 'code'), F('公司', 'name'), F('事件', 'text'), F('情绪', 'sent'), F('备注', 'long')],
      },
      { token: 'O', label: '轮动观察', cls: 'observe', fields: [F('观察', 'long')] },
      { token: 'C', label: '置信度', cls: 'conf', fields: [F('置信度', 'num')] },
    ],
  },
  'news-holdings': {
    agent: 'news-holdings',
    rows: [
      {
        token: 'H',
        label: '持仓传导',
        cls: 'hold',
        fields: [
          F('代码', 'code'),
          F('名称', 'name'),
          F('判定', 'sent'),
          F('力度', 'num'),
          F('传导链', 'long'),
          F('来源', 'text'),
        ],
      },
      { token: 'X', label: '跨持仓联动', cls: 'cross', fields: [F('代码', 'code'), F('说明', 'long')] },
      { token: 'A', label: '动作提示', cls: 'action', fields: [F('提示', 'long')] },
      { token: 'C', label: '置信度', cls: 'conf', fields: [F('置信度', 'num')] },
    ],
  },
  'news-chief': {
    agent: 'news-chief',
    rows: [
      { token: 'T', label: '主题', cls: 'topic', fields: [F('主题', 'name'), F('驱动逻辑', 'long')] },
      {
        token: 'W',
        label: '关注标的',
        cls: 'watch',
        fields: [F('代码', 'code'), F('名称', 'name'), F('逻辑', 'long'), F('触发条件', 'long')],
      },
      { token: 'R', label: '开放风险', cls: 'risk', fields: [F('风险', 'long')] },
      {
        token: 'H',
        label: '持仓转写',
        cls: 'hold',
        fields: [
          F('代码', 'code'),
          F('名称', 'name'),
          F('判定', 'sent'),
          F('力度', 'num'),
          F('周期', 'text'),
          F('事件', 'text'),
          F('标题', 'long'),
          F('来源', 'text'),
          F('备注', 'long'),
        ],
      },
      { token: 'N', label: '给市场研究', cls: 'note', fields: [F('参考', 'long')] },
      { token: 'E', label: '编辑备注', cls: 'editor', fields: [F('备注', 'long')] },
      { token: 'C', label: '置信度', cls: 'conf', fields: [F('置信度', 'num')] },
    ],
  },
};

/** 该 agent 是否有行式协议（没有 = 散文输出，如晚间复盘） */
export const protoSpecOf = (agentId: string | undefined): ProtoSpec | null =>
  (agentId && PROTO_SPECS[agentId]) || null;

export const isGateAgent = (agentId: string | undefined): boolean => agentId === 'news-gate';

/** 拆一行 `token | a | b | c`；无竖线或 token 不在表内 → null */
export function parseProtoLine(line: string, spec: ProtoSpec): ProtoRow | null {
  const parts = line.split('|').map((s) => s.trim());
  if (parts.length < 2) return null;
  const token = parts[0].toUpperCase();
  const rs = spec.rows.find((r) => r.token === token);
  if (!rs) return null;
  const vals = parts.slice(1);
  const cells: ProtoCell[] = rs.fields.map((f, i) => ({
    k: f.k,
    kind: f.kind ?? 'text',
    v: (vals[i] ?? '').trim(),
  }));
  // 声明之外的字段（模型多写了几列）不丢：并进最后一个单元格
  const extra = vals.slice(rs.fields.length).filter(Boolean);
  if (extra.length) {
    const last = cells[cells.length - 1];
    if (last) last.v = [last.v, ...extra].filter(Boolean).join(' · ');
  }
  return { token, label: rs.label, cls: rs.cls, cells, raw: line.trim() };
}

/** 把整段输出按协议拆行；空行忽略，拆不出的进 plain */
export function parseProto(text: string, spec: ProtoSpec): ParsedProto {
  const rows: ProtoRow[] = [];
  const plain: string[] = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    const row = parseProtoLine(t, spec);
    if (row) rows.push(row);
    else plain.push(t);
  }
  return { rows, plain };
}

/** 新闻门卫：`12 macro` / `33 skip` / `7 holdings`（空格分隔，非竖线协议） */
export function parseGate(text: string): GateParsed {
  const out: GateParsed = { skip: [], macro: [], holdings: [], plain: [] };
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    const m = t.match(/^(\d+)\s+(skip|macro|holdings|micro)\b/i);
    if (!m) {
      out.plain.push(t);
      continue;
    }
    const no = Number(m[1]);
    const dir = m[2].toLowerCase();
    if (dir === 'skip') out.skip.push(no);
    else if (dir === 'macro') out.macro.push(no);
    else if (dir === 'holdings') out.holdings.push(no);
    // micro 是默认值，门卫不写 —— 真写了也不展示（等于没改）
  }
  return out;
}

/** 折叠卡片的「总结」行：把协议压成一句话（不展开也能看懂这轮干了啥） */
export function protoSummary(text: string, agentId: string | undefined): string {
  if (isGateAgent(agentId)) {
    const g = parseGate(text);
    const kept = g.skip.length + g.macro.length + g.holdings.length;
    if (!kept) return '本轮无剔除 · 全部条目保留（默认 micro）';
    return `剔除 ${g.skip.length} · 转宏观 ${g.macro.length} · 转持仓 ${g.holdings.length}`;
  }
  const spec = protoSpecOf(agentId);
  if (!spec) return '';
  const { rows } = parseProto(text, spec);
  const n = (token: string) => rows.filter((r) => r.token === token).length;
  const conf = rows.find((r) => r.token === 'C')?.cells[0]?.v;
  const confText = conf ? ` · 置信 ${conf}` : '';
  if (agentId === 'news-macro') {
    const v = rows.find((r) => r.token === 'V');
    const view = v?.cells[0]?.v ?? '';
    return `${view ? `${view} · ` : ''}主线 ${n('D')} · 风险 ${n('R')}${confText}`;
  }
  if (agentId === 'news-micro') {
    return `主题 ${n('T')} · 个股事件 ${n('E')}${n('O') ? ' · 轮动观察 1' : ''}${confText}`;
  }
  if (agentId === 'news-holdings') {
    const up = rows.filter((r) => r.token === 'H' && r.cells[2]?.v.includes('利好')).length;
    const down = rows.filter((r) => r.token === 'H' && r.cells[2]?.v.includes('利空')).length;
    return `持仓传导 ${n('H')}（利好 ${up} / 利空 ${down}）· 联动 ${n('X')} · 动作 ${n('A')}${confText}`;
  }
  // news-chief
  return `主题 ${n('T')} · 关注 ${n('W')} · 风险 ${n('R')} · 持仓 ${n('H')}${confText}`;
}
