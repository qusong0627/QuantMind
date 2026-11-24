import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { TrainingProvenanceView } from '../TrainingProvenanceView';

vi.mock('../../../services/modelTrainingService', () => ({
  modelTrainingService: {
    getTrainingRunRequest: vi.fn(),
  },
}));

// 评估/审计区块各自会拉接口，这里只验证「训练溯源」自身布局，桩掉重组件。
vi.mock('../TrainingResultView', () => ({
  StrictEvalSection: () => <div>严格评估桩</div>,
  CompareRunsModal: () => null,
  FactorSelectionReport: () => <div>因子筛选桩</div>,
}));
vi.mock('../AuditSection', () => ({
  AuditSection: () => <div>数据审计桩</div>,
}));

const mockedGetRequest = vi.mocked(modelTrainingService.getTrainingRunRequest);

const sampleResult = {
  metrics: {
    train: { rmse: 0.1, auc: 0.5, ic: 0.1312, rank_ic: 0.1125, rank_icir: 0.87 },
    val: { rmse: 0.11, auc: 0.5, ic: 0.092, rank_ic: 0.083, rank_icir: 0.71 },
    test: { rmse: 0.12, auc: 0.5, ic: 0.061, rank_ic: 0.055, rank_icir: 0.52 },
    score_direction: 'normal',
  },
  metadata: {
    display_name: 'T+5 CN 测试模型',
    model_id: 'mdl_provenance',
    market: 'CN',
    target_horizon_days: 5,
    target_mode: 'regression',
    execution_price: 'open',
    label_formula: 'label = close(T+6) / open(T+1) - 1',
    training_window: '2016-01-01 → 2024-12-31',
    feature_count: 30,
    requested_feature_count: 42,
    requested_feature_count_original: 42,
    resolved_feature_count: 35,
    missing_features: ['vpin'],
    feature_categories: ['动量', '流动性'],
    benchmark: 'SH000300',
    objective: 'regression',
    metric: 'l2',
    target_id: 'cn_t5_rank',
    target_version: 1,
    target_hash: 'abcdef1234567890',
    lineage: {
      config_hash: 'cfg-hash-1234567890',
      git_commit: 'deadbeefcafe1234',
      app_image: 'quantmind-train:test',
      factor_catalog_versions: { l1_factors: '20261101' },
      factor_schema_hash: 'schema-hash-123456',
      seed: 7,
    },
    factor_selection: {
      features: [{ name: 'f_mom_20', status: 'selected' }],
      stage_counts: { input: 42 },
      selected: ['f_mom_20'],
    },
  },
  artifacts: ['model.txt', 'pred.parquet'],
  summary: { status: '训练完成', notes: '模型已注册，可进入评估。' },
  model_registration: { status: 'ready', model_id: 'mdl_provenance' },
  completed_at: '2026-11-24T12:00:00Z',
};

const sampleDetail = {
  runId: 'train_provenance_1',
  status: 'completed',
  progress: 100,
  logs: 'line one\nline two',
  result: sampleResult,
  isCompleted: true,
};

const sampleRequest = {
  features: ['f_mom_20', 'f_liq_5'],
  target_horizon_days: 5,
  target_mode: 'regression',
  execution_price: 'open',
  context: { market: 'CN' },
  train_start: '2016-01-01',
  train_end: '2024-12-31',
  valid_start: '2025-01-01',
  valid_end: '2025-06-30',
  test_start: '2025-07-01',
  test_end: '2025-12-31',
};

const renderView = () =>
  render(
    <TrainingProvenanceView
      runId="train_provenance_1"
      meta={{ display_name: 'T+5 CN 测试模型', market: 'CN', created_at: '2026-11-24T10:00:00Z' }}
      detail={sampleDetail}
    />,
  );

describe('TrainingProvenanceView 训练溯源', () => {
  beforeEach(() => {
    mockedGetRequest.mockReset();
    mockedGetRequest.mockResolvedValue({ request: sampleRequest } as never);
  });

  test('档案头展示状态、T+N 与测试集指标', () => {
    renderView();
    expect(screen.getByText('T+5 CN 测试模型')).toBeInTheDocument();
    expect(screen.getByText('已完成')).toBeInTheDocument();
    expect(screen.getAllByText('T+5').length).toBeGreaterThan(0);
    expect(screen.getByText('测试集 RankIC')).toBeInTheDocument();
    expect(screen.getAllByText('0.0550').length).toBeGreaterThan(0);
    expect(screen.getByText('入模特征')).toBeInTheDocument();
    expect(screen.getAllByText('30').length).toBeGreaterThan(0);
  });

  test('结果页展示特征漏斗、配置、血缘与产物', async () => {
    renderView();
    expect(screen.getByText('训练结论')).toBeInTheDocument();
    expect(screen.getByText('数据与特征')).toBeInTheDocument();
    expect(screen.getByText('请求特征')).toBeInTheDocument();
    expect(screen.getByText('目录解析')).toBeInTheDocument();
    expect(screen.getByText('实际入模')).toBeInTheDocument();
    expect(screen.getByText('缺失 1')).toBeInTheDocument();
    expect(screen.getByText('训练配置')).toBeInTheDocument();
    expect(screen.getByText('可复现血缘')).toBeInTheDocument();
    expect(screen.getByText('cn_t5_rank@v1 · abcdef12')).toBeInTheDocument();
    expect(screen.getByText('严格评估桩')).toBeInTheDocument();
    expect(screen.getByText('数据审计桩')).toBeInTheDocument();
    expect(screen.getByText('训练产物')).toBeInTheDocument();
    await waitFor(() => expect(mockedGetRequest).toHaveBeenCalledWith('train_provenance_1'));
  });

  test('切换到日志页展示原始终端日志', () => {
    renderView();
    fireEvent.click(screen.getByRole('button', { name: /日志/ }));
    expect(screen.getByText('训练日志')).toBeInTheDocument();
    expect(screen.getByText(/line one/)).toBeInTheDocument();
  });

  test('切换到提交配置页展示钉扎载荷', async () => {
    renderView();
    fireEvent.click(screen.getByRole('button', { name: /提交配置/ }));
    expect(await screen.findByText('提交配置')).toBeInTheDocument();
    expect(await screen.findByText(/"f_mom_20"/)).toBeInTheDocument();
  });

  test('没有 run 时给出说明而不是空面板', () => {
    render(<TrainingProvenanceView runId={null} />);
    expect(screen.getByText('该模型没有关联训练任务')).toBeInTheDocument();
  });
});
