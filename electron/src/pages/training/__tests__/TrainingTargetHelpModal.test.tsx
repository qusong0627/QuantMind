import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TrainingTargetHelpModal } from '../TrainingTargetHelpModal';

describe('TrainingTargetHelpModal 标签截面变换说明', () => {
  test('open 时展示六种变换与按模型建议', () => {
    render(<TrainingTargetHelpModal open onClose={() => {}} />);
    expect(screen.getByText('标签截面变换：怎么选、为什么')).toBeInTheDocument();
    expect(screen.getByText('截面 rank（默认）')).toBeInTheDocument();
    expect(screen.getByText('截面 zscore')).toBeInTheDocument();
    expect(screen.getByText('原始收益')).toBeInTheDocument();
    expect(screen.getByText('top-tail（只学最强尾部）')).toBeInTheDocument();
    expect(screen.getByText('同市值桶内超额 rank')).toBeInTheDocument();
    expect(screen.getByText('同市值桶内超额 zscore')).toBeInTheDocument();
    expect(screen.getByText('LightGBM / XGBoost / CatBoost')).toBeInTheDocument();
    expect(screen.getByText(/不要跨 transform 直接比 IC/)).toBeInTheDocument();
  });
});
