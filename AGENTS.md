# QuantMind Agent Instructions

## Project Overview

QuantMind is a quantitative trading platform with Python backend (FastAPI) and Electron/React/TypeScript frontend.

## Backend

### Services (all run in single container via `backend/main_oss.py`)
- **api** (8000): User auth, strategy management, community
- **engine** (8001): Qlib backtesting, AI strategy generation, model inference
- **trade** (8002): Order management, positions, risk control
- **stream** (8003): Real-time quotes, WebSocket push

### Commands
```bash
# Start all services (Docker)
docker-compose up -d

# Run single service locally
SERVICE_MODE=api python backend/main_oss.py

# Tests (run from project root)
python backend/run_tests.py unit        # Unit tests
python backend/run_tests.py integration # Integration tests
python backend/run_tests.py all         # All tests

# Lint/format
ruff check backend/
ruff format backend/
```

### Key Architecture Notes
- Feature engineering is externalized: 48-dim features written to `market_data_daily` table by external service
- Trade service enforces "local-first" order persistence before external submission
- Redis DB allocation: 0=general, 1=auth, 2=trade, 3=market, 4=backtest, 5=cache

## Frontend (Electron app in `electron/`)

### Commands
```bash
# Install dependencies (from project root)
npm install

# Development (Electron desktop)
npm run dev

# Development (Web browser - for server deployment)
npm run dev:web

# Production preview (Web mode)
npm run dashboard:preview

# Type check
npm run typecheck

# Build
npm run dashboard:build

# Package for distribution (Electron)
npm run dashboard:package:win
```

### Deployment Modes
- **Electron Desktop**: `npm run dev` - Native desktop experience
- **Web Browser**: `npm run dev:web` - Access via http://localhost:3000
- See `docs/WEB_DEPLOYMENT.md` for production deployment guide

### Structure
- `electron/src/features/` - Feature modules (quantbot, strategy-wizard, auth, user-center)
- `electron/src/services/` - API clients and service layer
- `electron/electron/` - Electron main process code

### API Configuration
Default API base: `http://localhost:8000`
Override via `VITE_API_BASE_URL` environment variable.

## Testing

### Backend Tests
```bash
python backend/run_tests.py unit          # Fast unit tests
python backend/run_tests.py integration   # Requires DB/Redis
python backend/run_tests.py trade-long-short  # QMT MVP chain tests
```

### Frontend Tests
```bash
cd electron && npm test           # Vitest unit tests
npm run test:e2e                  # Playwright e2e (from root)
```

## Code Style

### Python
- Line length: 88 (black/ruff default)
- Use ruff for linting and formatting
- Many legacy files have relaxed lint rules (see pyproject.toml per-file-ignores)

### TypeScript
- Run `npm run typecheck` before committing frontend changes
- ESLint config in `electron/.eslintrc.cjs`

## Environment

Required `.env` keys (see `docker-compose.yml` for defaults):
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `REDIS_HOST`, `REDIS_PORT`
- `SECRET_KEY`, `JWT_SECRET_KEY`
- `STORAGE_MODE=local` for OSS edition

## Important Files

- `backend/main_oss.py` - Unified entry point for all backend services
- `backend/run_tests.py` - Test runner with multiple modes
- `docker-compose.yml` - Local deployment configuration
- `docs/系统架构文档.md` - Architecture documentation (Chinese)
