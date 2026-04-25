#!/usr/bin/env python3
"""
Organic Web Checker — standalone scheduler worker.

Deploy as a separate Railway service alongside the web app.
Set START_COMMAND to: python worker.py

This process imports app.py (which initialises the DB schema but does NOT
start the in-process scheduler thread, since INLINE_SCHEDULER is not set).
It then polls for due scheduled_checks every POLL_INTERVAL seconds.
"""
import os
import sys
import time

POLL_INTERVAL = int(os.environ.get('WORKER_POLL_INTERVAL', '60'))

# Importing app initialises DB schema and loads helpers without starting Flask.
# INLINE_SCHEDULER is not set here, so no daemon thread is created.
from app import process_due_scheduled_checks

def main():
    print(f'[WORKER] Scheduler worker started. Poll interval: {POLL_INTERVAL}s')
    iteration = 0
    while True:
        iteration += 1
        try:
            fired = process_due_scheduled_checks(iteration)
            if fired:
                print(f'[WORKER] iteration={iteration} fired={fired}')
        except Exception as exc:
            print(f'[WORKER] unhandled error on iteration {iteration}: {exc}', file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
