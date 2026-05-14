# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QuantMind is a quantitative trading platform with Python backend (FastAPI) and Electron/React/TypeScript frontend. The OSS edition uses single-container deployment where all backend services run in one container.

## Backend Services (all via `backend/main_oss.py`)

| Service | Port | Responsibility |
|---------|------|----------------|
| api | 8000 | User auth, strategy management, community |
| engine | 8001 | Qlib backtesting, AI strategy generation, model inference |
| trade | 8002 | Order management, positions, risk control |
| stream | 8003 | Real-time quotes, WebSocket push |

## Commands

### Backend
```bash
# Start all services (Docker)
docker-compose up -d

# Run single service locally
SERVICE_MODE=api python backend/main_oss.py

# Tests (run from project root)
python backend/run_tests.py unit        # Unit tests
python backend/run_tests.py integration # Integration tests
python backend/run_tests.py all         # All tests
python backend/run_tests.py trade-long-short  # QMT MVP chain tests

# Lint/format
ruff check backend/
ruff format backend/
```

### Frontend (Electron app in `electron/`)
```bash
npm install              # Install dependencies
npm run dev              # Development (Electron desktop)
npm run dev:web          # Development (Web browser)
npm run typecheck        # Type check
npm run dashboard:build  # Production build
```

## Architecture Notes

- **Feature engineering**: 48-dim features written to `market_data_daily` table by external service
- **Trade service**: Enforces "local-first" order persistence before external submission
- **Redis DB allocation**: 0=general, 1=auth, 2=trade, 3=market, 4=backtest, 5=cache
- **Shared modules**: `backend/shared/` contains cross-service code (DB manager, Redis client, config, logging)
- **Strategy storage**: `backend/shared/strategy_storage.py` is the single entry point for all strategy CRUD operations

## Stock Code Standardization (CRITICAL)

- **Mandatory Format**: Prefix-based (e.g., `SH600036`). **All internal Redis keys, database fields, and API parameters MUST use this format.**
- **Forbidden Format**: Suffix-based (e.g., `600036.SH`). **Do NOT use this format in any new code or configuration.**
- **Normalization Utilities**: 
  - **Backend**: `backend/shared/stock_utils.py` -> `StockCodeUtil.to_prefix(code)`
  - **Frontend**: `electron/src/utils/portfolioUtils.ts` -> `normalizeStockCode(code)`
- **Redis Key Patterns**:
  - Snapshot: `market:snapshot:sh600036` (lowercase prefix for snapshot keys)
  - Series: `market:series:SH600036` (Standard Prefix-based for sequences)
- **Market Auto-Identification**:
  - `SH`: 6xxxxx, 9xxxxx
  - `SZ`: 0xxxxx, 3xxxxx, 2xxxxx
  - `BJ`: 4xxxxx, 8xxxxx

## Environment

Required `.env` keys (defaults in `docker-compose.yml`):
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `REDIS_HOST`, `REDIS_PORT`
- `SECRET_KEY`, `JWT_SECRET_KEY`
- `STORAGE_MODE=local` for OSS edition

## Code Style

- Python: Line length 88, use ruff for linting/formatting
- TypeScript: Run `npm run typecheck` before committing frontend changes

## Deployment Workflow

After making code changes, always:
1. **Commit to git**: Create a commit with descriptive message
2. **Deploy to server**: SSH to `quant-server` and pull/deploy updates

```bash
# Local: commit changes
git add .
git commit -m "descriptive message"

# Deploy to quant-server
ssh quant-server "cd /opt/quantmind && git pull && docker-compose restart"
```

## Key Files

- `backend/main_oss.py` - Unified entry point for all backend services
- `backend/run_tests.py` - Test runner with multiple modes
- `backend/shared/` - Shared modules across services
- `docker-compose.yml` - Local deployment configuration
