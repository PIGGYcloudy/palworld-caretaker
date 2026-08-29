"""Pure-Python host diagnostics coverage with synthetic procfs fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.diagnostics import (  # noqa: E402
    collect_system_metrics, memory_stats_from_meminfo, parse_meminfo,
)


class SystemMetricsTests(unittest.TestCase):
    def test_parse_meminfo_and_memory_calculation(self):
        parsed = parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB\nBad: nope\n")
        self.assertEqual(parsed, {"MemTotal": 1024000, "MemAvailable": 256000})
        self.assertEqual(memory_stats_from_meminfo(parsed), (1024000, 256000, 768000, 75.0))

    def test_missing_available_uses_safe_older_kernel_approximation(self):
        parsed = parse_meminfo(
            "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\n"
            "Cached: 150 kB\nSReclaimable: 50 kB\nShmem: 25 kB\n"
        )
        total, available, used, percent = memory_stats_from_meminfo(parsed)
        self.assertEqual((total, available, used), (1024000, 384000, 640000))
        self.assertEqual(percent, 62.5)
        self.assertEqual(memory_stats_from_meminfo({"MemTotal": 0}), (None, None, None, None))

    @unittest.skipUnless(os.name == "posix", "procfs metrics collection is POSIX-specific")
    def test_collection_reads_procfs_disk_and_palworld_rss_without_shell_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc, saved, process = root / "proc", root / "saved", root / "proc" / "123"
            process.mkdir(parents=True); saved.mkdir()
            (proc / "meminfo").write_text("MemTotal: 2000 kB\nMemAvailable: 500 kB\n", encoding="utf-8")
            (proc / "loadavg").write_text("1.25 0.50 0.25 1/2 123\n", encoding="utf-8")
            (proc / "uptime").write_text("1000.00 100.00\n", encoding="utf-8")
            (process / "comm").write_text("PalServer-Linux-Shipping\n", encoding="utf-8")
            (process / "cmdline").write_bytes(b"/srv/palworld/PalServer-Linux-Shipping\0-port=8211\0")
            (process / "statm").write_text("100 4 0 0 0 0 0\n", encoding="utf-8")
            # starttime is field 22, represented by index 19 after the final ')'.
            (process / "stat").write_text("123 (PalServer) S " + "0 " * 18 + "10000 0\n", encoding="utf-8")

            metrics = collect_system_metrics(saved, proc_root=proc)

            self.assertEqual((metrics.memory_total_bytes, metrics.memory_available_bytes), (2048000, 512000))
            self.assertEqual((metrics.memory_used_bytes, metrics.memory_percent), (1536000, 75.0))
            self.assertEqual(metrics.cpu_load_1m, 1.25)
            self.assertEqual(metrics.process_pid, 123)
            self.assertGreater(metrics.process_rss_bytes or 0, 0)
            self.assertIsNotNone(metrics.process_uptime_seconds)
            self.assertGreater(metrics.disk_total_bytes or 0, 0)

    def test_collection_is_fail_safe_for_missing_or_malformed_procfs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"; proc.mkdir()
            (proc / "meminfo").write_text("MemTotal: invalid\n", encoding="utf-8")
            (proc / "loadavg").write_text("not-a-number\n", encoding="utf-8")
            metrics = collect_system_metrics(root / "does-not-exist", proc_root=proc)
            self.assertIsNone(metrics.memory_total_bytes)
            self.assertIsNone(metrics.memory_percent)
            self.assertIsNone(metrics.disk_total_bytes)
            self.assertIsNone(metrics.process_rss_bytes)

    @unittest.skipUnless(hasattr(os, "getloadavg"), "os.getloadavg is unavailable on this platform")
    def test_oversized_meminfo_and_loadavg_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"; proc.mkdir()
            (proc / "meminfo").write_bytes(b"MemTotal: 1000 kB\n" + b"x" * (64 * 1024))
            (proc / "loadavg").write_bytes(b"1.25 " + b"x" * 1024)
            with patch("palworld_caretaker.diagnostics.os.getloadavg", side_effect=OSError):
                metrics = collect_system_metrics(root, proc_root=proc)
            self.assertIsNone(metrics.memory_total_bytes)
            self.assertIsNone(metrics.cpu_load_1m)

    def test_oversized_process_procfs_files_are_rejected(self):
        cases = (
            ("uptime", b"1000.00 " + b"x" * (64 * 1024), "process_uptime_seconds"),
            ("statm", b"100 4 " + b"x" * (64 * 1024), "process_rss_bytes"),
            ("stat", b"123 (PalServer) S " + b"0 " * 18 + b"10000 " + b"x" * (64 * 1024), "process_uptime_seconds"),
            ("cmdline", b"/srv/palworld/PalServer\0" + b"x" * (64 * 1024), "process_pid"),
        )
        for filename, contents, rejected_field in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                proc, saved, process = root / "proc", root / "saved", root / "proc" / "123"
                process.mkdir(parents=True); saved.mkdir()
                (proc / "uptime").write_text("1000.00 100.00\n", encoding="utf-8")
                (process / "cmdline").write_bytes(b"/srv/palworld/PalServer\0")
                (process / "statm").write_text("100 4\n", encoding="utf-8")
                (process / "stat").write_text("123 (PalServer) S " + "0 " * 18 + "10000 0\n", encoding="utf-8")
                target = proc / filename if filename == "uptime" else process / filename
                target.write_bytes(contents)

                metrics = collect_system_metrics(saved, proc_root=proc)

                self.assertIsNone(getattr(metrics, rejected_field))

    def test_corrupt_meminfo_lines_and_missing_fields_are_ignored(self):
        parsed = parse_meminfo(
            "NoColon\nMemTotal:\nMemAvailable: -1 kB\nMemFree: 25 MiB\n"
            "MemTotal: 200 kB extra fields\nMemAvailable: 50 kB\n"
        )
        self.assertEqual(parsed, {"MemTotal": 204800, "MemAvailable": 51200})
        self.assertEqual(memory_stats_from_meminfo({"MemAvailable": 51200}), (None, None, None, None))

    @unittest.skipUnless(os.name == "posix", "procfs metrics collection is POSIX-specific")
    def test_process_match_requires_an_exact_cmdline_binary_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc, saved = root / "proc", root / "saved"
            proc.mkdir(); saved.mkdir()
            decoy = proc / "100"; decoy.mkdir()
            (decoy / "comm").write_text("PalServer\n", encoding="utf-8")
            (decoy / "cmdline").write_bytes(b"/usr/bin/not-palserver\0--name=PalServer\0")

            metrics = collect_system_metrics(saved, proc_root=proc)

            self.assertIsNone(metrics.process_pid)
            self.assertIsNone(metrics.process_rss_bytes)

            game = proc / "101"; game.mkdir()
            (game / "cmdline").write_bytes(b"/srv/palworld/PalServer-Linux-Test\0-port=8211\0")
            (game / "statm").write_text("10 2\n", encoding="utf-8")
            metrics = collect_system_metrics(saved, proc_root=proc)
            self.assertEqual(metrics.process_pid, 101)
            self.assertGreater(metrics.process_rss_bytes or 0, 0)


if __name__ == "__main__":
    unittest.main()
