# Contributing

Thank you for helping improve this project.

## Development checks

Run these checks before opening a pull request:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/*.py tests/*.py
bash -n install-palworld.sh upgrade-palworld-manager.sh scripts/*.sh
shellcheck install-palworld.sh upgrade-palworld-manager.sh scripts/*.sh \
  scripts/palworld-control scripts/palworld-rest-firewall scripts/palworld-discord-configure
```

The integration tests use temporary directories and fake service commands. They
exercise backup and restore failure handling without touching the local systemd
instance or any real Palworld data. Release tests create an isolated Git
repository and verify archive contents, reproducibility, and checksums.

To create release artifacts from a clean committed tree:

```bash
scripts/package-release.sh --version 0.4.0 --output-dir dist
(cd dist && sha256sum --check SHA256SUMS)
```

The packager archives the selected Git commit rather than the ambient working
directory. Untracked local files are excluded; tracked or staged changes cause
the command to fail so an artifact cannot silently differ from its source commit.

Never include a real `palworld.env`, Discord token, server password, save file,
backup archive, or host-specific configuration in a commit.

Changes to shutdown, update, backup, restore, firewall, or privilege boundaries
should include tests and document their failure behavior.
