/**
 * 推理排名 → 精简版个股终端弹窗
 *
 * 只保留个股终端的两件东西：K 线主图 + 底部模型分数副图（复用终端同一张 KlineChart），
 * 去掉终端的详情 Tab、自选股、财报等外围。
 *
 * 两处刻意的口径选择：
 *   1. 分数曲线锁定「跑出该排名的那个模型」（run 的 model_id），而不是用户默认模型——
 *      否则看到的分数和排名不是同一个模型给的。
 *   2. K 线上用竖线标出 run 的基准日（inference_date），看得出这个排名是基于哪天数据算的。
 *
 * 代码格式（仓库分层口径）：排名行给的是前缀式 SH600519；/market/kline 要后缀式
 * 600519.SH，分数接口要裸数字 600519，两次转换都走 portfolioUtils 的统一工具。
 */
import { useEffect, useMemo, useState } from 'react';
import { Empty, Modal, Spin } from 'antd';
import { KlineChart, type IndicatorConfig } from '../../stock-terminal/components/kline/KlineChart';
import type { KlineBar } from '../../stock-terminal/types';
import { stockTerminalService } from '../../stock-terminal/services/stockTerminalService';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { toSuffixCode } from '../../../utils/portfolioUtils';

/** 与个股终端一致：主图开 MA、副图只留成交量 */
const KLINE_CONFIG: IndicatorConfig = { ma: true, subplots: ['vol'] };
const KLINE_BARS = 500;   // 近 2 年日 K
const ZOOM_BARS = 200;    // 首屏聚焦最近 200 根，可向左拖看更早
const SCORE_DAYS = 750;   // 分数窗口需覆盖 K 线全区间，否则左侧交易日没有分数
const CHART_HEIGHT = 560;

interface Props {
  open: boolean;
  onClose: () => void;
  /** 排名行代码，前缀式（SH600519） */
  symbol: string;
  name?: string;
  /** 该次 run 的模型：分数曲线锁定它 */
  modelId?: string;
  /** run 的基准日（inference_date）：在 K 线上标竖线 */
  asOfDate?: string;
}

export function StockMiniTerminalModal({ open, onClose, symbol, name, modelId, asOfDate }: Props) {
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [scorePoints, setScorePoints] = useState<{ date: string; value: number }[]>([]);
  const [loading, setLoading] = useState(false);

  const suffixSymbol = useMemo(() => toSuffixCode(symbol), [symbol]);
  const bareCode = useMemo(() => suffixSymbol.split('.')[0], [suffixSymbol]);

  useEffect(() => {
    if (!open || !symbol) return;
    let cancelled = false;
    setLoading(true);
    setBars([]);
    setScorePoints([]);

    const endD = new Date();
    const startD = new Date(endD);
    startD.setFullYear(startD.getFullYear() - 2);
    const iso = (d: Date) => d.toISOString().slice(0, 10);

    // 两个请求互相独立：分数接口（getStockInferenceHistory）失败会抛，而 K 线接口
    // （getDailyKline）内部已兜底为空数组。用 allSettled 是为了「分数挂了仍然出 K 线」，
    // 否则 Promise.all 一旦被分数拒绝，K 线明明取到了也会显示成「暂无数据」。
    void (async () => {
      const [klineRes, scoreRes] = await Promise.allSettled([
        stockTerminalService.getDailyKline(suffixSymbol, KLINE_BARS, 'qfq', iso(startD), iso(endD)),
        modelTrainingService.getStockInferenceHistory(bareCode, SCORE_DAYS, modelId || undefined),
      ]);
      if (cancelled) return;
      if (klineRes.status === 'fulfilled') {
        setBars(klineRes.value ?? []);
      }
      if (scoreRes.status === 'fulfilled') {
        setScorePoints(
          (scoreRes.value?.items ?? [])
            .filter((it) => it.fusion_score != null)
            .map((it) => ({
              date: String(it.trade_date).slice(0, 10),
              value: Number(it.fusion_score),
            }))
            .sort((a, b) => a.date.localeCompare(b.date)),
        );
      }
      setLoading(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [open, symbol, suffixSymbol, bareCode, modelId]);

  const zoomStart = bars.length > ZOOM_BARS ? ((bars.length - ZOOM_BARS) / bars.length) * 100 : 0;

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={1000}
      centered
      destroyOnClose
      title={
        <span className="text-sm font-black text-slate-800">
          {name || symbol}
          <span className="ml-2 font-mono text-xs font-bold text-slate-400">{symbol}</span>
        </span>
      }
    >
      <div style={{ height: CHART_HEIGHT }}>
        {loading ? (
          <div className="h-full flex flex-col items-center justify-center gap-3">
            <Spin />
            <span className="text-xs text-slate-500 font-semibold">正在加载 K 线与模型分数…</span>
          </div>
        ) : bars.length ? (
          <KlineChart
            bars={bars}
            config={KLINE_CONFIG}
            height={CHART_HEIGHT}
            scorePoints={scorePoints}
            showScoreSubplot
            zoomStart={zoomStart}
            zoomEnd={100}
            selectedDate={asOfDate}
          />
        ) : (
          <div className="h-full flex items-center justify-center">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span className="text-xs">暂无 K 线数据（该标的无行情覆盖，或行情接口未取到）</span>}
            />
          </div>
        )}
      </div>
    </Modal>
  );
}

export default StockMiniTerminalModal;
