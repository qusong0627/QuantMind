import React, { useState } from 'react';
import { Modal } from 'antd';
import { HelpCircle, SlidersHorizontal, Gauge, ShieldAlert } from 'lucide-react';

/** 参数配置手册：点击触发按钮/「训练超参」标题，居中弹窗展示。 */
export interface ParameterHelpPopoverProps {
  /** 自定义触发元素（默认渲染「参数用法」按钮）。 */
  children?: React.ReactNode;
}

export const ParameterHelpPopover: React.FC<ParameterHelpPopoverProps> = ({ children }) => {
  const [open, setOpen] = useState(false);
  const content = (
    <div className="max-h-[72vh] overflow-y-auto p-4 text-[11px] leading-relaxed text-slate-600">
      <div className="rounded-xl border border-indigo-100 bg-indigo-50/50 px-3.5 py-3">
        <div className="text-[12px] font-black text-slate-700">先看这三条，再动参数</div>
        <ol className="mt-1.5 list-decimal space-y-1 pl-5">
          <li>
            <b>一次只动一轴</b>：同一特征、同一目标、同一 T+N，只改一组参数；改完看样本外 RankIC、TopN 扣费、回撤。
          </li>
          <li>
            <b>早停靠验证集</b>：`num_boost_round` 是上限，真正停在哪由 `early_stopping_rounds` + 验证集决定；验证集 IC 变差就停。
          </li>
          <li>
            <b>越复杂越容易过拟合</b>：A 股日频信噪比低，优先加正则/降学习率/增叶子最小样本，而不是加大模型。
          </li>
        </ol>
      </div>

      <div className="mt-4">
        <div className="flex items-center gap-2 text-[12px] font-black text-slate-700">
          <SlidersHorizontal size={14} className="text-indigo-500" />
          通用参数（所有模型）
        </div>
        <table className="mt-2 w-full border-collapse text-[10px]">
          <thead className="bg-slate-50 text-[9px] font-bold text-slate-500">
            <tr>
              <th className="px-2 py-1.5 text-left">参数</th>
              <th className="px-2 py-1.5 text-left">怎么用</th>
              <th className="px-2 py-1.5 text-left">建议</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-t border-slate-100">
              <td className="px-2 py-1.5 font-bold">Objective</td>
              <td className="px-2 py-1.5">回归用 MSE/MAE；排序用 lambdarank/xrank 等（仅树模型）</td>
              <td className="px-2 py-1.5">选股默认 <b>regression + RMSE</b>；要做纯排序再试 ranking，必须同特征 A/B</td>
            </tr>
            <tr className="border-t border-slate-100">
              <td className="px-2 py-1.5 font-bold">Metric</td>
              <td className="px-2 py-1.5">训练监控指标，不直接等于收益</td>
              <td className="px-2 py-1.5">回归 RMSE/MAE；排序 IC/RankIC；分类 AUC（训练端会强制）</td>
            </tr>
            <tr className="border-t border-slate-100">
              <td className="px-2 py-1.5 font-bold">seed</td>
              <td className="px-2 py-1.5">同配置 + 同种子可复现</td>
              <td className="px-2 py-1.5">正式模型至少跑 <b>3 个种子</b>看 IC/收益离散度；别用单次最优种子当结论</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="mt-4">
        <div className="flex items-center gap-2 text-[12px] font-black text-slate-700">
          <Gauge size={14} className="text-indigo-500" />
          按模型的起步值与调参优先级
        </div>
        <table className="mt-2 w-full border-collapse text-[10px]">
          <thead className="bg-slate-50 text-[9px] font-bold text-slate-500">
            <tr>
              <th className="px-2 py-1.5 text-left">模型</th>
              <th className="px-2 py-1.5 text-left">起步值</th>
              <th className="px-2 py-1.5 text-left">优先调</th>
              <th className="px-2 py-1.5 text-left">避坑</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">LightGBM</td>
              <td className="px-2 py-2 font-mono text-[9px]">
                leaves 63 · min_data 200 · lr 0.02 · feature_fraction 0.8 · bagging 0.8/freq 1 ·
                lambda_l2 1 · early 100
              </td>
              <td className="px-2 py-2">num_leaves → learning_rate → min_data_in_leaf</td>
              <td className="px-2 py-2 text-rose-600">
                lr&gt;0.1 且 leaves&gt;255 很容易把训练 IC 拉高、样本外塌；路径平滑 0–1 足够
              </td>
            </tr>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">XGBoost</td>
              <td className="px-2 py-2 font-mono text-[9px]">
                depth 6 · min_child_weight 10 · subsample 0.8 · colsample 0.8 · lr 0.03 ·
                reg_lambda 1 · early 100
              </td>
              <td className="px-2 py-2">max_depth → lr → min_child_weight/正则</td>
              <td className="px-2 py-2 text-rose-600">depth&gt;10 在日频截面基本过拟合；subsample/colsample 别同时 &lt;0.6</td>
            </tr>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">CatBoost</td>
              <td className="px-2 py-2 font-mono text-[9px]">
                depth 6 · l2_leaf_reg 5 · random_strength 1 · bagging_temperature 0.5 ·
                lr 0.05 · od_wait 100
              </td>
              <td className="px-2 py-2">depth → l2_leaf_reg → lr</td>
              <td className="px-2 py-2 text-rose-600">
                bagging_temperature 越大越随机；类别特征（行业）CatBoost 原生支持最好
              </td>
            </tr>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">RandomForest</td>
              <td className="px-2 py-2 font-mono text-[9px]">n_estimators 500 · max_depth 12</td>
              <td className="px-2 py-2">max_depth → n_estimators</td>
              <td className="px-2 py-2 text-rose-600">深度不限 + 噪声特征会记住训练集；树多只降方差，不救过拟合</td>
            </tr>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">Ridge / Linear</td>
              <td className="px-2 py-2 font-mono text-[9px]">alpha 1–10（特征标准化后）</td>
              <td className="px-2 py-2">alpha</td>
              <td className="px-2 py-2 text-rose-600">
                alpha 太小 → 共线特征炸方差；alpha 太大 → 全被压成均值；必须配标准化
              </td>
            </tr>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">
                MLP / GRU / LSTM / ALSTM / Transformer / TFT / TCN / TabNet
              </td>
              <td className="px-2 py-2 font-mono text-[9px]">
                hidden 128 · layers 2 · dropout 0.2 · lr 3e-4 · batch 1024 · epochs 100 ·
                seq 30
              </td>
              <td className="px-2 py-2">lr → dropout → hidden/layers → seq_len</td>
              <td className="px-2 py-2 text-rose-600">
                layers&gt;3 在表格日频上大多过拟合；seq_len 太短覆盖不到动量窗口；batch 过大收敛慢
              </td>
            </tr>
            <tr className="border-t border-slate-100 align-top">
              <td className="px-2 py-2 font-bold text-slate-700">TCN / TFT 专属</td>
              <td className="px-2 py-2 font-mono text-[9px]">kernel_size 5 · num_heads 4</td>
              <td className="px-2 py-2">kernel 5→7；heads 4→8</td>
              <td className="px-2 py-2 text-rose-600">TFT heads 必须整除 hidden；TCN kernel 太大在小样本上过拟合</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="mt-4">
        <div className="flex items-center gap-2 text-[12px] font-black text-slate-700">
          <ShieldAlert size={14} className="text-amber-500" />
          训练上下文与其他开关
        </div>
        <ul className="mt-1.5 list-disc space-y-1 pl-5">
          <li>
            <b>手续费/滑点</b>：A 股建议佣金 0.0003–0.001、滑点 0.0005–0.002；回测/评估必须与训练上下文一致，否则收益不可对账。
          </li>
          <li>
            <b>成交价</b>：open = T+1 开盘（严格实盘），close = 旧口径；换成交价必须重训，不能拿旧模型直接改。
          </li>
          <li>
            <b>基准指数</b>：A 股小盘选股用中证1000/中证500，大盘用沪深300；基准决定超额口径。
          </li>
          <li>
            <b>截面预处理</b>：填充/缩尾/标准化会改变特征量纲，与旧模型不可直接比较；开启后同批模型才可比。
          </li>
          <li>
            <b>行业作为特征</b>：仅 CatBoost 原生处理类别特征；其他模型建议走行业中性化标签/截面变换，而不是硬编码。
          </li>
          <li>
            <b>分位模型（P10/P50/P90）</b>：仅 LightGBM、仅 A 股单模型；用于区间/风险，不用于排序。
          </li>
          <li>
            <b>WFA 稳定性诊断</b>：仅树/线性，耗时约 2–3 倍；不是每轮必开，模型候选期再跑。
          </li>
        </ul>
      </div>

      <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2 text-[10px] text-slate-500">
        这些是起步建议，不是最优解。任何参数组合都要过「样本外 + 扣费 + 风格剥离（FF5）+ 容量」四道闸；
        单看训练 IC 调参等于自欺。
      </div>
    </div>
  );

  return (
    <>
      <span
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') setOpen(true);
        }}
        className="inline-flex cursor-pointer items-center"
        title="点击查看通用参数、各模型起步值与调参避坑"
      >
        {children ?? (
          <span className="inline-flex items-center gap-1 rounded-md border border-indigo-100 bg-indigo-50/60 px-1.5 py-0.5 text-[9px] font-bold text-indigo-600 transition-colors hover:bg-indigo-100">
            <HelpCircle size={10} />
            参数用法
          </span>
        )}
      </span>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        centered
        width={980}
        destroyOnHidden
        style={{ maxWidth: '96vw' }}
        title={<span className="text-sm font-black text-slate-800">参数配置：用法与按模型建议</span>}
        styles={{ body: { padding: 0, maxHeight: '80vh', overflow: 'hidden' } }}
      >
        {content}
      </Modal>
    </>
  );
};
