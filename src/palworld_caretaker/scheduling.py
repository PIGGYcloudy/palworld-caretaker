"""Shared local-clock schedule gates for all deployment types."""
from datetime import datetime
from .settings import normalize_backup_schedule


def schedule_slot(schedule: str, now: datetime | None = None) -> str | None:
    schedule = normalize_backup_schedule(schedule)
    now = now or datetime.now()
    if schedule == "off":
        return None
    if schedule.startswith("daily-"):
        return now.strftime("%Y-%m-%d") if now.strftime("%H:%M") == schedule[6:] else None
    amount = int(schedule[6:-1])
    # Calendar hours anchored at 1970-01-01 local time: intervals over 24 hours
    # do not reset at midnight. Days run at local midnight.
    hours = (now.date().toordinal() - datetime(1970, 1, 1).toordinal()) * 24 + now.hour
    interval = amount * (24 if schedule.endswith("d") else 1)
    return str(hours // interval) if now.minute == 0 and hours % interval == 0 else None


def run_windows_schedules(dependencies, stop):
    """The native panel owns the worker; closing its browser does not stop it."""
    from .config import load_config
    attempted = {}
    while not stop.wait(15):
        try:
            values = load_config(dependencies.config.directory).values
            for key, action in (("UPDATE_TIME", dependencies.trigger_maintenance),
                                ("BACKUP_TIME", lambda: dependencies.action("backup"))):
                schedule = values.get(key, "off")
                if key == "BACKUP_TIME" and values.get("PALWORLD_BACKUP_SCHEDULE_ENABLED") == "false":
                    continue
                slot = schedule_slot(schedule)
                identity = (schedule, slot)
                if slot is None or attempted.get(key) == identity:
                    continue
                attempted[key] = identity
                try:
                    action()
                    dependencies.record_audit("scheduled_" + key.lower(), "completed")
                    if key == "UPDATE_TIME":
                        backup = values.get("BACKUP_TIME", "off")
                        attempted["BACKUP_TIME"] = (backup, schedule_slot(backup))
                except (OSError, RuntimeError, ValueError):
                    dependencies.record_audit("scheduled_" + key.lower(), "failed")
        except (OSError, RuntimeError, ValueError):
            dependencies.record_audit("scheduler", "failed")


if __name__ == "__main__":
    import sys
    from .config import load_config
    values = load_config(sys.argv[1]).values
    if schedule_slot(values.get("UPDATE_TIME", "off")) is not None:
        print("update")
    elif values.get("PALWORLD_BACKUP_SCHEDULE_ENABLED") != "false" and schedule_slot(values.get("BACKUP_TIME", "off")) is not None:
        print("backup")
    else:
        print("off")
