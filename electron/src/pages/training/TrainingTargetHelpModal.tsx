import React from 'react';
import { Modal, Tag } from 'antd';
import { clsx } from 'clsx';
import { BookOpen, Target, AlertTriangle } from 'lucide-react';

/** 标签截面变换的用法说明与按模型建议（只解释口径，不替用户改配置）。 */
export interface TrainingTargetHelpModalProps {
  open: boolean;
  onClose: () => void;
}

const TRANSFORMS: Array<{
  key: string;
  name: string;
  formula: string;
  good: string;
  bad: string;
  models: string;
}> = [
  {
    key: 'rank',
    name: '截面 rank（默认）',
    formula: '每日 pct rank − 0.5（≈ −0.5 ~ +0.5）',
    good: '只看“排第几”，对涨跌停、肥尾、极端收益最稳；A 股截面选股的基线',
    bad: '丢掉幅度信息，不能直接当收益/仓位映射',
    models: '全部模型首选；树模型/线性/NN 都适用',
  },
  {
    key: 'zscore',
    name: '截面 zscore',
    formula: '每日 (x − 当日均值) / 当日标准差',
    good: '保留相对幅度，NN/线性训练更平滑；便于做分数加权',
    bad: '对极端值敏感；A 股涨跌停/重组股会拉偏 std',
    models: 'MLP/GRU/LSTM/ALSTM/Transformer/TFT/TCN/TabNet、Linear；树模型可对比',
  },
  {
    key: 'raw',
    name: '原始收益',
    formula: '原始 T+N 收益，不做截面处理',
    good: '保留真实收益幅度；研究/归因时最直观',
    bad: '肥尾、异方差、涨跌停主导；NN/线性容易被少数极端样本带偏',
    models: '树模型且能接受肥尾时可用；NN/线性不建议；务必配极端标签清洗',
  },
  {
    key: 'top_tail',
    name: 'top-tail（只学最强尾部）',
    formula: '每日最强 top pct 给 0.5~1 分，其余置 0',
    good: '目标与“只做 TopN 长多”一致；头部样本权重集中',
    bad: '中段信息全部丢掉；零标签过多，全截面 IC/回归幅度会失真',
    models: 'LightGBM/XGBoost/CatBoost 的 TopN 挑战者；NN 不建议（零梯度多）',
  },
  {
    key: 'size_excess_rank',
    name: '同市值桶内超额 rank',
    formula: '先减同市值桶均值，再每日 rank',
    good: '剥离市值 beta，缓解小盘 tilt；看“同规模里谁更强”',
    bad: '需要特征列表含 total_cap，否则提交被拒；绝对收益/IC 可能变低',
    models: '想认真做 size-neutral 的树/线性/NN 都可以；先与 rank 同特征 A/B',
  },
  {
    key: 'size_excess_zscore',
    name: '同市值桶内超额 zscore',
    formula: '先减同市值桶均值，再每日 zscore',
    good: '在去市值 beta 的同时保留幅度；适合 NN/线性',
    bad: '同样依赖 total_cap；桶内样本少时 std 不稳',
    models: 'NN/线性做 size-neutral 的首选；树模型可与 size_excess_rank 对比',
  },
];

const MODEL_ROWS: Array<{ model: string; first: string; alternative: string; avoid: string }> = [
  {
    model: 'LightGBM / XGBoost / CatBoost',
    first: 'rank',
    alternative: 'top_tail（10–20%）做 TopN 挑战者；zscore + 清洗 1–5% 做幅度版',
    avoid: 'raw（除非先做极端值清洗并接受肥尾）',
  },
  {
    model: 'Linear / Ridge',
    first: 'rank 或 zscore',
    alternative: 'size_excess_rank（去市值 beta）',
    avoid: 'raw、top_tail（零标签多，线性拟合不稳）',
  },
  {
    model: 'MLP / GRU / LSTM / ALSTM',
    first: 'zscore 或 rank',
    alternative: 'size_excess_zscore（去市值 beta）',
    avoid: 'raw、top_tail',
  },
  {
    model: 'Transformer / TFT / TCN / TabNet',
    first: 'zscore 或 rank',
    alternative: 'size_excess_zscore；配极端标签清洗 1–3%',
    avoid: 'raw、top_tail',
  },
  {
    model: '多模型融合 / 集合训练',
    first: '所有成员用同一 transform（通常 rank）',
    alternative: '先单模型选型，再统一口径融合',
    avoid: '成员混用 rank/zscore/top_tail（分数不可比）',
  },
  {
    model: '分类目标 / 分位回归',
    first: '不适用',
    alternative: '分类跳过截面变换；分位回归固定 raw（界面会锁定）',
    avoid: '以为改了 transform 会影响这两类目标',
  },
];

const Section: React.FC<{ icon: React.ReactNode; title: string; children: React.ReactNode }> = ({
  icon,
  title,
  children,
}) => (
  <div className="space-y-2">
    <div className="flex items-center gap-2 text-[12px] font-black text-slate-700">
      {icon}
      {title}
    </div>
    {children}
  </div>
);

export const TrainingTargetHelpModal: React.FC<TrainingTargetHelpModalProps> = ({ open, onClose }) => (
  <Modal
    open={open}
    onCancel={onClose}
    footer={null}
    width={980}
    centered
    destroyOnHidden
    style={{ maxWidth: '96vw' }}
    title={<span className="text-sm font-black text-slate-800">标签截面变换：怎么选、为什么</span>}
    styles={{ body: { padding: 18, maxHeight: '80vh', overflowY: 'auto' } }}
  >
    <div className="space-y-5 text-[11px] leading-relaxed text-slate-600">
      <div className="rounded-xl border border-indigo-100 bg-indigo-50/50 px-3.5 py-3">
        <div className="font-bold text-slate-700">一句话</div>
        <p className="mt-1">
          截面变换只改「模型学什么」，不改特征、不引入未来数据。所有变换都按**交易日截面**计算；
          先确定你是做「全截面排序」还是「只做 TopN」，再按模型类型选。
        </p>
      </div>

      <Section icon={<BookOpen size={14} className="text-indigo-500" />} title="六种变换的定义与适用边界">
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full min-w-[860px] border-collapse text-[10px]">
            <thead className="bg-slate-50 text-[9px] font-bold text-slate-500">
              <tr>
                <th className="px-2.5 py-2 text-left">变换</th>
                <th className="px-2.5 py-2 text-left">公式</th>
                <th className="px-2.5 py-2 text-left">适合</th>
                <th className="px-2.5 py-2 text-left">不适合</th>
                <th className="px-2.5 py-2 text-left">推荐模型</th>
              </tr>
            </thead>
            <tbody>
              {TRANSFORMS.map((row, index) => (
                <tr key={row.key} className={clsx(index % 2 === 1 && 'bg-slate-50/40')}>
                  <td className="px-2.5 py-2 font-bold text-slate-700">{row.name}</td>
                  <td className="px-2.5 py-2 font-mono text-[9px] text-slate-500">{row.formula}</td>
                  <td className="px-2.5 py-2 text-emerald-700">{row.good}</td>
                  <td className="px-2.5 py-2 text-rose-600">{row.bad}</td>
                  <td className="px-2.5 py-2 text-slate-600">{row.models}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section icon={<Target size={14} className="text-indigo-500" />} title="按模型类型的中肯建议">
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full min-w-[760px] border-collapse text-[10px]">
            <thead className="bg-slate-50 text-[9px] font-bold text-slate-500">
              <tr>
                <th className="px-2.5 py-2 text-left">模型</th>
                <th className="px-2.5 py-2 text-left">首选</th>
                <th className="px-2.5 py-2 text-left">可对比</th>
                <th className="px-2.5 py-2 text-left">不建议</th>
              </tr>
            </thead>
            <tbody>
              {MODEL_ROWS.map((row, index) => (
                <tr key={row.model} className={clsx(index % 2 === 1 && 'bg-slate-50/40')}>
                  <td className="px-2.5 py-2 font-bold text-slate-700">{row.model}</td>
                  <td className="px-2.5 py-2">
                    <Tag className="m-0 rounded-md border-0 bg-emerald-50 text-[9px] font-bold text-emerald-700">
                      {row.first}
                    </Tag>
                  </td>
                  <td className="px-2.5 py-2 text-slate-600">{row.alternative}</td>
                  <td className="px-2.5 py-2 text-rose-600">{row.avoid}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section icon={<AlertTriangle size={14} className="text-amber-500" />} title="操作建议与陷阱">
        <ol className="list-decimal space-y-1.5 pl-5">
          <li>
            <b>一次只动一轴</b>：同一批特征、同一模型、同一 T+N，只换 transform 做 A/B；不要同时改特征和标签。
          </li>
          <li>
            <b>全截面排序</b>优先 rank；<b>只做 TopN</b> 用 rank 做基线、top_tail 做挑战者。top_tail 的零标签多，
            全截面 IC 会失真，改看 Top 分位收益/价差。
          </li>
          <li>
            <b>NN/线性</b>优先 zscore 或 rank；raw 容易被肥尾带偏。配「极端标签清洗」1–5%（仅训练段，验证/测试不动）。
          </li>
          <li>
            <b>担心小盘 beta</b> 用 size_excess_*，但必须先选含 <code>total_cap</code> 的特征；它常让 IC 变低、
            但剥离市值后的 residual alpha 更干净，要和 rank 同特征对照看。
          </li>
          <li>
            <b>分类目标 / 分位回归</b>不受此下拉影响：分类跳过截面变换，分位回归固定 raw（界面会锁定）。
          </li>
          <li>
            <b>不要跨 transform 直接比 IC</b>：rank、zscore、top_tail 的标签分布不同，IC 不可横向比较；
            要看同一 transform 下的样本外、扣费、TopN 表现。
          </li>
          <li>
            <b>rank 丢掉幅度</b>：如果后续要按分数做仓位加权，rank 只能给顺序；需要幅度就用 zscore 或 raw。
          </li>
        </ol>
      </Section>

      <div className="rounded-xl border border-slate-200 bg-slate-50/60 px-3.5 py-3 text-[10px] text-slate-500">
        本说明是口径建议，不是收益承诺。任何 transform 都要过样本外 + 扣费 + 风格剥离（FF5）再谈上线；
        具体实现见 <code>docker/training/preprocessing.py::cross_sectional_label_transform</code>。
      </div>
    </div>
  </Modal>
);
