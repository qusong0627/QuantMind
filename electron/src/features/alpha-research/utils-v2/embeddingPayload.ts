/**
 * 向量检索（Embedding）配置的「表单 → 请求体」组装。
 *
 * 单独成文件是为了可测：这三行判断承载的是**三态语义**（`undefined` = 不动、
 * `''` = 清除、有值 = 设置），一旦有人改成「无条件全量提交」，只改模型名的
 * 请求就会顺手清掉已存的 Key —— 编译通过、UI 不报错、后端也返回 200，只有
 * 子进程真去检索时才发现换了供应商。这种回归只能靠单测拦住。
 *
 * 后端对应实现见 backend/services/engine/routers/alpha_agent.py 的
 * `build_embedding_payload`（同一套语义，两侧各有测试）。
 */

/** 服务端已知的 embedding 状态；`undefined` 表示状态**没拉到**（后端连不上）。 */
export interface EmbeddingStatus {
  model: string;
  base_url: string;
  has_key: boolean;
  key_masked: string;
}

export interface EmbeddingForm {
  model: string;
  baseUrl: string;
  apiKey: string;
}

export interface EmbeddingSavePayload {
  model?: string;
  baseUrl?: string;
  apiKey?: string;
}

/**
 * 组装 PUT `/alpha-research/.../llm-config/embedding` 的请求体。
 *
 * - `model` / `baseUrl`：只有**相对服务端初值真的变了**才提交。不能无脑提交，
 *   因为状态没拉到时表单是空表，用户只补一个 Key 就会把已存的 model/baseUrl
 *   静默清掉。改成空串仍会提交（= 清空），语义不受影响。
 * - `apiKey`：留空 = 不动。服务端只回掩码、无法回填明文，无脑提交空串会把
 *   已存的 Key 抹掉；要清除请用「清除 Key」（它显式发空串）。
 *
 * @param status 服务端已知状态；为 `undefined` 时**只允许**提交 Key（其余字段
 *   无从比对），配合调用方「状态没拉到就不许写」的守卫一起用。
 * @returns 空对象表示没有需要保存的改动。
 */
export function buildEmbeddingSavePayload(
  form: EmbeddingForm,
  status: EmbeddingStatus | undefined,
): EmbeddingSavePayload {
  const payload: EmbeddingSavePayload = {};
  if (!status) return payload;

  const model = form.model.trim();
  const baseUrl = form.baseUrl.trim();
  if (model !== status.model) payload.model = model;
  if (baseUrl !== status.base_url) payload.baseUrl = baseUrl;

  const apiKey = form.apiKey.trim();
  if (apiKey) payload.apiKey = apiKey;

  return payload;
}
