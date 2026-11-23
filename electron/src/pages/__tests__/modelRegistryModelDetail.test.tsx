import { beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { UserModelRecord } from '../../services/modelTrainingService';
import { modelTrainingService } from '../../services/modelTrainingService';
import { listScores } from '../../features/eval-center/services/evalCenterService';
import { ModelDetailPanel } from '../modelRegistryPanels';

// 融合/评估相关子组件不在本测试范围；详情面板本身是纯展示。
vi.mock('../../components/shared/EvalScoreBadge', () => ({
  EvalScoreBadge: () => null,
}));

// 跨模型共用口径依赖最新评分（A 级、A/B 兜底）；测试里给显式评级，不碰网络。
vi.mock('../../features/eval-center/services/evalCenterService', () => ({
  listScores: vi.fn(),
}));

const mockedListScores = vi.mocked(listScores);

beforeEach(() => {
  mockedListScores.mockReset();
  mockedListScores.mockResolvedValue({
    success: true,
    data: [
      { object_type: 'model', object_id: 'mdl_detail', grade: 'A' },
      { object_type: 'model', object_id: 'mdl_other', grade: 'A' },
    ],
    meta: { count: 2, object_type: 'model' },
  } as any);
});

const FEATURES = Array.from({ length: 80 }, (_, i) => `feat_${i + 1}`);

function detailModel(): UserModelRecord {
  return {
    tenant_id: 'default',
    user_id: '1',
    model_id: 'mdl_detail',
    source_run_id: '',
    status: 'ready',
    storage_path: '',
    model_file: '',
    is_default: false,
    created_at: '2026-11-01T00:00:00Z',
    metrics_json: {},
    metadata_json: {
      display_name: '详情测试模型',
      model_type: 'lightgbm',
      target_horizon_days: 5,
      target_mode: 'regression',
      label_formula: 'ref($close,-5)/$close-1',
      features: FEATURES,
      feature_categories: ['Alpha158多窗', '动量', '波动与风险'],
      performance_metrics: {
        train: { mean_ic: 0.1532, rank_ic: 0.1433, icir: 1.204 },
        valid: { mean_ic: 0.121, rank_ic: 0.093, icir: 0.985 },
        test: { mean_ic: 0.1227, rank_ic: 0.105, icir: 0.063 },
      },
      train_start: '2020-01-01',
      train_end: '2023-12-31',
      val_start: '2024-01-01',
      val_end: '2024-12-31',
      test_start: '2025-01-01',
      test_end: '2025-12-31',
      wfa: {
        enabled: true,
        windows: [
          { window_idx: 0, val_start: '2024-01-01', val_end: '2024-06-30', ic: 0.08 },
          { window_idx: 1, val_start: '2024-07-01', val_end: '2024-12-31', ic: -0.02 },
        ],
        stability: 'unstable',
        ic_mean: 0.05,
        ic_std: 0.03,
        overall_icir: 0.4,
        positive_rate: 0.6,
        strategy: 'rolling',
        model_type: 'lightgbm',
      },
    },
  };
}

describe('ModelDetailPanel 一屏可读', () => {
  test('性能指标按三段展示，测试集 IC/IR 可见', () => {
    render(<ModelDetailPanel model={detailModel()} />);
    expect(screen.getByText('性能指标')).toBeInTheDocument();
    expect(screen.getAllByText('训练集').length).toBeGreaterThan(0);
    expect(screen.getAllByText('验证集').length).toBeGreaterThan(0);
    expect(screen.getAllByText('测试集').length).toBeGreaterThan(0);
    expect(screen.getByText('0.1227')).toBeInTheDocument();
    expect(screen.getByText('0.063')).toBeInTheDocument();
    // 六格数值：IC / RankIC / IR 三段分开取，RankIC 不能靠 IC 顶替
    expect(screen.getByText('0.1433')).toBeInTheDocument();
    expect(screen.getByText('0.0930')).toBeInTheDocument();
    expect(screen.getByText('0.1050')).toBeInTheDocument();
    // 验证/测试卡展示「较上段」IC 相对变化（验证 vs 训练、测试 vs 验证）
    expect(screen.getByText('较上段 -21.0%')).toBeInTheDocument();
    expect(screen.getByText('较上段 +1.4%')).toBeInTheDocument();
  });

  test('metadata.metrics 平铺的 test_rank_ic 单独进 RankIC 格', () => {
    const model = detailModel();
    const meta = model.metadata_json as Record<string, any>;
    delete meta.performance_metrics;
    meta.metrics = {
      train_ic: 0.16, train_rank_ic: 0.15, train_rank_icir: 1.4,
      valid_ic: 0.13, valid_rank_ic: 0.12, valid_rank_icir: 0.9,
      test_ic: 0.12, test_rank_ic: 0.11, test_rank_icir: 0.7,
    };
    render(<ModelDetailPanel model={model} />);
    expect(screen.getByText('0.1600')).toBeInTheDocument();
    expect(screen.getByText('0.1100')).toBeInTheDocument();
    expect(screen.getByText('0.700')).toBeInTheDocument();
  });

  test('写明怎么用：截面排名分口径与预测周期', () => {
    render(<ModelDetailPanel model={detailModel()} />);
    expect(screen.getByText('截面排名分')).toBeInTheDocument();
    expect(screen.getByText(/T\+5；跨模型/)).toBeInTheDocument();
  });

  test('训练时间标注在三段指标卡上，特征清单默认折叠可展开', async () => {
    render(<ModelDetailPanel model={detailModel()} />);
    // 原样本时间轴的信息（起止日期 / 天数 / 占比）并入性能指标的三段卡
    expect(screen.getByText(/2025-01-01 → 2025-12-31/)).toBeInTheDocument();
    expect(screen.queryByText('样本时间轴')).not.toBeInTheDocument();
    const featureCard = screen.getByText('特征工程').closest('section') as HTMLElement;
    // 分类 chips 直接展示（不再要「入模分类」标题）
    expect(within(featureCard).getByText('Alpha158多窗')).toBeInTheDocument();
    // 全部特征都渲染在固定高度的滚动区内（右侧滚动条查看其余特征），不再是弹层
    expect(within(featureCard).getByText('feat_80')).toBeInTheDocument();
  });

  test('WFA 诊断展示稳定性与窗口', () => {
    render(<ModelDetailPanel model={detailModel()} />);
    expect(screen.getByText('WFA 稳定性诊断')).toBeInTheDocument();
    expect(screen.getByText('不稳定')).toBeInTheDocument();
    expect(screen.getByText('-0.0200')).toBeInTheDocument();
  });
});

describe('ModelDetailPanel 特征口径', () => {
  test('只有 feature_columns 的系统模型也能出特征清单', () => {
    const model = detailModel();
    const meta = model.metadata_json as Record<string, any>;
    delete meta.features;
    meta.feature_columns = ['sys_feat_alpha'];
    render(<ModelDetailPanel model={model} />);
    expect(screen.getAllByText('sys_feat_alpha').length).toBeGreaterThan(0);
  });
});

function otherModel(): UserModelRecord {
  const base = detailModel();
  return {
    ...base,
    model_id: 'mdl_other',
    metadata_json: {
      ...(base.metadata_json as Record<string, any>),
      display_name: '其他模型',
      features: ['feat_1', 'feat_2', 'feat_3', 'feat_99'],
      label_formula: 'label = future_return(T, T+3)',
    },
  };
}

describe('ModelDetailPanel 特征 × 模型图谱', () => {
  async function openPanel() {
    const user = userEvent.setup();
    const onSelectModel = vi.fn();
    render(
      <ModelDetailPanel
        model={detailModel()}
        allModels={[detailModel(), otherModel()]}
        onSelectModel={onSelectModel}
      />,
    );
    await user.click(screen.getByRole('button', { name: '特征 × 模型图谱' }));
    const panel = await screen.findByTestId('feature-matrix');
    return { user, panel, onSelectModel };
  }

  test('弹出图谱：共用特征显示使用模型数，点模型可跳转', async () => {
    const { user, panel, onSelectModel } = await openPanel();
    expect(within(panel).getAllByText('2 个模型').length).toBeGreaterThan(0);
    await user.click(within(panel).getAllByRole('button', { name: /其他模型/ })[0]);
    expect(onSelectModel).toHaveBeenCalledWith('mdl_other');
  });

  test('点「共用最多」特征，图谱弹出并聚焦该特征', async () => {
    const user = userEvent.setup();
    render(
      <ModelDetailPanel
        model={detailModel()}
        allModels={[detailModel(), otherModel()]}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'feat_1 ×2' }));
    const panel = await screen.findByTestId('feature-matrix');
    const input = within(panel).getByPlaceholderText('搜索特征名 / 模型名…') as HTMLInputElement;
    expect(input.value).toBe('feat_1');
  });

  test('搜索特征名只保留命中行', async () => {
    const { user, panel } = await openPanel();
    const input = within(panel).getByPlaceholderText('搜索特征名 / 模型名…');
    await user.type(input, 'feat_99');
    expect(within(panel).getByText('feat_99')).toBeInTheDocument();
    expect(within(panel).queryByText('feat_4')).not.toBeInTheDocument();
  });

  test('只看共用：隐藏仅当前模型使用的特征', async () => {
    const { user, panel } = await openPanel();
    await user.click(within(panel).getByLabelText(/只看共用/));
    expect(within(panel).getByText('feat_1')).toBeInTheDocument();
    expect(within(panel).queryByText('feat_30')).not.toBeInTheDocument();
  });

  test('矩阵视图：共用特征 × 模型 heat 格', async () => {
    const { user, panel } = await openPanel();
    await user.click(within(panel).getByRole('tab', { name: '矩阵' }));
    expect(within(panel).getByText(/共用特征 Top/)).toBeInTheDocument();
    expect(within(panel).getAllByText('feat_1').length).toBeGreaterThan(0);
  });

  test('共用统计只算 A 级模型：C 级模型不计入', async () => {
    mockedListScores.mockResolvedValueOnce({
      success: true,
      data: [
        { object_type: 'model', object_id: 'mdl_detail', grade: 'A' },
        { object_type: 'model', object_id: 'mdl_other', grade: 'C' },
      ],
      meta: { count: 2, object_type: 'model' },
    } as any);
    const user = userEvent.setup();
    render(<ModelDetailPanel model={detailModel()} allModels={[detailModel(), otherModel()]} />);
    // A 级只有 1 个、A/B 兜底仍不足 2 个 → 共用统计为空，不把 C 级模型算进来
    expect(await screen.findByText(/跨模型共用 · A\/B 级（暂无）/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '特征 × 模型图谱' }));
    const panel = await screen.findByTestId('feature-matrix');
    expect(within(panel).queryByText('feat_99')).not.toBeInTheDocument();
  });

  test('A 级不足 2 个时降到 A/B：B 级模型参与共用统计', async () => {
    mockedListScores.mockResolvedValueOnce({
      success: true,
      data: [
        { object_type: 'model', object_id: 'mdl_detail', grade: 'A' },
        { object_type: 'model', object_id: 'mdl_other', grade: 'B' },
      ],
      meta: { count: 2, object_type: 'model' },
    } as any);
    render(<ModelDetailPanel model={detailModel()} allModels={[detailModel(), otherModel()]} />);
    expect(await screen.findByText(/跨模型共用 · A\/B 级 2 个模型/)).toBeInTheDocument();
  });
});

describe('ModelDetailPanel 训练图表', () => {
  test('无训练记录时给出空态提示', () => {
    render(<ModelDetailPanel model={detailModel()} />);
    expect(screen.getByText(/该模型没有训练记录图表/)).toBeInTheDocument();
  });

  test('训练记录就绪时展示 IC 评估图与三段指标卡（报告不在详情页重复）', async () => {
    const spy = vi.spyOn(modelTrainingService, 'getTrainingRun').mockResolvedValue({
      runId: 'run_chart_1',
      status: 'completed',
      progress: 100,
      logs: '',
      isCompleted: true,
      result: {
        metrics: {
          train: { ic: 0.17, rank_ic: 0.14 },
          val: { ic: 0.13, rank_ic: 0.09 },
          test: { ic: 0.14, rank_ic: 0.10 },
        },
        eval_report: {
          return_basis: 'label_return',
          n_days: 5,
          n_rows: 1000,
          by_split: {
            test: { mean: 0.105, icir: 1.303, win_rate: 0.887 },
            train: { mean: 0.0955, icir: 0.94, win_rate: 0.83 },
            valid: { mean: 0.0897, icir: 0.68, win_rate: 0.78 },
          },
          yearly: [
            { year: 2025, mean: 0.099, icir: 0.9 },
            { year: 2026, mean: 0.07, icir: 0.5 },
          ],
          groups: {
            n_groups: 10,
            mean_returns: [0.01, 0.02, 0.01, 0.02, 0.03, 0.02, 0.03, 0.04, 0.03, 0.05],
          },
          long_short: { sharpe: 1.2, ann_return: 0.15, curve: [['2025-01-01', 0], ['2025-02-01', 0.05]] },
          tradability: { applied: true, excluded_rows: 42 },
        },
      },
    } as any);
    const model = detailModel();
    model.source_run_id = 'run_chart_1';
    render(<ModelDetailPanel model={model} />);
    expect(await screen.findByText(/IC 评估/)).toBeInTheDocument();
    expect(screen.getByText('0.1000')).toBeInTheDocument();
    expect(screen.getAllByText('样本外').length).toBeGreaterThan(0);
    // 模型评估报告只在「训练溯源」展示，详情页不再重复铺一份
    expect(screen.queryByText('模型评估报告')).not.toBeInTheDocument();
    expect(screen.queryByText(/多空组合累计净值/)).not.toBeInTheDocument();
    spy.mockRestore();
  });
});
