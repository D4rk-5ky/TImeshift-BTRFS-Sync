# Commented code map

Compact map of the **current** codebase for v0.1.13. It documents active
commands, classes, functions, and the shell commands they build/run. Historical
changes belong in `VERSIONING.md`.

The purpose of this file is to explain what each part does and why the code is
conservative around Btrfs UUID safety, local/SSH source transport, dry-run
safety, retention-based cache handling, state metadata refresh, and pipeline
logging.

## App flow

`cli.py` parses a command and loads `config.toml`. Source-side operations are
routed through `source.py`:

- `source.mode = "ssh"` wraps source shell commands in the existing SSH runner.
- `source.mode = "local"` runs the same source shell commands locally with
  `sh -c`, skipping SSH setup/tests while keeping the same source sudo and
  command settings.

Most commands run through the logging wrapper, which creates split log files and
sends optional notifications. `sync.py` verifies required source/destination
roots with `preflight.py`, reads source Timeshift snapshots, optionally creates a
manual snapshot, keeps already pending app-created manual snapshots in
oldest-to-newest order after interrupted runs, re-reads the source list,
refreshes mutable state metadata, proves the source and destination share a
valid Btrfs parent, runs `<source btrfs send> | optional mbuffer | <local btrfs
receive>`, writes `state.json`, and optionally runs retention pruning.

## Active CLI commands

| Command | What it does | Important safety behavior |
| --- | --- | --- |
| `init-config` | Writes the packaged commented TOML template. | Does not overwrite unless `--force` is used. |
| `test-source` | Verifies the configured source endpoint and source sudo commands. | In SSH mode it tests SSH first; in local mode SSH is skipped. |
| `test-ssh` | Backward-compatible alias for `test-source`. | Kept so old scripts do not break; local mode still skips SSH. |
| `list-source` | Lists source Timeshift snapshots. | Fast by default; `--verify-btrfs` performs slower UUID/read-only checks. |
| `sync` | Pulls/copies missing Timeshift Btrfs subvolumes. | Defaults can dry-run; real transfer requires run mode; incremental parents must match UUIDs. |
| `prune` | Applies destination retention rules. | Real deletion requires `--run --yes-delete`. |
| `create-manual` | Creates a source Timeshift on-demand snapshot. | Runs path preflight first; existing destination also requires UUID-confirmed source identity. |
| `show-state` | Prints local `state.json`. | Read-only; can show raw JSON with `--json`. |
| `destroy-leftovers` | Destroys configured source send-cache/destination leftovers when retiring the app setup. | Dry-run by default; real deletion requires explicit target flag, `--run`, long danger flag, and two typed confirmations. It never deletes `source.snapshot_root`. |

## Module purpose

| File | Purpose |
| --- | --- |
| `__main__.py` | Lets `python3 -m timeshift_btrfs_sync` call the CLI. |
| `cli.py` | Command-line parser, command handlers, logging wrapper, notifications. |
| `config.py` | TOML dataclasses and validation, including `source.mode`. |
| `source.py` | Source command transport abstraction for SSH or local source mode. |
| `sync.py` | Main send/receive transaction and Btrfs safety decisions. |
| `preflight.py` | Required path availability checks before on-demand creation or send/receive work. |
| `btrfs.py` | Btrfs command builders, metadata parser, source send-cache helpers. |
| `destroy.py` | Destructive retirement cleanup for configured source send-cache root and destination target root. |
| `remote_index.py` | Per-run Btrfs subvolume indexes for source and destination path/UUID lookups. |
| `payload_stats.py` | Normalized source/destination payload statistics used to explain raw subvolume count differences. |
| `timeshift.py` | Source Timeshift list/create command helpers and parser. |
| `commands.py` | Local subprocess runner and send/receive stream pipeline. |
| `state.py` | `state.json` loading, saving, relative paths, metadata refresh, sync markers. |
| `retention.py` | Retention keep/delete planning, destination pruning, and matching source send-cache pruning. |
| `log.py` | Split run logs: `.log`, `.err`, `.btrfs`, `.mbuffer`, `.succes`. |
| `notify.py` | Shared notification payload/timestamp builder. |
| `mail.py` | Optional SMTP status email with safe attachment filtering. |
| `mqtt.py` | Optional MQTT status JSON publishing. |
| `ssh.py` | SSH command wrapper, password-file environment handling, and safe ControlMaster socket validation. |
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

- `ManualSnapshotConfig`: automatic source Timeshift on-demand snapshot settings.
- `SourceConfig`: source mode, Timeshift root, subvolume names, command paths,
  sudo prefix, cache root, and discovery/cache behavior. `mode` is `ssh` by
  default and may be `local` to run source commands on the same machine.
- `DestinationConfig`: destination root, snapshot folder, and receive behavior.
- `StreamConfig`: optional stream helper settings such as `mbuffer`.
- `StreamConfig.command()`: returns the configured stream helper argv or `None`.
- `RetentionConfig`: destination retention counts and pruning options.
- `RetentionConfig.counts_by_tag()`: maps native Timeshift tags `H/D/W/M/B/O` to
  configured keep counts.
- `AppConfig`: full validated config object passed through the app.
- `ConfigError`: raised when TOML is invalid or unsafe.
- `_table()`: validates that a TOML section is a table; avoids silently accepting
  wrong section types. Missing optional sections become empty tables.
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
- `load_config()`: reads TOML, builds dataclasses, validates `source.mode`, and
  validates SSH only when `source.mode = "ssh"`. In local mode, `[ssh]` may be
  omitted and a placeholder SSH config is kept only for backward-compatible
  `config.ssh` access.

### `ssh.py`

- `_is_relative_to()`: path containment helper used to reject shared temporary
  ControlPath locations without broad string matching.
- `validate_control_path_safety()`: verifies that SSH ControlMaster has an
  explicit absolute ControlPath. If the parent directory is missing, it creates
  it with owner-only permissions. Existing parents must be owned by the user
  running the app, private, and not under shared temporary storage.
- `SSHConfig`: immutable SSH connection/auth settings.
- `SSHConfig.target()`: returns the `user@host` or `host` target string.
- `SSHConfig.uses_password_auth()`: reports whether password/sshpass mode is
  configured.
- `SSHConfig._read_password()`: reads password text from either inline config or
  password file.
- `SSHConfig.environment()`: builds environment variables for password auth.
- `SSHConfig.base_command()`: builds the base `ssh`/`sshpass ssh` argv, including
  optional ControlMaster/ControlPersist connection reuse.
- `SSHRunner`: helper that owns an `SSHConfig` and remote command defaults.
- `SSHRunner.__init__()`: stores SSH config for later command building.
- `SSHRunner.command()`: wraps a source shell command in the configured SSH argv.
- `SSHRunner.run()`: executes a source shell command through SSH with the shared
  command runner.
- `SSHRunner.environment()`: exposes SSH password environment variables.
- `SSHRunner.test()`: runs a simple remote command to confirm SSH works.

### `source.py`

- `SourceRunner`: source command endpoint wrapper used by sync, prune, preflight,
  Timeshift helpers, Btrfs helpers, and destroy-leftovers.
- `SourceRunner.from_config()`: creates `mode="ssh"` with `SSHRunner` or
  `mode="local"` without SSH from validated config.
- `SourceRunner.uses_ssh`: true when source commands are executed through SSH.
- `SourceRunner.location`: returns `remote` for SSH or `local` for local mode;
  Btrfs metadata helpers use this label in error messages.
- `SourceRunner.display_location`: human-readable source endpoint label.
- `SourceRunner.command()`: builds argv for one source shell command. SSH mode
  returns `ssh ... <command>`; local mode returns `sh -c <command>`.
- `SourceRunner.run()`: runs one source command and captures stdout/stderr using
  either `SSHRunner.run()` or local `run_local()`.
- `SourceRunner.environment()`: returns SSH password environment variables when
  needed; local mode returns `None`.
- `SourceRunner.test()`: verifies the source endpoint. SSH mode tests SSH;
  local mode verifies that local shell execution works.

### `preflight.py`

- `PathPreflightError`: raised before on-demand creation or send/receive when a
  required configured root is unavailable.
- `PathCheck`: one path availability result for terminal reporting.
- `_btrfs_path_check_script()`: builds a small POSIX shell script that checks
  paths with `btrfs subvolume list -o` instead of generic sudo filesystem
  commands.
- `_parse_path_check_output()`: parses structured path-check sentinel lines.
- `_source_path_checks()`: checks `source.snapshot_root` and configured
  `source.cache_root` in one source command. SSH mode wraps this in SSH; local
  mode runs it locally.
- `_local_target_path_check()`: checks `destination.target_root` locally without
  creating anything.
- `check_required_sync_paths()`: prints the sync path preflight and refuses to
  continue before manual snapshot creation or send/receive when a required root
  is missing or inaccessible.

### `commands.py`

- `CommandError`: exception containing command text, return code, stdout, stderr.
- `CommandError.__init__()`: stores command failure details for CLI summaries and
  notifications.
- `Completed`: minimal successful command result with return code/stdout/stderr.
- `sudo_prefix()`: returns `sudo -n` prefix when a command must run as root
  without prompting.
- `quote_join()`: shell-quotes argv for readable logs.
- `remote_double_quote()`: quotes a source shell string for nested SSH commands.
- `_merged_env()`: merges optional command environment with the current process.
- `run_local()`: runs a normal local command, logs command/result, and raises
  `CommandError` on failure.
- `_start_pipeline_readers()`: starts tee threads from one stream-routing table.
- `_failed_stderr()`: combines captured stderr-like streams that belong in an
  error message after a failed pipeline.
- `_log_failed_streams()`: copies captured pipeline streams into `.err` only when
  the pipeline actually fails.
- `stream_pipeline()`: runs `<source btrfs send> | optional mbuffer | <local
  btrfs receive>`. It buffers normal Btrfs/mbuffer stderr because successful
  `btrfs send` writes status like `At subvol ...` to stderr. That status goes to
  `.btrfs`/`.mbuffer` during success and is copied to `.err` only if the
  pipeline fails.

### `remote_index.py`

- `BtrfsIndex`: short-lived path/UUID lookup table for one Btrfs root.
- `BtrfsIndex.add()`: stores one `SubvolumeMeta` by path, UUID, and received UUID.
- `BtrfsIndex.discard()`: removes one path and its UUID lookup entries after deletion.
- `BtrfsIndex.contains()`: checks if a path is indexed as a Btrfs subvolume.
- `BtrfsIndex.meta()`: returns indexed metadata for a path.
- `BtrfsIndex.child_paths()`: returns indexed descendants deepest-first.
- `BtrfsIndex.is_empty()`: checks whether an indexed path has indexed child subvolumes.
- `BtrfsIndex.remove_tree()`: removes a deleted root and descendants from the index.
- `normalize_path()`: normalizes path strings for stable dictionary keys.
- `is_under()`: confirms path/root containment without broad matching.
- `listed_path_to_absolute()`: converts Btrfs relative list paths back to configured absolute paths.
- `_clean_uuid()`: normalizes Btrfs `-` UUID fields to `None`.
- `parse_subvolume_list()`: parses `btrfs subvolume list -u -q -R` output into metadata.
- `_index_from_list_output()`: helper for constructing an index from list output.
- `build_local_btrfs_index()`: builds a local/destination Btrfs index without SSH.
- `_remote_recursive_index_script()`: builds the single source shell script used
  to recursively list source cache subvolumes.
- `build_source_btrfs_index()`: builds a source cache index using either SSH mode
  or local mode through `SourceRunner`.
- `build_remote_btrfs_index()`: compatibility wrapper for SSH source indexes.
- `refresh_source_path()`: refreshes one source path after creation/deletion-sensitive work.
- `refresh_remote_path()`: compatibility wrapper for refreshing one SSH source path.
- `refresh_local_path()`: refreshes one destination path after receive/delete-sensitive work.

### `payload_stats.py`

- `PayloadTreeStats`: normalized count object for a source cache, direct-send
  state view, or destination tree. It separates raw subvolume totals from real
  `@`/`@home` payload entries.
- `PayloadTreeStats.total_payload`: number of normalized payload entries.
- `PayloadTreeStats.total_cache_payload`: number of source payload entries coming
  from app-owned source cache.
- `PayloadTreeStats.total_direct_payload`: number of source payload entries coming
  from protected direct Timeshift sends.
- `normalize_path()`: normalizes path strings before relative matching.
- `_relative_parts()`: returns path parts below a configured root, or `None` for outside paths.
- `_recount_payload()`: rebuilds per-subvolume counters from the normalized payload set.
- `_add_payload()`: recognizes paths ending in configured subvolume names such as `@` and `@home`.
- `source_send_cache_stats()`: classifies source cache paths into real payload subvolumes and helper/container subvolumes.
- `destination_payload_stats()`: classifies destination paths into received payload subvolumes.
- `direct_send_payload_stats()`: reads state only for reporting and counts protected Timeshift original direct-send entries as source-side payload; it does not make deletion decisions.
- `merge_source_payload_stats()`: combines app-owned source-cache payload with protected direct-send payload before comparison.
- `PayloadMatchStats`: comparison object for normalized source and destination payload sets.
- `PayloadMatchStats.source_only`: source payload entries not present on the destination.
- `PayloadMatchStats.destination_only`: destination payload entries not present on the source side.
- `PayloadMatchStats.ok`: true when normalized source and destination payload sets match.
- `compare_payloads()`: builds a `PayloadMatchStats` object.
- `_format_count_line()`: creates aligned text output lines.
- `render_payload_match()`: renders the `SOURCE / DESTINATION SNAPSHOT MATCH` block.

### `btrfs.py`

- `_clean_uuid()`: normalizes Btrfs `-` UUID output to `None`.
- `parse_subvolume_show()`: parses `btrfs subvolume show` into `SubvolumeMeta`.
- `remote_btrfs_cmd()`: builds source-side Btrfs argv with optional sudo. The name
  is kept for compatibility; local source mode can still reuse the command text.
- `local_btrfs_cmd()`: builds destination-side Btrfs argv with optional sudo.
- `get_subvolume_meta()`: shared metadata reader for a local argv command; avoids
  separate parser paths that could disagree.
- `source_get_subvolume_meta()`: reads source Btrfs metadata through `SourceRunner`.
- `_validate_cache_snapshot_name()`: rejects unsafe cache snapshot names.
- `_validate_cache_subvolume_name()`: rejects unsafe cache child names.
- `readonly_cache_parent_path()`: path for one timestamp folder inside cache root.
- `readonly_cache_path()`: path for one cached read-only subvolume.
- `_subvolume_list_paths()`: parses paths from `btrfs subvolume list -o`.
- `_cache_path_suffixes()`: computes allowed relative/absolute match suffixes.
- `_listed_cache_path_matches()`: checks a listed subvolume is the intended cache
  path, not a similarly named Timeshift path elsewhere.
- `source_list_child_subvolumes()`: lists existing child subvolumes below a source
  cache parent through SSH or local source mode.
- `source_cache_existing_paths()`: lists `source.cache_root` once and returns
  requested timestamp cache parent subvolumes that currently exist.
- `source_cache_existing_child_paths()`: lists one timestamp cache parent and
  returns nested `@`/`@home` cache children that actually exist.
- `source_cache_contains()`: tests if a specific source cache subvolume exists.
- `source_cache_is_empty()`: checks whether a source cache parent has any children left.
- `remote_list_child_subvolumes()`, `remote_cache_existing_paths()`,
  `remote_cache_existing_child_paths()`, `remote_cache_contains()`, and
  `remote_cache_is_empty()`: SSH compatibility wrappers around the source helpers.
- `cache_child_display_path()`: formats cache child paths for logs.
- `source_ensure_cache_parent()`: creates the timestamp cache parent if missing
  and updates the source cache index when one is supplied.
- `source_ensure_readonly_send_path()`: returns the original Timeshift path when
  it is already read-only, otherwise creates/reuses an app-owned read-only cache
  snapshot for the current send.
- `source_delete_subvolume()`: deletes one source Btrfs subvolume through SSH or local source mode.
- `source_send_cmd()`: builds the argv for `btrfs send`, including `-p` for
  incremental sends, wrapped through SSH or local source mode.
- `remote_ensure_cache_parent()`, `remote_ensure_readonly_send_path()`,
  `remote_delete_subvolume()`, and `remote_send_cmd()`: SSH compatibility wrappers.
- `path_is_under_cache()`: tells cleanup whether a path belongs to cache root.
- `local_receive_cmd()`: builds `btrfs receive` argv for the destination folder.
- `delete_local_subvolume()`: deletes a destination Btrfs subvolume.

### `timeshift.py`

- `timeshift_cmd()`: builds source-side Timeshift argv with optional sudo.
- `normalize_tags()`: keeps only native Timeshift tags `H/D/W/M/B/O`.
- `parse_timeshift_list()`: parses `timeshift --list` into snapshots while
  keeping tags/comment/path mutable.
- `list_source_snapshots()`: runs Timeshift through `SourceRunner`, parses the
  result, and optionally reads Btrfs metadata for configured subvolumes.
- `list_remote_snapshots()`: compatibility wrapper for SSH source listing.
- `create_remote_manual_snapshot_cmd()`: builds `timeshift --create --comments`.
- `create_source_manual_snapshot()`: runs manual creation through `SourceRunner`.
  It intentionally does not pass explicit `--tags O` because Timeshift on-demand
  snapshots are already tag `O`, and some versions reject explicit `--tags O`.
- `create_remote_manual_snapshot()`: compatibility wrapper for SSH manual creation.

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
- `mark_subvolume_synced()`: records successful receive metadata after a transfer,
  including whether the exact `send_path` is app-owned source cache or a
  protected read-only Timeshift original.
- `send_path_kind_for_state_subvolume()`: returns the stored/fallback ownership kind.
- `state_send_path_is_app_cache()`: true only for app-owned send-cache paths that prune may delete.
- `state_send_path_is_protected_timeshift_original()`: true for direct read-only Timeshift original send paths that prune must never delete.
- `remove_snapshot_from_state()`: removes a snapshot after successful pruning.
- `refresh_state_metadata_and_report()`: shared sync/prune helper that refreshes
  mutable metadata, reports changed snapshot names, and saves only when allowed.
- `latest_synced_before()`: finds the newest older synced parent candidate,
  including saved send-cache parents when the original Timeshift snapshot was pruned.

### `sync.py`

- `SyncError`: fatal sync safety/logic error.
- `_local_meta()`: reads destination Btrfs metadata through the shared parser.
- `_source_meta()`: reads source Btrfs metadata through `SourceRunner`.
- `_human_blank()`: prints a blank line in human-readable summaries.
- `_human_rule()`: prints section dividers for terminal/log summaries.
- `_record_sync_event()`: adds one sync/full/incremental/skipped event to the run
  summary without changing state.
- `_print_sync_summary()`: writes the readable `SYNC SUMMARY` to terminal and `.succes`.
- `prepare_destination()`: creates destination directories needed for a real run.
- `list_source_snapshots()`: runs Timeshift source discovery and optionally checks
  Btrfs metadata for every configured subvolume.
- `source_snapshot_index()`: builds a name-to-snapshot dict for the current source list stage.
- `confirm_source_identity_before_manual_snapshot()`: shared source identity guard
  for automatic and standalone manual snapshot creation. Empty destinations may
  create a first full seed; non-empty destinations require a UUID-confirmed anchor.
- `_is_app_manual_snapshot()`: identifies source Timeshift tag `O` snapshots whose
  comment contains `manual_snapshot.marker`.
- `_pending_app_manual_snapshots()`: finds existing app-created on-demand snapshots
  that are not fully synced yet, so retry runs keep them in normal order.
- `_maybe_create_manual_snapshot()`: optionally creates a Timeshift manual
  snapshot and still preserves older pending app-created snapshots in the send queue.
- `_snapshots_in_sync_order()`: sorts source snapshots oldest-to-newest.
- `_select_initial_sync_snapshots()`: on a fresh destination, applies the retention
  planner and selects only snapshots that would be kept.
- `print_snapshot_table()`: displays source snapshots and tags.
- `_dest_subvolume_path()`: destination path for one received subvolume.
- `_target_snapshot_dir()`: destination path for one snapshot folder.
- `_destination_has_existing_snapshots()`: detects non-empty destination; used to decide whether a full seed is allowed.
- `_snapshot_destination_paths_exist()`: verifies expected destination paths before skipping a state-complete snapshot.
- `_preview_send_path()`: predicts direct read-only send versus cache use during dry-run previews.
- `_send_path_kind_text()`: explains whether the selected send path is protected Timeshift original or app-owned cache.
- `_ensure_source_send_path()`: verifies/creates the current read-only send path
  through `SourceRunner`.
- `_cleanup_incomplete_destination_receive()`: removes only the current partial
  destination receive before retry and invalidates the destination index entry.
- `_read_local_destination_parent_metadata()`: reads metadata for a candidate destination parent.
- `_match_source_path_to_destination_received_uuid()`: compares source path UUID to destination `received_uuid`; this is the core incremental identity rule.
- `_select_verified_parent_send_path()`: tries saved `send_path` first, then the original Timeshift path. It never recreates a missing parent cache snapshot.
- `_state_uuid_values_for_path()`: returns trusted UUID values remembered for a state path.
- `_find_confirmed_sync_floor()`: finds a safe high-watermark after pruning by confirming source/destination UUID history.
- `_filesystem_parent_candidates()`: finds older candidates present in both source and state.
- `_select_parent()`: chooses full seed or verified incremental parent. Full sends
  are allowed only for empty-destination seeding rules.
- `sync_once()`: complete sync transaction for one config/run. It creates the
  `SourceRunner`, skips SSH tests in local mode, runs preflight, discovers source
  snapshots, optionally creates manual snapshots, sends/receives data, writes
  state, and optionally prunes.

### `retention.py`

- `PrunePlan`: stores retention keep/delete decisions for reporting and execution.
- `PrunePlan.add_keep()`: records a snapshot and reason to keep.
- `PrunePlan.add_delete()`: records a snapshot and reason to delete.
- `_is_app_created_ondemand()`: distinguishes app-created on-demand snapshots from normal user-created Timeshift on-demand snapshots.
- `_delete_reason_for_snapshot()`: explains the first applicable delete reason.
- `_delete_reasons()`: returns all human-readable delete reasons.
- `_source_cache_delete_paths()`: returns cached `send_path` entries for a snapshot selected by retention. It only returns app-owned paths under `source.cache_root`.
- `_protected_timeshift_send_paths()`: returns direct Timeshift original send paths so prune plans/execution can show that they are protected.
- `_destination_delete_paths()`: returns tracked destination subvolume paths for the same prune item.
- `source_snapshot_state()`: builds temporary state-like data from the source Timeshift list so fresh/full sync can reuse the retention planner.
- `initial_sync_keep_names()`: returns retained source snapshot names for a fresh destination seed.
- `_cleanup_source_cache_for_pruned_snapshot()`: checks one timestamp send-cache
  parent, lists nested `@`/`@home` children, and deletes only app-owned cache
  subvolumes through `SourceRunner`.
- `build_prune_plan()`: computes retention keep/delete decisions from state, source tags, and config; it does not delete anything.
- `_delete_destination_snapshot_for_prune()`: deletes destination Btrfs subvolumes for one snapshot and returns true only when destination paths are confirmed gone or already absent.
- `_delete_prune_item()`: runs coordinated per-snapshot destination cleanup and source send-cache cleanup before removing state.
- `print_prune_plan()`: prints retention summary and delete plan to terminal and `.succes`.
- `prune()`: prints the plan and only deletes in real mode with explicit confirmation. It creates a `SourceRunner` for source-cache cleanup.

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
- `RunLogger.success()`: writes readable summaries to `.succes`. The misspelling is intentionally kept because the project already exposed this filename.
- `RunLogger.success_text()`: reads `.succes` for notification bodies.
- `RunLogger.err()`: writes real failure text to `.err` and remembers the tail.
- `RunLogger.command()`: logs a command before running it.
- `RunLogger.completed()`: logs command return code and output after success.
- `RunLogger.pipeline_commands()`: logs the send/mbuffer/receive pipeline argv.
- `RunLogger.pipeline_summary()`: logs pipeline return codes.
- `RunLogger.stream_text()`: routes streamed text to `.btrfs`, `.mbuffer`, `.err`, and/or terminal.
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
- `create_run_logger()`: creates one timestamped logger under the configured log directory.
- `tee_pipe_to_log()`: background reader used by the pipeline to stream command output without deadlocking pipes.

### `notify.py`

- `utc_timestamp()`: returns one UTC ISO timestamp for notification payloads.
- `build_notification_payload()`: builds the shared status dictionary used by both MQTT and email so the two notification channels stay consistent.

### `mail.py`

- `MailConfig`: SMTP notification settings.
- `MailConfig.resolved_password()`: returns password from inline config or file.
- `_subject()`: builds success/failure email subject.
- `_body()`: builds fallback plain-text email body.
- `_success_body_from_paths()`: uses `.succes` as readable success body when it is available.
- `_filter_attachments()`: includes only existing non-empty log files.
- `_attach_file()`: attaches one log file to an email.
- `send_status()`: sends SMTP notification and optional attachments.

### `mqtt.py`

- `MQTTConfig`: MQTT notification settings.
- `MQTTConfig.resolved_password()`: returns password from inline config or file.
- `publish_status()`: publishes the shared JSON status payload to MQTT.

### `cli.py`

- `new_subparser()`: creates one subcommand parser with the shared raw-text help formatter and handler assignment.
- `add_config_arg()`: adds common `--config/-c`.
- `add_run_mode_args()`: adds paired `--dry-run` and `--run` flags.
- `add_yes_delete_arg()`: adds explicit deletion confirmation flag.
- `_failure_exit_code()`: maps known exceptions to stable process exit codes.
- `_stderr_tail_for_exception()`: chooses useful stderr tail text for failure notifications.
- `_send_notifications()`: sends MQTT/email status after logged commands.
- `_mail_attachment_paths()`: selects non-empty log files for email attachments.
- `_with_logging()`: shared wrapper for log creation, command execution, notification sending, and exit code handling.
- `_resolve_dry_run()`: merges command flags with `default_dry_run` config.
- `cmd_init_config()`: writes the packaged config template.
- `cmd_test_ssh()`: tests the configured source endpoint and required source sudo commands. It is used by both `test-source` and the backward-compatible `test-ssh` alias.
- `_refresh_state_metadata_from_timeshift()`: refreshes mutable state metadata for commands that inspect state/source without running a full sync.
- `cmd_list_source()`: displays source Timeshift snapshots.
- `cmd_sync()`: loads config, resolves dry-run mode, and calls `sync_once()`.
- `cmd_prune()`: loads config, refreshes metadata, and runs retention pruning.
- `cmd_create_manual()`: runs the standalone manual snapshot command after the same source identity guard used by automatic manual creation.
- `cmd_destroy_leftovers()`: loads config and runs the destructive retirement cleanup command.
- `cmd_show_state()`: prints local state summary or raw JSON.
- `build_parser()`: builds the top-level argparse parser and active subcommands.
- `main()`: CLI entrypoint and final exception-to-exit-code handler.

### `destroy.py`

- `DestroyResult`: summary object for one destructive cleanup root.
- `DestroyResult.success`: true when a target has no cleanup errors.
- `_safe_cleanup_path()`: refuses relative paths, `/`, and broad system roots before any destructive delete.
- `_listed_path_to_absolute()`: converts Btrfs `subvolume list` relative paths back to absolute paths below the configured root.
- `_is_under()`: verifies a candidate path stays inside the selected cleanup root.
- `_sort_deepest_first()`: orders subvolumes deepest-first so child subvolumes are deleted before parents.
- `_collect_recursive_subvolumes()`: walks Btrfs child subvolumes one level at a time so nested cache children are found before deleting the timestamp parent.
- `_run_quiet()`: runs cleanup probes/deletes without duplicating expected stderr noise.
- `_run_source_quiet()`: runs quiet source-side cleanup commands through `SourceRunner`.
- `_path_exists_status()`: separates missing paths from probe failures so reruns can be idempotent.
- `_local_exists()`: checks local destination path existence using configured sudo.
- `_source_exists()`: checks source path existence using configured source mode and source sudo.
- `_local_subvolume_meta()`: detects whether a local cleanup root itself is a Btrfs subvolume.
- `_source_subvolume_meta()`: detects whether a source cleanup root itself is a Btrfs subvolume.
- `_local_child_subvolumes()`: lists local child Btrfs subvolumes below a cleanup root.
- `_source_child_subvolumes()`: lists source child Btrfs subvolumes below a cleanup root.
- `_local_remove_empty_child_dirs()`: removes empty ordinary directories left by deleted local child subvolumes before parent deletion.
- `_local_remove_stale_path()`: removes an ordinary local directory that remains at a path after the subvolume at that path was deleted.
- `_confirm_or_raise()`: requires exact typed confirmation instead of yes/no.
- `_delete_local_tree()`: recursively discovers and deletes local child subvolumes deepest-first, then removes stale ordinary directories/files.
- `_source_delete_subvolumes_batched()`: deletes many source-cache subvolumes in one source command during `destroy-leftovers`.
- `_delete_source_tree()`: recursively discovers and deletes source child subvolumes deepest-first, then removes stale ordinary directories when normal permissions allow it.
- `_mode_text()`: returns the exact typed phrase for the chosen destructive mode.
- `_print_target()`: prints one configured cleanup root before any deletion.
- `_print_result()`: prints one target result with subvolume count and errors.
- `_result_by_label()`: finds the source or destination destroy result used for normalized payload reporting.
- `_load_payload_state()`: loads state.json only for reporting protected direct-send payloads; destroy-leftovers still ignores state for delete decisions.
- `_print_payload_match_if_available()`: prints the normalized source/destination payload match block when both source cache and destination target were selected.
- `destroy_leftovers()`: main retirement cleanup entry point. It ignores retention/state by design and attempts source/destination targets independently so one failing side does not prevent the other side from being cleaned.

### `lock.py`

- `FileLock`: context manager for one lock file.
- `FileLock.__init__()`: stores the lock path.
- `FileLock.__enter__()`: creates/acquires the lock non-blocking.
- `FileLock.__exit__()`: unlocks and closes the lock file.

## Safety invariants and why they exist

- **Source transport is only a wrapper choice.** Local mode and SSH mode share the
  same Btrfs, Timeshift, state, preflight, pruning, and destroy-leftovers logic.
- **Full send only into empty destination.** Prevents mixing two unrelated backup
  chains in the same target root.
- **Incremental parent must match destination `received_uuid`.** Btrfs incremental
  receive needs the source parent UUID to match the destination parent received UUID.
- **Missing parent cache snapshots are not recreated.** A recreated cache snapshot
  receives a new UUID, so it cannot be the same parent already received on the destination.
- **Current writable snapshots may be cached read-only.** That is safe because the
  new cache snapshot becomes the current send object, not a fake replacement for an old parent.
- **Destination paths in state are relative.** Moving the whole backup root should
  not break state; path escape is still rejected.
- **Timeshift metadata refresh is restricted.** Only `tags`, `comment`, `created`,
  and `path` may refresh from `timeshift --list`; UUID, send path, destination
  path, parent, and status fields are transfer identity and must not change.
- **Pipeline stderr is buffered.** Successful `btrfs send` and `mbuffer` write
  normal status/progress to stderr, so `.err` is written only when the pipeline fails.
- **Real pruning requires `--run --yes-delete`.** Dry-run and explicit delete
  confirmation are separate so a config mistake cannot silently delete backups.
- **Destroy-leftovers is deliberately separate from prune.** It ignores state and
  retention only for retiring the setup, refuses broad paths, and requires `--run`,
  a long danger flag, and two typed confirmations.
- **Manual snapshot creation checks source identity on non-empty destinations.** It
  prevents creating a fresh source snapshot for the wrong mounted OS/source endpoint
  and then appending it to an existing backup chain.
- **`timeshift --create` omits explicit `--tags O`.** Timeshift creates on-demand
  snapshots with tag `O` by default, and some versions reject explicit `--tags O`.
- **Password pair validation stays explicit.** It protects secret handling and
  avoids accidentally accepting conflicting inline/file password settings.
