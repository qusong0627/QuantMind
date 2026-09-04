/**
 * FinBERT 中文金融情感模型（管理员）
 *
 * 展示 FinBERT 模型介绍与部署指南，并提供实时健康状态探测。
 * 独立 tab，与词条/标签管理职责分离。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Modal,
  Row,
  Space,
  Steps,
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

export const AdminFinbertModel: React.FC = () => {
  const [guideOpen, setGuideOpen] = useState(false);
  const [finbertStatus, setFinbertStatus] = useState<FinbertStatus | null>(null);

  const loadFinbertStatus = useCallback(async () => {
    try {
      const s = await newsService.adminFinbertStatus();
      setFinbertStatus(s as unknown as FinbertStatus);
    } catch {
      setFinbertStatus(null);
    }
  }, []);

  useEffect(() => {
    loadFinbertStatus();
  }, [loadFinbertStatus]);

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
              title: '启用开关',
              description: (
                <span>
                  <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">/opt/quantmind/.env</code> 设
                  <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">NEWS_USE_FINBERT=true</code> 后重建（默认关闭，避免打满 worker）
                </span>
              ),
              status: 'wait',
            },
            {
              title: '触发历史重算',
              description: (
                <span>
                  <code className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-100 break-all">POST /api/v1/news/enrichment/rebuild-all?force=true</code>
                  （日常新资讯由 Celery 每分钟自动处理）
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
          <div style={{ marginTop: 6, color: '#9a3412' }}>若 +finbert 占比为 0，按上方 4 步完成「装 torch → 下权重 → 开开关 → 重算」后再查。</div>
        </div>
      </Modal>
    </div>
  );
};

export default AdminFinbertModel;