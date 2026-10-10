/**
 * 训练数据集页「发布状态条」（2026-10-10 机构级重排）。
 *
 * 替换原先挤在右侧 Col lg=6 里的「分类映射草稿」卡 + 页面底部的「已发布特征集
 * 版本」卡：发布是这一页的主任务，线上 vs 草稿的口径差必须一眼可辨，而不是缩在
 * 角落里让人滚到页尾找版本号。
 *
 * 口径纪律：启用数一律走 catalogMath.countEnabledFeatures（与训练侧
 * `enabled !== false` 逐字同口径），**不得**用后端 feature_count；增减 delta 按
 * 逻辑因子 ID 逐键比对（diffEnabledFeatures），没有线上版本时不显示 delta。
 *
 * 测试契约（AdminTrainingDatasets.test.tsx 锁死，勿改文案）：
 * 「编辑中」「发布此草稿」「新建草稿」，及其触发的确认框流程。
 */
import React from 'react';
import { Button, Form, Input, Tag, Tooltip, Typography } from 'antd';
import { PlusOutlined, RocketOutlined } from '@ant-design/icons';

import { Panel, StatusDot } from '../ui/AdminPrimitives';
import { countEnabledFeatures, diffEnabledFeatures } from './catalogMath';

const { Text } = Typography;

interface TrainingCatalogStatusBarProps {
    published: any | null;
    draft: any | null;
    creating: boolean;
    /** 新建草稿（版本名已经过表单校验）；草稿创建+播种由页面执行 */
    onCreateDraft: (versionName: string) => void;
    /** 以当前线上版本为蓝本复制一份草稿 */
    onClonePublished: () => void;
    /** 发布此草稿（页面负责确认框） */
    onPublish: () => void;
}

/** 草稿相对线上的增减（0 增减时说「与线上一致」，不摆一排 0）。 */
const DeltaText: React.FC<{ delta: { added: number; removed: number } | null }> = ({ delta }) => {
    if (!delta) return null;
    if (delta.added === 0 && delta.removed === 0) {
        return <span className="text-[11px] text-slate-400">与线上一致</span>;
    }
    return (
        <Tooltip title="按逻辑因子 ID 逐键比对启用特征（不含停用项）">
            <span className="text-[11px]">
                相对线上
                {delta.added > 0 && <span className="admin-num text-emerald-600"> +{delta.added}</span>}
                {delta.removed > 0 && <span className="admin-num text-rose-600"> −{delta.removed}</span>}
            </span>
        </Tooltip>
    );
};

export const TrainingCatalogStatusBar: React.FC<TrainingCatalogStatusBarProps> = ({
    published,
    draft,
    creating,
    onCreateDraft,
    onClonePublished,
    onPublish,
}) => {
    const [draftForm] = Form.useForm();
    const draftEnabled = draft ? countEnabledFeatures(draft) : 0;
    const publishedEnabled = published ? countEnabledFeatures(published) : 0;
    const delta = draft ? diffEnabledFeatures(draft, published) : null;

    return (
        <Panel bodyClassName="px-4 py-2.5">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                {/* 线上口径 */}
                <div className="flex min-w-0 items-center gap-2">
                    <StatusDot tone={published ? 'ok' : 'idle'} />
                    <span className="shrink-0 text-[13px] font-semibold text-slate-800">线上版本</span>
                    {published ? (
                        <>
                            <Text strong className="truncate" style={{ maxWidth: 240 }}>{published.version_name}</Text>
                            <Tooltip title={`版本 ID：${published.version_id}`}>
                                <span className="admin-num cursor-help text-[11px] text-slate-400">
                                    ｜启用 {publishedEnabled}
                                </span>
                            </Tooltip>
                        </>
                    ) : (
                        <Text type="secondary" className="text-xs">
                            尚未发布 —— 训练页不会把它作为 QuantDB 直读训练集
                        </Text>
                    )}
                </div>

                <div className="h-5 w-px shrink-0 bg-slate-200" />

                {/* 草稿口径 */}
                {draft ? (
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <Tag color="blue" className="!mr-0">编辑中</Tag>
                        <Tooltip title={`版本 ID：${draft.version_id}`}>
                            <Text strong className="cursor-help truncate" style={{ maxWidth: 240 }}>
                                {draft.version_name}
                            </Text>
                        </Tooltip>
                        <span className="admin-num text-xs text-slate-600">启用 {draftEnabled}</span>
                        <DeltaText delta={delta} />
                        <Button
                            type="primary"
                            icon={<RocketOutlined />}
                            onClick={onPublish}
                            disabled={draftEnabled === 0}
                        >
                            发布此草稿
                        </Button>
                        {draftEnabled === 0 && (
                            <span className="text-[11px] text-amber-600">启用数为 0 不可发布</span>
                        )}
                    </div>
                ) : (
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <Tag className="!mr-0">未创建</Tag>
                        <Form
                            form={draftForm}
                            layout="inline"
                            onFinish={(values) => onCreateDraft(String(values.version_name || '').trim())}
                        >
                            <Form.Item
                                name="version_name"
                                rules={[{ required: true, message: '请输入版本名称' }]}
                                style={{ marginBottom: 0, marginInlineEnd: 8 }}
                            >
                                <Input placeholder="例如：2026-08 默认因子集" style={{ width: 220 }} />
                            </Form.Item>
                            <Button
                                type="primary"
                                htmlType="submit"
                                loading={creating}
                                icon={<PlusOutlined />}
                            >
                                新建草稿
                            </Button>
                        </Form>
                        {published && (
                            <Button onClick={onClonePublished}>从线上版本复制</Button>
                        )}
                        <span className="text-[11px] text-slate-400">
                            新建后自动导入当前数据源全部字段；发布前对训练不可见
                        </span>
                    </div>
                )}
            </div>
        </Panel>
    );
};
