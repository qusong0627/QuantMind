import React, { useEffect, useRef, useState } from 'react';
import { Card, Button, Space, Typography, Progress, List, Tag, Alert, Modal, Form, Input, Select, message, Row, Col } from 'antd';
import {
    CheckCircleOutlined,
    CloseCircleOutlined,
    WarningOutlined,
    CloudUploadOutlined,
    DownloadOutlined,
    SyncOutlined,
    InfoCircleOutlined
} from '@ant-design/icons';
import { useWizardStore } from '../store/wizardStore';
import type { ValidationCheck } from '../types';
import { useAuth } from '../../auth/hooks';
import { deletePoolFile, saveToCloud } from '../services/wizardService';
import { QLIB_REBALANCE_DAY_LABEL, resolveRebalanceDays } from '../../../shared/qlib/rebalance';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

interface Props {
    onBack?: () => void;
}

const QlibValidatorAndSave: React.FC<Props> = ({ onBack }) => {
    const {
        generated,
        setGenerated,
        validationResult,
        setValidationResult,
        saveStatus,
        markAsCloudSaved,
        markAsDownloaded,
        pool,
        qlibParams,
        conditions,
        poolFile,
        clearPoolFile,
    } = useWizardStore();
    const { user } = useAuth();

    const [validating, setValidating] = useState(false);
    const [repairing, setRepairing] = useState(false);
    const [savingToCloud, setSavingToCloud] = useState(false);
    const [saveModalVisible, setSaveModalVisible] = useState(false);
    const [saveForm] = Form.useForm();
    const savedRef = useRef(false);
    const saveLockRef = useRef(false);

    // 验证函数 - 仅语法检查
    const handleValidate = async () => {
        setValidating(true);
        setValidationResult(null); // 清除之前的结果

        try {
            const { validateQlibCode } = await import('../services/wizardService');

            const codeToValidate = generated?.code || '';
            if (!codeToValidate) {
                message.error('策略代码未生成');
                setValidating(false);
                return;
            }

            // 调用真实API
            const response = await validateQlibCode({
                code: codeToValidate,
                context: {
                    start_date: '2023-01-01',
                    end_date: '2024-01-01',
                    universe_size: pool?.items?.length || 0
                },
                mode: 'syntax_only'
            });

            if (response.success && response.valid) {
                const result = {
                    valid: response.valid,
                    checks: response.checks || [],
                    warnings: response.warnings || [],
                    executionPreview: response.execution_preview || null
                };

                setValidationResult(result);
            message.success('语法检查通过！');
            } else if (response.success && !response.valid) {
                // 验证失败但API调用成功
                const result = {
                    valid: false,
                    checks: response.checks || [],
                    warnings: response.warnings || [],
                    executionPreview: null
                };
                setValidationResult(result);
                message.warning('语法检查未通过，请查看结果');
            } else {
                throw new Error(response.error || '验证失败');
            }
        } catch (error: any) {
            console.error('Validation error:', error);
            message.error(`语法检查失败: ${error.message || '未知错误'}`);

            // 设置错误状态
            setValidationResult({
                valid: false,
                checks: [{
                    type: 'error' as const,
                    passed: false,
                    message: error.message || '语法检查服务异常'
                }],
                warnings: [],
                executionPreview: null
            });
        } finally {
            setValidating(false);
        }
    };

    const handleAiRepair = async () => {
        try {
            const codeToRepair = generated?.code || '';
            if (!codeToRepair) {
                message.error('策略代码未生成');
                return;
            }
            if (!validationResult) {
                message.warning('请先执行一次语法检查');
                return;
            }
            if (validationResult.valid) {
                message.info('当前语法已通过，无需修复');
                return;
            }

            const errMsg = (validationResult.checks || [])
                .filter((c) => !c.passed)
                .map((c) => `${c.message}${c.details ? ` | ${c.details}` : ''}`)
                .join('; ')
                .slice(0, 2000); // 避免过长

            setRepairing(true);

            const { repairQlibCode, validateQlibCode } = await import('../services/wizardService');
            const repairRes = await repairQlibCode({
                code: codeToRepair,
                error: errMsg || '语法错误',
                max_rounds: 3,
            });

            if (!repairRes?.success) {
                throw new Error(repairRes?.error || 'AI 修复失败');
            }

            const repairedCode = String(repairRes.code || '').trim();
            if (!repairedCode) {
                throw new Error('AI 未返回有效代码');
            }

            // 更新代码并自动再次进行语法检查
            setGenerated({ ...(generated || {}), code: repairedCode });

            const recheck = await validateQlibCode({
                code: repairedCode,
                context: {
                    start_date: '2023-01-01',
                    end_date: '2024-01-01',
                    universe_size: pool?.items?.length || 0
                },
                mode: 'syntax_only'
            });

            const nextResult = {
                valid: Boolean(recheck?.valid),
                checks: recheck?.checks || [],
                warnings: recheck?.warnings || [],
                executionPreview: recheck?.execution_preview || null
            };
            setValidationResult(nextResult);

            if (recheck?.success && recheck?.valid) {
                message.success('AI 修复完成，语法检查已通过！');
            } else {
                message.warning(repairRes?.error || 'AI 已尝试修复，但语法仍未通过，请继续点击“AI 修复”或手工调整');
            }
        } catch (e: any) {
            console.error('[QlibValidatorAndSave] AI repair error:', e);
            message.error(`AI 修复失败: ${e?.message || '未知错误'}`);
        } finally {
            setRepairing(false);
        }
    };

    // 保存到云端
    const handleSaveToCloud = async () => {
        if (saveLockRef.current) {
            message.info('正在保存，请勿重复点击');
            return;
        }

        saveLockRef.current = true;
        setSavingToCloud(true);
        try {
            const values = await saveForm.validateFields();
            const storedUserStr = localStorage.getItem('user');
            const storedUser = storedUserStr ? JSON.parse(storedUserStr) : null;
            // 兼容 user_service 的 user 对象字段（user_id），同时保留旧字段（id）。
            const anyUser = user as any;
            const userId = String(
                anyUser?.user_id ||
                anyUser?.id ||
                storedUser?.user_id ||
                storedUser?.id ||
                'dev_user_001'
            );

            const payload = {
                user_id: userId,
                strategy_name: values.name,
                code: generated?.code || mockCode,
                metadata: {
                    description: values.description,
                    tags: values.tags || [],
                    conditions,
                    stock_pool: pool,
                    pool_file_url: poolFile?.fileUrl,
                    qlib_params: qp,
                    qlib_validated: Boolean(validationResult?.valid),
                    validation_result: validationResult,
                },
            };

            const res = await saveToCloud(payload);
            if (!res?.success) {
                throw new Error(res?.error || '保存失败');
            }

            markAsCloudSaved(res.cloud_url, res.strategy_id);
            savedRef.current = true;
            message.success('策略已成功保存到个人中心！');
            setSaveModalVisible(false);
        } catch (error: any) {
            const msg = error?.message || '保存失败，请重试';
            console.error('[QlibValidatorAndSave] saveToCloud failed:', error);
            message.error(msg);
        } finally {
            setSavingToCloud(false);
            saveLockRef.current = false;
        }
    };

    useEffect(() => {
        return () => {
            if (savedRef.current) {
                return;
            }
            if (poolFile?.fileUrl) {
                deletePoolFile({ file_url: poolFile.fileUrl, file_key: poolFile.fileKey })
                    .finally(() => {
                        clearPoolFile();
                    });
            }
        };
    }, [poolFile, clearPoolFile]);

    // 本地下载
    const handleDownloadLocal = () => {
        const codeToDownload = generated?.code || mockCode;
        if (!codeToDownload) {
            message.error('策略代码未生成');
            return;
        }

        const blob = new Blob([codeToDownload], { type: 'text/x-python' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `strategy_${Date.now()}.py`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);

        markAsDownloaded();
        message.success('策略代码已下载到本地');
    };

    const getCheckIcon = (check: ValidationCheck) => {
        if (check.passed) {
            return <CheckCircleOutlined className="text-green-500" />;
        }
        return <CloseCircleOutlined className="text-red-500" />;
    };

    const sanitizeQlibParams = (params?: typeof qlibParams) => {
        const next = {
            ...(params ?? { strategy_type: 'TopkDropout', topk: 10, n_drop: 2, rebalance_days: 5 }),
            rebalance_days: resolveRebalanceDays(params),
        };

        if (next.strategy_type === 'TopkWeight') {
            delete next.n_drop;
        } else if (typeof next.n_drop !== 'number') {
            next.n_drop = 2;
        }

        return next;
    };

    // Mock策略代码（LLM生成完成前的占位）
    const qp = sanitizeQlibParams(qlibParams);
    const rebalanceDays = qp.rebalance_days;
    const nDropLine = qp.strategy_type === 'TopkDropout' ? `\n    "n_drop": ${qp.n_drop},` : '';
    const mockCode = `# QuantMind 智能策略
# 生成时间: ${new Date().toLocaleString()}

STRATEGY_CONFIG = {
    "strategy_type": "${qp.strategy_type}",
    "topk": ${qp.topk},${nDropLine}
    "rebalance_days": ${rebalanceDays},
    "universe": "${pool?.items?.length || 0} stocks",
}`;

    return (
        <div className="h-full flex p-2 gap-2 overflow-hidden">
            {/* 左侧：代码区域（AI-IDE风格） - 自动滚动 */}
            <div className="flex-1 flex flex-col overflow-hidden">
                <div className="mb-1">
                    <Text strong style={{ fontSize: '13px' }}>策略代码</Text>
                </div>
                <div
                    className="flex-1 border rounded-lg overflow-auto"
                    style={{
                        backgroundColor: '#1e1e1e',
                        border: '1px solid #3c3c3c',
                        boxShadow: '0 2px 8px rgba(0,0,0,0.15)'
                    }}
                >
                    <pre
                        className="m-0 p-3"
                        style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: '12px',
                            lineHeight: '1.5',
                            color: '#d4d4d4',
                            backgroundColor: 'transparent',
                            whiteSpace: 'pre',
                            wordWrap: 'break-word',
                            minHeight: '100%'
                        }}
                    >
                        {generated?.code || mockCode}
                    </pre>
                </div>
            </div>

            {/* 右侧：策略参数（表单输入）- 无滚动条 */}
            <div className="w-80 flex flex-col overflow-hidden">
                <div className="mb-1">
                    <Text strong style={{ fontSize: '13px' }}>策略参数</Text>
                </div>
                <Card
                    size="small"
                    className="flex-1 flex flex-col"
                    styles={{
                        body: {
                            padding: '8px',
                            flex: 1,
                            display: 'flex',
                            flexDirection: 'column',
                            overflow: 'hidden'
                        }
                    }}
                >
                    {/* 操作按钮区域（置顶） */}
                    <div className="mb-2 flex gap-2">
                        <Button
                            type="default"
                            size="small"
                            icon={validating ? <SyncOutlined spin /> : <CheckCircleOutlined />}
                            onClick={handleValidate}
                            loading={validating}
                            className="flex-1"
                            style={{ height: '32px', fontSize: '12px', borderRadius: '16px' }}
                        >
                            {validating ? '检查中' : '语法检查'}
                        </Button>

                        <Button
                            type="primary"
                            size="small"
                            icon={<CloudUploadOutlined />}
                            onClick={() => setSaveModalVisible(true)}
                            disabled={!validationResult?.valid || saveStatus.savedToCloud || savingToCloud}
                            className="flex-1"
                            style={{ height: '32px', fontSize: '12px', borderRadius: '16px' }}
                        >
                            {saveStatus.savedToCloud ? '已保存' : '保存策略'}
                        </Button>
                    </div>

                    {/* 参数表单区域 */}
                    <div className="flex-1" style={{ overflow: 'hidden' }}>
                        <Form layout="vertical" size="small">
                            <Form.Item label={<span style={{ fontSize: '11px' }}>策略名称</span>} style={{ marginBottom: '6px' }}>
                                <Input
                                    value={`策略_${new Date().toLocaleDateString()}`}
                                    placeholder="智能选股策略"
                                    style={{ borderRadius: '16px', fontSize: '11px', height: '30px' }}
                                />
                            </Form.Item>

                            <Form.Item label={<span style={{ fontSize: '11px' }}>策略类型</span>} style={{ marginBottom: '6px' }}>
                                <Input
                                    value={qp.strategy_type === 'TopkDropout' ? 'TopkDropoutStrategy' : 'TopkWeightStrategy'}
                                    readOnly
                                    style={{ backgroundColor: '#f5f5f5', borderRadius: '16px', fontSize: '11px', height: '30px' }}
                                />
                            </Form.Item>

                            <Form.Item label={<span style={{ fontSize: '11px' }}>选股数量（TopK）</span>} style={{ marginBottom: '6px' }}>
                                <Input
                                    value={`${qp.topk} 只`}
                                    readOnly
                                    style={{ backgroundColor: '#f5f5f5', borderRadius: '16px', fontSize: '11px', height: '30px' }}
                                />
                            </Form.Item>

                            <Form.Item label={<span style={{ fontSize: '11px' }}>调仓周期</span>} style={{ marginBottom: '6px' }}>
                                <Input
                                    value={QLIB_REBALANCE_DAY_LABEL[rebalanceDays] ?? `${rebalanceDays}天`}
                                    readOnly
                                    style={{ backgroundColor: '#f5f5f5', borderRadius: '16px', fontSize: '11px', height: '30px' }}
                                />
                            </Form.Item>

                            {qp.strategy_type === 'TopkDropout' && (
                                <Form.Item label={<span style={{ fontSize: '11px' }}>每期剔除数（n_drop）</span>} style={{ marginBottom: '6px' }}>
                                    <Input
                                        value={`${qp.n_drop} 只`}
                                        readOnly
                                        style={{ backgroundColor: '#f5f5f5', borderRadius: '16px', fontSize: '11px', height: '30px' }}
                                    />
                                </Form.Item>
                            )}

                            <Form.Item label={<span style={{ fontSize: '11px' }}>股票池规模</span>} style={{ marginBottom: '6px' }}>
                                <Input
                                    value={`${pool?.items?.length || 0} 只股票`}
                                    readOnly
                                    style={{ backgroundColor: '#f5f5f5', borderRadius: '16px', fontSize: '11px', height: '30px' }}
                                />
                            </Form.Item>
                        </Form>

                        {/* Validation status area, moved inside the scrollable area if it gets too long */}
                        {validationResult && (
                            <div className="mt-2 pt-2" style={{ borderTop: '1px solid #f0f0f0' }}>
                                <Text strong className="mb-1 block" style={{ fontSize: '12px' }}>语法检查结果</Text>

                                <List
                                    size="small"
                                    dataSource={validationResult.checks}
                                    renderItem={(check) => (
                                        <List.Item style={{ padding: '2px 0', border: 'none' }}>
                                            <Space size="small">
                                                {getCheckIcon(check)}
                                                <Text style={{ fontSize: '11px' }}>{check.message}</Text>
                                            </Space>
                                        </List.Item>
                                    )}
                                />

                                {validationResult.warnings.length > 0 && (
                                    <Alert
                                        type="warning"
                                        message={<span style={{ fontSize: '11px' }}>警告</span>}
                                        description={
                                            <ul className="mb-0 pl-3" style={{ fontSize: '11px' }}>
                                                {validationResult.warnings.map((warning, idx) => (
                                                    <li key={idx}>{warning}</li>
                                                ))}
                                            </ul>
                                        }
                                        className="mt-1"
                                        showIcon
                                    />
                                )}

                                {validationResult.valid && (
                                    <Alert
                                        type="success"
                                        message={<span style={{ fontSize: '11px' }}>语法通过</span>}
                                        className="mt-1"
                                        showIcon
                                    />
                                )}

                                {!validationResult.valid && (
                                    <div className="mt-2">
                                        <Button
                                            block
                                            size="small"
                                            type="default"
                                            icon={<SyncOutlined spin={repairing} />}
                                            loading={repairing}
                                            onClick={handleAiRepair}
                                            style={{ height: '32px', fontSize: '12px', borderRadius: '16px' }}
                                        >
                                            AI 修复（自动重试，直至语法通过）
                                        </Button>
                                    </div>
                                )}
                            </div>
                        )}

                        {validating && (
                            <div className="mt-2 pt-2" style={{ borderTop: '1px solid #f0f0f0' }}>
                                <Progress percent={66} status="active" size="small" />
                                <Text type="secondary" className="mt-1 block" style={{ fontSize: '11px' }}>
                                    正在进行语法检查...
                                </Text>
                            </div>
                        )}
                    </div>
                </Card>
            </div>

            {/* 保存到云端模态框 */}
            <Modal
                title="保存策略到个人中心"
                open={saveModalVisible}
                centered
                onCancel={() => {
                    if (!savingToCloud) setSaveModalVisible(false);
                }}
                onOk={handleSaveToCloud}
                okText={savingToCloud ? '保存中...' : '保存'}
                cancelText="取消"
                width={600}
                confirmLoading={savingToCloud}
                okButtonProps={{ disabled: savingToCloud }}
                cancelButtonProps={{ disabled: savingToCloud }}
                maskClosable={!savingToCloud}
                keyboard={!savingToCloud}
                closable={!savingToCloud}
            >
                {savingToCloud && (
                    <Alert
                        type="info"
                        showIcon
                        icon={<SyncOutlined spin />}
                        message="正在保存到云端，请勿重复点击或关闭弹窗"
                        className="mb-3"
                    />
                )}
                <Form
                    form={saveForm}
                    layout="vertical"
                    initialValues={{
                        name: `策略_${new Date().toLocaleDateString()}`,
                        description: '',
                        tags: []
                    }}
                >
                    <Form.Item
                        label="策略名称"
                        name="name"
                        rules={[{ required: true, message: '请输入策略名称' }]}
                    >
                        <Input placeholder="为策略起一个易于识别的名称" style={{ borderRadius: '16px' }} />
                    </Form.Item>

                    <Form.Item label="策略描述" name="description">
                        <TextArea
                            rows={4}
                            placeholder="描述策略的选股逻辑、适用场景等"
                            style={{ borderRadius: '16px' }}
                        />
                    </Form.Item>

                    <Form.Item label="标签" name="tags">
                        <Select
                            mode="tags"
                            placeholder="添加标签便于分类（如：价值、成长、动量等）"
                            style={{ borderRadius: '16px' }}
                            options={[
                                { value: '价值', label: '价值' },
                                { value: '成长', label: '成长' },
                                { value: '动量', label: '动量' },
                                { value: '技术', label: '技术' },
                            ]}
                        />
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    );
};

export default QlibValidatorAndSave;
