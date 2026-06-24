# Config and CLI audit for v0.2.4

This file records the audit requested after v0.2.3.

## Audit result

- All argparse command flags are present in `python3 -m timeshift_btrfs_sync --help` or the matching subcommand help.
- All argparse command flags are described in `README.md` under **Complete CLI command reference**.
- All config options parsed by `timeshift_btrfs_sync/config.py` are present in `config.example.toml`.
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
