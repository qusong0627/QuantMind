import React, { useState, useEffect } from 'react';
import {
  Modal, Form, Input, Select, Tag, Alert, Button, message, Divider, Space
} from 'antd';
import { Sparkles, Upload, ShieldCheck, Layers, Brain } from 'lucide-react';
import { modelHubService } from '../../services/modelHubService';
import { UserModelRecord } from '../../services/modelTrainingService';
import { getMeta, getMetrics, resolveMetricNumber, modelDisplayName } from '../modelRegistryUtils';

const { TextArea } = Input;

interface PublishModelModalProps {
  open: boolean;
  onClose: () => void;
  userModels: UserModelRecord[];
  initialModelId?: string | null;
  onSuccess?: () => void;
}

export const PublishModelModal: React.FC<PublishModelModalProps> = ({
  open,
  onClose,
  userModels,
  initialModelId,
  onSuccess,
}) => {
  const [form] = Form.useForm();
  const [selectedModelId, setSelectedModelId] = useState<string | null>(initialModelId || null);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    if (!open || !initialModelId) return;
    setSelectedModelId(initialModelId);
    form.setFieldsValue({ model_id: initialModelId });
  }, [initialModelId, open, form]);

  const currentModel = userModels.find((m) => m.model_id === selectedModelId);
  const meta = currentModel ? getMeta(currentModel) : null;
  const metrics = currentModel ? getMetrics(currentModel) : null;

  const handlePublish = async () => {
    try {
      const values = await form.validateFields();
      if (!currentModel) {
        message.warning('请选择要发布的本地模型');
        return;
      }

      setPublishing(true);
      const sharpe = resolveMetricNumber(metrics, ['sharpe', 'sharpe_ratio', 'annual_sharpe']) || 0;
      const testIC = resolveMetricNumber(metrics, ['test_ic', 'ic', 'ic_mean']) || 0;
      const rankIC = resolveMetricNumber(metrics, ['rank_ic', 'test_rank_ic']) || 0;
      const annualReturn = resolveMetricNumber(metrics, ['annualized_return', 'annual_return']) || 0;
      const maxDrawdown = resolveMetricNumber(metrics, ['max_drawdown']) || 0;
      const calmar = resolveMetricNumber(metrics, ['calmar_ratio', 'calmar']) || 0;

      // 由后端打包本地模型目录（tar.gz）并上传广场，前端只传元数据与 model_id
      const result = await modelHubService.publishLocalModel({
        model_id: currentModel.model_id,
        name: values.name,
        description: values.description,
        market: meta?.market || 'CN',
        algorithm: meta?.algorithm || meta?.model_type || 'CatBoost',
        target_horizon: meta?.target_horizon || 'T+5',
        target_mode: meta?.target_mode || 'classification',
        test_ic: testIC,
        rank_ic: rankIC,
        sharpe_ratio: sharpe,
        annual_return: annualReturn,
        max_drawdown: maxDrawdown,
        calmar_ratio: calmar,
        visibility: values.visibility || 'public',
      });

      message.success('模型已成功发布至社区模型广场！');
      onSuccess?.();
      onClose();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message;
      message.error(`发布失败: ${detail || '未知错误'}`);
    } finally {
      setPublishing(false);
    }
  };

  return (
    <Modal
      title={
        <div className="flex items-center gap-2">
          <Sparkles size={18} className="text-blue-600" />
          <span className="font-black text-slate-800">一键发布模型到广场</span>
        </div>
      }
      open={open}
      onCancel={onClose}
      width={520}
      forceRender
      footer={[
        <Button key="cancel" className="rounded-xl font-bold" onClick={onClose}>
          取消
        </Button>,
        <Button
          key="submit"
          type="primary"
          icon={<Upload size={14} />}
          loading={publishing}
          className="rounded-xl bg-blue-600 font-bold"
          onClick={handlePublish}
        >
          确认发布上线
        </Button>,
      ]}
    >
      <div className="space-y-4 pt-2">
        <Alert
          type="info"
          showIcon
          className="rounded-xl text-xs"
          message="共享模型提示"
          description="发布后，模型元数据与回测指标将同步至社区广场，供其他量化开发者浏览与一键导入验证。"
        />

        <Form form={form} layout="vertical">
          <Form.Item label="选择本地模型" required>
            <Select
              placeholder="选择已训练完成的模型"
              value={selectedModelId}
              onChange={(val) => {
                setSelectedModelId(val);
                const m = userModels.find((it) => it.model_id === val);
                if (m) {
                  form.setFieldsValue({ name: modelDisplayName(m) });
                }
              }}
              options={userModels.map((m) => ({
                value: m.model_id,
                label: `${modelDisplayName(m)} (${m.model_id})`,
              }))}
            />
          </Form.Item>

          {currentModel && (
            <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 mb-4 space-y-2">
              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400 font-semibold">算法类型:</span>
                <span className="font-bold text-slate-700">{meta?.algorithm || meta?.model_type || 'CatBoost'}</span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400 font-semibold">夏普比率 / 测试 IC:</span>
                <span className="font-bold text-slate-700">
                  {resolveMetricNumber(metrics, ['sharpe', 'sharpe_ratio'])?.toFixed(2) || '—'} /{' '}
                  {resolveMetricNumber(metrics, ['test_ic', 'ic'])?.toFixed(3) || '—'}
                </span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400 font-semibold">特征数量:</span>
                <span className="font-bold text-slate-700">{(meta?.features || []).length} 个因子</span>
              </div>
            </div>
          )}

          <Form.Item
            name="name"
            label="广场公开名称"
            rules={[{ required: true, message: '请输入模型在广场展示的名称' }]}
          >
            <Input placeholder="如：L2-CatBoost-T5 增强突破策略" className="rounded-xl" />
          </Form.Item>

          <Form.Item name="description" label="策略简介与说明">
            <TextArea
              rows={3}
              placeholder="简述模型的选股范围、调仓周期、因子侧重与适用行情..."
              className="rounded-xl"
            />
          </Form.Item>

          <Form.Item name="visibility" label="可见性范围" initialValue="public">
            <Select
              options={[
                { value: 'public', label: '公开 (全社区可见)' },
                { value: 'unlisted', label: '凭分享码可见 (不展示在公有广场)' },
              ]}
              className="rounded-xl"
            />
          </Form.Item>
        </Form>
      </div>
    </Modal>
  );
};
