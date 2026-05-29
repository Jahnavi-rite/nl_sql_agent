# NL SQL Agent

A production-grade AI-powered SQL agent that converts natural language questions into SQL queries.

> **Phase 0** — Foundation setup. This establishes the monorepo structure, backend/frontend communication, Docker services, and developer tooling. AI/SQL features come in later phases.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    NL_SQL_AGENT (Monorepo)               │
│                                                         │
│  ┌─────────────┐     HTTP      ┌──────────────────┐    │
│  │   Frontend   │ ◄──────────► │     Backend      │    │
│  │  Next.js 14  │              │    FastAPI        │    │
│  │  :3000       │              │    :8000          │    │
│  └─────────────┘              └────────┬─────────┘    │
│                                        │               │
│                              ┌─────────┴──────────┐    │
│                              │                    │    │
│                        ┌─────▼─────┐       ┌──────▼──┐ │
│                        │ PostgreSQL │       │  Redis  │ │
│                        │   :5432    │       │  :6379  │ │
│                        └───────────┘       └─────────┘ │
│                                                         │
│                    Docker Compose (dev)                  │
└─────────────────────────────────────────────────────────┘
```

**How it works:**
1. User opens the frontend at `localhost:3000`
2. Frontend calls `GET /health` on the backend at `localhost:8000`
3. Backend returns `{"status": "ok", "version": "0.1.0"}`
4. Frontend displays "Backend status: ok"

---

## Prerequisites

Install these before starting:

| Tool | Version | Purpose |
|------|---------|---------|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | 4.x+ | Runs all services in containers |
| [Python](https://www.python.org/downloads/) | 3.11+ | Backend language |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | latest | Python package manager (fast alternative to pip) |
| [Node.js](https://nodejs.org/) | 20.x+ | Frontend runtime |
| [Make](https://gnuwin32.sourceforge.net/packages/make.htm) | 4.x+ | Command runner (or use PowerShell commands) |

---

## Quick Start

```bash
# 1. Clone and enter the project
cd NL_SQL_AGENT

# 2. Copy environment variables
cp .env.example .env

# 3. Start everything (one command!)
make dev

# That's it! Visit:
#    Frontend → http://localhost:3000
#    Backend  → http://localhost:8000/health
```

### Manual Setup (without Make)

```bash
# Copy env file
cp .env.example .env

# Start Docker services
docker compose -f docker/compose.dev.yml up -d --build

# Wait ~30 seconds for services to initialize, then visit:
#   Frontend: http://localhost:3000
#   Backend:  http://localhost:8000/health
```

---

## Project Structure

```
NL_SQL_AGENT/
├── backend/                    # FastAPI Python backend
│   ├── app/
│   │   ├── api/                # HTTP route handlers
│   │   │   └── health.py       # GET /health endpoint
│   │   ├── core/               # Config, logging, shared utilities
│   │   │   ├── config.py       # Environment variable management
│   │   │   └── logging.py      # Structured logging setup
│   │   ├── agents/             # [Future] AI agent definitions
│   │   ├── models/             # [Future] Database models
│   │   ├── sandbox/            # [Future] SQL execution sandbox
│   │   ├── services/           # [Future] Business logic
│   │   ├── validators/         # [Future] SQL safety checks
│   │   └── main.py             # FastAPI app entry point
│   ├── tests/                  # pytest test suite
│   └── pyproject.toml          # Python dependencies & tool config
│
├── frontend/                   # Next.js TypeScript frontend
│   ├── app/
│   │   ├── layout.tsx          # Root layout (Tailwind, fonts)
│   │   └── page.tsx            # Home page
│   ├── components/
│   │   └── HealthStatus.tsx    # Backend health status display
│   ├── lib/
│   │   └── api.ts              # Backend API client
│   └── package.json            # Node dependencies
│
├── docker/
│   ├── compose.dev.yml         # Docker Compose for development
│   └── seeds/                  # [Future] Database seed data
│
├── docs/                       # [Future] Additional documentation
├── .github/workflows/ci.yml   # GitHub Actions CI pipeline
├── .env.example                # Environment variable template
├── .gitignore                  # Files to ignore in git
├── Makefile                    # Developer commands
├── README.md                   # This file
└── IMPLEMENTATION_GUIDE.md     # Phase roadmap
```

---

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make dev` | Start all services via Docker Compose |
| `make docker-up` | Build and start containers |
| `make docker-down` | Stop containers |
| `make lint` | Run ruff (Python) + ESLint (TypeScript) |
| `make lint-backend` | Lint Python code only |
| `make lint-frontend` | Lint TypeScript code only |
| `make test` | Run all tests |
| `make test-backend` | Run pytest |
| `make test-frontend` | Run frontend tests |
| `make typecheck` | Run mypy + tsc type checking |
| `make clean` | Remove build artifacts |

---

## Environment Variables

See `.env.example` for all available variables. Key ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKEND_PORT` | `8000` | FastAPI server port |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed frontend origins |
| `POSTGRES_USER` | `nlsql` | Database username |
| `POSTGRES_PASSWORD` | `nlsql_dev_password` | Database password |
| `POSTGRES_DB` | `nlsql_agent` | Database name |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend URL for frontend |

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Backend | FastAPI + Python 3.11 | Async, auto-docs, type-safe |
| Frontend | Next.js 14 + TypeScript | Server components, great DX |
| Styling | Tailwind CSS | Utility-first, fast prototyping |
| Database | PostgreSQL 16 | Robust, production-ready |
| Cache | Redis 7 | Fast in-memory store |
| Containers | Docker Compose | Consistent dev environment |
| CI | GitHub Actions | Automated quality checks |
| Linting | ruff (Python), ESLint (TS) | Fast, comprehensive |
| Types | mypy (Python), tsc (TS) | Catch bugs before runtime |

---

## Troubleshooting

**Port already in use?**
```bash
# Find what's using the port
lsof -i :8000   # or :3000, :5432, :6379
# Kill the process or change the port in .env
```

**Docker containers won't start?**
```bash
# Check logs
docker compose -f docker/compose.dev.yml logs
# Reset everything
docker compose -f docker/compose.dev.yml down -v
docker compose -f docker/compose.dev.yml up -d --build
```

**uv not found?**
```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
```
