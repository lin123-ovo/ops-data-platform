# Operations demo

The demo syncs synthetic work orders and a service directory from a dedicated local PostgreSQL database. Both source tables are read in one transaction. The worker publishes their changes and checkpoints atomically and leaves the last published snapshot available when a sync fails.

## SLA definitions

All metrics use the SLA due date in the Asia/Hong_Kong time zone. The selected start date is inclusive and the end date is exclusive. Queries accept completed date ranges of up to 90 days.

| Metric | Definition |
| --- | --- |
| Tickets due | Tickets with a due date in the selected range |
| SLA breaches | Due tickets that are unresolved or were resolved after the deadline |
| SLA compliance | Tickets resolved by their deadline divided by tickets due. A zero denominator displays no data. |

Tickets with no matching service remain in the overall totals. They appear in a separate group and do not receive a rank. Services with zero tickets due also do not receive a rank.

## Repeat the workflow

Follow the [local setup](../README.md#local-setup), then open the [operations workspace](http://127.0.0.1:5174/ops).

- Sync the initial data and compare the service ranking in the dashboard and the operations mode in Agent Chat. A fresh seed gives the data collection gateway three tickets due, two SLA breaches, and 33.33% compliance.
- From `backend`, run `.venv/bin/python scripts/seed_ops_demo.py --add-breach`. Sync and refresh. The first addition produces four tickets due, three breaches, and 25% compliance for the gateway.
- Click the compliance rate to inspect the new ticket. An older metric card retains its original numbers. Its drill-down requests a refresh if the published data version has advanced.
- To exercise a directory-only update, run `.venv/bin/python scripts/seed_ops_demo.py --update-service network 'Network Access' 'Network On-call'` and sync again. The owner and version update while ticket counts stay the same.

## Upgrade an existing local demo

Stop the existing backend and sync worker. Apply the migration with `.venv/bin/python scripts/migrate_ops.py --apply` from `backend`, restart the backend, run the seed script, and restart the worker. The seed assigns service IDs to the standard sample tickets and retains existing work orders, runbooks, and catalog corrections.
