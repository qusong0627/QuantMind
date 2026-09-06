import { message } from 'antd';
import type { InferenceRankingResult } from '../../services/modelTrainingService';
import { buildCsvText, downloadCsvFile } from '../../utils/csvExport';

/** 把 stdout/stderr 按行拆分：ERROR/CRITICAL/TRACEBACK 归入错误输出，其余归入标准输出。 */
export function splitInferenceLogs(stdout?: string | null, stderr?: string | null): {
  stdout: string;
  stderr: string;
} {
  const infoLines: string[] = [];
  const errorLines: string[] = [];
  const pushLines = (raw: string, source: 'stdout' | 'stderr') => {
    raw.split(/\r?\n/).forEach((line) => {
      const text = line.trimEnd();
      if (!text) return;
      const upper = text.toUpperCase();
      const isError = /\b(ERROR|CRITICAL|EXCEPTION|TRACEBACK|FAILED|FAILURE)\b/.test(upper);
      const isInfo = /\bINFO\b/.test(upper);
      const isWarn = /\b(WARNING|WARN)\b/.test(upper);
      if (isError) {
        errorLines.push(text);
        return;
      }
      if (source === 'stderr' && isInfo && !isWarn) {
        infoLines.push(text);
        return;
      }
      infoLines.push(text);
    });
  };
  if (stdout) pushLines(stdout, 'stdout');
  if (stderr) pushLines(stderr, 'stderr');
  return {
    stdout: infoLines.join('\n'),
    stderr: errorLines.join('\n'),
  };
}

/** 把排名结果导出为 CSV 并触发下载，返回是否成功。 */
export function exportRankingCsv(result: InferenceRankingResult): boolean {
  if (!result || result.rankings.length === 0) {
    message.warning('暂无可导出的排名数据');
    return false;
  }
  const rows: unknown[][] = result.rankings.map(r => [r.rank, r.code, r.name, r.score, r.signal]);
  try {
    const csv = buildCsvText(
      ['排名', '股票代码', '股票名称', '预测得分', '信号'],
      rows,
      { textColumns: [1], filename: '' },
    );
    const ok = downloadCsvFile(
      csv,
      `ranking_${result.target_date || 'result'}_${result.summary?.run_id || 'run'}.csv`,
    );
    if (!ok) throw new Error('下载失败');
    message.success(`已导出 ${result.rankings.length} 条排名数据`);
    return true;
  } catch (err: any) {
    message.error(`导出失败: ${err?.message ?? '未知错误'}`);
    return false;
  }
}
