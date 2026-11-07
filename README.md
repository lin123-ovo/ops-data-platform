# Ops Data Platform (Graph RAG)

A local workbench for SLA analysis and knowledge-grounded operations support. It synchronizes work orders and a service catalog into PostgreSQL. The dashboard and the agent use the same metric service, and the agent retrieves runbooks to provide cited response steps.

The demo uses synthetic work orders, services, and a runbook. The backend calculates the SLA metrics. The language model summarizes the retrieved procedures.

## Features

- **Automatic agent chat.** Greetings and writing requests can be answered directly. The agent selects knowledge-base search, Alibaba Cloud web search, or both for questions that need evidence. Web citations use `[W1]` and document citations use `[1]`.
- **SLA analytics.** See last week's due and overdue work orders and SLA compliance rate. Filter by service, compare service rankings, identify responsible teams, and drill down into individual work orders. The metric catalog lists formulas, source tables, and the latest successful sync.
- **Incremental synchronization.** Work orders and services have separate sync checkpoints and are published in a single database transaction. A failed sync preserves the last successful version. Conversation history retains metric snapshots, and drill-down links from older versions prompt users to refresh.
- **Knowledge retrieval.** Parse, check, and chunk documents with optional OCR. Default retrieval combines BM25 and Milvus vector search using Reciprocal Rank Fusion (RRF). Optional Graph RAG adds up to two hops of Neo4j traversal, with relationship paths and excerpts from the source documents.
- **Operations Q&A.** Use the agent to see the same SLA metrics and service rankings as the dashboard, alongside response steps cited from the runbook.

## Technology Stack

| Area | Tools |
| --- | --- |
| Frontend | Vue 3, TypeScript, Element Plus |
| Backend | FastAPI, SQLAlchemy, LangGraph |
| Data and retrieval | PostgreSQL, Redis, Milvus, Neo4j, BM25, RRF |
| Models and local services | DashScope chat and embedding APIs, Docker Compose |

## Local Setup

Use Python 3.11, uv, Docker Compose, and Node.js 22.18 or later in the 22.x series, or Node.js 24.12 or later. Runbook ingestion and chat require a DashScope API key.

### 1. Start the data services

Create `deploy/.env` and replace the password placeholders with your own values. The connection strings below assume hexadecimal passwords.

```dotenv
POSTGRES_USER=ops_admin
POSTGRES_PASSWORD=REPLACE_WITH_POSTGRES_HEX
POSTGRES_DB=ops_platform
REDIS_PASSWORD=REPLACE_WITH_REDIS_HEX
```

Run from the project root.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d postgres redis milvus
```

### 2. Configure and start the backend

Create `backend/.env`. Reuse the PostgreSQL and Redis passwords from step 1 and choose a separate password for the read-only demo account.

```dotenv
DASHSCOPE_API_KEY=REPLACE_WITH_DASHSCOPE_API_KEY
POSTGRES_DSN=postgresql+psycopg://ops_admin:REPLACE_WITH_POSTGRES_HEX@127.0.0.1:5432/ops_platform
REDIS_URL=redis://:REPLACE_WITH_REDIS_HEX@127.0.0.1:6379/0
MILVUS_HOST=127.0.0.1
MILVUS_COLLECTION=ops_platform_documents
MILVUS_DIMENSION=1536
OPS_ENABLED=true
OPS_SOURCE_DSN=postgresql://rag_ops_source_reader:REPLACE_WITH_SOURCE_HEX@127.0.0.1:5432/rag_ops_source_demo
```

```sh
cd backend
python3.11 -m venv .venv
uv pip install --python .venv/bin/python -r requirements-local.lock
.venv/bin/python scripts/migrate_ops.py --apply
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 3. Seed the demo and start the sync worker

After the backend starts, open a new terminal at the project root. The seed script creates the demo source database and its read-only account, then ingests the sample runbook.

```sh
cd backend
.venv/bin/python scripts/seed_ops_demo.py
.venv/bin/python scripts/run_ops_worker.py
```

### 4. Start the frontend

In another terminal at the project root, run the following commands.

```sh
cd front/rag_front
npm ci
VITE_OPS_ENABLED=true npm run dev
```

Open the [Dashboard](http://127.0.0.1:5174/dashboard), [Operations Workbench](http://127.0.0.1:5174/ops), or [API documentation](http://127.0.0.1:8000/api/v1/docs).

For Neo4j setup, migrations, and the graph-building worker, see the [Graph RAG setup guide](docs/graph-rag.md).

Use **English** in the top-right toolbar to switch the workspace and new agent replies to English. The browser remembers your language choice. Existing conversations and source excerpts display their original text.

### Alibaba Cloud web search

Web search reuses the existing `DASHSCOPE_API_KEY`. It is enabled by default and uses a separate `qwen-plus` search call. Optional settings in `backend/.env` are shown below. Restart the backend after changing them.

```dotenv
ALIYUN_WEB_SEARCH_ENABLED=true
ALIYUN_WEB_SEARCH_MODEL=qwen-plus
ALIYUN_WEB_SEARCH_TIMEOUT_SECONDS=30
```

The selected model and key must support Alibaba Cloud Model Studio web search. Set `ALIYUN_WEB_SEARCH_ENABLED=false` to disable web search while retaining chat and knowledge-base search. `GET /api/v1/chat/capabilities` reports configuration availability without making a search request.

Each `search_web` call uses `enable_search=true`, forced search, and returned source citations. The application retains up to 20 source links per request and allows at most two distinct searches per turn. Repeated identical searches reuse that turn's result. Topic and time-range inputs guide the query and the provider's freshness filter. Search calls include only search terms and a short system instruction, and may use internal document context in their terms. Operations mode uses internal metrics and runbooks.

Alibaba Cloud returns a search-grounded model summary and source links. The UI labels the summary as model-generated and keeps it separate from any webpage excerpts supplied by the provider. Source numbers are remapped to the application's `[W1]` format, including when separate searches return the same URL. History retains the summary and source links. Missing sources, service failures, authentication errors, and rate limits remain visible.

Try `Hello`, `Explain RAG using my knowledge base`, or `Find today's weather in Hong Kong and cite sources`. Weather questions without a location first ask for a city.

Offline tests mock Alibaba Cloud HTTP responses. For a real connection check, confirm that an enabled `search_web` call returns clickable sources, then refresh to verify history. Search invokes the provider's model and search service and consumes the corresponding account quota. See [Alibaba Cloud web search documentation](https://help.aliyun.com/zh/model-studio/web-search/) for supported models and API settings.

## Walkthrough

1. In the Operations Workbench, trigger a sync and select Last Week. The fresh demo shows **3 due work orders, 2 SLA breaches, and 33.33% compliance** for the Data Collection Gateway.
2. In Agent Chat, select Operations Data and Last Week. Try asking “Which service had the lowest SLA compliance last week, and how should we respond?” Review the ranking, responsible team, and cited runbook excerpts.
3. From `backend`, run `.venv/bin/python scripts/seed_ops_demo.py --add-breach`, then sync and refresh. The first added breach changes the gateway metrics to **4 due work orders, 3 breaches, and 25% compliance**. Select the compliance rate to find the new work order in the underlying records.

See the [operations demo and upgrade guide](docs/ops-demo.md) for more scenarios.

## Retrieval Evaluation

| Evaluation set | Result |
| --- | --- |
| Hybrid retrieval on 60 fixed questions over 156 chunks | Recall@10 **95.83%**, MRR@10 **0.877**, Hit Rate@10 **100%** |
| Hybrid vs. graph-enhanced retrieval on 20 synthetic cross-document, two-hop questions | Top-5 complete evidence hit rate rose from **65%** to **100%** |

The two evaluations use separate corpora and question sets. The figures measure retrieval performance. The SLA workflow was verified locally with synthetic data.

## Development and Testing

- Run `.venv/bin/python -B tests/run_tests.py` from `backend`. With local PostgreSQL running, set `OPS_TEST_POSTGRES=1` to include integration tests for sync transactions and service rankings.
- To verify actual model routing, run `RUN_LIVE_AGENT_ROUTING_TESTS=1 .venv/bin/python -m unittest tests.test_agent_routing_live -v` from `backend`. These opt-in tests use the configured model (billable usage), but do not execute search tools. They cover explicit weather locations, a city supplied in a follow-up, missing locations, greetings, and both model streaming settings. Offline tests skip these cases by default.
- Run `VITE_OPS_ENABLED=true npm run build` from `front/rag_front` to type-check and build the frontend.
