# Commented code map

Compact map of the current codebase. It is kept in sync with the refactored
helpers and avoids old historical notes.

## Flow

`cli.py` loads config and logging, `sync.py` reads `timeshift --list`, optionally
creates a manual snapshot and re-reads the list, refreshes mutable state metadata,
selects verified full/incremental sends, runs the stream pipeline, updates
`state.json`, then optionally prunes by native Timeshift tags.

## Modules

| File | Purpose |
| --- | --- |
| `cli.py` | CLI, logging wrapper, notifications, config-template copy. |
| `config.py` | TOML dataclasses and validation. |
| `sync.py` | Main backup logic and safety decisions. |
| `btrfs.py` | Btrfs commands, metadata parser, cache helpers. |
| `timeshift.py` | Remote Timeshift list/create parser and commands. |
| `commands.py` | Subprocess runner and stream pipeline. |
| `state.py` | `state.json`, relative paths, sync markers. |
| `retention.py` | Keep/delete planning and pruning. |
| `log.py` | `.log`, `.err`, `.btrfs`, `.mbuffer`, `.succes`. |
| `mail.py`, `mqtt.py` | Optional status notifications. |
| `ssh.py`, `lock.py`, `models.py` | SSH wrapper, lock file, shared dataclasses. |

## Functions and classes

### `models.py`

- `SubvolumeMeta`: Btrfs UUID/received UUID/parent UUID/read-only metadata.
- `SnapshotMeta`: Timeshift snapshot name, path, tags, comment, subvolumes.
- `SnapshotMeta.sort_key()`: timestamp/name sort key.

### `config.py`

- `ManualSnapshotConfig`, `SourceConfig`, `DestinationConfig`, `StreamConfig`, `RetentionConfig`, `AppConfig`: typed config sections.
- `StreamConfig.command()`: stream helper argv.
- `RetentionConfig.counts_by_tag()`: retention counts for `H/D/W/M/B/O`.
- `ConfigError`: invalid config error.
- `_table()`, `_optional_str()`, `_positive_int()`, `_stripped()`, `_bool()`, `_int()`, `_as_str()`, `_as_path()`, `_as_bool()`, `_as_int()`, `_string_list()`: TOML value readers/validators.
- `load_config()`: read and validate config.

### `ssh.py`

- `SSHConfig`: SSH settings; `target()`, `uses_password_auth()`, `_read_password()`, `environment()`, `base_command()` build auth/env/argv.
- `SSHRunner`: remote runner; `command()`, `run()`, `environment()`, `test()` build/run/test SSH commands.

### `commands.py`

- `CommandError`: failed command details.
- `Completed`: command result.
- `sudo_prefix()`, `quote_join()`, `remote_double_quote()`, `_merged_env()`: command formatting/env helpers.
- `run_local()`: normal local command runner.
- `_join_text()`: join process buffers.
- `stream_pipeline()`: `ssh btrfs send | mbuffer | btrfs receive`; only failed pipeline stderr goes to `.err`.

### `btrfs.py`

- `_clean_uuid()`: turns Btrfs `-` UUID into `None`.
- `parse_subvolume_show()`: single parser for `btrfs subvolume show`.
- `remote_btrfs_cmd()`, `local_btrfs_cmd()`: Btrfs argv builders.
- `get_subvolume_meta()`: shared local/remote metadata reader.
- `_validate_cache_snapshot_name()`, `_validate_cache_subvolume_name()`: cache path guards.
- `readonly_cache_parent_path()`, `readonly_cache_path()`: cache path builders.
- `_subvolume_list_paths()`, `_cache_path_suffixes()`, `_listed_cache_path_matches()`: parse/match `subvolume list -o` output.
- `remote_list_child_subvolumes()`: list cache children.
- `remote_cache_contains()`, `remote_cache_is_empty()`, `cache_child_display_path()`: cache tests/display.
- `remote_ensure_cache_parent()`: create cache parent when missing.
- `remote_ensure_readonly_send_path()`: use original read-only snapshot or create read-only cache snapshot.
- `path_is_under_cache()`: cache path test.
- `remote_delete_subvolume()`, `remote_try_delete_cache_subvolume()`: source cache deletion.
- `remote_send_cmd()`, `local_receive_cmd()`, `delete_local_subvolume()`: send/receive/delete argv builders.

### `timeshift.py`

- `timeshift_cmd()`: remote Timeshift argv.
- `normalize_tags()`: native Timeshift tags only.
- `parse_timeshift_list()`: parse snapshot names, tags, comments, paths.
- `list_remote_snapshots()`: run and parse `timeshift --list`.
- `create_remote_manual_snapshot_cmd()`: build manual snapshot command.
- `create_remote_manual_snapshot()`: run manual snapshot creation; no explicit `--tags O`.

### `state.py`

- `empty_state()`: new state dict.
- `_safe_relative_path()`, `destination_path_to_relative()`, `resolve_destination_path()`, `normalize_destination_paths()`: relative destination path handling.
- `load_state()`, `save_state()`: JSON I/O.
- `refresh_snapshot_metadata_from_source()`: update only mutable tags/comment/created/path.
- `refresh_state_metadata_and_report()`: shared refresh/report/save helper used by sync and prune paths.
- `snapshot_is_synced()`: check all expected subvolumes are ok.
- `mark_subvolume_synced()`: save successful receive metadata.
- `remove_snapshot_from_state()`: drop pruned snapshot.
- `latest_synced_before()`: newest older synced parent for a subvolume.

### `sync.py`

- `SyncError`: fatal sync error.
- `_local_meta()`, `_remote_meta()`: compact metadata wrappers.
- `_human_blank()`, `_human_rule()`: summary text helpers; tag display uses shared `tags_text()`.
- `_record_sync_event()`, `_print_sync_summary()`: `.succes` sync statistics.
- `prepare_destination()`: prepare destination paths.
- `list_source_snapshots()`: read source list, optionally verify Btrfs metadata.
- `source_snapshot_index()`: one Timeshift snapshot dict for the current stage.
- `confirm_source_identity_before_manual_snapshot()`: shared guard for automatic and standalone manual snapshot creation; empty destination allows first full seed, otherwise UUID anchor is required.
- `_maybe_create_manual_snapshot()`: create manual snapshot and force a new source list.
- `_snapshots_in_sync_order()`, `print_snapshot_table()`: order/display snapshots.
- `_dest_subvolume_path()`, `_target_snapshot_dir()`: destination path builders.
- `_destination_has_existing_snapshots()`: non-empty destination check used by send and manual-snapshot guards.
- `_preview_send_path()`, `_ensure_source_send_path()`: choose/create current read-only send path.
- `_cleanup_superseded_source_cache()`: delete old cache child; delete parent only when empty.
- `_cleanup_incomplete_destination_receive()`: remove partial receives.
- `_read_local_destination_parent_metadata()`: read destination parent metadata.
- `_match_source_path_to_destination_received_uuid()`: shared source UUID vs destination `received_uuid` check.
- `_select_verified_parent_send_path()`: validate saved `send_path`, then original source path; never recreate parent cache.
- `_state_uuid_values_for_path()`: UUIDs trusted from state for a path.
- `_find_confirmed_sync_floor()`: high-watermark after pruning.
- `_filesystem_parent_candidates()`: candidates present in source and state.
- `_select_parent()`: full seed or verified incremental parent.
- `sync_once()`: complete sync transaction.

### `retention.py`

- `PrunePlan`: keep/delete plan; `add_keep()`, `add_delete()` add decisions.
- `_is_app_created_ondemand()`: classify app-created on-demand snapshots; tag display uses shared `tags_text()`.
- `_delete_reason_for_snapshot()`, `_delete_reasons()`: human delete reasons.
- `build_prune_plan()`: compute retention plan.
- `_delete_snapshot()`: delete destination subvolumes for one snapshot.
- `print_prune_plan()`: readable plan output.
- `prune()`: dry-run or apply retention.

### `log.py`

- `RunLogger`: owns log files; methods `attachment_paths()`, `success_text()`, `last_stderr_tail()`, `info()`, `err()`, `btrfs_out()`, `mbuffer()`, `success()`, `command()`, `completed()`, `pipeline_commands()`, `pipeline_summary()`, `stream_text()` write/read run logs.
- `TeeTextIO`: terminal/file tee.
- `emit_success_summary()`, `terminal_stdout()`, `terminal_stderr()`, `get_logger()`, `active_logger()`, `create_run_logger()`, `tee_pipe_to_log()`: global logging helpers.

### `notify.py`

Shared notification helpers. `utc_timestamp()` creates the common UTC timestamp, and `build_notification_payload()` builds the single status dictionary reused by MQTT and email.

### `mail.py` / `mqtt.py`

- `MailConfig`, `MQTTConfig`: notification config and password-file support.
- `utc_timestamp()`, `build_payload()`: shared status payload shape.
- Mail helpers `_subject()`, `_body()`, `_success_body_from_paths()`, `_filter_attachments()`, `_attach_file()`, `send_status()`: SMTP status mail and non-empty attachments.
- `publish_status()`: MQTT JSON publish.

### `cli.py`

- `_failure_exit_code()`, `_stderr_tail_for_exception()`: failure summaries.
- `_publish_mqtt_status()`, `_send_mail_status()`, `_mail_attachment_paths()`: notification bridge.
- `_with_logging()`: command wrapper for logging, notifications, exit code.
- `_resolve_dry_run()`: global/command dry-run merge.
- `cmd_init_config()`, `cmd_test_ssh()`, `cmd_list_source()`, `cmd_sync()`, `cmd_prune()`, `cmd_create_manual()`, `cmd_show_state()`: command handlers.
- `_refresh_state_metadata_from_timeshift()`: state metadata refresh for non-sync views.
- `build_parser()`, `main()`: argparse and entrypoint.

### `lock.py`

- `FileLock`: lock-file context manager; `__enter__()` acquires, `__exit__()` releases.

## Safety invariants

- Full send only into an empty destination.
- Incremental parent must match destination `received_uuid`.
- Required cache parents must not be recreated with new UUIDs.
- Destination paths in state are relative to `destination.target_root`.
- Timeshift tags/comments refresh from `timeshift --list` without resend.
