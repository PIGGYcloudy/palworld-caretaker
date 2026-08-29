#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import signal
import time

from palworld_manager import ApiError, PalworldAPI, env_bool, env_int, load_runtime_config, read_state, service_active, service_lifecycle, write_state
from palworld_caretaker.operations import OperationLock, OperationLockBusy

CONFIG = os.environ.get("PALWORLD_CONFIG", "/srv/palworld/config/palworld.env")
STATE = os.environ.get("PALWORLD_STATE", "/var/lib/palworld-manager/idle-state.json")
stopping = False


def stop_handler(*_args):
    global stopping
    stopping = True


def attempt_idle_shutdown(api, state, *, dry_run: bool, shutdown_wait: int, lock_factory=OperationLock) -> None:
    """Recheck and stop while holding the deployment-wide operation lock.

    The initial poll deliberately remains outside the lock.  Once the idle
    threshold is reached, every decision capable of stopping the server--the
    final player checks, save, and shutdown--is one mutually exclusive unit.
    """
    try:
        with lock_factory():
            logging.info("rechecking players before shutdown")
            final_players = api.players()
            if final_players:
                state["idle_since"] = None
                logging.info("shutdown cancelled; players_online=%d", len(final_players))
            elif dry_run:
                logging.warning("dry-run: would save world and request graceful shutdown")
                state["idle_since"] = time.time()
            else:
                logging.info("saving world")
                api.save()
                logging.info("world save succeeded")
                time.sleep(min(5, shutdown_wait))
                final_players = api.players()
                if final_players:
                    state["idle_since"] = None
                    logging.info("shutdown cancelled after save; players_online=%d", len(final_players))
                else:
                    logging.info("requesting graceful shutdown")
                    api.shutdown(shutdown_wait, "Server stopping because it has been empty.")
                    state["shutdown_requested"] = True
                    logging.info("Palworld stopped due to inactivity")
    except OperationLockBusy:
        # A concurrent backup/update/web action owns the service transition;
        # retain the idle timer and retry after the next normal interval.
        logging.info("another Palworld operation is active; idle shutdown deferred")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_runtime_config(CONFIG)
    enabled = env_bool(config, "PALWORLD_IDLE_SHUTDOWN_ENABLED", True)
    dry_run = env_bool(config, "PALWORLD_IDLE_WATCHER_DRY_RUN", False)
    timeout = env_int(config, "PALWORLD_IDLE_TIMEOUT_MINUTES", 10, 1, 1440) * 60
    interval = env_int(config, "PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS", 60, 5, 3600)
    grace = env_int(config, "PALWORLD_STARTUP_GRACE_SECONDS", 600, 0, 86400)
    shutdown_wait = env_int(config, "PALWORLD_SHUTDOWN_WAIT_SECONDS", 30, 1, 300)
    api = PalworldAPI(config)
    manager_user = config.get("PALWORLD_MANAGER_USER", "palworld-manager")
    state = read_state(STATE)
    seen_offline = False

    while not stopping:
        now = time.time()
        if not enabled or not service_active():
            if not seen_offline and enabled:
                logging.info("Palworld server is not running")
            seen_offline = True
            state = {"lifecycle": "", "idle_since": None, "observed_at": now, "shutdown_requested": False}
            write_state(STATE, state)
            time.sleep(interval)
            continue

        lifecycle = service_lifecycle()
        if not lifecycle or lifecycle != state.get("lifecycle"):
            state = {"lifecycle": lifecycle, "idle_since": None, "observed_at": now,
                     "grace_until": now + grace, "shutdown_requested": False, "api_ready": False}
            write_state(STATE, state)
            logging.info("Palworld server detected as running")
        seen_offline = False
        if now < float(state.get("grace_until", now)):
            time.sleep(min(interval, max(1, state["grace_until"] - now)))
            continue
        if state.get("shutdown_requested"):
            time.sleep(interval)
            continue
        try:
            players = api.players()
            if not state.get("api_ready"):
                logging.info("Palworld API ready")
                state["api_ready"] = True
            if players:
                state["idle_since"] = None
                logging.info("players_online=%d, idle timer reset", len(players))
            else:
                if state.get("idle_since") is None:
                    state["idle_since"] = now
                idle = now - float(state["idle_since"])
                logging.info("players_online=0, idle=%d/%d minutes", int(idle // 60), timeout // 60)
                if idle >= timeout:
                    attempt_idle_shutdown(
                        api, state, dry_run=dry_run, shutdown_wait=shutdown_wait,
                        lock_factory=lambda: OperationLock(manager_user=manager_user),
                    )
        except ApiError as exc:
            # Unknown is never equivalent to zero. Requiring a fresh continuous
            # zero-player window after an API fault is intentionally fail-closed.
            state["idle_since"] = None
            logging.warning("API unavailable; idle shutdown suppressed (%s)", exc)
        state["observed_at"] = now
        write_state(STATE, state)
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    raise SystemExit(main())
