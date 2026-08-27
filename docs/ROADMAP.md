# Roadmap

## 0.1 — Safe Linux release

- Remove host-specific assumptions from installation and backup paths.
- Make installation idempotent on supported Linux distributions.
- Support a configurable local backup destination without requiring a NAS.
- Add install, upgrade, diagnostic, and uninstall documentation.
- Expand tests around backup, restore, maintenance, and authorization failures.

## 0.2 — Operations experience

- Add a schema-backed configuration model.
- Add backup integrity checks and optional off-host destinations.
- Improve Discord-first start, status, update, and idle-shutdown workflows.
- Add structured diagnostics and audit logs without telemetry.

## Later

- Add a localhost-only configuration panel.
- Add a Docker backend as an optional deployment target.
- Evaluate native Windows support after the Linux and Docker paths stabilize.
