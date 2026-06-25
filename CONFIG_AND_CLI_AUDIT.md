# Config and CLI audit for v0.4.5

This file records the audit requested after v0.2.3.

## Audit result

- All argparse command flags are present in `python3 -m timeshift_btrfs_sync --help` or the matching subcommand help.
- All argparse command flags are described in `README.md` under **Complete CLI command reference**.
- All config options parsed by `timeshift_btrfs_sync/config.py`, including `[mqtt]` and `[manual_snapshot]`, are present in `config.example.toml`.
- All config options are described in `README.md` under **Complete config option reference**.
- `init-config` now writes the same complete commented example as `config.example.toml`.

## CLI commands and flags checked

### Global

- `--help`
- `--version`

### `init-config`

- `--path PATH`
- `--force`

### `test-ssh`

- `--config CONFIG`, `-c CONFIG`

### `list-source`

- `--config CONFIG`, `-c CONFIG`
- `--verify-btrfs`

### `sync`

- `--config CONFIG`, `-c CONFIG`
- `--dry-run`
- `--run`
- `--limit LIMIT`
- `--snapshot SNAPSHOT`
- `--resend`
- `--prune`
- `--yes-delete`

### `prune`

- `--config CONFIG`, `-c CONFIG`
- `--dry-run`
- `--run`
- `--yes-delete`

### `create-manual`

- `--config CONFIG`, `-c CONFIG`
- `--comment COMMENT`

### `show-state`

- `--config CONFIG`, `-c CONFIG`
- `--json`

## Config options checked

### Top-level

- `name`
- `default_dry_run`
- `prune_after_sync`
- `log_dir`
- `state_file`
- `lock_file`

### `[mqtt]`

- `enabled`
- `host`
- `port`
- `topic`
- `username`
- `password`
- `password_file`
- `client_id`
- `qos`
- `retain`
- `timeout`
- `notify_on_success`
- `notify_on_failure`

### `[manual_snapshot]`

- `enabled`
- `cleanup_enabled`
- `require_verified_source`
- `comment`
- `marker`
- `retention_count`

### `[ssh]`

- `host`
- `user`
- `port`
- `identity_file`
- `password`
- `password_file`
- `compression`
- `cipher`
- `extra_args`

### `[source]`

- `sudo`
- `btrfs_command`
- `timeshift_command`
- `snapshot_root`
- `subvolumes`
- `cache_root`
- `create_readonly_cache`
- `cleanup_superseded_cache`
- `verify_subvolumes_at_discovery`
- `verify_incremental_parent`
- `verify_incremental_parent_once_per_run`
- `allow_incremental_without_parent_match`
- `send_compressed_data`
- `send_proto`

### `[destination]`

- `target_root`
- `sudo`
- `btrfs_command`
- `create_target_root`
- `cleanup_incomplete_receive`
- `compression`
- `set_compression_before_receive`
- `set_compression_after_receive`

### `[stream]`

- `use_mbuffer`
- `mbuffer_command`
- `mbuffer_size`
- `mbuffer_rate`
- `mbuffer_extra_args`
- `btrfs_verbose`

### `[retention]`

- `hourly`
- `daily`
- `weekly`
- `monthly`
- `boot`
- `ondemand`
- `cleanup_ondemand`
- `yearly`
- `keep_latest`
- `keep_latest_common_parent`
- `protected_snapshots`

## Help commands used during audit

```bash
python3 -m timeshift_btrfs_sync --help
python3 -m timeshift_btrfs_sync init-config --help
python3 -m timeshift_btrfs_sync test-ssh --help
python3 -m timeshift_btrfs_sync list-source --help
python3 -m timeshift_btrfs_sync sync --help
python3 -m timeshift_btrfs_sync prune --help
python3 -m timeshift_btrfs_sync create-manual --help
python3 -m timeshift_btrfs_sync show-state --help
```


## 0.2.5 audit addition

- Added and documented `destination.cleanup_incomplete_receive`.
- Confirmed `config.example.toml` parses with the new option.
- `init-config` now writes the updated full config example.


## 0.2.6 audit addition

- Added and documented every `[mqtt]` config option.
- Confirmed `config.example.toml` includes the full `[mqtt]` section.
- Confirmed `init-config` writes the same full commented config including `[mqtt]`.
- Confirmed MQTT support is optional: paho-mqtt is imported only when publishing is enabled.


## 0.2.8 audit addition

- Confirmed no new config or CLI flags were added for high-watermark sync; it is automatic normal sync behavior.
- Confirmed `destination.set_compression_after_receive` remains documented and present in `config.example.toml`, but now defaults to `false`.
- Confirmed `config.example.toml` parses with the new default.
- Confirmed `init-config` writes the same full commented config example.


## 0.2.14 audit addition

- Added and documented `manual_snapshot.require_verified_source`.
- Confirmed `config.example.toml` includes the new source verification guard.
- Confirmed `init-config` writes the same full commented config including the verification guard.
- Confirmed no new CLI flags were needed; verified automatic manual snapshot creation is controlled from config.
- Confirmed the existing `create-manual` command also respects `manual_snapshot.require_verified_source` by default.


## 0.2.13 audit addition

- Added and documented `manual_snapshot.cleanup_enabled`.
- Added and documented `retention.cleanup_ondemand`.
- Confirmed `config.example.toml` includes both independent on-demand cleanup controls.
- Confirmed `init-config` writes the same full commented config including the new cleanup controls.
- Confirmed no new CLI flags were needed; on-demand creation/cleanup is controlled from config plus existing prune/--yes-delete safety.


## 0.2.16 audit addition

- Confirmed no new config options were added.
- Confirmed no new CLI flags were added.
- Confirmed manual snapshot command building now uses readable remote-safe double-quote escaping for `timeshift --create --comments`.


## 0.2.16 audit addition

No new CLI flags or config options were added. The audit remains valid.

Runtime behavior changed for failed manual Timeshift snapshot creation:
`timeshift_btrfs_sync.commands.CommandError` now includes both stdout and stderr
when available, and `timeshift_btrfs_sync.timeshift.create_remote_manual_snapshot`
asks the command runner to mirror stdout when the create command fails.


## 0.4.1 version bump audit

- Bumped package version from `0.2.20` to `0.4.1`.
- Updated `pyproject.toml`, `timeshift_btrfs_sync/__init__.py`, README version text, config header, generated config text, MQTT JSON examples, and VERSIONING.md.
- No CLI flags or config options changed.


## 0.2.19 audit note

Docs-only README front matter update. No CLI flags or config options changed.

## 0.2.17 audit note

Manual snapshot create commands intentionally omit explicit `--tags O`; Timeshift defaults creates to on-demand/tag O and some versions reject explicit O.


## 0.2.19 mail notification audit

- Added `[mail]` config section to `config.example.toml` and `README.md`.
- Added `timeshift_btrfs_sync/mail.py`.
- Added `MailConfig` parsing and validation in `config.py`.
- Added success/failure mail notification calls in `cli.py`.
- Verified mail disabled does not require any third-party Python dependency.

## 0.2.20 mail attachment audit

- Added `[mail].attach_logs` to `config.example.toml` and README.
- Added `[mail].max_attachment_bytes` to `config.example.toml` and README.
- Added `MailConfig.attach_logs` and `MailConfig.max_attachment_bytes`.
- Added mail attachment support in `timeshift_btrfs_sync/mail.py` using Python standard library `email.message` attachments.
- Added `RunLogger.attachment_paths()` so the mail layer can attach `.log`, `.err`, `.mbuffer`, and `.btrfs-out` files if they exist.
- Updated `cli.py` so success/failure mail notifications receive current run log paths when `log_dir` is enabled.
- Confirmed no new CLI flags were needed; attachment behavior is controlled in config.

## 0.4.2 safe config defaults audit

- Replaced `config.example.toml` with the user-provided safe-default baseline.
- Updated `init-config` so it writes the same config.
- Confirmed the example parses as valid TOML.


## 0.4.4 simplified docs audit

- Replaced `README.md` with the simplified version supplied by the user.
- Replaced `VERSIONING.md` with the version-history-focused version supplied by the user.
- Kept detailed config and CLI audit information in this file instead of expanding README.md again.
- Confirmed no CLI flags or config options changed.
- Confirmed `config.example.toml` still parses and `init-config` still writes the same config.


## 0.4.4 short README audit

- README was shortened to focus on current behavior, how features work, and why they are needed.
- Detailed version history remains in VERSIONING.md.
- config.example.toml remains the complete commented config reference.


## 0.4.5 README reference audit

- README keeps the shorter current-behavior style and avoids changelog/history clutter.
- README includes a compact command reference for every argparse flag.
- README includes a compact config reference for every option present in `config.example.toml`.
- No CLI flags or config options changed.
