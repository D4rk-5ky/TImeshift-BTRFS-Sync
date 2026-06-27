# Commented code map

Compact map of the **current** codebase. It documents active commands,
classes, and functions only. Removed older function/command names are
intentionally not listed here; historical changes belong in `VERSIONING.md`.

The purpose of this file is to explain what each part does and why some code is
intentionally conservative, especially around Btrfs UUID safety, dry-run safety,
retention-based cache handling, state metadata refresh, and pipeline logging.

## App flow

`cli.py` parses a command and loads `config.toml`. Most commands run through the
logging wrapper, which creates split log files and sends optional notifications.
`sync.py` reads source Timeshift snapshots, optionally creates a manual snapshot,
re-reads the source list, refreshes mutable state metadata, proves the source and
destination share a valid Btrfs parent, runs `btrfs send | mbuffer | btrfs
receive`, writes `state.json`, and optionally runs retention pruning.

## Active CLI commands

| Command | What it does | Important safety behavior |
| --- | --- | --- |
| `init-config` | Writes the packaged commented TOML template. | Does not overwrite unless `--force` is used. |
| `test-ssh` | Verifies SSH and source sudo commands. | Tests access before any sync/delete operation. |
| `list-source` | Lists source Timeshift snapshots. | Fast by default; `--verify-btrfs` performs slower UUID/read-only checks. |
| `sync` | Pulls missing Timeshift Btrfs subvolumes. | Defaults can dry-run; real transfer requires run mode; incremental parents must match UUIDs. |
| `prune` | Applies destination retention rules. | Real deletion requires `--run --yes-delete`. |
| `create-manual` | Creates a source Timeshift on-demand snapshot. | Existing destination requires UUID-confirmed source identity first. |
| `show-state` | Prints local `state.json`. | Read-only; can show raw JSON with `--json`. |

## Module purpose

| File | Purpose |
| --- | --- |
| `__main__.py` | Lets `python3 -m timeshift_btrfs_sync` call the CLI. |
| `cli.py` | Command-line parser, command handlers, logging wrapper, notifications. |
| `config.py` | TOML dataclasses and validation. |
| `sync.py` | Main send/receive transaction and Btrfs safety decisions. |
| `btrfs.py` | Btrfs command builders, metadata parser, source send-cache helpers. |
| `timeshift.py` | Remote Timeshift list/create command helpers and parser. |
| `commands.py` | Local subprocess runner and send/receive stream pipeline. |
| `state.py` | `state.json` loading, saving, relative paths, metadata refresh, sync markers. |
| `retention.py` | Retention keep/delete planning, destination pruning, and matching source send-cache pruning. |
| `log.py` | Split run logs: `.log`, `.err`, `.btrfs`, `.mbuffer`, `.succes`. |
| `notify.py` | Shared notification payload/timestamp builder. |
| `mail.py` | Optional SMTP status email with safe attachment filtering. |
| `mqtt.py` | Optional MQTT status JSON publishing. |
| `ssh.py` | SSH command wrapper and password-file environment handling. |
| `lock.py` | Per-config lock file guard. |
| `models.py` | Shared dataclasses and display helpers. |

## Functions and classes

### `models.py`

- `SubvolumeMeta`: metadata returned by `btrfs subvolume show`; stores UUID,
  parent UUID, received UUID, and read-only flag.
- `SnapshotMeta`: parsed Timeshift snapshot with name, created text, tags,
  comment, path, and subvolume metadata list.
- `SnapshotMeta.sort_key()`: sorts timestamp-named snapshots oldest-to-newest;
  falls back safely when a name is not a normal Timeshift timestamp.
- `tags_text()`: shared display helper; formats tags as `O H D W M` or `none`.

### `config.py`

- `ManualSnapshotConfig`: automatic source on-demand snapshot settings.
- `SourceConfig`: source SSH, Timeshift root, subvolume names, cache root, and
  discovery/cache behavior. Cache cleanup is retention-based so sync keeps every
  read-only cache snapshot it creates until prune deletes the matching destination snapshot.
- `DestinationConfig`: destination root, snapshot folder, and receive behavior.
- `StreamConfig`: optional stream helper settings such as `mbuffer`.
- `StreamConfig.command()`: returns the configured stream helper argv or `None`.
- `RetentionConfig`: destination retention counts and pruning options.
- `RetentionConfig.counts_by_tag()`: maps native Timeshift tags `H/D/W/M/B/O` to
  configured keep counts.
- `AppConfig`: full validated config object passed through the app.
- `ConfigError`: raised when TOML is invalid or unsafe.
- `_table()`: validates that a TOML section is a table; avoids silently accepting
  wrong section types.
- `_optional_str()`: reads optional strings while preserving current behavior for
  fields where non-strings are ignored rather than fatal.
- `_positive_int()`: validates positive integer settings.
- `_stripped()`: converts values to stripped strings for legacy-compatible fields.
- `_bool()`: reads booleans without accepting strings like `yes` or `no`.
- `_int()`: reads integer fields with the same explicit type checks as before.
- `_as_str()`: strict string reader for required string values.
- `_as_path()`: strict path reader built on `_as_str()`.
- `_as_bool()`: strict boolean reader used where wrong types must error.
- `_as_int()`: strict integer reader with optional minimum value.
- `_string_list()`: validates list-of-string config fields such as subvolumes.
- `load_config()`: reads TOML, builds all config dataclasses, and performs
  safety validation. Password/password_file pair checks remain explicit because
  that validation protects secrets and should not be hidden in a broad helper.

### `ssh.py`

- `SSHConfig`: immutable SSH connection/auth settings.
- `SSHConfig.target()`: returns the `user@host` or `host` target string.
- `SSHConfig.uses_password_auth()`: reports whether password/sshpass mode is
  configured.
- `SSHConfig._read_password()`: reads password text from either inline config or
  password file.
- `SSHConfig.environment()`: builds environment variables for password auth.
- `SSHConfig.base_command()`: builds the base `ssh`/`sshpass ssh` argv.
- `SSHRunner`: helper that owns an `SSHConfig` and remote command defaults.
- `SSHRunner.__init__()`: stores SSH config for later command building.
- `SSHRunner.command()`: wraps a remote command in the configured SSH command.
- `SSHRunner.run()`: executes a remote command with the shared command runner.
- `SSHRunner.environment()`: exposes SSH password environment variables.
- `SSHRunner.test()`: runs a simple remote command to confirm SSH works.

### `commands.py`

- `CommandError`: exception containing command text, return code, stdout, stderr.
- `CommandError.__init__()`: stores command failure details for CLI summaries and
  notifications.
- `Completed`: minimal successful command result with return code/stdout/stderr.
- `sudo_prefix()`: returns `sudo -n` prefix when a command must run as root
  without prompting.
- `quote_join()`: shell-quotes argv for readable logs.
- `remote_double_quote()`: quotes a remote shell string for nested SSH commands.
- `_merged_env()`: merges optional command environment with the current process.
- `run_local()`: runs a normal local command, logs command/result, and raises
  `CommandError` on failure.
- `_start_pipeline_readers()`: starts tee threads from one stream-routing table.
- `_failed_stderr()`: combines captured stderr-like streams that belong in an
  error message after a failed pipeline.
- `_log_failed_streams()`: copies captured pipeline streams into `.err` only when
  the pipeline actually fails.
- `stream_pipeline()`: runs `ssh btrfs send | optional mbuffer | btrfs receive`.
  It buffers normal Btrfs/mbuffer stderr because successful `btrfs send` writes
  status like `At subvol ...` to stderr. That status goes to `.btrfs`/`.mbuffer`
  during success and is copied to `.err` only if the pipeline fails.

### `btrfs.py`

- `_clean_uuid()`: normalizes Btrfs `-` UUID output to `None`.
- `parse_subvolume_show()`: parses `btrfs subvolume show` into `SubvolumeMeta`.
- `remote_btrfs_cmd()`: builds source-side Btrfs argv with optional sudo.
- `local_btrfs_cmd()`: builds destination-side Btrfs argv with optional sudo.
- `get_subvolume_meta()`: shared local/remote metadata reader; avoids separate
  parser paths that could disagree.
- `_validate_cache_snapshot_name()`: rejects unsafe cache snapshot names.
- `_validate_cache_subvolume_name()`: rejects unsafe cache child names.
- `readonly_cache_parent_path()`: path for one timestamp folder inside cache root.
- `readonly_cache_path()`: path for one cached read-only subvolume.
- `_subvolume_list_paths()`: parses paths from `btrfs subvolume list -o`.
- `_cache_path_suffixes()`: computes allowed relative/absolute match suffixes.
- `_listed_cache_path_matches()`: checks a listed subvolume is the intended cache
  path, not a similarly named Timeshift path elsewhere.
- `remote_list_child_subvolumes()`: lists existing child subvolumes below a source
  cache parent.
- `remote_cache_existing_paths()`: lists `source.cache_root` once and returns requested timestamp cache parent subvolumes that currently exist. It is intentionally not used to prove nested `@`/`@home` children because Btrfs may only list the timestamp parents from that root.
- `remote_cache_existing_child_paths()`: lists one timestamp cache parent and returns nested `@`/`@home` cache children that actually exist. This fixes the earlier false "already gone" cleanup report when the root listing showed only parents.
- `remote_cache_contains()`: tests if a specific cache subvolume exists.
- `remote_cache_is_empty()`: checks whether a cache parent has any children left.
- `cache_child_display_path()`: formats cache child paths for logs.
- `remote_ensure_cache_parent()`: creates the timestamp cache parent if missing.
- `remote_ensure_readonly_send_path()`: returns an existing read-only source path
  or creates a read-only cache snapshot for the current send. It may create the
  current send snapshot, but parent selection elsewhere must not recreate missing
  parent cache snapshots because the UUID would change.
- `path_is_under_cache()`: tells cleanup whether a path belongs to cache root.
- `remote_delete_subvolume()`: deletes a remote Btrfs subvolume.
- `remote_send_cmd()`: builds `btrfs send` argv, including `-p` for incremental.
- `local_receive_cmd()`: builds `btrfs receive` argv for the destination folder.
- `delete_local_subvolume()`: deletes a destination Btrfs subvolume.

### `timeshift.py`

- `timeshift_cmd()`: builds source-side Timeshift argv with optional sudo.
- `normalize_tags()`: keeps only native Timeshift tags `H/D/W/M/B/O`.
- `parse_timeshift_list()`: parses `timeshift --list` into snapshots while
  keeping tags/comment/path mutable.
- `list_remote_snapshots()`: runs Timeshift remotely and parses the result.
- `create_remote_manual_snapshot_cmd()`: builds `timeshift --create --comments`.
- `create_remote_manual_snapshot()`: runs manual creation. It intentionally does
  not pass explicit `--tags O` because Timeshift on-demand snapshots are already
  tag `O`, and some versions reject explicit `--tags O`.

### `state.py`

- `empty_state()`: creates a new state object.
- `_safe_relative_path()`: rejects paths that would escape the target root.
- `destination_path_to_relative()`: stores destination paths relative to
  `destination.target_root` so the whole backup root can be moved safely.
- `resolve_destination_path()`: resolves relative state paths under current
  target root.
- `normalize_destination_paths()`: migrates older absolute destination paths into
  safe relative paths on load.
- `load_state()`: reads JSON state or creates empty state, then normalizes paths.
- `save_state()`: atomically writes pretty JSON state.
- `refresh_snapshot_metadata_from_source()`: updates only mutable Timeshift
  metadata: `tags`, `comment`, `created`, and `path`. It must not touch UUID,
  send path, destination path, parent, or status fields.
- `snapshot_is_synced()`: returns whether all expected subvolumes are marked ok.
- `mark_subvolume_synced()`: records successful receive metadata after a transfer.
- `remove_snapshot_from_state()`: removes a snapshot after successful pruning.
- `refresh_state_metadata_and_report()`: shared sync/prune helper that refreshes
  mutable metadata, reports changed snapshot names, and saves only when allowed.
- `latest_synced_before()`: finds the newest older synced parent candidate, including saved send-cache parents when the original Timeshift snapshot was pruned.

### `sync.py`

- `SyncError`: fatal sync safety/logic error.
- `_local_meta()`: reads destination Btrfs metadata through the shared parser.
- `_remote_meta()`: reads source Btrfs metadata through the shared parser.
- `_human_blank()`: prints a blank line in human-readable summaries.
- `_human_rule()`: prints section dividers for terminal/log summaries.
- `_record_sync_event()`: adds one sync/full/incremental/skipped event to the run
  summary without changing state.
- `_print_sync_summary()`: writes the readable `SYNC SUMMARY` to terminal and
  `.succes`.
- `prepare_destination()`: creates destination directories needed for a real run.
- `list_source_snapshots()`: runs Timeshift source discovery and optionally checks
  Btrfs metadata for every configured subvolume.
- `source_snapshot_index()`: builds a name-to-snapshot dict for the current source
  list stage.
- `confirm_source_identity_before_manual_snapshot()`: shared source identity guard
  for automatic and standalone manual snapshot creation. Empty destinations may
  create a first full seed; non-empty destinations require a UUID-confirmed
  source/destination anchor.
- `_maybe_create_manual_snapshot()`: optionally creates a Timeshift manual
  snapshot, then forces a fresh `timeshift --list` so the new snapshot is handled
  by the normal send loop.
- `_snapshots_in_sync_order()`: sorts source snapshots oldest-to-newest for safe send order.
- `_select_initial_sync_snapshots()`: on a fresh/empty destination, applies the retention planner to the source Timeshift list and selects only snapshots that would be kept, avoiding first-sync transfers that prune would immediately delete.
- `print_snapshot_table()`: displays source snapshots and tags.
- `_dest_subvolume_path()`: destination path for one received subvolume.
- `_target_snapshot_dir()`: destination path for one snapshot folder.
- `_destination_has_existing_snapshots()`: detects non-empty destination; used to
  decide whether a full seed is allowed.
- `_preview_send_path()`: predicts whether a writable snapshot would use cache,
  without creating anything during dry-run previews.
- `_ensure_source_send_path()`: verifies/creates the current read-only send path.
- `_cleanup_incomplete_destination_receive()`: removes partial destination
  receives after failed attempts before retrying.
- `_read_local_destination_parent_metadata()`: reads metadata for a candidate
  destination parent.
- `_match_source_path_to_destination_received_uuid()`: compares a source path UUID
  to destination `received_uuid`; this is the core incremental identity rule.
- `_select_verified_parent_send_path()`: tries saved `send_path` first, then the
  original Timeshift path. It never recreates a missing parent cache snapshot,
  because the recreated snapshot would get a new UUID and no longer match the
  destination `received_uuid`.
- `_state_uuid_values_for_path()`: returns trusted UUID values remembered for a
  state path.
- `_find_confirmed_sync_floor()`: finds a safe high-watermark after pruning by
  confirming source/destination UUID history.
- `_filesystem_parent_candidates()`: finds older candidates present in both source
  and state.
- `_select_parent()`: chooses full seed or verified incremental parent. It allows
  parentless full sends only when the destination was empty at run start, so
  multi-subvolume first seeds can finish after the first subvolume makes the
  destination non-empty; normal existing-destination sends still require UUID
  proof.
- `sync_once()`: complete sync transaction for one config/run, including source
  discovery, optional manual creation, metadata refresh, send/receive, state
  writes, and summaries. It intentionally keeps all created source send-cache snapshots; cache cleanup happens later through retention/prune.

### `retention.py`

- `PrunePlan`: stores retention keep/delete decisions for reporting and execution.
- `PrunePlan.add_keep()`: records a snapshot and reason to keep.
- `PrunePlan.add_delete()`: records a snapshot and reason to delete.
- `_is_app_created_ondemand()`: distinguishes app-created on-demand snapshots
  from normal user-created Timeshift on-demand snapshots when pruning rules need
  that distinction.
- `_delete_reason_for_snapshot()`: explains the first applicable delete reason.
- `_delete_reasons()`: returns all human-readable delete reasons.
- `_source_cache_delete_paths()`: returns cached `send_path` entries for a snapshot
  selected by retention. It only returns app-created paths under `source.cache_root`.
- `_destination_delete_paths()`: returns tracked destination subvolume paths for
  the same prune item so the delete plan shows both sides before execution.
- `source_snapshot_state()`: builds temporary state-like data from the source Timeshift list so fresh/full sync can reuse the exact same retention planner without writing transfer identity fields.
- `initial_sync_keep_names()`: returns the retained source snapshot names for a fresh destination seed. It prevents sending snapshots that would be pruned immediately after the first sync.
- `_cleanup_source_cache_for_pruned_snapshot()`: checks one timestamp send-cache parent, then lists that parent to find nested `@`/`@home` children before deleting them. This exists because listing only `source.cache_root` can show timestamp parents but not nested child subvolumes, which previously made cleanup report children as missing while they still existed.
- `build_prune_plan()`: computes retention keep/delete decisions from state,
  source tags, and config; it does not delete anything.
- `_delete_destination_snapshot_for_prune()`: deletes destination Btrfs
  subvolumes for one snapshot and returns true only when destination paths are
  confirmed gone or already absent. It reports an unavailable destination root
  without aborting so source send-cache cleanup can still be attempted.
- `_delete_prune_item()`: runs the coordinated per-snapshot delete. It attempts
  destination cleanup and source send-cache cleanup before deciding whether the
  `state.json` entry can be removed. This prevents stale state removal when only
  one side was cleaned up.
- `print_prune_plan()`: prints retention summary and delete plan to terminal and
  `.succes`, including destination paths and source send-cache paths for each
  delete candidate.
- `prune()`: prints the plan and only deletes in real mode with explicit
  confirmation. It removes `state.json` entries only after destination paths and
  app source send-cache paths are confirmed gone or already absent, making failed
  cleanup safely retryable.

### `log.py`

- `RunLogger`: owns one run's split log files.
- `RunLogger.__post_init__()`: creates file handles after dataclass construction.
- `RunLogger.close()`: closes all opened log handles.
- `RunLogger.attachment_paths()`: returns log files that exist and are non-empty.
- `RunLogger._write()`: low-level write/flush helper.
- `RunLogger._remember_stderr()`: stores recent stderr lines for failure emails.
- `RunLogger.last_stderr_tail()`: returns the latest stderr tail.
- `RunLogger._line()`: writes one labeled line.
- `RunLogger.info()`: writes normal log lines.
- `RunLogger.mbuffer()`: writes mbuffer progress to `.mbuffer`.
- `RunLogger.btrfs_out()`: writes Btrfs send/receive status to `.btrfs`.
- `RunLogger.success()`: writes readable summaries to `.succes`. The misspelling
  is intentionally kept because the project already exposed this filename.
- `RunLogger.success_text()`: reads `.succes` for notification bodies.
- `RunLogger.err()`: writes real failure text to `.err` and remembers the tail.
- `RunLogger.command()`: logs a command before running it.
- `RunLogger.completed()`: logs command return code and output after success.
- `RunLogger.pipeline_commands()`: logs the send/mbuffer/receive pipeline argv.
- `RunLogger.pipeline_summary()`: logs pipeline return codes.
- `RunLogger.stream_text()`: routes streamed text to `.btrfs`, `.mbuffer`, `.err`,
  and/or terminal.
- `emit_success_summary()`: writes summary text to terminal and `.succes`.
- `TeeTextIO`: file-like object that writes to two text streams.
- `TeeTextIO.__init__()`: stores primary and secondary streams.
- `TeeTextIO.write()`: writes to both streams.
- `TeeTextIO.flush()`: flushes both streams.
- `TeeTextIO.isatty()`: follows the primary stream terminal status.
- `TeeTextIO.fileno()`: exposes the primary file descriptor.
- `TeeTextIO.writable()`: reports writable stream behavior.
- `TeeTextIO.__getattr__()`: delegates unknown attributes to the primary stream.
- `terminal_stdout()`: returns stdout or logger tee for normal output.
- `terminal_stderr()`: returns stderr or logger tee for error/status output.
- `get_logger()`: returns the active run logger, if any.
- `active_logger()`: context manager that installs one active logger.
- `create_run_logger()`: creates one timestamped logger under the configured log
  directory.
- `tee_pipe_to_log()`: background reader used by the pipeline to stream command
  output without deadlocking pipes.

### `notify.py`

- `utc_timestamp()`: returns one UTC ISO timestamp for notification payloads.
- `build_notification_payload()`: builds the shared status dictionary used by
  both MQTT and email so the two notification channels stay consistent.

### `mail.py`

- `MailConfig`: SMTP notification settings.
- `MailConfig.resolved_password()`: returns password from inline config or file.
- `_subject()`: builds success/failure email subject.
- `_body()`: builds fallback plain-text email body.
- `_success_body_from_paths()`: uses `.succes` as readable success body when it is
  available.
- `_filter_attachments()`: includes only existing non-empty log files.
- `_attach_file()`: attaches one log file to an email.
- `send_status()`: sends SMTP notification and optional attachments.

### `mqtt.py`

- `MQTTConfig`: MQTT notification settings.
- `MQTTConfig.resolved_password()`: returns password from inline config or file.
- `publish_status()`: publishes the shared JSON status payload to MQTT.

### `cli.py`

- `new_subparser()`: creates one subcommand parser with the shared raw-text help
  formatter and handler assignment.
- `add_config_arg()`: adds common `--config/-c`.
- `add_run_mode_args()`: adds paired `--dry-run` and `--run` flags.
- `add_yes_delete_arg()`: adds explicit deletion confirmation flag.
- `_failure_exit_code()`: maps known exceptions to stable process exit codes.
- `_stderr_tail_for_exception()`: chooses useful stderr tail text for failure
  notifications.
- `_send_notifications()`: sends MQTT/email status after logged commands.
- `_mail_attachment_paths()`: selects non-empty log files for email attachments.
- `_with_logging()`: shared wrapper for log creation, command execution,
  notification sending, and exit code handling.
- `_resolve_dry_run()`: merges command flags with `default_dry_run` config.
- `cmd_init_config()`: writes the packaged config template.
- `cmd_test_ssh()`: tests SSH and required source sudo commands.
- `_refresh_state_metadata_from_timeshift()`: refreshes mutable state metadata for
  commands that inspect state/source without running a full sync.
- `cmd_list_source()`: displays source Timeshift snapshots.
- `cmd_sync()`: loads config, resolves dry-run mode, and calls `sync_once()`.
- `cmd_prune()`: loads config, refreshes metadata, and runs retention pruning.
- `cmd_create_manual()`: runs the standalone manual snapshot command after the
  same source identity guard used by automatic manual creation.
- `cmd_show_state()`: prints local state summary or raw JSON.
- `build_parser()`: builds the top-level argparse parser and active subcommands.
- `main()`: CLI entrypoint and final exception-to-exit-code handler.

### `lock.py`

- `FileLock`: context manager for one lock file.
- `FileLock.__init__()`: stores the lock path.
- `FileLock.__enter__()`: creates/acquires the lock non-blocking.
- `FileLock.__exit__()`: unlocks and closes the lock file.

## Safety invariants and why they exist

- **Full send only into empty destination.** Prevents mixing two unrelated backup
  chains in the same target root.
- **Incremental parent must match destination `received_uuid`.** Btrfs incremental
  receive needs the source parent UUID to match the destination parent received
  UUID.
- **Missing parent cache snapshots are not recreated.** A recreated cache snapshot
  receives a new UUID, so it cannot be the same parent already received on the
  destination.
- **Current writable snapshots may be cached read-only.** That is safe because the
  new cache snapshot becomes the current send object, not a fake replacement for
  an old parent. Sync keeps these cache snapshots until retention deletes the
  matching destination snapshot so short-lived hourly parents do not erase common
  UUID ground too early.
- **Destination paths in state are relative.** Moving the whole backup root should
  not break state; path escape is still rejected.
- **Timeshift metadata refresh is restricted.** Only `tags`, `comment`, `created`,
  and `path` may refresh from `timeshift --list`; UUID, send path, destination
  path, parent, and status fields are transfer identity and must not change.
- **Pipeline stderr is buffered.** Successful `btrfs send` and `mbuffer` write
  normal status/progress to stderr, so `.err` is written only when the pipeline
  fails.
- **Real pruning requires `--run --yes-delete`.** Dry-run and explicit delete
  confirmation are separate so a config mistake cannot silently delete backups.
- **Manual snapshot creation checks source identity on non-empty destinations.** It
  prevents creating a fresh source snapshot for the wrong mounted OS/source host
  and then appending it to an existing backup chain.
- **`timeshift --create` omits explicit `--tags O`.** Timeshift creates on-demand
  snapshots with tag `O` by default, and some versions reject explicit `--tags O`.
- **Password pair validation stays explicit.** It protects secret handling and
  avoids accidentally accepting conflicting inline/file password settings.
