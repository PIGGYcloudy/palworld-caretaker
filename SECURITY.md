# Security Policy

## Reporting a vulnerability

Do not open a public issue for vulnerabilities that could expose credentials,
allow arbitrary command execution, bypass Discord authorization, or damage save
data. Contact the maintainer privately through the security contact that will be
published with the GitHub repository.

Until that contact is configured, do not publish a suspected vulnerability or
include secrets, tokens, passwords, save files, or server logs in an issue.

## Deployment boundary

- Keep the Palworld REST API bound to localhost and do not expose TCP port 8212.
- Keep the Discord allowlists fail-closed and use dedicated administrator roles;
  `DISCORD_PALWORLD_ADMIN_ROLE_IDS` is required for announcements, moderation,
  backups, diagnostics, stop, and update commands.
- Keep the Web UI on `127.0.0.1:8765`. SaveGames exports save first, reject
  symlinked entries, and enforce `PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES`.
- Review scripts before running them with root privileges.
- Test idle shutdown in dry-run mode before enabling it.
- Keep backups separate from the live server and test restoration periodically.
