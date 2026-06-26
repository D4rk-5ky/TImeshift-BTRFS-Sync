# timeshift-btrfs-sync v0.5.8

> ⚠️ AI-assisted / vibe-coded experimental software. Use at your own risk.

## Disclaimer

This project is AI-assisted / vibe-coded software created as a hobby project. It has not been professionally audited and may contain bugs, unsafe behavior, data-loss issues, security problems, or incorrect assumptions.

You are responsible for reviewing the code, testing it in a safe environment, making backups, and understanding what it does before using it on real data. The author is not responsible for damage, data loss, broken systems, security issues, or other problems caused by using this software.

## Data Loss Warning

This application can perform destructive operations, including deleting Btrfs subvolumes, snapshots, and backup data. Always test with dry-runs first, check the generated plans, and keep a separate working backup.

## License

MIT License. See [`LICENSE`](LICENSE).

## What it does

`timeshift-btrfs-sync` is a destination-pull backup tool for Timeshift Btrfs snapshots. It runs on the backup/destination machine, connects to the source over SSH, and pulls Timeshift snapshots with `btrfs send` / `btrfs receive`.

It supports full and incremental sends, Timeshift snapshot discovery, writable source snapshots through a read-only send cache, safe destination pruning, optional automatic Timeshift on-demand snapshots, split logs, MQTT notifications, and email notifications with optional log attachments.

Version history is kept in [`VERSIONING.md`](VERSIONING.md). The complete commented config template is [`config.example.toml`](config.example.toml).

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

This is needed because Timeshift listing/creation, Btrfs send, Btrfs metadata checks, read-only cache creation, and source cache cleanup require elevated source access.

## Destination layout

The destination `target_root` is the backup job folder. The app creates and owns:

```text
<target_root>/snapshots/       received Btrfs snapshots
<target_root>/.ts-btrfs-sync/  state.json, lock file, logs
```

`state.json` records successfully received snapshots and the metadata needed for incremental sends. Do not delete only `state.json` while keeping `snapshots/`, and do not delete only `snapshots/` while keeping old state.

State destination paths are stored relative to `destination.target_root`, for example `snapshots/2026-06-23_07-10-24/@`. This means you can move the whole target root to another mount point, update `destination.target_root`, and the app will resolve existing state paths under the new target root. Older absolute state paths are normalized when the state is loaded.

A full reset means deleting both `snapshots/` and `.ts-btrfs-sync/`. Received `@` and `@home` entries are Btrfs subvolumes, so delete them with `btrfs subvolume delete` before removing ordinary folders.

## How sync works

Normal sync flow:

```text
1. Run sudo -n timeshift --list on the source.
2. Parse Timeshift snapshot names and tags.
3. Build expected paths from source.snapshot_root and source.subvolumes.
4. Skip snapshots already received or older than the confirmed sync floor.
5. Use full send only when the destination has no snapshots yet.
6. Use incremental send when a UUID-confirmed parent is available.
7. Error out if the destination already has snapshots but no matching parent can be proven.
7. Receive into <target_root>/snapshots/<snapshot>/<subvolume>.
8. Save metadata to state.json after each successful receive.
```

Fast discovery is used by default. It avoids Btrfs metadata checks for every old snapshot and delays those checks until a subvolume is actually going to be sent. Use `list-source --verify-btrfs` or `source.verify_subvolumes_at_discovery = true` when you want slower up-front checks.

## Incremental parent guard

The old unsafe escape hatch to continue after a parent mismatch has been removed. The `source.verify_incremental_parent` option has also been removed because incremental parent verification is now mandatory. If the destination has no snapshots at all, the app can start with a normal full sync. If matching snapshots exist, the app uses an incremental send after proving the source parent matches the destination parent. If the destination already contains snapshots but no matching parent can be proven, the app refuses to send and tells the user to use an empty/separate backup directory for a new full sync or repair the existing backup state/cache.


Incremental Btrfs send uses:

```bash
btrfs send -p <parent> <current>
```

The parent must represent the same Btrfs snapshot on both source and destination. Before using a destination snapshot as an incremental parent, the app compares:

```text
source parent UUID == destination parent Received UUID
```

This protects the backup from mixing snapshots from another OS, another source host, or a reset backup chain. By default, the first incremental parent for each subvolume name is checked during a run. Later incrementals in the same run trust the chain that the app just created.

## Source read-only send cache

`btrfs send` requires read-only source snapshots. If Timeshift snapshots are writable, the app can create read-only source cache snapshots under `source.cache_root`.

Only the top-level `cache_root` should be created manually. Per-snapshot cache parents and read-only send snapshots are created with Btrfs commands:

```bash
sudo -n btrfs subvolume create <cache_root>/<snapshot-name>
sudo -n btrfs subvolume snapshot -r <original> <cache_root>/<snapshot>/<subvolume>
```

The app checks cache paths with `btrfs subvolume list -o <cache_root>` so normal Timeshift snapshot paths with the same date/name are not mistaken for existing cache snapshots.

The newest cache snapshot is kept because it is the next incremental parent. Older superseded cache snapshots are deleted only after a newer send succeeds. After deleting one child cache snapshot, the app checks `btrfs subvolume list -o <cache_root>/<snapshot-name>` and deletes the timestamp cache parent only when no child subvolumes remain.

## Optional automatic on-demand snapshots

When `manual_snapshot.enabled = true`, `sync` can create a source Timeshift on-demand snapshot before normal syncing.

The app first runs `timeshift --list`. If `manual_snapshot.require_verified_source = true`, it checks the configured source against existing `state.json` history by Btrfs UUID. This avoids creating snapshots on the wrong mounted OS or wrong source host.

The create command intentionally omits `--tags O` because Timeshift creates on-demand/tag `O` snapshots by default, and some Timeshift versions reject explicit `--tags O`.

Automatic creation is skipped when `--snapshot <name>` is used, because that command targets one existing snapshot.

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
# Preview only
ts-btrfs prune --config ./config.toml --dry-run

# Real prune only
ts-btrfs prune --config ./config.toml --run --yes-delete

# Real sync and real prune
ts-btrfs sync --config ./config.toml --run --prune --yes-delete
```

Normal/user-created Timeshift on-demand snapshots are kept unless `retention.cleanup_ondemand = true`. App-created on-demand snapshots are controlled separately by `manual_snapshot.cleanup_enabled` and `manual_snapshot.retention_count`.

## Logging and notifications

Set top-level `log_dir` to enable split per-run logs. Logging starts immediately after the config is loaded and before command work begins. Normal app stdout is copied to `.log`. **All external-command stderr is mirrored to the terminal and written to `.err`**, including expected probe failures, mbuffer stderr, and Btrfs send/receive stderr. Transfer streams are still split into their specialized logs so they are easier to read:

```text
*.log        normal command/control output
*.err        every stderr stream and error output
*.mbuffer    mbuffer progress and summary, also stderr-copy goes to .err
*.btrfs-out  Btrfs send/receive verbose output, also stderr-copy goes to .err
```

Email notifications can attach these log files when `mail.attach_logs = true`. Missing files and 0-byte files are skipped. `mail.max_attachment_bytes` can limit attachment size.

MQTT notifications publish simple JSON status to the configured topic. Failure messages include exit code, error text, and latest captured stderr. MQTT uses optional `paho-mqtt`; email uses Python standard library `smtplib` / `email`.

## Transfer output

`mbuffer` is the useful live throughput display. It can show rate, total transferred, elapsed time, and buffer fill. Btrfs verbose output is optional and can be useful for debugging, but it is operation/detail output, not a percentage progress bar.

The app does not estimate a progress bar from Btrfs disk-usage values because those values can be very different from the real send-stream size.

## Destination filesystem compression

The app does not set destination Btrfs compression properties. If you want received backup snapshots to be stored compressed on the receiving end, mount the receiving Btrfs filesystem/subvolume with compression enabled before running the app.

For example, configure the receiving mount outside this app with a Btrfs mount option such as `compress=zstd` or `compress=zstd:<level>` in `/etc/fstab`, then use that mounted path as `destination.target_root`.

`source.send_compressed_data = true` only controls the Btrfs send stream. It can send already-compressed source extents efficiently when supported, but it does not configure destination compression. Destination compression is decided by how the receiving Btrfs filesystem/subvolume is mounted or configured outside the app.

If an old config still contains `destination.compression`, `destination.set_compression_before_receive`, or `destination.set_compression_after_receive`, the app refuses to start and tells you to remove those obsolete keys.


## Installation and executable builds

Install instructions, editable install steps, and PyInstaller executable build commands are kept in [`INSTALL.md`](INSTALL.md).

The short version for normal source installs is:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
ts-btrfs --version
```

For PyInstaller builds, see the dedicated `INSTALL.md` section for both folder-style and one-file executables.

## Usual test flow

```bash
ts-btrfs test-ssh --config ./config.toml
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

Start from the included example:

```bash
cp config.example.toml config.toml
nano config.toml
```

Or generate it:

```bash
ts-btrfs init-config --path ./config.toml
```

`config.example.toml` contains all options with safe defaults. Keep `default_dry_run = true`, `manual_snapshot.require_verified_source = true`, and `retention.cleanup_ondemand = false` unless you intentionally want less conservative behavior. Incremental sends require a proven matching parent; there is no unsafe override to continue when source and destination parent metadata does not match.

## Command reference

All flags are also visible with `python3 -m timeshift_btrfs_sync <command> --help`.

### Global

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--help` | Shows help for the main command or subcommand. | Use it to check the exact supported flags in the installed version. |
| `--version` | Prints the app version. | Useful when matching behavior to `VERSIONING.md` or a downloaded zip. |

### `init-config`

Writes the complete commented config template.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--path PATH` | Writes the template to `PATH`; default is `./ts-btrfs.toml`. | Lets you create a config in the folder or name you prefer. |
| `--force` | Overwrites the target file if it already exists. | Needed when refreshing an existing generated template. Review changes before replacing a real config. |

### `test-ssh`

Tests source SSH access and the required source sudo commands.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed so the app knows the source host, SSH settings, and command paths. |

### `list-source`

Lists source Timeshift snapshots.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed for SSH and source snapshot settings. |
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
| `--config`, `-c` | Loads the chosen TOML config. | Needed for source SSH, Timeshift command, and manual snapshot safety settings. |
| `--comment COMMENT` | Passes a custom comment to `timeshift --create --comments`. | Useful to identify why the snapshot was created and to include the configured marker text. |

### `show-state`

Shows the local state tracking file.

| Flag | What it does | Why it may be needed |
|---|---|---|
| `--config`, `-c` | Loads the chosen TOML config. | Needed to locate `state.json`. |
| `--json` | Prints raw `state.json`. | Useful for debugging parent metadata or automation parsing. |

## Config reference

Every option below is present in `config.example.toml`. Commented entries are optional but supported.

### Top-level options

| Option | What it does | Why it may be needed |
|---|---|---|
| `name` | Human-readable job name used in output, notifications, and log filenames. | Helps recognize which backup job sent a mail/MQTT message or produced a log. |
| `default_dry_run` | Makes commands preview by default unless `--run` is passed. Dry-run skips destination preparation, lock creation, receives, state writes, manual snapshot creation, and prune deletion. | Safe default to avoid accidental writes or deletes while checking the plan. |
| `prune_after_sync` | Automatically runs the prune step after successful sync. | Useful for scheduled jobs, but real deletion still requires `--run --yes-delete`. |
| `log_dir` | Directory for split per-run log files; blank/omitted disables file logging. The log directory is created before command work begins. | Needed for persistent debug logs and email log attachments. This is the one intentional dry-run write when file logging is enabled. |
| `state_file` | Optional custom path for `state.json`; default is under `<target_root>/.ts-btrfs-sync/`. | Use only when you need app metadata outside `target_root`. |
| `lock_file` | Optional custom path for the lock file; default is under `<target_root>/.ts-btrfs-sync/`. | Prevents two jobs from writing the same target at the same time. |

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
| `attach_logs` | Attaches `.log`, `.err`, `.mbuffer`, and `.btrfs-out` if they exist and are non-empty. | Useful for failure diagnostics without logging into the backup host. Requires `log_dir`. |
| `max_attachment_bytes` | Per-file attachment size cap; `0` means no cap. | Prevents huge verbose logs from being mailed. |
| `notify_on_success` | Sends success emails. | Disable if you only want failure mail. |
| `notify_on_failure` | Sends failure emails. | Usually keep true so failed backups alert you. |

### `[ssh]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `host` | Source hostname or IP. | Required so the destination can pull snapshots from the source. |
| `user` | SSH user on the source. | Use a dedicated low-privilege user with only the minimal sudo rules. |
| `port` | Optional SSH port. | Needed if the source does not use port `22`. |
| `identity_file` | SSH private key path passed with `ssh -i`. | Recommended for unattended scheduled jobs. |
| `compression` | Adds `ssh -C`. | Can help on slow links; often unnecessary on fast LANs or already-compressed streams. |
| `cipher` | Adds `ssh -c <cipher>`. | Lets you choose a fast cipher for your hardware/network. Omit for OpenSSH defaults. |
| `password` | SSH password passed through `sshpass -e`. | Less safe than key auth; use only if needed. Do not use with `BatchMode=yes`. |
| `password_file` | File containing the SSH password for `sshpass -e`. | Safer than storing the SSH password directly in config. |
| `extra_args` | Extra OpenSSH arguments as a string list. | Commonly used for `BatchMode=yes` with key auth or host-key behavior. |

### `[manual_snapshot]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `enabled` | Makes normal `sync` create one source Timeshift on-demand snapshot before syncing. | Useful when you want every sync run to start with a fresh source snapshot. |
| `cleanup_enabled` | Allows destination prune to delete old app-created on-demand snapshots recognized by marker. | Keeps app-created manual snapshots from growing forever. Real deletion still needs prune plus `--yes-delete`. |
| `require_verified_source` | Requires a UUID-confirmed source/state match before creating a source snapshot. | Prevents creating stale snapshots on the wrong mounted OS, wrong source, or wrong `snapshot_root`. |
| `comment` | Comment passed to `timeshift --create --comments`. | Makes the snapshot recognizable in Timeshift and should include the marker. |
| `marker` | Text used to recognize app-created on-demand snapshots. | Separates app-created on-demand snapshots from your normal manual Timeshift snapshots. |
| `retention_count` | Number of app-created on-demand snapshots to keep by marker. | Gives app-created snapshots independent retention from normal `O` snapshots. |

### `[source]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `sudo` | Source sudo prefix, normally `sudo -n`. | Required for Timeshift/Btrfs commands without interactive prompts. |
| `btrfs_command` | Source Btrfs command name/path. | Use an absolute path if the remote sudo PATH is restricted. |
| `timeshift_command` | Source Timeshift command name/path. | Use an absolute path if needed by sudo or your distro. |
| `snapshot_root` | Source Timeshift snapshot root. | The app builds `<snapshot_root>/<snapshot>/<subvolume>` from this. |
| `subvolumes` | Subvolume names expected inside each Timeshift snapshot, usually `@` and `@home`. | Controls what gets sent for each Timeshift snapshot. |
| `verify_subvolumes_at_discovery` | Verifies every listed snapshot/subvolume during discovery. | Slower but useful when validating a new layout. Keep false for fast normal dry-runs. |
| `verify_incremental_parent_once_per_run` | Verifies only the first parent per subvolume name during a run, then trusts the chain created by that run. | Reduces repeated metadata checks while keeping the initial safety check. |
| `cache_root` | Source-side root for read-only send-cache snapshots. | Needed when Timeshift snapshots are writable and cannot be sent directly. |
| `create_readonly_cache` | Creates read-only cache snapshots for writable source snapshots. | Required for writable Timeshift snapshots because `btrfs send` needs read-only sources. |
| `cleanup_superseded_cache` | Deletes old cache snapshots after newer successful sends supersede them. | Prevents the source cache from growing forever while keeping the newest parent. |
| `send_compressed_data` | Adds `btrfs send --compressed-data`. | Attempts to preserve already-compressed source extents when supported. It does not configure destination compression; mount the receiving Btrfs filesystem/subvolume with compression enabled if you want destination compression. |
| `send_proto` | Adds `btrfs send --proto <N>`. | Needed only when you intentionally want a specific Btrfs send protocol version. |

### `[destination]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `target_root` | Local backup root. | Required. The app stores received snapshots and metadata under this path. |
| `sudo` | Destination sudo prefix, normally `sudo -n`. | Required for local `btrfs receive` and subvolume delete commands. |
| `btrfs_command` | Destination Btrfs command name/path. | Use an absolute path if needed by sudo or your distro. |
| `create_target_root` | Creates target and metadata directories if missing. | Convenient for first setup. Disable if you want missing paths to be an error. |
| `cleanup_incomplete_receive` | Removes incomplete destination receives not recorded in state. | Allows safe retry after cancelled transfers. Only Btrfs subvolumes or empty dirs are auto-deleted. |

### `[stream]`

| Option | What it does | Why it may be needed |
|---|---|---|
| `use_mbuffer` | Inserts `mbuffer` between SSH send and local receive. | Gives useful throughput/total display and smooths network/disk bursts. |
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
| `yearly` | Optional non-native yearly retention count. | Available for custom tagging/extension; Timeshift does not normally use yearly tags. |
| `keep_latest` | Always keeps the newest synced snapshot. | Extra safety so retention does not remove the newest backup. |
| `keep_latest_common_parent` | Keeps the newest likely common parent for incremental safety. | Reduces risk of pruning the parent needed for future incrementals. |
| `protected_snapshots` | Snapshot names that are never pruned. | Use for important snapshots you want retention to ignore. |
