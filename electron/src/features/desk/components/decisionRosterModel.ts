/** 决策名册（P2.9）视图模型：**纯函数**，只做展示与载荷组装。
 *
 * 校验**不在这里重写一遍**：谁算合法（模型名占位符、归一后重名、家数上限、key 变量名
 * 冲突）的唯一出处是后端 `shared/decision_llm_client.resolve_roster`，保存失败时后端
 * 回的是逐条人话原因（点名第几项/哪家/哪个变量）。前端再写一套「我以为的规则」，
 * 迟早出现「界面放行、后端拒收」或反过来——那才是真的坑用户。
 * 这里只保留两件**纯展示**的事：把服务端返回的状态翻成中文，以及把编辑草稿组装成载荷。
 */

/** 与后端 `order_contract.normalize_agent` 同口径（去空白 + 截断），仅用于展示分组。 */
export const AGENT_LEN = 64;

export interface RosterEntryView {
  index: number;
  model: string;
  agent: string;
  ok: boolean;
  error: string;
  base_url: string;
  base_url_env: string;
  api_key_env: string;
  api_key_inline: boolean;
  api_key_set: boolean;
  timeout: number | null;
  max_tokens: number | null;
  temperature: number | null;
  status: RoundStatus | null;
}

/** `trade:decision-round:last:{agent}` 的 JSON（`decision_round_core.as_status`）。 */
export interface RoundStatus {
  ts?: string;
  day?: string;
  slot?: string;
  slot_label?: string;
  schema?: string;
  round_id?: string;
  status?: string;
  note?: string;
  agent?: string;
  mode?: string;
  decisions?: number;
  legs?: number;
  submitted?: number;
  failed?: number;
  watch_armed?: number;
  audit_rows?: number;
  errors?: string[];
}

export interface RosterState {
  roster_configured: boolean;
  source: string;
  entries: RosterEntryView[];
  limit: number;
  env: string;
  runtime_env_path: string;
  round: { enabled: boolean; env: string; note: string };
  last: RoundStatus | null;
  status_error: string;
  single: { ok: boolean; error: string };
  error: string;
}

export interface RosterDraft {
  model: string;
  baseUrl: string;
  apiKey: string;
  clearKey: boolean;
  timeout: string;
  maxTokens: string;
  temperature: string;
}

export interface RosterSaveEntry {
  model: string;
  base_url?: string;
  api_key?: string;
  clear_api_key?: boolean;
  timeout?: number;
  max_tokens?: number;
  temperature?: number;
}

export function blankDraft(): RosterDraft {
  return {
    model: '', baseUrl: '', apiKey: '', clearKey: false,
    timeout: '', maxTokens: '', temperature: '',
  };
}

export function draftFromEntry(entry: RosterEntryView): RosterDraft {
  return {
    model: entry.model,
    // 变量名形态的端点不可回填成字面值（回填就等于把变量名写死成字面值）
    baseUrl: entry.base_url_env ? '' : entry.base_url || '',
    apiKey: '',
    clearKey: false,
    timeout: entry.timeout === null ? '' : String(entry.timeout),
    maxTokens: entry.max_tokens === null ? '' : String(entry.max_tokens),
    temperature: entry.temperature === null ? '' : String(entry.temperature),
  };
}

/** 草稿 → PUT 载荷。留空的字段**不传**（=沿用原值），空 key 只有在用户明确点过
 *  「清除」时才发 clear_api_key——「没填」和「要删掉」是两件事，混了会静默删 key。 */
export function buildSavePayload(drafts: RosterDraft[]): { entries: RosterSaveEntry[] } {
  const entries = drafts.map((draft) => {
    const entry: RosterSaveEntry = { model: draft.model.trim() };
    const baseUrl = draft.baseUrl.trim();
    if (baseUrl) entry.base_url = baseUrl;
    const apiKey = draft.apiKey.trim();
    if (apiKey) entry.api_key = apiKey;
    if (draft.clearKey) entry.clear_api_key = true;
    const timeout = toNumber(draft.timeout);
    if (timeout !== null) entry.timeout = timeout;
    const maxTokens = toNumber(draft.maxTokens);
    if (maxTokens !== null) entry.max_tokens = maxTokens;
    const temperature = toNumber(draft.temperature);
    if (temperature !== null) entry.temperature = temperature;
    return entry;
  });
  return { entries };
}

function toNumber(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

// ── 展示文案（后端状态 → 中文）─────────────────────────────────────

export type Tone = 'ok' | 'warn' | 'bad' | 'off';

export function sourceLabel(source: string): string {
  if (source === 'roster') return '名册';
  if (source === 'single') return '单家三件套';
  if (source === 'none') return '未配置';
  return source || '未知';
}

export function sourceTone(source: string): Tone {
  if (source === 'roster') return 'ok';
  if (source === 'single') return 'warn';
  return 'bad';
}

export function rosterHeadline(state: RosterState): string {
  if (!state.roster_configured) {
    return state.source === 'single' ? '单家三件套（未启名册）' : '一家都没配';
  }
  const bad = state.entries.filter((e) => !e.ok).length;
  const head = `名册 ${state.entries.length}/${state.limit} 家`;
  return bad ? `${head} · ${bad} 家不可用` : head;
}

/** 这家的 key 从哪来 / 有没有配（**永不回显值**：后端只回布尔与变量名）。 */
export function entryKeyText(entry: RosterEntryView): string {
  if (entry.api_key_inline) return '已配置（直接写在名册里，建议改存变量）';
  if (entry.api_key_env) {
    return entry.api_key_set ? `已配置（变量 ${entry.api_key_env}）` : `未配置（变量 ${entry.api_key_env} 是空的）`;
  }
  return entry.api_key_set ? '已配置（回落全局三件套）' : '未配置';
}

export function entryEndpointText(entry: RosterEntryView): string {
  if (entry.base_url) return entry.base_url;
  if (entry.base_url_env) return `变量 ${entry.base_url_env}`;
  return '（回落全局端点）';
}

/** 逐家调参：一个都没覆盖时说「跟随全局」，别显示成三个空值。 */
export function tuningText(entry: RosterEntryView): string {
  const parts: string[] = [];
  if (entry.timeout !== null) parts.push(`超时 ${entry.timeout}s`);
  if (entry.max_tokens !== null) parts.push(`上限 ${entry.max_tokens}`);
  if (entry.temperature !== null) parts.push(`温度 ${entry.temperature}`);
  return parts.length ? parts.join(' · ') : '跟随全局';
}

const ROUND_LABELS: Record<string, string> = {
  ok: '完成',
  skipped: '跳过',
  aborted: '中止',
  llm_failed: '模型未就绪',
  error: '出错',
};

export function roundStatusLabel(status: string | undefined): string {
  if (!status) return '无记录';
  return ROUND_LABELS[status] || status; // 未登记的取值原样回显，不编
}

export function roundStatusTone(status: string | undefined): Tone {
  if (status === 'ok') return 'ok';
  if (status === 'skipped' || status === 'aborted') return 'warn';
  if (status === 'llm_failed' || status === 'error') return 'bad';
  return 'off';
}

/** 某家最后一条状态镜像的一句话（时间 + 槽位 + 结果 + 提交腿数）。 */
export function entryStatusLine(status: RoundStatus | null): string {
  if (!status) return '无状态镜像';
  const when = String(status.ts || '').replace('T', ' ').slice(0, 16);
  const bits = [when, status.slot_label || status.slot || '', roundStatusLabel(status.status)];
  if (typeof status.legs === 'number') bits.push(`腿 ${status.legs}`);
  if (typeof status.submitted === 'number') bits.push(`提交 ${status.submitted}`);
  if (status.failed) bits.push(`失败 ${status.failed}`);
  const head = bits.filter(Boolean).join(' · ');
  return status.note ? `${head} —— ${status.note}` : head;
}

/** 「最后完成的一家」汇总（不带家段那把键的语义）。 */
export function lastRoundLine(state: RosterState | null): string {
  if (!state) return '—';
  if (!state.last) return '无状态镜像（这台机器上还没跑成过一轮）';
  return entryStatusLine(state.last);
}

/** 清空按钮的禁用原因（空串 = 可清空）。条件取服务端的 `single.ok`，不自己判。 */
export function clearGuard(state: RosterState): string {
  if (!state.roster_configured) return '当前没有名册可清';
  if (!state.single.ok) {
    return '单家三件套未配置：清空后决策轮一家都跑不了，后端会拒绝并回滚。请先配好 QM_DECISION_LLM_BASE_URL / _API_KEY / _MODEL';
  }
  return '';
}

/** 家数上限提示（**只提示不拦**：真正的上限判定在后端，报错也是后端的话）。 */
export function overLimitHint(state: RosterState, count: number): string {
  return count > state.limit ? `已 ${count} 家、超过上限 ${state.limit} 家，保存会被拒绝` : '';
}
