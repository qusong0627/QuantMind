/**
 * 持仓预警的多通道投递（桌面系统通知 + 声音）
 *
 * 为什么不是 React hook：提醒必须**不依赖当前页面**。站内面板只挂在仪表盘/交易台，
 * 用户盯持仓时未必开着那一页；把投递绑在某个组件的生命周期上，就会「切走了就不响」。
 * 这里是纯模块，谁调都行，状态在模块级。
 *
 * **去重按 id 单调递增**（`qm_holding_alerts.id` 是 BIGSERIAL）：模块内记 `lastDeliveredId`，
 * 只播 id 更大的。首次调用只播种不播报——否则一进页面就把历史预警全轰一遍。
 * 这个去重是跨调用方共享的：面板轮询与 WS 实时路径同时喂进来也只会响一次。
 */

import type {
  HoldingAlertConfig,
  HoldingAlertItem,
  HoldingAlertSeverity,
} from './holdingAlertService';
import { DEFAULT_ALERT_CONFIG, SEVERITY_ORDER } from './holdingAlertService';

/** 已投递到的最大预警 id（会话内单调递增；0 = 还没播种） */
let lastDeliveredId = 0;
/** 是否已播种。未播种时第一次调用只记录水位、不出声 */
let seeded = false;

let audioContext: AudioContext | null = null;
/** 浏览器自动播放策略导致 AudioContext 被挂起的告警只打一次，不刷屏 */
let audioWarned = false;

export interface DeliveryOutcome {
  delivered: number;
  desktop: number;
  sound: number;
}

const NO_DELIVERY: DeliveryOutcome = { delivered: 0, desktop: 0, sound: 0 };

/** 与后端 `meets_min_severity` 同口径：低于阈值只留痕，不打扰 */
function meetsMinSeverity(severity: HoldingAlertSeverity, min: string): boolean {
  return (SEVERITY_ORDER[severity] ?? 0) >= (SEVERITY_ORDER[min as HoldingAlertSeverity] ?? 1);
}

/**
 * 桌面通知：`window.electronAPI.showNotification` 在两种运行形态下都可用——
 * Electron 里是 preload 的 IPC（`electron/main.ts` 弹原生 Notification），
 * 浏览器里是 `utils/electronCompat.ts` 注入的同名降级实现（浏览器 Notification + 申请授权）。
 */
function showDesktop(title: string, body: string): boolean {
  try {
    const api = window.electronAPI as { showNotification?: (t: string, b: string) => Promise<void> } | undefined;
    if (!api?.showNotification) return false;
    void api.showNotification(title, body);
    return true;
  } catch (err) {
    console.warn('[alertDelivery] 桌面通知失败', err);
    return false;
  }
}

/** 声音：Web Audio 现场合成（仓库里没有任何音频资源，加一个 mp3 只为「叮」一声不划算） */
function playChime(severity: HoldingAlertSeverity): boolean {
  try {
    const Ctor = window.AudioContext
      || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return false;
    audioContext = audioContext || new Ctor();
    const ctx = audioContext;
    // 自动播放策略：没有用户手势前 ctx 是 suspended，resume() 是异步的，这里不 await
    if (ctx.state === 'suspended') {
      void ctx.resume();
    }
    if (ctx.state !== 'running') {
      if (!audioWarned) {
        audioWarned = true;
        console.warn('[alertDelivery] 音频上下文未运行（浏览器需先有一次用户交互），本次跳过声音提示');
      }
      return false;
    }

    // critical 两声高音（1200Hz），warning 一声低音（700Hz）——音色本身区分轻重
    const critical = severity === 'critical';
    const freq = critical ? 1200 : 700;
    const beeps = critical ? 2 : 1;
    const start = ctx.currentTime;
    for (let i = 0; i < beeps; i += 1) {
      const at = start + i * 0.22;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, at);
      // 指数衰减包络：避免方波式「咔」的爆音
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.09, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.18);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(at);
      osc.stop(at + 0.2);
    }
    return true;
  } catch (err) {
    console.warn('[alertDelivery] 声音提示失败', err);
    return false;
  }
}

/**
 * 投递一批预警（调用方按任意节奏喂；内部只播「没播过的」）。
 *
 * @param items 预警列表（顺序不限，内部按 id 升序播报）
 * @param config 用户配置（关总开关 / 关通道 / 低于 min_severity 都不播）
 */
export function deliverNewAlerts(
  items: HoldingAlertItem[],
  config: HoldingAlertConfig | null | undefined,
): DeliveryOutcome {
  const list = Array.isArray(items) ? items : [];
  const maxId = list.reduce((acc, item) => Math.max(acc, Number(item?.id) || 0), 0);

  if (!seeded) {
    // 播种：把当前已有的当成「已读」，避免挂载瞬间对历史预警狂轰
    seeded = true;
    lastDeliveredId = maxId;
    return NO_DELIVERY;
  }
  if (maxId <= lastDeliveredId) return NO_DELIVERY;

  const fresh = list
    .filter((item) => Number(item?.id) > lastDeliveredId)
    .sort((a, b) => Number(a.id) - Number(b.id));
  // 水位先走：这条不管播不播都算「见过」，否则下一轮还会拿它再判一次
  lastDeliveredId = maxId;

  // **配置缺失按默认（全开）处理，不静默失声**：这是风险提醒，配置读不到就一声不吭，
  // 代价是用户的仓位在无人知晓时恶化；多响一次只是吵。显式关掉（enabled=false）照旧尊重。
  const cfg = config ?? DEFAULT_ALERT_CONFIG;
  if (cfg.enabled === false) return NO_DELIVERY;

  const outcome: DeliveryOutcome = { delivered: 0, desktop: 0, sound: 0 };
  for (const item of fresh) {
    const severity = (item.severity || 'warning') as HoldingAlertSeverity;
    if (!meetsMinSeverity(severity, cfg.min_severity || 'warning')) continue;
    const title = item.title || `${item.stockName || item.symbol} 持仓预警`;
    const body = item.content || `${item.symbol} 触发持仓预警`;
    let hit = false;
    if (cfg.notify_desktop !== false && showDesktop(title, body)) {
      outcome.desktop += 1;
      hit = true;
    }
    if (cfg.notify_sound !== false && playChime(severity)) {
      outcome.sound += 1;
      hit = true;
    }
    if (hit) outcome.delivered += 1;
  }
  return outcome;
}

/**
 * 试听（设置卡「试听」按钮用）：直接播一次，不走水位。
 * 副作用是**顺带解锁 AudioContext**——浏览器要求先有用户手势，点过试听后真正
 * 的预警声音才响得出来。
 */
export function previewAlertSound(severity: HoldingAlertSeverity = 'critical'): boolean {
  return playChime(severity);
}

/**
 * 重置投递状态（登出 / 测试用；生产代码不要随手调，会重复播报）。
 *
 * 连音频上下文一起释放：它按会话缓存（浏览器只该有一个），留着旧实例会让「换了个
 * AudioContext 环境」的后续调用继续用旧的那个。
 */
export function resetAlertDeliveryState(): void {
  lastDeliveredId = 0;
  seeded = false;
  audioContext = null;
  audioWarned = false;
}
