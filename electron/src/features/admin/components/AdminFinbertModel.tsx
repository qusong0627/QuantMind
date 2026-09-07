/**
 * FinBERT 中文金融情感模型（管理员）
 *
 * 展示 FinBERT 模型介绍与部署指南，并提供实时健康状态探测。
 * 独立 tab，与词条/标签管理职责分离。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Modal,
  Progress,
  Row,
  Space,
  Steps,
  Switch,
  Tag,
  Typography,
} from 'antd';
import {
  ApiOutlined,
  BookOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  ExperimentOutlined,
  FileTextOutlined,
  HistoryOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { newsService } from '../../news/services/newsService';

const { Title, Text, Paragraph } = Typography;

interface FinbertStatus {
  available: boolean;
  use_finbert: boolean;
  model: string;
  device: number;
  sample_inference: { label: string; confidence: number } | null;
  db_total_24h: number;
  db_finbert_ratio_24h: number | null;
  tip: string;
}

interface SwitchStatus {
  enabled: boolean;
  device: number;
  installed: boolean;
  model_ready: boolean;
  model_failed: boolean;
  cpu_threads: number | null;
}

interface RebuildProgress {
  running: boolean;
  total: number;
  processed: number;
  ok: number;
  failed: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  force: boolean;
  elapsed_seconds: number;
  eta_seconds: number | null;
}

export const AdminFinbertModel: React.FC = () => {
  const [guideOpen, setGuideOpen] = useState(false);
  const [finbertStatus, setFinbertStatus] = useState<FinbertStatus | null>(null);
  const [switchStatus, setSwitchStatus] = useState<SwitchStatus | null>(null);
  const [toggling, setToggling] = useState(false);
  const [toggleWarning, setToggleWarning] = useState<string | null>(null);
  const [rebuildProgress, setRebuildProgress] = useState<RebuildProgress | null>(null);
  const [rebuildStarting, setRebuildStarting] = useState(false);
  const [rebuildNotice, setRebuildNotice] = useState<string | null>(null);

  const loadFinbertStatus = useCallback(async () => {
    try {
      const s = await newsService.adminFinbertStatus();
      setFinbertStatus(s as unknown as FinbertStatus);
    } catch {
      setFinbertStatus(null);
    }
  }, []);

  const loadSwitchStatus = useCallback(async () => {
    try {
      const s = await newsService.adminFinbertToggleStatus();
      setSwitchStatus(s);
    } catch {
      setSwitchStatus(null);
    }
  }, []);

  const loadRebuildProgress = useCallback(async () => {
    try {
      const p = await newsService.getRebuildProgress();
      setRebuildProgress(p);
      if (!p.running && p.finished_at) {
        setRebuildNotice(
          p.error
            ? `回填异常中断：${p.error}`
            : `回填完成：成功 ${p.ok} 篇，失败 ${p.failed} 篇（耗时 ${Math.round(p.elapsed_seconds)}s）`
        );
      }
    } catch {
      // 进度接口暂不可达时静默，保留下次轮询
    }
  }, []);

  useEffect(() => {
    loadFinbertStatus();
    loadSwitchStatus();
    loadRebuildProgress();
  }, [loadFinbertStatus, loadSwitchStatus, loadRebuildProgress]);

  // 回填运行中每 2s 轮询进度（断点续跑：中断重跑自动跳过已完成行）
  useEffect(() => {
    if (!rebuildProgress?.running) return;
    const timer = setInterval(() => {
      loadRebuildProgress();
    }, 2000);
    return () => clearInterval(timer);
  }, [rebuildProgress?.running, loadRebuildProgress]);

  const handleToggle = async (checked: boolean) => {
    setToggling(true);
    setToggleWarning(null);
    setRebuildNotice(null);
    try {
      const res = await newsService.adminFinbertToggle(checked);
      setToggleWarning(res.warning ?? null);
      if (!checked) {
        setRebuildNotice('已停用：新打分回退词典法（历史 +finbert 结果保留，不降级重写）');
      } else {
        setRebuildNotice('已启用：模型就绪后自动触发历史回填（断点续跑），也可手动点下方按钮');
      }
      await loadSwitchStatus();
      await loadFinbertStatus();
    } catch {
      setRebuildNotice('开关切换失败，请确认登录态与接口可用性');
    } finally {
      setToggling(false);
    }
  };

  const startRebuild = async () => {
    setRebuildStarting(true);
    setRebuildNotice(null);
    try {
      const res = await newsService.rebuildAllEnrichment(false);
      if (res.started) {
        setRebuildNotice('历史回填已启动（后台线程，可离开页面；中断重跑会自动跳过已完成行）');
        setRebuildProgress({
          running: true,
          total: res.total,
          processed: res.processed,
          ok: res.ok,
          failed: res.failed,
          started_at: res.started_at ?? null,
          finished_at: null,
          error: null,
          force: false,
          elapsed_seconds: res.elapsed_seconds ?? 0,
          eta_seconds: res.eta_seconds ?? null,
        } as RebuildProgress);
      } else {
        setRebuildNotice(res.reason === 'already_running' ? '已有回填在运行中，请等待' : '回填启动失败');
        await loadRebuildProgress();
      }
    } catch {
      setRebuildNotice('回填启动失败：接口不可达');
    } finally {
      setRebuildStarting(false);
    }
  };

  const percent = rebuildProgress?.total
    ? Math.min(100, Math.round((rebuildProgress.processed / rebuildProgress.total) * 100))
    : 0;

  return (
    <div className="p-6 space-y-4">
      {/* 顶部标题 */}
      <div className="flex items-center justify-between pb-1">
        <div>
          <Title level={4} style={{ margin: 0, fontWeight: 700 }}>
            <ExperimentOutlined style={{ marginRight: 8, color: '#6366f1' }} />
            FinBERT 中文金融情感模型
          </Title>
          <Text type="secondary" style={{ fontSize: 13 }}>
            对 Huntly RSS 资讯做中文金融情感打分 · 与词典法融合（0.6 词法 + 0.4 FinBERT）
          </Text>
        </div>
        <Space>
          <Button icon={<FileTextOutlined />} onClick={() => setGuideOpen(true)} style={{ borderRadius: 6 }}>
            完整部署指南
          </Button>
          <Button icon={<ReloadOutlined />} onClick={loadFinbertStatus} style={{ borderRadius: 6 }}>
            重新探测
          </Button>
        </Space>
      </div>

      <Card
        style={{ borderRadius: 10, border: '1px solid #e2e8f0' }}
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <ApiOutlined style={{ color: '#6366f1' }} />
            <span style={{ fontWeight: 600 }}>实时健康状态</span>
            {finbertStatus ? (
              finbertStatus.available ? (
                <Tag color="success" icon={<CheckCircleFilled />}>已就绪</Tag>
              ) : finbertStatus.use_finbert ? (
                <Tag color="warning" icon={<CloseCircleFilled />}>加载失败</Tag>
              ) : (
                <Tag icon={<CloseCircleFilled />}>已关闭</Tag>
              )
            ) : (
              <Tag>探测中…</Tag>
            )}
          </span>
        }
      >
        {finbertStatus ? (
          <Row gutter={[24, 12]}>
            <Col xs={24} lg={10}>
              <div style={{ fontSize: 13, color: '#475569', marginBottom: 6 }}>
                <b>模型：</b>{finbertStatus.model || 'bardsai/finance-sentiment-zh-base'}
                <span style={{ marginLeft: 8, color: '#94a3b8' }}>(RoBERTa-zh ≈100MB 三分类)</span>
              </div>
              <div style={{ fontSize: 13, color: '#475569', marginBottom: 6 }}>
                <b>推理设备：</b>
                {finbertStatus.device === -1 ? 'CPU' : `GPU${finbertStatus.device}`}
                <span style={{ marginLeft: 8, color: '#94a3b8' }}>
                  · 启用={String(finbertStatus.use_finbert)}
                </span>
              </div>
              <div style={{ fontSize: 13, color: '#475569', marginBottom: 6 }}>
                <b>近 24h 写入：</b>{finbertStatus.db_total_24h} 篇，
                <b style={{ marginLeft: 4 }}>+finbert 占比：</b>
                {finbertStatus.db_finbert_ratio_24h == null
                  ? '—'
                  : `${(finbertStatus.db_finbert_ratio_24h * 100).toFixed(0)}%`}
              </div>
              {finbertStatus.sample_inference && (
                <div style={{ fontSize: 13, color: '#475569', marginBottom: 6 }}>
                  <b>样例推理：</b>
                  <Tag
                    color={
                      finbertStatus.sample_inference.label === 'bullish'
                        ? 'red'
                        : finbertStatus.sample_inference.label === 'bearish'
                        ? 'green'
                        : 'default'
                    }
                    style={{ margin: '0 4px' }}
                  >
                    {finbertStatus.sample_inference.label}
                  </Tag>
                  conf={finbertStatus.sample_inference.confidence.toFixed(3)}
                </div>
              )}
              <div
                style={{
                  fontSize: 12,
                  color: '#64748b',
                  marginTop: 8,
                  paddingTop: 8,
                  borderTop: '1px dashed #e2e8f0',
                }}
              >
                <ThunderboltOutlined style={{ marginRight: 4, color: '#f59e0b' }} />
                {finbertStatus.tip}
              </div>
            </Col>
            <Col xs={24} lg={14}>
              <Paragraph style={{ marginBottom: 6, fontSize: 13 }}>
                <b>作用：</b>对 Huntly RSS 标题做中文金融情感打分（<Tag color="red" style={{ margin: 0 }}>利好</Tag> / <Tag color="green" style={{ margin: 0 }}>利空</Tag> / <Tag style={{ margin: 0 }}>中性</Tag>）。
              </Paragraph>
              <Paragraph style={{ marginBottom: 6, fontSize: 13 }}>
                <b>生效标记：</b><code>news_article_enrichment.model_version</code> 含
                <Tag color="purple" style={{ margin: '0 4px' }}>+finbert</Tag>
                后缀即代表 FinBERT 真实参与推理。
              </Paragraph>
              <Paragraph style={{ marginBottom: 0, fontSize: 12, color: '#64748b' }}>
                部署位置：<code>backend/services/api/news/sentiment.py</code>（懒加载）·
                权重下载：<code>backend/scripts/download_finbert.py</code>（ModelScope → hf-mirror → HF 三源回退）·
                调度：Celery <code>news_enrich_recent</code>（每分钟）
              </Paragraph>
            </Col>
          </Row>
        ) : (
          <div style={{ fontSize: 12, color: '#94a3b8' }}>无法连接后端 /enrichment/finbert-status</div>
        )}
      </Card>

      {/* ============ 启用开关 & 历史回填控制 ============ */}
      <Card
        style={{ borderRadius: 10, border: '1px solid #e2e8f0' }}
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <HistoryOutlined style={{ color: '#6366f1' }} />
            <span style={{ fontWeight: 600 }}>启用与控制</span>
            {switchStatus && (
              <Tag color={switchStatus.enabled ? 'purple' : 'default'}>
                {switchStatus.enabled ? '已启用' : '已停用'}
              </Tag>
            )}
          </span>
        }
      >
        <Row gutter={[24, 16]}>
          <Col xs={24} lg={10}>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space size={12} wrap>
                <Switch
                  checked={switchStatus?.enabled ?? false}
                  loading={toggling}
                  disabled={switchStatus ? !switchStatus.installed : false}
                  checkedChildren="开"
                  unCheckedChildren="关"
                  onChange={handleToggle}
                />
                <Text style={{ fontSize: 13 }}>
                  FinBERT 推理开关（运行时即时生效，无需重建镜像）
                </Text>
              </Space>
              {switchStatus && (
                <Space size={6} wrap>
                  <Tag style={{ margin: 0 }}>
                    设备：
                    {switchStatus.device === -1 ? 'CPU' : `GPU${switchStatus.device}`}
                  </Tag>
                  {switchStatus.cpu_threads != null && (
                    <Tag color="gold" style={{ margin: 0 }}>
                      推理线程限制 {switchStatus.cpu_threads}（防打满）
                    </Tag>
                  )}
                  <Tag
                    color={
                      switchStatus.model_ready
                        ? 'success'
                        : switchStatus.model_failed
                          ? 'error'
                          : 'default'
                    }
                    style={{ margin: 0 }}
                  >
                    {switchStatus.model_ready
                      ? '模型已就绪'
                      : switchStatus.model_failed
                        ? '模型加载失败'
                        : '模型待加载'}
                  </Tag>
                </Space>
              )}
              {!switchStatus?.installed && (
                <Alert
                  type="warning"
                  showIcon
                  message="模型未安装，无法开启"
                  description="请先在容器内执行 backend/scripts/download_finbert.py 下载权重。"
                />
              )}
              {toggleWarning && (
                <Alert type="warning" showIcon message={toggleWarning} style={{ maxWidth: 420 }} />
              )}
            </Space>
          </Col>
          <Col xs={24} lg={14}>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space size={12} align="start" wrap>
                <Button
                  type="primary"
                  ghost
                  icon={<HistoryOutlined />}
                  loading={rebuildStarting}
                  disabled={rebuildProgress?.running ?? false}
                  onClick={startRebuild}
                  style={{ borderRadius: 6 }}
                >
                  历史回填（断点续跑）
                </Button>
                <Text type="secondary" style={{ fontSize: 12, maxWidth: 360 }}>
                  对存量纯词典法文章用 FinBERT 重新融合打分；已含 +finbert 的行自动跳过，
                  中断后重跑自动续上，不重复计算。
                </Text>
              </Space>
              {rebuildProgress?.running && (
                <div style={{ width: '100%', maxWidth: 480 }}>
                  <Progress percent={percent} size="small" />
                  <Text style={{ fontSize: 12, color: '#475569' }}>
                    已扫描 {rebuildProgress.processed.toLocaleString()} /{' '}
                    {rebuildProgress.total.toLocaleString()} · 成功{' '}
                    {rebuildProgress.ok.toLocaleString()} · 失败{' '}
                    {rebuildProgress.failed.toLocaleString()}
                    {rebuildProgress.eta_seconds != null
                      ? ` · 预计还需 ${Math.ceil(rebuildProgress.eta_seconds / 60)} 分钟`
                      : ''}
                  </Text>
                </div>
              )}
              {rebuildNotice && (
                <Alert
                  type={rebuildNotice.startsWith('回填完成') ? 'success' : 'info'}
                  showIcon
                  message={rebuildNotice}
                  style={{ maxWidth: 480 }}
                />
              )}
              {rebuildProgress?.error && (
                <Alert type="error" showIcon message={`上次回填异常：${rebuildProgress.error}`} />
              )}
            </Space>
          </Col>
        </Row>
      </Card>

      {/* ============ FinBERT 模型介绍 & 部署指南 Modal ============ */}
      <Modal
        open={guideOpen}
        onCancel={() => setGuideOpen(false)}
        footer={null}
        width={720}
        destroyOnHidden
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <ExperimentOutlined style={{ color: '#6366f1' }} />
            <span>FinBERT 中文金融情感模型 · 简介</span>
            <Tag color="purple" style={{ marginLeft: 4 }}>+finbert</Tag>
          </span>
        }
      >
        {/* 顶部：模型一句话 */}
        <div
          style={{
            background: 'linear-gradient(135deg, #eef2ff 0%, #f5f3ff 100%)',
            border: '1px solid #c7d2fe',
            borderRadius: 8,
            padding: 14,
            marginBottom: 16,
          }}
        >
          <div style={{ fontSize: 14, color: '#1e293b', fontWeight: 600, marginBottom: 6 }}>
            <BookOutlined style={{ marginRight: 6, color: '#6366f1' }} />
            bardsai/finance-sentiment-zh-base （RoBERTa-zh，≈100MB，三分类情感）
          </div>
          <div style={{ fontSize: 12, color: '#475569' }}>
            对 Huntly RSS 资讯做中文金融情感打分：
            <Tag color="red" style={{ margin: '0 4px' }}>利好 bullish</Tag>
            <Tag color="green" style={{ margin: '0 4px' }}>利空 bearish</Tag>
            <Tag style={{ margin: '0 4px' }}>中性 neutral</Tag>
            ，与本地词典法加权融合（0.6 词法 + 0.4 FinBERT，置信度 ≥ 0.55 时启用）。
          </div>
        </div>

        {/* 部署步骤（Timeline 式简洁向导） */}
        <Steps
          direction="vertical"
          size="small"
          current={-1}
          responsive={false}
          items={[
            {
              title: '安装 PyTorch（CPU）',
              description: (
                <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">
                  sudo bash deploy/install-model-deps.sh
                </code>
              ),
              status: 'wait',
            },
            {
              title: '下载模型权重',
              description: (
                <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">
                  docker exec quantmind python3 /app/backend/scripts/download_finbert.py
                </code>
              ),
              status: 'wait',
            },
            {
              title: '启用开关（面板按钮 / 或文件）',
              description: (
                <span>
                  上方「启用与控制」开关一键开启（运行时即时生效）。等效于写入
                  <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">
                    /opt/quantmind/data/finbert/enabled
                  </code>
                  ；CPU 环境推理线程自动限制为 2（<code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">FINBERT_CPU_THREADS</code> 可调），防止打满
                </span>
              ),
              status: 'wait',
            },
            {
              title: '触发历史重算（按钮，或开启后自动执行）',
              description: (
                <span>
                  「历史回填（断点续跑）」按钮对存量纯词典法文章补跑 FinBERT 融合打分；
                  开启开关后模型就绪也会自动触发一次。日常新资讯由 Celery 每分钟自动处理
                </span>
              ),
              status: 'wait',
            },
          ]}
        />

        <div style={{ marginTop: 14, fontSize: 12, color: '#64748b' }}>
          <div style={{ marginBottom: 6 }}>
            <b>验证生效：</b>查询近 24h 写入里带 <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100">+finbert</code> 的占比，占比即真实参与推理。
          </div>
          <pre
            className="font-mono text-xs px-3 py-2 rounded overflow-x-auto"
            style={{ background: '#0f172a', color: '#e2e8f0', margin: 0 }}
          >docker exec quantmind-db psql -U quantmind -d quantmind -c "SELECT model_version, count(*) FROM news_article_enrichment GROUP BY model_version;"</pre>
          <div style={{ marginTop: 6, color: '#9a3412' }}>
            若 +finbert 占比为 0：确认「启用与控制」开关已开启、模型 Tag 显示已就绪，
            再点一次「历史回填（断点续跑）」，观察进度条推进。
          </div>
        </div>
      </Modal>
    </div>
  );
};

export default AdminFinbertModel;