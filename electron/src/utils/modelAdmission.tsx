import { Modal } from 'antd';

/** 后端标准化评估闸门返回的 409 详情（code=model_signal_eval_gate）。 */
export interface AdmissionGateDetail {
  code: string;
  action?: string;
  grade?: string;
  reasons?: string[];
  window_end?: string | null;
  message?: string;
}

export function getAdmissionGate(error: unknown): AdmissionGateDetail | null {
  const response = (
    error as { response?: { status?: number; data?: { detail?: unknown } } }
  )?.response;
  if (!response || response.status !== 409) return null;
  const detail = response.data?.detail;
  if (!detail || typeof detail !== 'object') return null;
  const gate = detail as AdmissionGateDetail;
  return gate.code === 'model_signal_eval_gate' ? gate : null;
}

const GRADE_LABELS: Record<string, string> = {
  red: '未通过',
  unknown: '未评估',
  yellow: '黄色预警',
  green: '通过',
};

/** 二次确认：明确展示未过项，确认后由调用方带 force=true 重试。 */
export function confirmAdmissionOverride(
  gate: AdmissionGateDetail,
  actionLabel = '仍然继续',
): Promise<boolean> {
  const reasons = (gate.reasons ?? []).filter(Boolean);
  return new Promise((resolve) => {
    Modal.confirm({
      title: '该模型未通过标准化评估',
      content: (
        <div className="space-y-2 text-[12px] leading-relaxed text-slate-600">
          <div>
            评估等级：
            <b className="text-rose-500">{GRADE_LABELS[gate.grade ?? 'unknown'] ?? gate.grade}</b>
            {gate.window_end ? <span className="text-slate-400">（窗口止 {gate.window_end}）</span> : null}
          </div>
          {reasons.length > 0 ? (
            <div>
              未过项：
              <span className="text-rose-500">{reasons.join('、')}</span>
            </div>
          ) : null}
          <div className="text-[11px] text-slate-400">
            继续会让该模型参与默认推理/策略绑定。建议先在模型详情「测试集实盘力」里完成标准化评估。
          </div>
        </div>
      ),
      okText: actionLabel,
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
}
