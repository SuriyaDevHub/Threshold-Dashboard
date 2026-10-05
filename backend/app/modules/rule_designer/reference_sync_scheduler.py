"""Automatic refresh for path-sourced reference files (see
reference_store.configure_source()/sync_now()). No background-task
infrastructure exists elsewhere in this app, so a bare asyncio loop is
the simplest fit — one shared poller for every path-configured file,
not a timer per file: simplest, and self-healing after a restart (a file
overdue by any amount just syncs on the next tick). Started/stopped from
app/main.py's lifespan.
"""
from __future__ import annotations

import asyncio
import time

from app.modules.rule_designer import reference_store

POLL_SECONDS = 60


def _tick() -> None:
    for f in reference_store.list_files():
        if f.source_mode != "path" or not f.auto_refresh_minutes:
            continue
        due_at = (f.last_synced_at or 0) + f.auto_refresh_minutes * 60
        if time.time() < due_at:
            continue
        try:
            reference_store.sync_now(f.id, "system")
        except ValueError:
            pass  # recorded on the file as last_sync_error — don't crash the loop


async def run_forever() -> None:
    while True:
        _tick()
        await asyncio.sleep(POLL_SECONDS)
