type StockIndexEntry = {
  symbol?: string;
  code?: string;
  exchange?: string;
  name?: string;
};

type StockIndexPayload = {
  items?: StockIndexEntry[];
};

const STOCK_INDEX_URL = new URL('../../../../../data/stocks/stocks_index.json', import.meta.url).href;

let stockNameMapPromise: Promise<Map<string, string>> | null = null;

function buildLookupKeys(rawSymbol?: string): string[] {
  const symbol = String(rawSymbol || '').trim().toUpperCase();
  if (!symbol) {
    return [];
  }

  const keys = new Set<string>([symbol]);

  if (/^(SH|SZ)\d{6}$/.test(symbol)) {
    const exchange = symbol.slice(0, 2);
    const code = symbol.slice(2);
    keys.add(code);
    keys.add(`${code}.${exchange}`);
  }

  if (/^\d{6}\.(SH|SZ)$/.test(symbol)) {
    const [code, exchange] = symbol.split('.');
    keys.add(code);
    keys.add(`${exchange}${code}`);
  }

  if (/^\d{6}$/.test(symbol)) {
    keys.add(symbol);
  }

  return [...keys];
}

async function loadStockNameMap(): Promise<Map<string, string>> {
  const response = await fetch(STOCK_INDEX_URL);
  if (!response.ok) {
    throw new Error(`load stocks_index.json failed: ${response.status}`);
  }

  const payload = (await response.json()) as StockIndexPayload;
  const entries = Array.isArray(payload.items) ? payload.items : [];
  const map = new Map<string, string>();

  for (const entry of entries) {
    const name = String(entry?.name || '').trim();
    if (!name) {
      continue;
    }

    const rawKeys = [
      ...buildLookupKeys(entry?.symbol),
      ...buildLookupKeys(entry?.code),
      ...buildLookupKeys(
        entry?.code && entry?.exchange ? `${String(entry.exchange).toUpperCase()}${String(entry.code)}` : undefined
      ),
    ];

    for (const key of rawKeys) {
      if (!map.has(key)) {
        map.set(key, name);
      }
    }
  }

  return map;
}

function getStockNameMap(): Promise<Map<string, string>> {
  if (!stockNameMapPromise) {
    stockNameMapPromise = loadStockNameMap().catch((error) => {
      stockNameMapPromise = null;
      throw error;
    });
  }

  return stockNameMapPromise;
}

export async function patchMissingStockNames<T extends { symbol?: string; name?: string }>(items: T[]): Promise<T[]> {
  if (!Array.isArray(items) || items.length === 0) {
    return items;
  }

  const needsPatch = items.some((item) => !String(item?.name || '').trim() && String(item?.symbol || '').trim());
  if (!needsPatch) {
    return items;
  }

  try {
    const nameMap = await getStockNameMap();

    return items.map((item) => {
      if (String(item?.name || '').trim()) {
        return item;
      }

      const matchedName = buildLookupKeys(item?.symbol)
        .map((key) => nameMap.get(key))
        .find((value) => Boolean(value));

      if (!matchedName) {
        return item;
      }

      return {
        ...item,
        name: matchedName,
      };
    });
  } catch (error) {
    console.warn('[stockNameIndex] failed to patch stock names', error);
    return items;
  }
}