import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type {
  WizardState,
  Condition,
  BuyRule,
  SellRule,
  QlibParams,
  ValidationResult,
  SaveStatus
} from '../types';

interface WizardActions {
  setConditions: (c: Condition | null) => void;
  setPool: (pool: WizardState['pool']) => void;
  setBuyRules: (rules: BuyRule[]) => void;
  setSellRules: (rules: SellRule[]) => void;
  setQlibParams: (params: QlibParams) => void;
  setRisk: (risk: WizardState['risk']) => void;
  setStrategyStyle: (style: WizardState['strategyStyle']) => void;
  setPositionConfig: (config: WizardState['positionConfig']) => void;
  setGenerated: (g: WizardState['generated']) => void;
  addCustomStock: (stock: { symbol: string; name: string; price?: number }) => void;
  removeCustomStock: (symbol: string) => void;
  setSelectedSymbols: (symbols: string[]) => void;
  setValidationResult: (result: ValidationResult | null) => void;
  markAsCloudSaved: (url: string, strategyId: string) => void;
  markAsDownloaded: () => void;
  setPoolFile: (poolFile: WizardState['poolFile']) => void;
  clearPoolFile: () => void;
  reset: () => void;
}

const initialState: WizardState = {
  conditions: null,
  pool: null,
  buyRules: [],
  sellRules: [],
  risk: {
    maxDrawdown: 0.1,
    maxPositionSize: 0.2,
    maxPositions: 10,
    stopLoss: 0.05,
    takeProfit: 0.15,
    commission: 0.0003,
    slippage: 0.001
  },
  strategyStyle: { type: 'conservative' },
  positionConfig: {
    enableDynamicPosition: false,
    bearMarketPosition: 0.3,
    normalMarketPosition: 0.6,
    bullMarketPosition: 0.9,
    strategyTotalPosition: 1.0,
    marketIndexSymbol: "000300.SH",
    detectionWindow: 20,
    volumeThreshold: 0.2
  },
  generated: undefined,
  customPool: [],
  selectedSymbols: [],
  qlibParams: {
    strategy_type: 'TopkDropout',
    topk: 10,
    n_drop: 2,
    rebalance_days: 5,
    rebalance_period: 'weekly',
  },
  validationResult: null,
  saveStatus: {
    savedToCloud: false,
    downloadedLocally: false,
  },
  poolFile: undefined,
};

export const useWizardStore = create<WizardState & WizardActions>()(
  persist(
    (set) => ({
      ...initialState,
      setConditions: (c) => set({ conditions: c }),
      setPool: (pool) => set({
        pool,
        selectedSymbols: (pool?.items || []).map(s => s.symbol)
      }),
      setSelectedSymbols: (selectedSymbols) => set({ selectedSymbols }),
      setBuyRules: (rules) => set({ buyRules: rules }),
      setSellRules: (rules) => set({ sellRules: rules }),
      setQlibParams: (params) => set({ qlibParams: params }),
      setRisk: (risk) => set({ risk }),
      setStrategyStyle: (strategyStyle) => set({ strategyStyle }),
      setPositionConfig: (positionConfig) => set({ positionConfig }),
      setGenerated: (g) => set({
        generated: g,
        // 重新生成策略时重置保存状态和验证结果
        saveStatus: { savedToCloud: false, downloadedLocally: false },
        validationResult: null,
      }),
      addCustomStock: (stock) => set((state) => {
        const exists = state.customPool?.some(s => s.symbol === stock.symbol);
        if (exists) return state;
        return { customPool: [...(state.customPool || []), stock] };
      }),
      removeCustomStock: (symbol) => set((state) => ({
        customPool: (state.customPool || []).filter(s => s.symbol !== symbol)
      })),
      setValidationResult: (result) => set({ validationResult: result }),
      markAsCloudSaved: (url, strategyId) => set((state) => ({
        saveStatus: {
          ...state.saveStatus,
          savedToCloud: true,
          cloudUrl: url,
          strategyId,
        }
      })),
      markAsDownloaded: () => set((state) => ({
        saveStatus: { ...state.saveStatus, downloadedLocally: true }
      })),
      setPoolFile: (poolFile) => set({ poolFile }),
      clearPoolFile: () => set({ poolFile: undefined }),
      reset: () => set({ ...initialState }),
    }),
    { name: 'strategy-wizard' }
  )
);
