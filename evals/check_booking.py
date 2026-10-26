"""Exercise a generated booking Store using real SQLite, restarts and concurrency.

This is an application-level evaluation harness. It does not inspect source text
or substitute mock persistence. Generated modules must implement the eval API.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path


def evaluate(module_path: Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location('evaluated_booking', module_path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    checks = []
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stores:
        database = Path(directory) / 'booking.sqlite'
        store = module.Store(str(database))
        if callable(getattr(store, 'close', None)):
            stores.callback(store.close)
        first = store.book(1, 101, 'slot-a')
        assert first['status'] == 'booked', first
        booking_id = first['booking_id']
        assert store.book(1, 101, 'slot-a')['booking_id'] == booking_id
        assert len(store.mine(101)) == 1
        checks.append({'name': 'same-update-one-booking', 'passed': True})
        restarted = module.Store(str(database))
        if callable(getattr(restarted, 'close', None)):
            stores.callback(restarted.close)
        assert restarted.book(1, 101, 'slot-a')['booking_id'] == booking_id
        checks.append({'name': 'replay-survives-restart', 'passed': True})
        assert restarted.mine(202) == []
        denied = restarted.cancel(2, 202, booking_id)
        assert denied['status'] == 'forbidden', denied
        assert restarted.book(3, 202, 'slot-a')['status'] == 'conflict'
        checks.append({'name': 'ownership-and-capacity', 'passed': True})
        try:
            spoofed = restarted.book(1, 202, 'slot-a')
        except (PermissionError, ValueError):
            spoofed = {'status': 'forbidden'}
        assert spoofed.get('status') != 'booked' and spoofed.get('booking_id') != booking_id, spoofed
        checks.append({'name': 'reused-delivery-does-not-leak', 'passed': True})
        cancel = restarted.cancel(4, 101, booking_id)
        assert cancel['status'] == 'cancelled', cancel
        assert restarted.cancel(4, 101, booking_id)['status'] == 'cancelled'
        assert restarted.book(5, 202, 'slot-a')['status'] == 'booked'
        checks.append({'name': 'cancellation-releases-capacity', 'passed': True})
        def compete(pair):
            update, actor = pair
            worker = module.Store(str(database))
            try:
                return worker.book(update, actor, 'contended-slot')
            finally:
                if callable(getattr(worker, 'close', None)):
                    worker.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete, ((6, 303), (7, 404))))
        assert sorted(result['status'] for result in outcomes) == ['booked', 'conflict'], outcomes
        checks.append({'name': 'independent-concurrent-writers', 'passed': True})
        for bad_user in (0, -1, True):
            try:
                restarted.book(99, bad_user, 'bad')
            except (ValueError, TypeError):
                continue
            raise AssertionError(f'Invalid user identity accepted: {bad_user!r}')
        checks.append({'name': 'invalid-identity-rejected', 'passed': True})
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', type=Path)
    args = parser.parse_args()
    try:
        checks = evaluate(args.module.resolve())
    except Exception as exc:
        print(json.dumps({'passed': False, 'error_type': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({'passed': True, 'checks': checks, 'live_telegram': False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
