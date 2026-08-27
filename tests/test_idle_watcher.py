"""Lock-boundary tests for the idle shutdown watcher."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("idle_watcher_under_test", ROOT / "scripts/palworld-idle-watcher.py")
assert SPEC and SPEC.loader
watcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watcher)


class IdleWatcherLockTests(unittest.TestCase):
    def test_final_player_checks_save_and_shutdown_share_one_global_lock(self):
        events: list[str] = []

        class Lock:
            def __enter__(self):
                events.append("lock-enter")
                return self

            def __exit__(self, *_args):
                events.append("lock-exit")

        class API:
            def __init__(self):
                self.player_checks = 0

            def players(self):
                self.player_checks += 1
                events.append(f"players-{self.player_checks}")
                return []

            def save(self):
                events.append("save")

            def shutdown(self, _wait, _message):
                events.append("shutdown")

        state = {"idle_since": 1, "shutdown_requested": False}
        original_sleep = watcher.time.sleep
        watcher.time.sleep = lambda _seconds: events.append("sleep")
        try:
            watcher.attempt_idle_shutdown(API(), state, dry_run=False, shutdown_wait=30, lock_factory=Lock)
        finally:
            watcher.time.sleep = original_sleep
        self.assertEqual(
            events, ["lock-enter", "players-1", "save", "sleep", "players-2", "shutdown", "lock-exit"],
        )
        self.assertTrue(state["shutdown_requested"])

    def test_busy_lock_performs_no_final_check_or_mutation(self):
        class BusyLock:
            def __enter__(self):
                raise watcher.OperationLockBusy("busy")

            def __exit__(self, *_args):
                return None

        class API:
            def players(self):
                self.fail("players must not run")

            def save(self):
                self.fail("save must not run")

            def shutdown(self, *_args):
                self.fail("shutdown must not run")

        watcher.attempt_idle_shutdown(
            API(), {"idle_since": 1, "shutdown_requested": False},
            dry_run=False, shutdown_wait=30, lock_factory=BusyLock,
        )


if __name__ == "__main__":
    unittest.main()
