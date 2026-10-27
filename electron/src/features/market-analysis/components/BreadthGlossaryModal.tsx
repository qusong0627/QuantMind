import React from 'react';
import { Modal } from 'antd';
import type { SentimentIndicator } from '../services/sentimentService';
import { BREADTH_META, BREADTH_ORDER } from './breadthReadings';

/**
 * 广度分项说明弹窗。
 *
 * 客户看不懂的不是数字，是「这个数字代表什么、多少算好」。所以每个指标都给四件事：
 * 它是什么、当前值、怎么看（阈值与方向）、现在这个读数意味着什么。
 */

const pct = (v: number | null) => (v === null ? '—' : `${v.toFixed(1)}%`);

interface Props {
  open: boolean;
  onClose: () => void;
  items: SentimentIndicator[];
}

export const BreadthGlossaryModal: React.FC<Props> = ({ open, onClose, items }) => {
  const byKey = new Map(items.map((i) => [i.key, i]));
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={860}
      title={
        <div>
          <div className="text-sm font-extrabold text-slate-800">广度分项 · 这些数字是什么意思</div>
          <div className="text-[11px] font-normal text-slate-500 mt-1">
            它们都在回答同一个问题：今天是多数股票在涨还是多数在跌、钱往哪边跑、跌得有多狠。
            点越靠右＝市场越冷；红色＝过热（追高风险），绿色＝超卖（反弹概率高）。
          </div>
        </div>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-[12px] border-collapse">
          <thead>
            <tr className="text-left text-slate-500 border-b border-slate-200">
              <th className="py-2 pr-3 font-bold whitespace-nowrap">指标</th>
              <th className="py-2 pr-3 font-bold">它是什么</th>
              <th className="py-2 pr-3 font-bold whitespace-nowrap">当前值</th>
              <th className="py-2 font-bold">怎么看 / 现在的含义</th>
            </tr>
          </thead>
          <tbody>
            {BREADTH_ORDER.map((key) => {
              const e = BREADTH_META[key];
              const it = byKey.get(key);
              const value = typeof it?.value === 'number' ? it.value : null;
              const unit = it?.unit ?? '';
              return (
                <tr key={key} className="border-b border-slate-100 align-top">
                  <td className="py-2.5 pr-3 font-bold text-slate-800 whitespace-nowrap">{e.name}</td>
                  <td className="py-2.5 pr-3 text-slate-600 leading-relaxed">{e.what}</td>
                  <td className="py-2.5 pr-3 font-mono font-bold text-slate-800 whitespace-nowrap">
                    {value === null ? '—' : unit === '%' ? pct(value) : value}
                  </td>
                  <td className="py-2.5 text-slate-600 leading-relaxed">
                    <div>{e.how}</div>
                    <div className="text-[11px] text-violet-700 font-bold mt-0.5">
                      现在：{e.reading(value)}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[11px] text-slate-400 leading-relaxed">
        补充：这些分项最终会被加权成一个 0–100 的「广度压力分」。分数越高说明越多股票在跌、市场越冷；
        80 以上是极端超卖区，20 以下是过热区。单个指标只说明一个侧面，不要单独用。
      </p>
    </Modal>
  );
};
