/**
 * 实盘节点形态开关（Windows 单机包）
 *
 * 实盘节点只做两件事：QuantBot 与实盘交易。新闻（Ubuntu 那台的 Huntly）和
 * 每日推理数据都在后端/同步脚本里，不占栏目。所以这个形态下：
 * 底部 Dock 整个不渲染（`FloatingNavBar` 直接 return null），根路径与其余栏目
 * 一律回落到实盘交易页（无本机实盘页时退回 /quantbot）；QuantBot 是那一页侧栏里
 * 「设置」下面的一栏（`features/local-live/LiveTradingPage.tsx` 的追加页签，
 * 右边内容区渲染），**不占左侧常驻栏**。开放式部署（容器栈）保持全栏目，
 * 靠构建期环境变量区分。
 *
 * 通过 VITE_LIVE_NODE_ONLY 控制：true 裁剪、false/缺省 全栏目。
 * 默认：两种环境都**不裁剪**——「缺省就全栏目」是安全侧，裁剪是实盘包
 * 构建时显式声明的（build_live_pack.sh 会对产物做断言，漏传直接失败）。
 *
 * 与 tradingFlags.ts / marketFlags.ts 同族：环境变量是唯一读取点，
 * 其他模块只 import 这个常量，不要各自读 import.meta.env。
 */

const envValue = (import.meta.env.VITE_LIVE_NODE_ONLY as string | undefined)?.toLowerCase();

export const LIVE_NODE_ONLY: boolean = envValue === 'true';
