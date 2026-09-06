import React, { useState } from 'react';
import { Modal, Skeleton } from 'antd';
import { nodeDot, nodeText } from '../topologyTypes';
import type { NodeState, TopologyNode } from '../topologyTypes';

const stateLabel: Record<NodeState, string> = {
    ok: '正常',
    warn: '注意',
    error: '异常',
    unknown: '未知',
};

const statePill = (state: NodeState): string => {
    switch (state) {
        case 'ok': return 'bg-emerald-50 text-emerald-700 border-emerald-200';
        case 'warn': return 'bg-amber-50 text-amber-700 border-amber-200';
        case 'error': return 'bg-rose-50 text-rose-700 border-rose-200';
        default: return 'bg-slate-100 text-slate-500 border-slate-200';
    }
};

interface InputLayerProps {
    nodes: TopologyNode[];
    loading: boolean;
}

/**
 * L1 输入层：横向节点带（行情 → 模型 → Redis → DB → 沙箱 → WS）
 * 每节点只显示一行关键信息，点击弹详情；加载中显示骨架，不阻塞其它层。
 */
const InputLayer: React.FC<InputLayerProps> = ({ nodes, loading }) => {
    const [active, setActive] = useState<TopologyNode | null>(null);

    return (
        <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-4">
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    <span className="text-[10px] font-black px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 tracking-widest">INPUT</span>
                    <h3 className="font-bold text-slate-800 text-sm">输入状态</h3>
                </div>
                <span className="text-[11px] text-slate-400">点击节点查看明细</span>
            </div>
            {loading && nodes.length === 0 ? (
                <div className="flex gap-2.5 overflow-hidden">
                    {[0, 1, 2, 3, 4].map((i) => (
                        <div key={i} className="flex-1 min-w-[140px] rounded-xl border border-slate-100 p-3">
                            <Skeleton active paragraph={{ rows: 1 }} title={{ width: '60%' }} />
                        </div>
                    ))}
                </div>
            ) : nodes.length === 0 ? (
                <div className="text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl py-4 text-center">
                    暂无输入检查项（准备度检测未返回）
                </div>
            ) : (
                <div className="flex items-stretch gap-0 overflow-x-auto custom-scrollbar pb-1">
                    {nodes.map((node, idx) => (
                        <React.Fragment key={node.key}>
                            <button
                                type="button"
                                onClick={() => setActive(node)}
                                className="relative flex-1 min-w-[150px] rounded-xl border border-slate-200 bg-slate-50/60 hover:bg-white hover:shadow-xs hover:border-slate-300 transition-all px-3 py-2.5"
                            >
                                <span className={`absolute left-3 top-3 w-2 h-2 rounded-full shrink-0 ${nodeDot(node.state)}`} />
                                <div className="text-center">
                                    <div className="text-[11px] font-black text-slate-500 tracking-wide truncate">{node.label}</div>
                                    <div className={`mt-0.5 text-xs font-bold truncate ${nodeText(node.state)}`} title={node.summary}>
                                        {node.summary}
                                    </div>
                                </div>
                            </button>
                            {idx < nodes.length - 1 && (
                                <div className="flex items-center px-1 shrink-0">
                                    <span className="text-slate-300 text-xs">→</span>
                                </div>
                            )}
                        </React.Fragment>
                    ))}
                </div>
            )}
            <Modal
                title={null}
                open={!!active}
                onCancel={() => setActive(null)}
                footer={null}
                width={400}
                centered
                styles={{ body: { padding: 0 } }}
            >
                {active && (
                    <div className="px-5 pt-4 pb-5">
                        {/* 头部：节点名 + 状态胶囊 */}
                        <div className="flex items-center gap-2 pb-3 border-b border-slate-100">
                            <span className={`w-2 h-2 rounded-full ${nodeDot(active.state)}`} />
                            <span className="text-sm font-black text-slate-800">{active.label}</span>
                            <span className={`ml-auto text-[11px] font-black px-2 py-0.5 rounded-full border ${statePill(active.state)}`}>
                                {stateLabel[active.state]}
                            </span>
                        </div>
                        {/* 一句话摘要 */}
                        <div className="py-2.5 text-xs font-bold text-slate-600">
                            {active.summary}
                        </div>
                        {/* 明细行：label 左、message 右，多条时紧凑列表 */}
                        <div className="rounded-xl bg-slate-50/70 border border-slate-100 divide-y divide-slate-100 max-h-64 overflow-y-auto custom-scrollbar">
                            {active.details.map((d) => (
                                <div key={d.label} className="flex items-start gap-2 px-3 py-2">
                                    <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${nodeDot(d.state)}`} />
                                    <div className="min-w-0">
                                        <div className="text-xs font-bold text-slate-700">{d.label}</div>
                                        <div className="text-[11px] text-slate-500 break-words">{d.message}</div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                )}
            </Modal>
        </section>
    );
};

export default InputLayer;
