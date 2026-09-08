from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from unittest.mock import Mock, patch

from palworld_caretaker.errors import ConfigError
from palworld_caretaker.scheduling import schedule_slot, run_windows_schedules
from palworld_caretaker.service import ServiceState
from palworld_caretaker.web import WebDependencies, WebUIError


class SchedulingTests(unittest.TestCase):
    def test_daily_off_and_invalid(self):
        now = datetime(2026, 9, 8, 4, 30)
        self.assertIsNotNone(schedule_slot('daily-04:30', now))
        self.assertEqual(schedule_slot('04:30', now), schedule_slot('daily-04:30', now))
        self.assertIsNone(schedule_slot('off', now))
        self.assertIsNone(schedule_slot('daily-04:31', now))
        for value in ('every-0h', 'every-366d', 'every--1h', 'every-1.5h', 'daily-24:00'):
            with self.assertRaises(ConfigError):
                schedule_slot(value, now)

    def test_intervals_do_not_reset_at_midnight(self):
        start = datetime(2026, 9, 1)
        for schedule, hours in (('every-7h', 7), ('every-48h', 48), ('every-3d', 72)):
            due = [start + timedelta(hours=i) for i in range(220)
                   if schedule_slot(schedule, start + timedelta(hours=i)) is not None]
            self.assertGreater(len(due), 2)
            self.assertTrue(all(b-a == timedelta(hours=hours) for a, b in zip(due, due[1:])))
            self.assertIsNone(schedule_slot(schedule, due[0] + timedelta(minutes=1)))

    def test_windows_worker_deduplicates_update_and_backup(self):
        dependencies = Mock()
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        values = {'UPDATE_TIME': 'daily-04:30', 'BACKUP_TIME': 'daily-04:30'}
        with patch('palworld_caretaker.config.load_config', return_value=SimpleNamespace(values=values)), \
             patch('palworld_caretaker.scheduling.schedule_slot', return_value='today'):
            run_windows_schedules(dependencies, stop)
        dependencies.trigger_maintenance.assert_called_once()
        dependencies.action.assert_not_called()

    def native_dependencies(self, state=ServiceState.ACTIVE, returncode=0):
        config = SimpleNamespace(scripts_root=Path('/scripts'), directory=Path('/config'),
                                 install_root=Path('/install'), server_root=Path('/server'))
        lifecycle = Mock()
        lifecycle.status.return_value.service = state
        dependencies = WebDependencies(config, Mock(), lifecycle, Mock(), Mock(),
            runner=Mock(return_value=SimpleNamespace(returncode=returncode)), operation_lock=nullcontext)
        dependencies._require_idle_maintenance = Mock()
        dependencies._graceful_stop = Mock()
        dependencies._wait_for_inactive = Mock()
        dependencies._start_server = Mock()
        return dependencies

    def test_windows_update_backs_up_before_steamcmd_and_restores_running_state(self):
        deps = self.native_dependencies()
        events = []
        deps.runner.side_effect = lambda *a, **kw: events.append('backup') or SimpleNamespace(returncode=0)
        with patch('palworld_caretaker.steamcmd.SteamCMD') as steam:
            steam.return_value.update.side_effect = lambda *a: events.append('update')
            deps._windows_maintenance(update=True)
        self.assertEqual(events, ['backup', 'update'])
        deps._wait_for_inactive.assert_called_once()
        deps._start_server.assert_called_once()
        self.assertFalse(deps.native_maintenance)

    def test_windows_backup_failure_never_updates_and_recovers(self):
        deps = self.native_dependencies(returncode=1)
        with patch('palworld_caretaker.steamcmd.SteamCMD') as steam:
            with self.assertRaises(WebUIError):
                deps._windows_maintenance(update=True)
            steam.assert_not_called()
        deps._start_server.assert_called_once()
        self.assertFalse(deps.native_maintenance)

    def test_windows_stopped_server_stays_stopped(self):
        deps = self.native_dependencies(ServiceState.INACTIVE)
        deps._windows_maintenance(update=False)
        deps._graceful_stop.assert_not_called()
        deps._start_server.assert_not_called()
