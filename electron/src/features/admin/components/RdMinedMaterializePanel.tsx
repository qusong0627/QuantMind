/**
 * RD 挖掘因子「物化」运维面板。
 *
 * 挂载点：后台「模型训练数据集」页（/admin/training-datasets）切到「自定义市场」
 * 时展示——rd_mined 库就落在 CUSTOM 市场；另在旧 RD 因子挖掘管理页（a_share
 * 市场）保留一处挂载，该页当前未挂进路由，属预留。
 *
 * 链路：挖掘（本页）→ 物化进 CUSTOM 市场 rd_mined 训练库 → 注册字段/发布目录
 * → 训练页直读。物化此前只有 CLI，页面上的「已完成」并不代表因子可训练；
 * 本面板把「还有几个因子没进训练库、库里现在多少列、目录是否最新、上一轮日志」
 * 摊开，并提供一次带确认的「开始物化」。
 *
 * 后端契约：
 *   GET  /admin/training-data/rd-mined/materialize/status
 *   POST /admin/training-data/rd-mined/materialize/start   （忙时 409；
 *        回包前已确认子进程真正持锁，所以「started」= 有物化在跑）
 * 物化不可中断：中途 kill 会在库里留下半列状态，只能靠重跑收尾——所以面板
 * 只有「开始」，没有「停止」。
 *
 * 完成判定的纪律（别改坏）：running 只能由**服务端响应**点亮，
 * `start()` 不许自己造一个 running 出来——子进程冷启 ~4s 才拿锁，起完立刻
 * 探测恒为 false，造出来的 true 会把它当成「运行→结束」的边，在刚开始就
 * 弹「已结束」并且再也不装轮询定时器（整轮运行从面板上消失）。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Col, Modal, Row, Space, Statistic, Tag, Tooltip,
  Typography, message,
} from 'antd';
import { PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons';

import { adminService } from '../services/adminService';
import type { RdMinedMaterializeStatus } from '../types';

const { Text } = Typography;

/** 运行中轮询间隔：物化以分钟/因子推进，10s 足够跟上进度且不给后端压力。 */
const POLL_INTERVAL_MS = 10_000;
/** 单因子物化的经验耗时（分钟），用于给出粗略预估。 */
const MINUTES_PER_FACTOR = 2;
/** 启动宽限：POST 成功后的探测窗口，期间 running=false 不当作「结束」。 */
const START_CONFIRM_GRACE_MS = 30_000;

const PENDING_REASON_LABELS: Record<string, string> = {
  new: '新因子',
  retry: '失败重试',
  code_changed: '代码已更新',
  force: '强制重算',
};

const SKIPPED_REASON_LABELS: Record<string, string> = {
  already_materialized: '已物化',
  rejected_duplicate: '值级重复被拒',
  no_code: '无代码',
  market_unsupported: '非 A 股市场',
};

function describeCounts(counts: Record<string, number> | undefined, labels: Record<string, string>): string {
  const entries = Object.entries(counts || {}).filter(([, n]) => n > 0);
  if (entries.length === 0) return '—';
  return entries.map(([key, n]) => `${labels[key] || key} ${n}`).join(' · ');
}

interface RdMinedMaterializePanelProps {
  /** 一轮物化由运行中转为结束时回调（宿主页据此刷新因子/统计）。 */
  onCompleted?: () => void;
}

export const RdMinedMaterializePanel: React.FC<RdMinedMaterializePanelProps> = ({ onCompleted }) => {
  const [status, setStatus] = useState<RdMinedMaterializeStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [logExpanded, setLogExpanded] = useState(false);
  /** 启动宽限窗口：POST 成功后短暂开启，让轮询先跑起来等子进程拿锁。 */
  const [graceActive, setGraceActive] = useState(false);

  // 只在服务端**确认过运行**后再看到 false 的那一次做完成提示；
  // ref 供轮询回调读取，避免闭包过期
  const confirmedRunningRef = useRef(false);
  const seqRef = useRef(0);
  const mountedRef = useRef(true);
  const graceTimerRef = useRef<number | null>(null);
  const pollErrorsRef = useRef(0);
  const onCompletedRef = useRef(onCompleted);
  onCompletedRef.current = onCompleted;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (graceTimerRef.current !== null) {
        window.clearTimeout(graceTimerRef.current);
        graceTimerRef.current = null;
      }
    };
  }, []);

  const clearGrace = useCallback(() => {
    if (graceTimerRef.current !== null) {
      window.clearTimeout(graceTimerRef.current);
      graceTimerRef.current = null;
    }
    setGraceActive(false);
  }, []);

  const fetchStatus = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    const seq = ++seqRef.current; // 迟到的旧响应不许覆盖新快照
    try {
      const next = await adminService.getRdMinedMaterializeStatus();
      if (seq !== seqRef.current || !mountedRef.current) return;
      pollErrorsRef.current = 0;
      const finished = confirmedRunningRef.current && !next.running;
      confirmedRunningRef.current = next.running;
      if (next.running) clearGrace();
      setStatus(next);
      if (finished) {
        const remaining = next.overview.candidates.pending;
        if (remaining === 0 && next.overview.catalog.up_to_date) {
          message.success('物化完成：训练目录已刷新到最新');
        } else if (remaining > 0) {
          message.warning(`本轮物化已结束，仍有 ${remaining} 个因子待物化（原因见面板，日志尾部有明细）`);
        } else {
          message.info('本轮物化已结束，请查看日志确认结果');
        }
        onCompletedRef.current?.();
      }
    } catch (error: any) {
      if (!mountedRef.current) return;
      if (!silent) {
        message.error(error?.response?.data?.detail || error?.message || '物化状态加载失败');
      } else {
        // 轮询静默失败不能让面板假装一切正常：一次可见告警，之后交给重试
        pollErrorsRef.current += 1;
        if (pollErrorsRef.current === 1) {
          message.warning('物化状态刷新失败，面板可能滞后（将自动重试）');
        }
      }
    } finally {
      if (!silent && mountedRef.current) setLoading(false);
    }
  }, [clearGrace]);

  useEffect(() => { void fetchStatus(); }, [fetchStatus]);

  // 运行中（或启动宽限期内）才轮询；结束即停（完成的那一次由 fetchStatus 判定）
  useEffect(() => {
    if (!status?.running && !graceActive) return undefined;
    const timer = window.setInterval(() => { void fetchStatus(true); }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [status?.running, graceActive, fetchStatus]);

  const start = () => {
    const pending = status?.overview.candidates.pending ?? 0;
    const estimate = Math.max(1, Math.round(pending * MINUTES_PER_FACTOR));
    Modal.confirm({
      title: '开始物化 RD 挖掘因子？',
      okText: '开始物化',
      cancelText: '取消',
      content: pending > 0
        ? `将把 ${pending} 个待物化因子写入 rd_mined 训练库，按经验约 ${MINUTES_PER_FACTOR} 分钟/个（预计 ${estimate} 分钟以内），完成后自动刷新字段注册并发布训练目录。物化不可中断：中途停止会在库里留下半列状态，只能重跑收尾。`
        : '当前没有待物化因子；本次只做字段注册与目录发布刷新（通常数秒）。',
      onOk: async () => {
        setStarting(true);
        try {
          const result = await adminService.startRdMinedMaterialize();
          if (!mountedRef.current) return;
          message.success(result.message || '物化已在后台启动');
          // 后端回包已确认持锁；宽限期只兜「确认→首次探测」之间的残余窗口，
          // 期间看到 false 不算结束（真正的结束必须由服务端确认过 true 之后）
          confirmedRunningRef.current = false;
          setGraceActive(true);
          if (graceTimerRef.current !== null) window.clearTimeout(graceTimerRef.current);
          graceTimerRef.current = window.setTimeout(() => {
            graceTimerRef.current = null;
            setGraceActive(false);
          }, START_CONFIRM_GRACE_MS);
          await fetchStatus(true);
        } catch (error: any) {
          if (!mountedRef.current) return;
          message.error(error?.response?.data?.detail || error?.message || '启动物化失败');
        } finally {
          if (mountedRef.current) setStarting(false);
        }
      },
    });
  };

  const overview = status?.overview;
  const candidates = overview?.candidates;
  const manifest = overview?.manifest;
  const library = overview?.library;
  const catalog = overview?.catalog;
  const running = Boolean(status?.running);
  const pending = candidates?.pending ?? 0;
  const log = status?.log;
  // 失败磁贴读的是清单的 error 状态（生产只写 materialized/rejected_duplicate/error，
  // 没有 failed 这个键——写成 failed 会永远显示 0 红不起来）
  const failedCount = manifest?.by_status?.error ?? 0;

  const stat = (title: string, value: number | string, color?: string, hint?: string) => (
    <Col xs={12} md={8} lg={4} key={title}>
      <Tooltip title={hint}>
        <Statistic title={title} value={value} valueStyle={color ? { color } : undefined} />
      </Tooltip>
    </Col>
  );

  return (
    <Card
      title={
        <Space>
          因子物化（训练库）
          {running ? <Tag color="processing">物化运行中</Tag> : <Tag>空闲</Tag>}
          {catalog?.up_to_date
            ? <Tag color="green">训练目录已最新</Tag>
            : <Tag color="orange">训练目录待刷新</Tag>}
        </Space>
      }
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} size="small" loading={loading} onClick={() => void fetchStatus()}>
            刷新
          </Button>
          <Tooltip title={running ? '已有物化在跑，结束前不能重复启动' : '后台执行一次物化，完成后自动发布训练目录'}>
            <Button
              type="primary"
              size="small"
              icon={<PlayCircleOutlined />}
              loading={starting}
              disabled={running || graceActive || loading}
              onClick={start}
            >
              开始物化
            </Button>
          </Tooltip>
        </Space>
      }
    >
      <div className="space-y-4">
        <Row gutter={[16, 12]}>
          {stat(
            '待物化',
            pending,
            pending > 0 ? '#d97706' : undefined,
            `原因分布：${describeCounts(candidates?.pending_reasons, PENDING_REASON_LABELS)}`,
          )}
          {stat(
            '已物化',
            manifest?.by_status?.materialized ?? 0,
            '#059669',
            `跳过明细：${describeCounts(candidates?.skipped, SKIPPED_REASON_LABELS)}`,
          )}
          {stat('值级重复被拒', manifest?.by_status?.rejected_duplicate ?? 0, undefined, '与库内既有因子高度相关的因子不会入库')}
          {stat('失败', failedCount, failedCount > 0 ? '#dc2626' : undefined, '失败因子会在下一次物化时自动重试')}
          {stat('库内因子列', library?.factor_columns ?? '—', undefined, library?.error ? `库读取异常：${library.error}` : `rd_mined 库（CUSTOM 市场），${library?.partitions ?? 0} 个分区`)}
          {stat('已发布列', catalog?.published_columns ?? '—', undefined, catalog?.error ? `目录读取异常：${catalog.error}` : `当前发布版本：${catalog?.published_version || '无'}`)}
        </Row>

        {candidates?.error && (
          <Alert type="warning" showIcon message={`候选查询失败：${candidates.error}`} />
        )}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>候选总数：{candidates?.total ?? '—'}</span>
          <span>清单条目：{manifest?.total ?? '—'}</span>
          <span>最近物化：{manifest?.last_at || '尚无记录'}</span>
          <span>库覆盖：{library?.min_date || '--'} ～ {library?.max_date || '--'}</span>
          <span>日志：<Text code className="text-xs">{log?.path || '—'}</Text></span>
        </div>

        {pending > 0 && !running && (
          <Alert
            type="info"
            showIcon
            message={`有 ${pending} 个因子尚未进入训练库（${describeCounts(candidates?.pending_reasons, PENDING_REASON_LABELS)}）`}
            description={`点右上角「开始物化」后，这些因子会逐个写入 rd_mined 库；完成后自动刷新字段注册并发布训练目录，训练页即可直读。`}
          />
        )}

        {log?.exists && (
          <div>
            <Button type="link" size="small" className="!px-0" onClick={() => setLogExpanded(!logExpanded)}>
              {logExpanded ? '收起运行日志' : `展开运行日志（最近 ${log.lines.length} 行）`}
            </Button>
            {logExpanded && (
              <pre className="bg-slate-900 text-slate-100 text-xs rounded-lg p-4 overflow-auto max-h-72 m-0">
                <code>{log.lines.join('\n') || '（日志为空）'}</code>
              </pre>
            )}
          </div>
        )}
      </div>
    </Card>
  );
};

export default RdMinedMaterializePanel;
