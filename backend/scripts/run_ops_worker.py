"""Poll for work-order sync jobs in a separate local process."""
import argparse
import logging
import signal
import sys
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.core.postgres import get_session_factory
from app.ops.models import OpsSyncRun
from app.ops.sync import SourceBatch, read_source, sync_once

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Process a queued or due run and exit')
    args = parser.parse_args()
    settings = get_settings()
    if not settings.ops_enabled or settings.ops_source_dsn is None:
        raise SystemExit('请设置 OPS_ENABLED=true 和 OPS_SOURCE_DSN')
    dsn = settings.ops_source_dsn.get_secret_value()
    logging.basicConfig(level=logging.INFO)
    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    factory = get_session_factory()

    def read(revision: int, service_revision: int) -> SourceBatch:
        return read_source(dsn, revision, service_revision)

    while not stop.is_set():
        try:
            run_id = sync_once(factory, read)
            if run_id is not None:
                logger.info('Ops sync run processed: %s', run_id)
                if args.once:
                    with factory() as db:
                        run = db.get(OpsSyncRun, run_id)
                        if run is None or run.status != 'succeeded':
                            raise SystemExit(run.error if run and run.error else '同步未成功完成')
        except SQLAlchemyError as exc:
            logger.error('Ops worker cannot use target database: %s', type(exc).__name__)
            if args.once:
                raise SystemExit(1) from None
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        if args.once:
            return
        stop.wait(2)


if __name__ == '__main__':
    main()
