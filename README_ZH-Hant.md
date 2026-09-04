# QuantMind OSS

QuantMind 是面向中國 A 股量化研究與交易的一體化平台，將資料、因子研究、模型訓練、推論、回測、模擬交易與運維監控串成可重現的工作流程。

完整的簡體中文介紹與產品截圖請見 [README.md](README.md)。

## 核心能力

- 市場看板、資金流分析、RSS 資訊與 QuantBot
- 基於 Qlib 的特徵工程、訓練任務、模型資產與批次推論
- 策略工作區、因子研究、組合回測與績效評估
- 模擬交易、訂單與持倉、風控預檢及服務監控

## 部署

支援 Ubuntu 22.04 / 24.04。正式環境建議使用完整部署包，其中包含 Docker 映像、PostgreSQL 業務資料、模型及 Qlib 資料。

```bash
# 完整線上部署（自 CDN 下載完整包，開箱即用）
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/full-deploy.sh | sudo bash

# 線上原始碼部署
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/deploy.sh | sudo bash
```

預設完整包位址為 `https://cdn.quantmind.cloud/quantmind-offline`。更新既有伺服器：

```bash
cd /opt/quantmind
sudo bash deploy/update.sh
```

| 服務 | 預設位址 |
| --- | --- |
| Web | `http://<伺服器 IP>:3000` |
| API 文件 | `http://<伺服器 IP>:8000/docs` |

離線包還原與進階選項請見 [deploy/README.md](deploy/README.md)。

## 開發

```bash
python backend/run_tests.py unit

cd electron
npm install
npm run dev
npm run typecheck
```

內部股票代碼統一使用前綴格式，例如 `SH600036`。

## 免責聲明

QuantMind 僅供研究與學習使用，不構成任何投資建議。
