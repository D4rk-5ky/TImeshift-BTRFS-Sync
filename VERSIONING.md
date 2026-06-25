# Versioning

The early experimental artifacts jumped version numbers too quickly. From the
commented/performance build onward, artifact versions are counted by zip number.

Corrected sequence:

```text
9th zip  -> 0.0.9
10th zip -> 0.1.0
11th zip -> 0.1.1
12th zip -> 0.1.2
13th zip -> 0.1.3
14th zip -> 0.1.4
15th zip -> 0.1.5
16th zip -> 0.1.6
17th zip -> 0.1.7
18th zip -> 0.1.8
19th zip -> 0.1.9
20th zip -> 0.2.0
21st zip -> 0.2.1
22nd zip -> 0.2.2
23rd zip -> 0.2.3
24th zip -> 0.2.4
25th zip -> 0.2.5
26th zip -> 0.2.6
27th zip -> 0.2.7
28th zip -> 0.2.8
29th zip -> 0.2.9
30th zip -> 0.2.10
31st zip -> 0.2.11
32nd zip -> 0.2.12
33rd zip -> 0.2.13
34th zip -> 0.2.14
35th zip -> 0.2.15
36th zip -> 0.2.16
37th zip -> 0.2.17
38th zip -> 0.2.18
39th zip -> 0.2.19
40th zip -> 0.2.20
41st zip -> 0.4.1
42nd zip -> 0.4.2
43rd zip -> 0.4.4
44th zip -> 0.4.5
```

This build is version `0.4.5`.

The version line was intentionally bumped to `0.4.0` at user request. Normal patch releases now continue from the 0.4.x line.


## Changelog

### 0.4.5

- README update: kept the short current-behavior style while adding compact explanations for every CLI flag and every `config.example.toml` option.
- No functional code changes.

### 0.4.4

- Updated `config.example.toml` and `init-config` output to the new safe-default baseline supplied by the user.
- Defaults now keep dry-run safety enabled, prune disabled unless explicitly requested, source identity checks enabled, manual snapshot guard enabled, normal on-demand cleanup disabled, and read-only destination property writes disabled.

### 0.4.1

- Version bump from `0.2.20` to `0.4.1`.
- No functional changes from `0.2.20`; this is the same mail-log-attachment build under the new version number.

### 0.2.20

- Added optional mail attachments for split run log files when `log_dir` is enabled.
- Mail can attach `.log`, `.err`, `.mbuffer`, and `.btrfs-out` files if they exist for the run.
- Added `mail.attach_logs` and `mail.max_attachment_bytes` config options.

### 0.2.19

- Reordered the README front section to put project name first, then the AI-assisted warning, disclaimer, data-loss warning, and license.
- Expanded the disclaimer and data-loss warning text.
- Added an explicit MIT license section near the top of the README.

### 0.2.17

- Manual Timeshift snapshot creation no longer passes explicit `--tags O`.
- Timeshift defaults manual creates to on-demand/tag `O`, and some versions reject explicit `O` despite listing it as valid.
- The generated command is now `timeshift --create --scripted --comments <comment>`.

### 0.2.16

- Manual Timeshift snapshot creation now uses readable remote-safe double-quote escaping for the `--comments` value.
- This avoids noisy nested single-quote escapes in terminal output and log files.

### 0.2.14

- Added `manual_snapshot.require_verified_source`, default `true`.
- Automatic manual snapshot creation now runs `timeshift --list` first and verifies the configured source against `state.json` with Btrfs UUID metadata before creating a new Timeshift snapshot.
- If the newest state snapshot is not on the source, the app walks backward through state until it finds a source snapshot that still exists and matches by UUID.
- If no UUID-confirmed source anchor exists, the app refuses to create a manual snapshot instead of risking creation on the wrong mounted OS/source.
- The same guard also applies to the one-off `create-manual` command by default.

### 0.2.13

- Added independent cleanup controls for app-created and normal/user-created on-demand snapshots.
- `manual_snapshot.cleanup_enabled` controls pruning of app-created tag `O` snapshots recognized by marker.
- `retention.cleanup_ondemand` controls pruning of normal/user-created Timeshift tag `O` snapshots.
- Default safety behavior keeps normal/user-created on-demand snapshots unless explicitly allowed.

### 0.2.12

- Added `[manual_snapshot]` config section.
- `sync --run` can create a source Timeshift tag `O` snapshot before sync.
- The created snapshot uses a configurable comment and marker.
- Added marker-based app-created on-demand retention with default count 10.

### 0.2.9

- Stopped trying to set destination compression on read-only received subvolumes.
- Changed `destination.set_compression_after_receive` default to `false`.
- If after-receive compression is explicitly enabled, read-only received subvolumes are detected and skipped safely.
- Added prune-safe high-watermark sync: after pruning old destination snapshots, normal sync uses the newest UUID-confirmed state/source match as a floor and skips older source snapshots instead of re-sending them.
- If the newest state snapshot is not present on the source, the app walks backward in `state.json` until it finds a source snapshot that exists and matches by Btrfs UUID.
- New state entries store both `original_source_uuid` and `send_source_uuid`, so writable Timeshift snapshots sent through read-only cache can be verified correctly later.

### 0.2.6

- Added optional MQTT status notifications using `paho-mqtt`.
- Added optional email status notifications using Python standard library `smtplib` / `email`.
- Added `timeshift_btrfs_sync/mqtt.py` so MQTT logic is isolated in one file.
- Added `timeshift_btrfs_sync/mail.py` so email logic is isolated in one file.
- Added `[mqtt]` config section with optional username/password/password_file.
- Success payloads include config `name`, command, exit code, timestamp, host, app, and version.
- Failure payloads include the same fields plus error text and the latest captured stderr tail.
- Added optional dependency extra: `python3 -m pip install -e '.[mqtt]'`.

### 0.2.5

- Mirrored captured command stderr to the terminal, while suppressing expected probe stderr.
- Added `destination.cleanup_incomplete_receive = true` to recover from interrupted receives.
- Automatically deletes incomplete destination Btrfs subvolumes that are not recorded in state.json, then retries the transfer.
- Added a separator after superseded source cache cleanup before the next send/receive block.
- Suppressed expected `Directory not empty` stderr when trying to delete a cache parent that still contains another cached subvolume.

### 0.2.4

- Audited all CLI flags, config options, README coverage, and `config.example.toml`.
- Expanded `python3 -m timeshift_btrfs_sync --help` and all subcommand help text.
- Added complete CLI and config reference sections to the README.
- Added `state_file` and `lock_file` to `config.example.toml`.
- Added `CONFIG_AND_CLI_AUDIT.md`.

### 0.2.3

- Documentation-only update.
- Added a dedicated README section explaining pruning, `prune_after_sync`, `--prune`, `--run`, and `--yes-delete`.
- Updated `config.example.toml` comments so it is clear that prune settings do not delete without `--yes-delete`.

### 0.2.2

- Split the old combined `.out` transfer log into `.mbuffer` and `.btrfs-out`.
- `.mbuffer` stores mbuffer progress/summary plus the transfer command header.
- `.btrfs-out` stores Btrfs send/receive verbose output plus send/receive command lines.
- `.log` remains for normal command/control output and `.err` remains for stderr/error output.

### 0.2.1

- Fixed mbuffer live progress output after adding Btrfs verbose/logging support.
- Changed the stream reader from `readline()` to chunked `os.read()` so carriage-return progress lines from mbuffer are shown immediately.
- `stream.btrfs_verbose` now controls only Btrfs send/receive verbose passthrough, not whether mbuffer progress is visible.
- No config change needed.

### 0.2.0

- Added optional split logging controlled by top-level `log_dir`.
- Added `timeshift_btrfs_sync/log.py` for all file logging logic.
- Added timestamped per-run `.log`, `.out`, and `.err` files.
- Normal captured command output goes to `.log`.
- Transfer/mbuffer output goes to `.out` so `.log` is not flooded.
- Errors/stderr are copied to `.err`.
- Send/receive command blocks are included in both `.log` and `.out`.

### 0.1.9

- Added optional `stream.btrfs_verbose = true`.
- Adds `-v` to `btrfs send` and `btrfs receive` when enabled.
- Lets Btrfs verbose output pass through live to the terminal during transfers.
- Documents that Btrfs verbose output is operation/detail logging, while `mbuffer` remains the useful throughput/total progress display.

### 0.1.8

- Added source-side cleanup for superseded read-only cache snapshots.
- Keeps the newest cache snapshot per subvolume so future incremental sends still have a valid parent.
- Cleanup uses only `sudo -n btrfs subvolume delete ...` on the source.
- Added `source.cleanup_superseded_cache = true` config option.

### 0.1.7

- Made transfer output more human-readable.
- Added blank lines between status messages, send commands, mbuffer commands, receive commands, and transfer blocks.
- Added visual separators after each send/receive block.
- Allowed mbuffer progress and summary lines to be shown live on the terminal during real transfers.

### 0.1.6

- Documented that `destination.target_root` creates both `snapshots/` and `.ts-btrfs-sync/`.
- Added full reset cleanup notes explaining that both folders must be removed before starting a new full sync.
- Clarified that received snapshots are Btrfs subvolumes and should be deleted with `btrfs subvolume delete`, not plain `rm -rf`.
- No intended code behavior change.

### 0.1.5

- Optimized incremental parent guard to verify once per subvolume name per run by default.
- Reuses saved parent `send_path` from `state.json` when possible.
- Stops reading remote source UUID metadata for every current send; state is updated from local destination `Received UUID` after receive.
- Keeps `source.verify_incremental_parent = true` as the safety default.

### 0.1.4

- Renamed the example source SSH/sudo user to `ts-btrfs-sync-user` everywhere.
- No intended code behavior change.

### 0.1.3

- Fixed read-only detection so `btrfs subvolume show` `Flags: readonly` is honored.
- Improved `ro=true` / `ro=false` parsing from `btrfs property get`.
- Avoided overwriting a known read-only result with an unknown property result.
- Manual Timeshift snapshots that are already read-only should now send directly instead of creating a cache snapshot.

### 0.1.2

- Fixed the empty destination folder guard.
- Empty in-progress snapshot directories no longer count as existing backups.
- The receive directory is created after parent selection, just before `btrfs receive`.

### 0.1.1

- Added incremental parent guard.
- Fast discovery still skips Btrfs metadata checks for all snapshots.
- Real incremental sends now verify only the selected parent by comparing source UUID with destination received_uuid.
- Added config options `source.verify_incremental_parent` and `source.allow_incremental_without_parent_match`.

### 0.1.0

- Added fast discovery mode with `source.verify_subvolumes_at_discovery = false` by default.
- Dry-run/listing no longer need to run Btrfs show/property checks for every snapshot/subvolume unless explicitly enabled.
- Btrfs read-only checks are delayed until a subvolume is actually going to be sent.

### 0.0.9

- Corrected project version number from the over-large experimental `0.4.0`.
- Added more explanatory comments/docstrings around functions, commands, config sections, and performance options.
- Added `VERSIONING.md` explaining the zip count and corrected version sequence.
