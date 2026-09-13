import React, { useEffect, useState } from 'react';
import { Card, Switch, Tag, Typography, Space, Divider, message, Spin, Alert } from 'antd';
import { RobotOutlined, SettingOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';
import { adminService } from '../services/adminService';

const { Title, Text } = Typography;

export const AdminSystemSettings: React.FC = () => {
    const [enabled, setEnabled] = useState<boolean | null>(null);
    const [loading, setLoading] = useState(false);
    const [detail, setDetail] = useState<any>(null);
    const [initialLoading, setInitialLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const st: any = await adminService.getFinbertStatus();
                if (!cancelled) {
                    const en = !!(st?.enabled ?? st?.data?.enabled);
                    setEnabled(en);
                    setDetail(st?.data ?? st);
                }
            } catch {
                if (!cancelled) setEnabled(null);
            } finally {
                if (!cancelled) setInitialLoading(false);
            }
        })();
        return () => { cancelled = true; };
    }, []);

    const handleToggle = async (checked: boolean) => {
        setLoading(true);
        const prev = enabled;
        setEnabled(checked);
        try {
            const res: any = await adminService.setFinbertEnabled(checked);
            const st = res?.data ?? res;
            const en = !!(st?.enabled ?? checked);
            setEnabled(en);
            setDetail(st);
            message.success(`FinBERT 已${en ? '开启' : '关闭'}${st?.model_ready ? '（模型就绪）' : checked ? '（后台加载中，约 10-20s 后生效）' : ''}`);
        } catch (e: any) {
            setEnabled(prev);
            message.error(e?.response?.data?.detail || '切换失败，请检查管理员权限');
        } finally {
            setLoading(false);
        }
    };

    if (initialLoading) {
        return (
            <div className="w-full flex flex-col items-center justify-center py-20">
                <Spin />
                <Text className="text-slate-400 text-xs mt-3">加载系统设置...</Text>
            </div>
        );
    }

    const notInstalled = !!detail && detail.installed === false;
    const noFramework = !!detail && detail.framework_ok === false;
    const cannotEnable = notInstalled || noFramework;

    return (
        <div className="w-full space-y-4">
            <div>
                <Title level={4} className="!m-0 !font-black !text-slate-800 flex items-center gap-2">
                    <SettingOutlined className="text-slate-700" /> 系统设置
                </Title>
                <Text className="text-slate-400 text-xs">基础设施与 AI 能力开关</Text>
            </div>

            <Card className="rounded-2xl border-slate-200 shadow-sm" styles={{ body: { padding: '16px' } }} title={<span className="text-sm font-black text-slate-800 flex items-center gap-2"><RobotOutlined /> FinBERT 中文金融情感</span>} extra={<Tag color={enabled ? 'success' : 'default'} className="m-0 border-none text-[11px]">{enabled ? '已开启' : enabled === null ? '未知' : '已关闭'}</Tag>}>
                <div className="flex items-center justify-between gap-4">
                    <div className="flex-1 min-w-0">
                        <div className="text-sm font-bold text-slate-800">启用 FinBERT 情感分析</div>
                        <div className="text-xs text-slate-500 mt-1 leading-relaxed">
                            开启后资讯/研报入库将走 <b>FinBERT-ZH</b>（`{detail?.model || 'bardsai/finance-sentiment-zh-base'}`，约 391M）离线推理，
                            覆盖字典法情感；关闭则仅走字典法，CPU 零开销。切换即时生效（持久化到 <code>/data/finbert/enabled</code>），无需重启。
                        </div>
                        <div className="text-[11px] text-slate-400 mt-1.5 flex flex-wrap gap-2">
                            <span>设备: <b>{String(detail?.device ?? -1)}</b> ({detail?.device === -1 ? 'CPU' : `GPU:${detail?.device}`})</span>
                            <span>· 就绪: {detail?.model_ready ? <CheckCircleOutlined className="text-emerald-500" /> : <CloseCircleOutlined className="text-slate-300" />}{detail?.model_ready ? ' 是' : detail?.model_failed ? ' 失败' : ' 加载中'}</span>
                            {detail?.override !== null && detail?.override !== undefined && <span>· 覆盖: {String(detail.override)}</span>}
                        </div>
                    </div>
                    <Space direction="vertical" align="center" size={4} className="shrink-0">
                        <Switch
                            checked={!!enabled}
                            loading={loading || enabled === null}
                            onChange={handleToggle}
                            checkedChildren="开启"
                            unCheckedChildren="关闭"
                            disabled={cannotEnable}
                        />
                        <Text className="text-[11px] text-slate-400">
                            {notInstalled ? '未安装' : noFramework ? '缺框架' : enabled ? '运行中' : '已停用'}
                        </Text>
                    </Space>
                </div>
                {notInstalled && (
                    <Alert
                        type="warning"
                        showIcon
                        className="rounded-xl text-xs !py-2 !px-3 !mb-3"
                        message="模型未安装"
                        description={`未检测到 ${detail.model} 权重（/app/models/finbert-zh-base 缺失），开关已自动关闭且无法开启，不会持续扫描占用资源。请先执行 backend/scripts/download_finbert.py 离线下载。`}
                    />
                )}
                {noFramework && !notInstalled && (
                    <Alert
                        type="warning"
                        showIcon
                        className="rounded-xl text-xs !py-2 !px-3 !mb-3"
                        message="缺少 PyTorch 推理框架"
                        description="权重已就绪，但当前镜像未包含 torch/transformers（离线镜像默认不装 PyTorch），开关已强制禁用。请在服务器上执行 sudo bash deploy/install-model-deps.sh 补装后重试。"
                    />
                )}
                <Divider className="!my-3" />
                <Alert type="info" showIcon className="rounded-xl text-xs !py-2 !px-3" message="提示" description="首次开启需后台加载模型约 10-20s，期间新入库资讯仍走字典法；已入库历史需重建 enrichment 才会回填 FinBERT 置信度。" />
            </Card>

            <Card className="rounded-2xl border-dashed border-slate-200 bg-slate-50/50" styles={{ body: { padding: '12px 16px' } }} title={<span className="text-sm font-bold text-slate-500">更多系统设置（占位）</span>}>
                <Text className="text-xs text-slate-400">后续可在此集中管理：数据同步开关、模型推理并发、QuantDB 缓存 TTL 等。</Text>
            </Card>
        </div>
    );
};

export default AdminSystemSettings;
