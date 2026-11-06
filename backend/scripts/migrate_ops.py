"""Opt-in migration for ops-owned tables."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import insert

from app.core.postgres import get_engine
from app.ops.models import OpsBase, OpsCheckpoint


# Keep the old version token until the first V2 publish changes the data.
UPGRADES = {
    'ops_checkpoint': {
        'service_revision': ('ALTER TABLE ops_checkpoint ADD COLUMN service_revision bigint NOT NULL DEFAULT 0',),
        'data_version': (
            'ALTER TABLE ops_checkpoint ADD COLUMN data_version bigint NOT NULL DEFAULT 0',
            'UPDATE ops_checkpoint SET data_version = revision WHERE id = 1',
        ),
    },
    'ops_ticket': {'service_id': ('ALTER TABLE ops_ticket ADD COLUMN service_id varchar(64)',)},
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with get_engine().begin() as connection:
        inspector = inspect(connection)
        for table in OpsBase.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                print(f'{table.name}: create required')
                continue
            missing = set(table.columns.keys()) - {column['name'] for column in inspector.get_columns(table.name)}
            upgrades = UPGRADES.get(table.name, {})
            incompatible = missing - upgrades.keys()
            if incompatible:
                raise SystemExit(f'{table.name}: incompatible table, missing {sorted(incompatible)}')
            print(f'{table.name}: ' + (f'add {sorted(missing)}' if missing else 'exists'))
            if args.apply:
                for column in sorted(missing):
                    for statement in upgrades[column]:
                        connection.execute(text(statement))
        if args.apply:
            OpsBase.metadata.create_all(connection)
            connection.execute(insert(OpsCheckpoint).values(id=1, revision=0).on_conflict_do_nothing())
            print('Ops tables ready.')


if __name__ == '__main__':
    main()
