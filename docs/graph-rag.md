# Graph RAG setup

The default retrieval mode combines BM25 and Milvus vector search with reciprocal rank fusion. The optional graph mode expands candidates across up to two Neo4j relationship hops. Extracted relationships retain references to document chunks and their source text.

## Enable the graph services

Complete the [base setup](../README.md#local-setup) first. Add a local `NEO4J_PASSWORD` to `deploy/.env` and use the same password in `backend/.env`.

```dotenv
GRAPH_ENABLED=true
NEO4J_URI=bolt://127.0.0.1:27687
NEO4J_USER=neo4j
NEO4J_PASSWORD=REPLACE_WITH_NEO4J_PASSWORD
NEO4J_DATABASE=neo4j
```

Run from the repository root.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.graph.yaml up -d neo4j
cd backend
.venv/bin/python scripts/migrate_graph.py --neo4j
.venv/bin/python scripts/migrate_graph.py --apply --neo4j
```

Restart the backend after changing its configuration. In another terminal, run `scripts/run_graph_worker.sh` from `backend`.

## Use graph retrieval

New documents are queued for indexing. For existing documents, use the graph build action in document management. Open the [graph workspace](http://127.0.0.1:5174/graph) to browse entities, relationship paths, and supporting excerpts.

Select graph-enhanced retrieval in the retrieval console or Agent Chat and specify a knowledge base. If Neo4j is unavailable, the retrieval service falls back to hybrid search and reports the degradation.

Source evidence is checked against the associated document chunk. The source panel lets users review whether an extracted relationship accurately reflects the text.
