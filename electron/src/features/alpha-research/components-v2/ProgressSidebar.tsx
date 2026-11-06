import React from 'react';
import { Card, CardContent } from './ui/Card';
import { Badge } from './ui/Badge';
import { ExecutionProgress } from '../types-v2';
import { Zap } from 'lucide-react';
import { formatNumber } from '../utils-v2';

interface ProgressSidebarProps {
  progress: ExecutionProgress;
  /** 已落库因子数（来自后端结构化因子，真实数据） */
  factorCount?: number;
  /** 最佳因子 RankIC；挖掘只产出 IC/RankIC/ICIR，其余收益类指标要等回补任务 */
  bestRankIc?: number | null;
  bestFactorName?: string | null;
}

export const ProgressSidebar: React.FC<ProgressSidebarProps> = ({
  progress,
  factorCount = 0,
  bestRankIc = null,
  bestFactorName = null,
}) => {
  return (
    <div className="space-y-3">
      <Card className="glass card-hover animate-fade-in-left">
        <CardContent className="p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <div className="p-2 rounded-lg bg-primary/10">
                <Zap className="h-4 w-4 text-primary animate-pulse" />
              </div>
              <div>
                <div className="text-sm font-bold">{progress.message || '准备中...'}</div>
                <div className="text-[10px] text-muted-foreground">
                  Round {progress.currentRound + 1}/{progress.totalRounds}
                </div>
              </div>
            </div>
            <Badge variant="default" className="text-xs font-mono">
              {progress.progress.toFixed(0)}%
            </Badge>
          </div>
          <div className="relative h-1.5 rounded-full overflow-hidden bg-secondary/30">
            <div
              className="absolute left-0 top-0 h-full bg-gradient-to-r from-blue-500 to-purple-500 transition-all duration-500"
              style={{ width: `${progress.progress}%` }}
            />
          </div>

          <div className="mt-3 space-y-1.5 border-t border-border/40 pt-2 text-[11px]">
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground">已产出因子</span>
              <span className="font-mono font-semibold">{factorCount}</span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground">
                最佳 RankIC{bestFactorName ? ` · ${bestFactorName}` : ''}
              </span>
              <span className="font-mono font-semibold">
                {formatNumber(bestRankIc, 4)}
              </span>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};
