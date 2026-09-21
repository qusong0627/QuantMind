/**
 * 交易模式切换统一入口（T-FE-18）：切到实盘一律先过危险确认卡——唯一实现。
 *
 * 此前 HeaderBar 与交易页各自实现切换，只有一处带确认（同动作双入口、闸门不一致）；
 * 两处入口现统一走本 hook：写偏好 → 派发 store，切换实盘前置二次确认。
 */

import React, { useCallback, useState } from 'react';
import { useAppDispatch, useAppSelector } from '../../store';
import { selectTradingMode, setTradingMode } from '../../store/slices/uiSlice';
import { DangerConfirmModal } from '../../components/shared/compliance/DangerConfirmModal';
import { DANGER_SCENARIOS } from '../../components/shared/compliance/dangerAction';
import { isLiveTradingEnabled } from '../../config/tradingFlags';

export type TradingModePref = 'real' | 'simulation';

/** 交易模式偏好键（唯一实现；初始化与切换都读这里） */
export const TRADING_MODE_PREF_KEY = 'qm:trading_mode_pref';

export function useTradingModeSwitch(): {
  tradingMode: TradingModePref;
  requestSwitch: (mode: TradingModePref) => void;
  confirmModal: React.ReactNode;
} {
  const dispatch = useAppDispatch();
  const rawTradingMode = useAppSelector(selectTradingMode);
  // 实盘开关关闭时对外一律报模拟盘：本 hook 是模式的两个入口（顶栏 / 交易页）的唯一
  // 收口点，在这里归一，下游组件不必各自判一遍
  const liveEnabled = isLiveTradingEnabled();
  const tradingMode: TradingModePref =
    rawTradingMode === 'real' && liveEnabled ? 'real' : 'simulation';
  const [pendingReal, setPendingReal] = useState(false);

  const applyMode = useCallback(
    (mode: TradingModePref) => {
      const next: TradingModePref = mode === 'real' && !liveEnabled ? 'simulation' : mode;
      localStorage.setItem(TRADING_MODE_PREF_KEY, next);
      dispatch(setTradingMode(next));
    },
    [dispatch, liveEnabled]
  );

  const requestSwitch = useCallback(
    (mode: TradingModePref) => {
      if (mode === tradingMode) return;
      if (mode === 'real') {
        // 开关关闭时直接吞掉，连确认卡都不弹——弹一张「我已知悉，切换实盘」
        // 却切不过去的卡，比没反应更像坏了
        if (!liveEnabled) return;
        setPendingReal(true); // 两步确认：确认卡未确认前不改状态
        return;
      }
      applyMode('simulation');
    },
    [tradingMode, applyMode, liveEnabled]
  );

  const confirmModal = (
    <DangerConfirmModal
      open={pendingReal}
      scenario={DANGER_SCENARIOS.switch_real}
      onConfirm={() => {
        applyMode('real');
        setPendingReal(false);
      }}
      onCancel={() => setPendingReal(false)}
    />
  );

  return { tradingMode, requestSwitch, confirmModal };
}
