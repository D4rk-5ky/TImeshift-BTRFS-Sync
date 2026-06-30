# timeshift-btrfs-sync

`timeshift-btrfs-sync` is a destination-side backup tool for Timeshift Btrfs snapshots. It can pull snapshots from another machine over SSH, or it can copy from local Timeshift snapshots on the same machine. Both modes use the same Btrfs send/receive, state, preflight, retention, pruning, and safety logic; only the source command transport changes.

> ⚠️ AI-assisted / vibe-coded experimental software. Use at your own risk.

## Disclaimer

This project is AI-assisted / vibe-coded software created as a hobby project. It has not been professionally audited and may contain bugs, unsafe behavior, data-loss issues, security problems, or incorrect assumptions.

You are responsible for reviewing the code, testing it in a safe environment, making backups, and understanding what it does before using it on real data. The author is not responsible for damage, data loss, broken systems, security issues, or other problems caused by using this software.

## Data Loss Warning

This application can perform destructive operations, including deleting Btrfs subvolumes, snapshots, and backup data. Always test with dry-runs first, check the generated plans, and keep a separate working backup.

## License

MIT License. See [`LICENSE`](LICENSE).

## What it does

`source.mode = "ssh"` is the SSH destination-pull mode. `source.mode = "local"` skips SSH and treats the local machine as both source-command endpoint and destination receiver, while still using separate `source.*` and `destination.*` command/sudo settings.

`timeshift-btrfs-sync` is a destination-pull backup tool for Timeshift Btrfs snapshots. It runs on the backup/destination machine, connects to the source over SSH by default, or uses local source mode on the same machine, and transfers Timeshift snapshots with `btrfs send` / `btrfs receive`.

It supports full and incremental sends, Timeshift snapshot discovery, writable source snapshots through a read-only send cache, safe destination pruning, optional automatic Timeshift on-demand snapshots, split logs, MQTT notifications, and email notifications with optional log attachments.

The complete commented config template is packaged at `timeshift_btrfs_sync/data/config.example.toml` and can be copied with `ts-btrfs init-config`.

## Packaged project layout

The release zip keeps package data as real directories. The config template lives only at:

```text
timeshift_btrfs_sync/data/config.example.toml
```

There should not be a root-level `config.example.toml` in the release zip. The `data` path must be a directory, not a file, because `init-config` reads the template as package data.

## Safety model

The safe defaults are intentionally conservative:

- `default_dry_run = true` previews changes unless `--run` is passed. In strict dry-run mode the app does not prepare the destination, create lock/state directories, run `btrfs receive`, or delete/prune snapshots.
- Destination pruning only deletes when `--run --yes-delete` is used.
- Incremental parents are verified with Btrfs UUID metadata before use.
- Automatic source-side manual snapshot creation can require a UUID-confirmed source identity first.
- Normal/user-created Timeshift on-demand snapshots are not pruned unless explicitly enabled.
- The app does not manage destination Btrfs compression; mount the receiving Btrfs filesystem/subvolume with compression enabled if you want compressed destination storage.

The source machine only needs passwordless sudo for Btrfs and Timeshift:

```sudoers
ts-btrfs-sync-user ALL=(root) NOPASSWD: /usr/bin/btrfs *
ts-btrfs-sync-user ALL=(root) NOPASSWD: /usr/bin/timeshift *
```

This is needed because Timeshift listing/creation, Btrfs send, Btrfs metadata checks, read-only cache creation, and source send-cache cleanup require elevated source access.

## Source sudoers and source cleanup

`destroy-leftovers --delete-source` keeps the source sudoers model narrow. On the source host it uses passwordless `sudo btrfs` only for Btrfs metadata and subvolume deletion. It does not require passwordless `find`, `test`, `rm`, `mkdir`, or `cat`.

When Btrfs leaves an empty ordinary directory behind after deleting a source cache subvolume, the app tries to remove that stale directory with normal non-sudo `rmdir`. If the source user does not have filesystem permission to remove that ordinary directory, the cleanup is reported as incomplete instead of asking for broad sudo access.


> ⚠️ AI-assisted / vibe-coded experimental software. Use at your own risk.

## Destination layout

The destination `target_root` is the backup job folder. The app creates and owns:

```text
<target_root>/snapshots/       received Btrfs snapshots
<target_root>/.ts-btrfs-sync/  state.json, lock file, logs
```

The destination `target_root` must be a Btrfs subvolume. If it is missing and `destination.create_target_root = true`, the app creates it with `btrfs subvolume create`. The lock-file parent is prepared before the rest of the sync/prune checks so a real job can acquire the lock early. Helper folders such as `snapshots/`, `.ts-btrfs-sync/`, the lock-file parent, and optional `log_dir` may be ordinary directories or Btrfs subvolumes. When a helper folder is missing during a real run, the app tries `btrfs subvolume create` first because the app works on Btrfs storage, then falls back to normal `mkdir` if Btrfs creation is not possible at that location. After creation, the helper path must still be writable by the app user because lock, state, logs, and per-snapshot receive folders are created before or around sudo Btrfs operations.

`state.json` records successfully received snapshots and the metadata needed for incremental sends. Do not delete only `state.json` while keeping `snapshots/`, and do not delete only `snapshots/` while keeping old state.

State destination paths are stored relative to `destination.target_root`, for example `snapshots/2026-06-23_07-10-24/@`. This means you can move the whole target root to another mount point, update `destination.target_root`, and the app will resolve existing state paths under the new target root. Absolute state paths are normalized when the state is loaded.

During `sync` and before standalone `prune`, mutable Timeshift metadata for already-synced snapshots is refreshed from the latest `timeshift --list`. This updates snapshot-level `tags`, `comment`, `created`, and `path` in `state.json` without re-sending data and without changing Btrfs UUID, parent-chain, send-path, destination-path, or status fields. This lets retention follow Timeshift when it later promotes or changes flags such as `O`, `H`, `D`, `W`, or `M`. The metadata refresh uses the fast Timeshift list path and does not run `btrfs subvolume show` for every already-synced snapshot.

A full reset means deleting both `snapshots/` and `.ts-btrfs-sync/`. Received `@` and `@home` entries are Btrfs subvolumes, so delete them with `btrfs subvolume delete` before removing ordinary folders.

## How sync works

Normal sync flow:

```text
1. In real-run mode, run lock path preflight before checking other sync paths. This prepares the lock-file parent first so the app can acquire the lock early. If the lock path chain includes destination.target_root, that component is created by the strict Btrfs subvolume rule.
2. Acquire the lock file.
3. Run sync path preflight for source.snapshot_root, source.cache_root, and destination.target_root. The Timeshift-owned snapshot_root must already exist and may be an ordinary directory on Btrfs. Missing source.cache_root and destination.target_root are created only by their own rules.
4. Prepare destination helper folders such as snapshots/, state_file.parent, lock_file.parent, and log_dir. Existing directories and Btrfs subvolumes are both accepted. Missing helpers are created with Btrfs subvolume creation first, then mkdir fallback if Btrfs creation is not possible.
5. Run `sudo -n timeshift --list` on the source endpoint: over SSH in `source.mode = "ssh"`, locally in `source.mode = "local"`.
6. Parse Timeshift snapshot names and tags.
7. Build expected paths from source.snapshot_root and source.subvolumes.
8. Skip snapshots already received or older than the confirmed sync floor.
9. Use full send only when the destination has no snapshots yet.
10. Use incremental send when a UUID-confirmed parent is available.
11. Error out if the destination already has snapshots but no matching parent can be proven.
12. Receive into <target_root>/snapshots/<snapshot>/<subvolume>.
13. Save metadata to state.json after each successful receive.
```

Fast discovery is used by default. It avoids Btrfs metadata checks for every old snapshot and delays those checks until a subvolume is actually going to be sent. Use `list-source --verify-btrfs` or `source.verify_subvolumes_at_discovery = true` when you want slower up-front checks.

## Sync path preflight

Before automatic on-demand snapshot creation and before any send/receive work, `sync` verifies that the required configured roots are actually reachable:

```text
source.snapshot_root
source.cache_root, when configured; missing cache roots are created as Btrfs subvolumes in real-run mode when create_readonly_cache = true
destination.target_root
```

The source-side checks are batched into source commands and use the configured source Btrfs command. In SSH mode those source commands are wrapped in SSH; in local mode they run locally. `source.snapshot_root` is Timeshift-owned: it must already exist, it may be an ordinary directory on a Btrfs filesystem, and the app never creates it. This prevents the app from hiding a missing Timeshift mount, wrong OS root, or wrong snapshot path by creating an empty replacement directory. `source.cache_root` is app-owned and is created as a Btrfs subvolume when it is missing and `create_readonly_cache = true`; an existing ordinary directory is refused. If `destination.target_root` is missing and `destination.create_target_root = true`, the app verifies that its parent already exists and is Btrfs-accessible, then creates the exact target root with `btrfs subvolume create <target_root>` and verifies it with `btrfs subvolume show`. If `destination.target_root` already exists, it must also pass `btrfs subvolume show`; an ordinary directory inside Btrfs is refused. This keeps the app-owned backup root explicit and prevents a later receive/prune run from continuing after a misleading preflight. Dry-run mode describes cache/target creation attempts without creating them, but missing `source.snapshot_root` is still an error.

If `source.snapshot_root` is missing, not a directory, or not Btrfs-accessible, the app fails before creating a fresh Timeshift on-demand snapshot and before trying to send data. The same early failure happens if the cache root exists as an ordinary directory instead of a Btrfs subvolume, the cache-root parent is not accessible, or the destination target-root parent cannot be used for Btrfs subvolume creation. This is intended to prevent avoidable leftover on-demand snapshots after a restored VM, changed mount point, wrong Timeshift snapshot path, wrong send-cache path, or broken destination.

`create-manual` also runs the same preflight before asking Timeshift to create a standalone on-demand snapshot.

## Incremental parent guard

Incremental parent verification is mandatory. If the destination has no snapshots at all, the app can start with a normal full sync. If matching snapshots exist, the app uses an incremental send after proving the source parent matches the destination parent. If the destination already contains snapshots but no matching parent can be proven, the app refuses to send and tells the user to use an empty/separate backup directory for a new full sync or repair the existing backup state/cache.

Incremental Btrfs send uses:

```bash
btrfs send -p <parent> <current>
```

The parent must represent the same Btrfs snapshot on both source and destination. Before using a destination snapshot as an incremental parent, the app compares:

```text
source parent UUID == destination parent Received UUID
```

This protects the backup from mixing snapshots from another OS, another source host, or a reset backup chain. Parent paths from previous runs are always checked before use. The app first tries the saved `send_path` from state.json; if that does not exist or does not match, it tries the original Timeshift source snapshot. It never creates a replacement cache snapshot while choosing an existing parent, because a recreated cache snapshot gets a new UUID and cannot match the destination parent.

## Source read-only send cache

`btrfs send` requires read-only source snapshots. If Timeshift snapshots are writable, the app can create read-only source send-cache snapshots under `source.cache_root`.

If a Timeshift snapshot child is already read-only, the app sends directly from that original Timeshift path instead of creating a duplicate source-cache snapshot. The state records this with `send_path_kind = "timeshift-original-readonly"`, and prune treats that path as protected Timeshift-owned data. The app may read and send from `source.snapshot_root`, but it must not delete, rename, move, or change original Timeshift snapshots; cleanup of `source.snapshot_root` remains Timeshift's job only.

The top-level `cache_root` does not have to be created manually. If `cache_root` is missing, real preflight creates it as a Btrfs subvolume before snapshot discovery/send work. The parent directory of `cache_root` must already exist and be Btrfs-accessible. Per-snapshot cache parents and read-only send snapshots are also created with Btrfs commands:

```bash
sudo -n btrfs subvolume create <cache_root>
sudo -n btrfs subvolume create <cache_root>/<snapshot-name>
sudo -n btrfs subvolume snapshot -r <original> <cache_root>/<snapshot>/<subvolume>
```

The app checks cache paths with Btrfs subvolume listings under `source.cache_root` and, for deletion, under each timestamp cache parent. This prevents normal Timeshift snapshot paths with the same date/name from being mistaken for app-created send-cache snapshots.

Every read-only cache snapshot created by `sync` is kept until retention runs. This preserves more possible source/destination UUID common ground when short-lived snapshots, such as hourly snapshots, disappear later. For each pruned snapshot, `prune` attempts both destination deletion and matching source send-cache deletion in one coordinated item. It removes the `state.json` entry only after destination subvolumes and source send-cache are both confirmed gone or already absent. If either side is unavailable, it still attempts the available side and keeps state so the next prune can retry.

Prune only deletes send paths that are explicitly app-owned source-cache paths below `source.cache_root`. If a snapshot was sent directly from a read-only Timeshift original, prune prints it as a protected original send path and never deletes it.

## Remote index/cache optimization

At the beginning of a sync run, the app builds short-lived Btrfs indexes for `source.cache_root` and `destination.target_root`. These indexes store paths, UUIDs, parent UUIDs, received UUIDs, and read-only state where Btrfs reports it. Later parent checks, sync-floor checks, and source send-cache cleanup can use dictionary lookups instead of repeatedly starting new source-side `btrfs subvolume list/show` probes.

The index is deliberately per-run only. It is refreshed or updated after operations that change the filesystem: cache snapshot creation refreshes the new source cache path, a successful receive refreshes the new destination path, and prune removes deleted source cache paths from the index. Safety-critical incremental matching still requires the same identity rule:

```text
source parent UUID == destination parent Received UUID
```

This reduces the overhead from many small SSH calls, especially when the SSH identity file is password-protected with high key-derivation iterations. The actual `btrfs send`/`receive` stream still uses one SSH pipeline per snapshot/subvolume that must be transferred.

On a fresh/full sync into an empty destination, the app first applies the active retention rules to the source Timeshift list and sends only the snapshots that would be kept. For example, if retention keeps the newest 6 hourly, 5 daily, 2 weekly, and 6 monthly snapshots, the first seed starts at the oldest snapshot in that kept set and then sends the kept snapshots in date order. Existing non-empty destinations still use the normal UUID-confirmed parent/floor safety logic.

## Optional automatic on-demand snapshots

When `manual_snapshot.enabled = true`, `sync` can create a source Timeshift on-demand snapshot before normal syncing.

The app first runs `timeshift --list`. If the destination already contains snapshots, it checks the configured source against existing `state.json` history by Btrfs UUID before creating the new source snapshot. If the destination is empty, the run may create a first snapshot and seed the backup with a full send; later snapshots then become incremental.

The create command intentionally omits `--tags O` because Timeshift creates on-demand/tag `O` snapshots by default, and some Timeshift versions reject explicit `--tags O`.

After creating the snapshot, the app re-reads `timeshift --list`. The new snapshot is not sent directly or prioritized. It is sent only when the normal oldest-to-newest snapshot loop reaches its timestamp, using the same full/incremental parent logic as every other snapshot.

Interrupted-run behavior: if a previous run created an app on-demand snapshot and then failed before that snapshot was fully synced, the next normal `sync` detects the existing app-created pending snapshot by tag `O` plus `manual_snapshot.marker`. It keeps that older pending snapshot in the normal oldest-to-newest order, but it still creates a fresh on-demand snapshot for the current run because the older pending snapshot may no longer represent the current system state. Both the older pending snapshot and the new snapshot are sent only when the normal oldest-to-newest loop reaches their timestamps.

Automatic creation is skipped when `--snapshot <name>` is used, because that command targets one existing snapshot.

## Run summaries

Every `sync` ends with a terminal-friendly `SYNC SUMMARY`. It shows how many full syncs and incremental syncs were planned or completed, how many entries were already synced, and which source/destination paths were used. Each transfer is labeled clearly as `FULL SYNC` or `INCREMENTAL`. When `log_dir` is enabled, this readable statistics block is written to `.succes`, not mixed into `.log`.

If a transfer is interrupted while `btrfs receive` has already created the destination path, that path is not marked as complete in `state.json`. With `destination.cleanup_incomplete_receive = true`, the next real sync deletes only that incomplete Btrfs subvolume or empty directory, invalidates the per-run destination index entry, and retries the same source snapshot/subvolume in the normal oldest-to-newest order.

This also applies when the failed snapshot is an app-created on-demand snapshot. The app does not move the on-demand snapshot to the front of the queue. It keeps the already sorted source snapshot list, deletes the partial destination path only when that on-demand snapshot/subvolume is reached, and then sends it at that exact point in the existing oldest-to-newest order. If automatic on-demand creation is enabled, a fresh on-demand snapshot for the current run is still created and then added to the same sorted queue.

Every `prune` now prints a `RETENTION SUMMARY`, a `RETENTION DELETE PLAN`, and a `RETENTION DELETE SUMMARY` after real deletion. Delete candidates are labeled as `WOULD DELETE` in dry-run mode or `DELETE` in real mode, and each entry includes the destination subvolumes, source send-cache subvolumes, Timeshift tags, and the reason it falls outside the active retention rules. The final summary reports attempted, completed, retry, and remaining state counts. When `log_dir` is enabled, these readable summaries are written to `.succes` and the normal run log.

## Pruning and retention

Pruning applies destination retention rules. It can be enabled from config with `prune_after_sync = true` or from CLI with `sync --prune`.

Real deletion requires all of these:

```text
1. non-dry-run mode
2. pruning enabled
3. --yes-delete passed
```

Examples:

```bash

## Destroy leftovers when retiring this setup

`destroy-leftovers` is a separate destructive command for the case where you no longer want to use this app/setup and want to remove app-created source send-cache and/or destination backup leftovers. It ignores retention rules and `state.json` because it is not a normal prune operation. It never deletes `source.snapshot_root`, because that belongs to Timeshift and contains the user's own source snapshots.

Dry-run is the default:

```bash

## Logging and notifications

Set top-level `log_dir` to enable split per-run logs. Logging starts immediately after the config is loaded and before command work begins. Normal app stdout is copied to `.log`. Normal command stderr is copied to `.err`. Transfer stderr is handled differently because successful `btrfs send` and `mbuffer` both write normal status/progress to stderr: that transfer text is kept in `.btrfs`/`.mbuffer`, and is copied to `.err` only if the transfer pipeline fails.

```text
*.log      normal command/control output
*.err      real command/pipeline error output
*.btrfs    Btrfs send/receive command headers and status/verbose output
*.mbuffer  mbuffer progress and summary
*.succes   readable sync/retention statistics and success mail body
```

Email notifications can attach these log files when `mail.attach_logs = true`. Missing files and 0-byte files are skipped. `mail.max_attachment_bytes` can limit attachment size. When `.succes` exists and has content, its text is used as the plain-text email message body.

MQTT notifications publish simple JSON status to the configured topic. Failure messages include exit code, error text, and latest captured stderr. MQTT uses optional `paho-mqtt`; email uses Python standard library `smtplib` / `email`.

## Transfer output

`mbuffer` is the useful live throughput display. It can show rate, total transferred, elapsed time, and buffer fill. Btrfs verbose output is optional and can be useful for debugging, but it is operation/detail output, not a percentage progress bar.

The app does not estimate a progress bar from Btrfs disk-usage values because those values can be very different from the real send-stream size.

## Destination filesystem compression

The app does not set destination Btrfs compression properties. If you want received backup snapshots to be stored compressed on the receiving end, mount the receiving Btrfs filesystem/subvolume with compression enabled before running the app.

For example, configure the receiving mount outside this app with a Btrfs mount option such as `compress=zstd` or `compress=zstd:<level>` in `/etc/fstab`, then use that mounted path as `destination.target_root`.

`source.send_compressed_data = true` only controls the Btrfs send stream. It can send already-compressed source extents efficiently when supported, but it does not configure destination compression. Destination compression is decided by how the receiving Btrfs filesystem/subvolume is mounted or configured outside the app.

## Installation and executable builds

Install instructions, editable install steps, and PyInstaller executable build commands are kept in [`INSTALL.md`](INSTALL.md).

For a normal source install:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
ts-btrfs --version
```

For PyInstaller builds, see the dedicated `INSTALL.md` section for both folder-style and one-file executables.

## Usual test flow

```bash
ts-btrfs test-source --config ./config.toml
ts-btrfs list-source --config ./config.toml
ts-btrfs sync --config ./config.toml --dry-run
ts-btrfs sync --config ./config.toml --run --limit 1
```

When that looks correct, run a full sync:

```bash
ts-btrfs sync --config ./config.toml --run
```

Run with pruning only when you are ready for destination deletes:

```bash
ts-btrfs sync --config ./config.toml --run --prune --yes-delete
```

## Configuration

Generate the included example config:

```bash
ts-btrfs init-config --path ./config.toml
nano config.toml
```

The packaged `timeshift_btrfs_sync/data/config.example.toml` file contains all options with safe defaults. Keep `default_dry_run = true` and `retention.cleanup_ondemand = false` unless you intentionally want less conservative behavior. Incremental sends require a proven matching parent; there is no unsafe override to continue when source and destination parent metadata does not match. Manual snapshot creation follows the same safety model: existing destinations require a UUID-confirmed source/destination anchor, while an empty destination may start with a full seed.

## Command reference

Top-level help lists every command. Command-specific flags are visible with `ts-btrfs <command> --help` or `python3 -m timeshift_btrfs_sync <command> --help`.

### Global

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--help` | Shows help for the main command or subcommand. | Use it to check the exact supported flags in the installed package. |
| `--version` | Prints the app version. | Useful when confirming which package is installed. |

### `init-config`

Writes the complete commented config template.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--path PATH` | Writes the template to `PATH`; default is `./ts-btrfs.toml`. | Lets you create a config in the folder or name you prefer. |
| `--force` | Overwrites the target file if it already exists. | Needed when refreshing an existing generated template. Review changes before replacing a real config. |

### `test-source` / `test-ssh`

Tests the configured source endpoint and the required source sudo commands. `test-ssh` is an alias for the same check. In `source.mode = "local"`, SSH is skipped.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed so the app knows source mode, source command paths, and SSH settings when SSH mode is used. |

### `list-source`

Lists source Timeshift snapshots.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed for source mode and source snapshot settings. |
| `--verify-btrfs` | Runs slower Btrfs checks for every configured source subvolume during listing. | Useful when validating a new `snapshot_root` or subvolume layout. Omit it for faster normal listing. |

### `sync`

Pulls missing source snapshot subvolumes to the destination.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed for all source, destination, stream, retention, and notification settings. |
| `--dry-run` | Prints the sync/prune plan without destination preparation, lock creation, receiving, state writing, manual snapshot creation, or deletion. | Safest way to inspect what the app intends to do without touching the destination, except optional log files. |
| `--run` | Performs real send/receive work. | Required for actual backup changes. |
| `--limit LIMIT` | Transfers at most this many subvolumes. | Useful for first live testing, for example `--run --limit 1`. |
| `--snapshot SNAPSHOT` | Syncs only one Timeshift snapshot name. | Useful for targeted testing or retrying one known snapshot. Automatic manual snapshot creation is skipped. |
| `--resend` | Tries to transfer even if `state.json` says it was already synced. | Useful for controlled repair/testing, but should be used carefully to avoid conflicts. |
| `--prune` | Runs destination pruning after sync. | Needed when you want retention cleanup after the backup. Real deletion still needs `--run --yes-delete`. |
| `--yes-delete` | Allows real pruning deletes when pruning is enabled and command is non-dry-run. | Extra safety confirmation for destructive deletion of destination snapshots. |

### `prune`

Applies destination retention without syncing first.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed for destination and retention settings. |
| `--dry-run` | Shows what would be deleted without creating a lock file, saving state, or deleting anything. | Use before real pruning to verify retention behavior. |
| `--run` | Allows pruning to run for real if `--yes-delete` is also present. | Required for actual deletion. |
| `--yes-delete` | Confirms real deletion. | Prevents accidental destructive retention cleanup. |

### `create-manual`

Creates one source Timeshift on-demand snapshot using the configured source. Timeshift assigns tag `O` by default.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed for source mode, Timeshift command, and manual snapshot safety settings. |
| `--comment COMMENT` | Passes a custom comment to `timeshift --create --comments`. | Useful to identify why the snapshot was created and to include the configured marker text. |

### `show-state`

Shows the local state tracking file.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed to locate `state.json`. |
| `--json` | Prints raw `state.json`. | Useful for debugging parent metadata or automation parsing. |

### `destroy-leftovers`

Destroys configured source/destination leftover trees when this app setup is being retired. This is not a prune command and does not use retention or `state.json` safety.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Required so the app knows the exact configured paths and job name. |
| `--delete-source` | Deletes `source.cache_root` when configured. | Removes app-created source send-cache leftovers only; never deletes `source.snapshot_root`. |
| `--delete-destination` | Deletes `destination.target_root`. | Removes the backup target tree, including received snapshots and `.ts-btrfs-sync`. |
| `--delete-both` | Deletes `source.cache_root` and `destination.target_root`. | Full retirement cleanup for app-created source send-cache plus backup destination; never deletes `source.snapshot_root`. |
| `--dry-run` | Shows the destructive cleanup plan. | Default mode; does not delete anything. |
| `--run` | Allows real deletion. | Still requires the long danger flag and typed confirmations. |
| `--i-understand-this-destroys-data` | Required with `--run`. | Prevents accidental execution of this destructive command. |

When `--delete-both` is used, the command prints `SOURCE / DESTINATION SNAPSHOT MATCH`. This is a reporting aid only; deletion still ignores state.json and only targets the explicitly configured source cache and destination target.

## Config reference

Every option below is present in the packaged `timeshift_btrfs_sync/data/config.example.toml`. Commented entries are optional but supported.

### Top-level options

| Option | What it does | Why it may be needed |
|---|---|---|
| `name` | Human-readable job name used in output, notifications, and log filenames. | Helps recognize which backup job sent a mail/MQTT message or produced a log. |
| `default_dry_run` | Makes commands preview by default unless `--run` is passed. Dry-run skips destination preparation, lock creation, receives, state writes, manual snapshot creation, and prune deletion. | Safe default to avoid accidental writes or deletes while checking the plan. |
| `prune_after_sync` | Automatically runs the prune step after successful sync. | Useful for scheduled jobs, but real deletion still requires `--run --yes-delete`. |
| `log_dir` | Directory for split per-run log files; blank/omitted disables file logging. The logger creates only the exact log directory when its parent already exists; destination helper preflight prepares missing log directories during real sync/prune. | Needed for persistent debug logs and email log attachments without letting logging accidentally create destination roots as ordinary directories. |
| `state_file` | Optional custom path for `state.json`; default is under `<target_root>/.ts-btrfs-sync/`. | Use only when you need app metadata outside `target_root`. |
| `lock_file` | Optional custom path for the lock file; default is under `<target_root>/.ts-btrfs-sync/`. In real sync/prune, the lock-file parent is prepared before other path checks and may be either a directory or Btrfs subvolume. | Prevents two jobs from writing the same target at the same time; if the lock path includes `target_root`, that component is created by the strict Btrfs subvolume rule. |

### `[mqtt]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `enabled` | Turns MQTT notifications on or off. | Keep false unless you want MQTT status messages. If false, `paho-mqtt` is not required. |
| `host` | MQTT broker hostname or IP. | Needed when MQTT is enabled so the app knows where to publish. |
| `port` | MQTT broker port, normally `1883`. | Change if your broker uses a non-default port. |
| `topic` | MQTT topic for JSON status messages. | Home Assistant sensors/automations subscribe to this topic. |
| `username` | Optional MQTT username. | Needed for brokers that require authentication. |
| `password` | Optional MQTT password directly in config. | Works, but `password_file` is safer. Use only one of `password` or `password_file`. |
| `password_file` | File containing the MQTT password. | Keeps secrets out of the main config file. |
| `client_id` | Optional fixed MQTT client ID. | Useful when you want a predictable MQTT client name. If omitted, one is generated. |
| `qos` | Publish QoS: `0`, `1`, or `2`. | Higher QoS can improve delivery guarantees but may add broker/client overhead. |
| `retain` | Retains the last status message on the broker. | Useful for Home Assistant to see the latest status after restart, but can show stale status. |
| `timeout` | Connect/publish timeout in seconds. | Avoids notification hangs if the broker is unreachable. |
| `notify_on_success` | Publishes success messages. | Disable if you only want failure alerts. |
| `notify_on_failure` | Publishes failure messages. | Usually keep true so failed backups alert you. |

### `[mail]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `enabled` | Turns email notifications on or off. | Keep false unless you want SMTP status mail. |
| `smtp_host` | SMTP server hostname or IP. | Required when mail is enabled. |
| `smtp_port` | SMTP server port, commonly `587` for STARTTLS or `465` for implicit SSL. | Must match your mail provider/server. |
| `smtp_ssl` | Uses implicit SSL with `smtplib.SMTP_SSL`. | Use for port `465` style SMTP. |
| `starttls` | Upgrades a plain SMTP connection with STARTTLS. | Use for port `587` style SMTP when `smtp_ssl = false`. |
| `timeout` | SMTP connect/send timeout in seconds. | Prevents notification delivery from hanging the backup process too long. |
| `username` | Optional SMTP username. | Needed when the SMTP server requires login. |
| `password` | Optional SMTP password directly in config. | Works, but `password_file` is safer. Use only one of `password` or `password_file`. |
| `password_file` | File containing the SMTP password. | Keeps secrets out of the main config file. |
| `from_addr` | Sender email address. | Required by most SMTP servers and for readable mail. |
| `to_addrs` | Recipient list. | Required when mail is enabled. |
| `subject_prefix` | Prefix added to success/failure subjects. | Helps filter or recognize backup emails. |
| `include_json` | Adds the JSON status payload to the email body. | Useful for debugging or parsing mail content. |
| `attach_logs` | Attaches non-empty `.log`, `.err`, `.btrfs`, `.mbuffer`, and `.succes` files. | Useful for diagnostics without logging into the backup host. Requires `log_dir`. The `.succes` text is also used as the email body when present. |
| `max_attachment_bytes` | Per-file attachment size cap; `0` means no cap. | Prevents huge verbose logs from being mailed. |
| `notify_on_success` | Sends success emails. | Disable if you only want failure mail. |
| `notify_on_failure` | Sends failure emails. | Usually keep true so failed backups alert you. |

### `[ssh]`

Used only when `source.mode = "ssh"`. In `source.mode = "local"`, the `[ssh]` section may be omitted and SSH settings are not validated.

| Option | What it does | Why it may be needed |
|---|---|---|
| `host` | Source hostname or IP. | Required only in SSH mode so the destination can pull snapshots from the source. |
| `user` | SSH user on the source. | Use a dedicated low-privilege user with only the minimal sudo rules. |
| `port` | Optional SSH port. | Needed if the source does not use port `22`. |
| `identity_file` | SSH private key path passed with `ssh -i`. | Recommended for unattended scheduled jobs. |
| `compression` | Adds `ssh -C`. | Can help on slow links; often unnecessary on fast LANs or already-compressed streams. |
| `cipher` | Adds `ssh -c <cipher>`. | Lets you choose a fast cipher for your hardware/network. Omit for OpenSSH defaults. |
| `control_master` | Adds OpenSSH `ControlMaster=auto`. | Reuses an existing SSH connection so password-protected keys are unlocked fewer times. Disabled by default because the local control socket must be protected. |
| `control_persist` | Adds OpenSSH `ControlPersist=<value>`. | Keeps the master connection alive between metadata probes and send commands. Default example is `10m`. |
| `control_path` | Adds OpenSSH `ControlPath=<path>`. | Required when `control_master = true`. If the parent directory is missing, the app creates it with owner-only access (`0700`). Existing parents must already be owned by the app user and private. |
| `password` | SSH password passed through `sshpass -e`. | Less safe than key auth; use only if needed. Do not use with `BatchMode=yes`. |
| `password_file` | File containing the SSH password for `sshpass -e`. | Safer than storing the SSH password directly in config. |
| `extra_args` | Extra OpenSSH arguments as a string list. | Commonly used for `BatchMode=yes` with key auth or host-key behavior. |

#### Safe SSH ControlMaster use

`control_master` is optional OpenSSH connection multiplexing. The first SSH command authenticates normally, then OpenSSH keeps a local master connection alive for `control_persist`. Later `ssh` commands reuse a Unix-domain control socket instead of unlocking the private key again. This is useful when the private key has a passphrase with high KDF iterations, because the app runs many short metadata commands around the larger `btrfs send` pipeline.

The security tradeoff is important: anyone who can access the local control socket may be able to reuse the already-authenticated SSH connection without knowing the private key passphrase. In this app that connection reaches the source SSH user, which often has restricted passwordless `sudo btrfs`/`timeshift` permissions, so the socket must be private.

A safe setup when the app runs as root on the destination is to use a private path under `/run`:

```toml
[ssh]
control_master = true
control_persist = "10m"
control_path = "/run/ts-btrfs-ssh/%C"
```

The app validates this at config load time. With `control_master = true`, `control_path` must be absolute. If the ControlPath parent directory is missing, the app creates it with owner-only permissions (`0700`) as the user running `ts-btrfs`; missing intermediate directories it creates are also set to `0700`. Existing directories are not ownership-fixed automatically: they must already be owned by the user running `ts-btrfs`, must not be readable/writable/searchable by group or other users, and must not be inside shared temporary locations such as `/tmp`, `/var/tmp`, or `/dev/shm`.

Leave `control_master = false` for maximum isolation, on shared machines, or anywhere you cannot guarantee the socket directory is private.

### `[manual_snapshot]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `enabled` | Makes normal `sync` create one source Timeshift on-demand snapshot before syncing. | Useful when you want every sync run to start with a fresh source snapshot. |
| `cleanup_enabled` | Allows destination prune to delete old app-created on-demand snapshots recognized by marker. | Keeps app-created manual snapshots from growing forever. Real deletion still needs prune plus `--yes-delete`. |
| `comment` | Comment passed to `timeshift --create --comments`. | Makes the snapshot recognizable in Timeshift and should include the marker. |
| `marker` | Text used to recognize app-created on-demand snapshots. | Separates app-created on-demand snapshots from your normal manual Timeshift snapshots. |
| `retention_count` | Number of app-created on-demand snapshots to keep by marker. | Gives app-created snapshots independent retention from normal `O` snapshots. |

### `[source]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `mode` | Chooses `ssh` or `local`. Default is `ssh`. | Use `local` when Timeshift snapshots and the destination are on the same machine and you want to skip SSH while keeping the same safety rules. |
| `sudo` | Source sudo prefix, normally `sudo -n`. | Required for Timeshift/Btrfs commands without interactive prompts. |
| `btrfs_command` | Source Btrfs command name/path. | Use an absolute path if the remote sudo PATH is restricted. |
| `timeshift_command` | Source Timeshift command name/path. | Use an absolute path if needed by sudo or your distro. |
| `snapshot_root` | Source Timeshift snapshot root. | Must already exist and may be an ordinary directory on Btrfs; the app builds `<snapshot_root>/<snapshot>/<subvolume>` from this and never creates it. |
| `subvolumes` | Subvolume names expected inside each Timeshift snapshot, usually `@` and `@home`. | Controls what gets sent for each Timeshift snapshot. |
| `verify_subvolumes_at_discovery` | Verifies every listed snapshot/subvolume during discovery. | Slower but useful when validating a new layout. Keep false for fast normal dry-runs. |
| `verify_incremental_parent_once_per_run` | Verifies only the first parent per subvolume name during a run, then trusts the chain created by that run. | Reduces repeated metadata checks while keeping the initial safety check. |
| `cache_root` | Source-side root for read-only send-cache snapshots. | Needed when Timeshift snapshots are writable and cannot be sent directly. If missing, real preflight creates it as a Btrfs subvolume when `create_readonly_cache = true`; its parent must already exist and be Btrfs-accessible. |
| `create_readonly_cache` | Creates read-only cache snapshots for writable source snapshots. | Required for writable Timeshift snapshots because `btrfs send` needs read-only sources. |
| `cleanup_superseded_cache` | Source send-cache cleanup setting used during prune. | `sync` keeps all created cache snapshots; `prune` deletes cache snapshots only when the same destination snapshot is deleted by retention. |
| `send_compressed_data` | Adds `btrfs send --compressed-data`. | Attempts to preserve already-compressed source extents when supported. It does not configure destination compression; mount the receiving Btrfs filesystem/subvolume with compression enabled if you want destination compression. |
| `send_proto` | Adds `btrfs send --proto <N>`. | Needed only when you intentionally want a specific Btrfs send protocol version. |

### `[destination]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `target_root` | Local backup root. | Required. The app stores received snapshots and metadata under this path. If missing and creation is enabled, preflight creates this exact path as a Btrfs subvolume. |
| `sudo` | Destination sudo prefix, normally `sudo -n`. | Required for local `btrfs receive` and subvolume delete commands. |
| `btrfs_command` | Destination Btrfs command name/path. | Use an absolute path if needed by sudo or your distro. |
| `create_target_root` | Allows preflight to create a missing `target_root` as a Btrfs subvolume and create internal metadata directories. | Convenient for first setup. Disable if you want missing paths to be an error. |
| `cleanup_incomplete_receive` | Removes incomplete destination receives not recorded in state. | Allows safe retry after cancelled transfers. Only Btrfs subvolumes or empty dirs are auto-deleted. |

### `[stream]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `use_mbuffer` | Inserts `mbuffer` between source send and local receive. | Gives useful throughput/total display and smooths network/disk bursts. |
| `mbuffer_command` | mbuffer command name/path. | Use an absolute path or alternative command name if needed. |
| `mbuffer_size` | Memory buffer size passed to `mbuffer -m`. | Larger buffers can smooth bursts; too large wastes RAM. |
| `mbuffer_rate` | Optional rate limit passed to `mbuffer -R`. | Useful if backups should not saturate network or disks. |
| `mbuffer_extra_args` | Extra mbuffer arguments as a string list. | Allows advanced mbuffer tuning without code changes. |
| `btrfs_verbose` | Adds `-v` to `btrfs send` and `btrfs receive`. | Useful for debugging stream operations. Can be noisy and is not a progress bar. |

### `[retention]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `hourly` | Number of newest Timeshift `H` snapshots to keep. | Controls hourly backup history on the destination. |
| `daily` | Number of newest Timeshift `D` snapshots to keep. | Controls daily backup history on the destination. |
| `weekly` | Number of newest Timeshift `W` snapshots to keep. | Controls weekly backup history on the destination. |
| `monthly` | Number of newest Timeshift `M` snapshots to keep. | Controls monthly backup history on the destination. |
| `boot` | Number of newest Timeshift `B` snapshots to keep. | Controls boot snapshot history on the destination. |
| `ondemand` | Number of newest normal/user-created Timeshift `O` snapshots to keep when `cleanup_ondemand = true`. | Ignored unless normal on-demand cleanup is explicitly enabled. |
| `cleanup_ondemand` | Allows pruning normal/user-created Timeshift `O` snapshots. | Default false protects manually created Timeshift snapshots. |
| `keep_latest` | Always keeps the newest synced snapshot. | Extra safety so retention does not remove the newest backup. |
| `keep_latest_common_parent` | Keeps the newest likely common parent for incremental safety. | Reduces risk of pruning the parent needed for future incrementals. |
| `protected_snapshots` | Snapshot names that are never pruned. | Use for important snapshots you want retention to ignore. |
