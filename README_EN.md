# QuantMind OSS

QuantMind is an integrated quantitative research and trading platform for the China A-share market. It connects data, factor research, model training, inference, backtesting, paper trading, and operational monitoring in one reproducible workflow.

For the current Chinese documentation and product screenshots, see [README.md](README.md).

## What it provides

- Market dashboards, capital-flow analysis, RSS intelligence, and QuantBot
- Qlib-based feature engineering, training jobs, model registry, and batch inference
- Strategy workspace, factor research, portfolio backtesting, and performance analysis
- Paper trading, orders, positions, risk preflight, and service monitoring

## Deployment

Ubuntu 22.04 / 24.04 is supported. The recommended production method is the complete deployment package, which includes Docker images, PostgreSQL business data, models, and Qlib data.

```bash
# Complete deployment (full package from CDN, production-ready)
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/full-deploy.sh | sudo bash

# Online source deployment
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/deploy.sh | sudo bash
```

The default complete package URL is `https://cdn.quantmind.cloud/quantmind-offline`. To update an existing server:

```bash
cd /opt/quantmind
sudo bash deploy/update.sh
```

| Service | Default URL |
| --- | --- |
| Web | `http://<server-ip>:3000` |
| API docs | `http://<server-ip>:8000/docs` |

See [deploy/README.md](deploy/README.md) for package recovery and deployment options.

## Development

```bash
python backend/run_tests.py unit

cd electron
npm install
npm run dev
npm run typecheck
```

Internal stock symbols use prefix notation, for example `SH600036`.

## Disclaimer

QuantMind is for research and educational purposes only. It is not investment advice.
