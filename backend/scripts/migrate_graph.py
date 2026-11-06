"""Add only graph-owned tables. Default is read-only preflight; --apply performs DDL."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import inspect
from app.core.postgres import get_engine
from app.models.graph import GraphState, GraphJob, GraphChatMeta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--neo4j', action='store_true', help='Also verify Neo4j; initialize constraints only with --apply')
    args = parser.parse_args()
    engine = get_engine()
    tables = [GraphState.__table__, GraphJob.__table__, GraphChatMeta.__table__]
    inspector = inspect(engine)
    for table in tables:
        exists = inspector.has_table(table.name)
        if exists:
            actual = {c['name'] for c in inspector.get_columns(table.name)}
            missing = set(table.columns.keys()) - actual
            if missing:
                raise SystemExit(f'{table.name}: incompatible existing table, missing {sorted(missing)}')
        print(f'{table.name}: {"exists" if exists else "create required"}')
    if args.apply:
        with engine.begin() as connection:
            for table in tables:
                table.create(connection, checkfirst=True)
        print('Graph tables ready; source tables and documents unchanged.')
    if args.neo4j:
        from app.graph.store import get_graph_store
        store = get_graph_store()
        try:
            store.run('RETURN 1 AS ok')
            if args.apply:
                store.initialize()
            print('Neo4j reachable')
        finally:
            store.close()


if __name__ == '__main__':
    main()
