export function describeError(error: unknown): string {
    return error instanceof Error ? error.message : '未知错误';
}

/** 取 axios 错误里的 HTTP 状态码（无响应时返回 undefined）。 */
export function httpStatusOf(error: unknown): number | undefined {
    return (error as { response?: { status?: number } } | undefined)?.response?.status;
}

/** YYYYMMDD → YYYY-MM-DD；非 8 位原样返回。 */
export function formatPartitionDate(raw?: string): string {
    if (!raw || raw.length !== 8) return raw ?? '—';
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
}

export function formatSize(sizeMb: number): string {
    if (typeof sizeMb !== 'number' || !Number.isFinite(sizeMb)) return '0 MB';
    if (sizeMb >= 1024) return `${(sizeMb / 1024).toFixed(2)} GB`;
    return `${sizeMb.toFixed(1)} MB`;
}
