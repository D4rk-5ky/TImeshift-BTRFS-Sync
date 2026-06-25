# Commented code map

This file explains the project sections and the shell commands the Python code
builds. The Python source also contains docstrings for modules, classes, and
functions.

## Main files

| File | Purpose |
| --- | --- |
| `timeshift_btrfs_sync/cli.py` | Parses CLI commands like `sync`, `prune`, `list-source`, and `test-ssh`. |
| `timeshift_btrfs_sync/config.py` | Reads TOML config and validates options. |
| `timeshift_btrfs_sync/ssh.py` | Builds SSH commands, including identity file, sshpass password mode, SSH compression, and cipher choice. |
| `timeshift_btrfs_sync/timeshift.py` | Uses `timeshift --list` and `timeshift --create` on the source. |
| `timeshift_btrfs_sync/btrfs.py` | Builds Btrfs commands for metadata, send, receive, cache snapshots, and delete. |
| `timeshift_btrfs_sync/sync.py` | Main sync loop: discover snapshots, choose parent, send/receive, update state. |
| `timeshift_btrfs_sync/commands.py` | Runs subprocess commands and manages the streaming pipeline with optional mbuffer. |
| `timeshift_btrfs_sync/log.py` | Owns optional split file logging and creates `.log`, `.mbuffer`, `.btrfs-out`, and `.err` files. |
| `timeshift_btrfs_sync/mqtt.py` | Owns optional MQTT JSON notifications using `paho-mqtt`. |
| `timeshift_btrfs_sync/state.py` | Reads/writes `state.json` and finds incremental parents. |
| `timeshift_btrfs_sync/retention.py` | Plans and applies destination pruning. |
| `timeshift_btrfs_sync/lock.py` | Prevents overlapping runs with a lock file. |
| `timeshift_btrfs_sync/models.py` | Dataclasses for snapshots and subvolumes. |

## Split logging logic

All file logging logic lives in `timeshift_btrfs_sync/log.py`. When top-level
`log_dir` is set, the CLI creates one `RunLogger` for the command and installs
it as the active logger.

Files created per run:

```text
*.log = normal commands, return codes, and captured command output
*.mbuffer = transfer command header plus mbuffer progress/summary
*.btrfs-out = send/receive command lines plus Btrfs verbose stream output
*.err = stderr/error output
```

The streaming pipeline in `commands.py` calls `log.py` helper functions so
mbuffer progress can be shown live on screen and written to `.mbuffer` without
flooding `.log`. Btrfs verbose output is written separately to `.btrfs-out`.

## MQTT notification logic

All MQTT logic lives in `timeshift_btrfs_sync/mqtt.py`. The module imports
`paho.mqtt.client` lazily only when `[mqtt] enabled = true`, so normal non-MQTT
runs do not require paho-mqtt.

Success and failure payloads are JSON with simple top-level fields for Home
Assistant MQTT sensors or automations:

```text
state/status = success or failure
job/name     = config name
command      = CLI command, for example sync
exit_code    = command exit code
error        = exception/error text on failure
stderr       = newest captured stderr tail on failure
```

## Manual snapshot config logic

Automatic on-demand snapshot creation is controlled by `[manual_snapshot]` and
implemented in `sync.py`. When enabled, a normal `sync` run first performs
source discovery with `timeshift --list` and, by default, requires a
UUID-confirmed source match from `state.json` before creating a new source
Timeshift tag `O` snapshot.

The manual snapshot command is built in `timeshift.py` and uses remote-safe
double-quote escaping for the comment so logs stay readable:

```bash
sudo -n timeshift --create --scripted --comments "<comment>"
```

The configured comment should contain `manual_snapshot.marker`. Destination
pruning uses that marker, when present in saved state comments, to keep the
newest `manual_snapshot.retention_count` app-created on-demand snapshots. This
is independent from normal `[retention].cleanup_ondemand`, which controls
whether user-created Timeshift tag `O` snapshots may be pruned.

## Source-side commands

These are the only commands that need passwordless sudo on the source. In fast
discovery mode, the app does not run the btrfs metadata commands for every
snapshot up front; it delays them until send time.

```bash
sudo -n timeshift --list
sudo -n timeshift --create --scripted --comments "..."
sudo -n btrfs subvolume show <path>
sudo -n btrfs property get -ts <path> ro
sudo -n btrfs subvolume create <cache_root>/<snapshot>
sudo -n btrfs subvolume snapshot -r <source> <cache>
sudo -n btrfs subvolume delete <superseded-cache>
sudo -n btrfs send [-p <parent>] [--compressed-data] [--proto N] <snapshot>
```

## Fast discovery logic

Controlled by:

```toml
[source]
verify_subvolumes_at_discovery = false
```

When false, `sync` discovery only does:

```bash
sudo -n timeshift --list
```

`list-source` is also fast by default. Use this when you want the slower
per-subvolume Btrfs verification during listing:

```bash
ts-btrfs list-source --config ./config.toml --verify-btrfs
```

Then the app constructs expected paths like:

```text
<snapshot_root>/<snapshot-name>/@
<snapshot_root>/<snapshot-name>/@home
```

Btrfs checks happen later only for subvolumes that are actually being sent.


## Incremental parent guard

Fast discovery avoids metadata checks for every snapshot, but the first selected
incremental parent for each subvolume name is still checked before real send.
Later incrementals in the same run trust the chain that this process just
created. The app compares:

```text
source parent UUID == destination parent Received UUID
```

Commands used for the first selected parent for a subvolume during a run:

```bash
sudo -n btrfs subvolume show <source-parent-send-path>
sudo -n btrfs subvolume show <destination-parent-subvolume>
```

This is what prevents accidentally using a destination snapshot from another OS
or source as the parent for the current source.


## Source cache cleanup logic

Writable Timeshift snapshots (`ro=false`) need temporary read-only cache
snapshots before `btrfs send`. The app keeps the newest cache snapshot for each
subvolume because it is the parent for the next incremental send. After a newer
snapshot has been received successfully, the older parent cache is superseded
and can be deleted safely.

Cleanup command built by `btrfs.py` and called from `sync.py`:

```bash
sudo -n btrfs subvolume delete <old-cache-subvolume>
```

The app also attempts to delete the empty per-snapshot cache parent. If another
child cache such as `@home` still exists, Btrfs refuses the parent delete and the
app ignores that expected failure.

## Destination-side commands

These run locally on the backup machine:

```bash
sudo -n btrfs receive <snapshot_dir>
sudo -n btrfs subvolume show <received_path>
sudo -n btrfs property get -ts <received_path> ro
sudo -n btrfs subvolume delete <old_snapshot_subvolume>
```

## Optional stream command

If enabled, mbuffer runs on the destination:

```bash
mbuffer -m 256M
```

The pipeline becomes:

```text
ssh source 'sudo -n btrfs send ...' | mbuffer -m 256M | sudo -n btrfs receive ...
```

## Incremental logic

Incremental send is chosen in `sync.py` using state from `state.json`:

1. Find the newest older snapshot already received for the same subvolume.
2. Ensure the source still has that parent snapshot/cache path.
3. Verify the first incremental parent for each subvolume name during the run, unless disabled.
4. Run `btrfs send -p <parent> <current>`.
5. Update `state.json` only after receive succeeds, using local destination metadata when possible.

## Prune-safe high-watermark logic

After destination pruning, source Timeshift may still list snapshots that were
intentionally deleted on the destination. `sync.py` avoids re-sending those old
snapshots by finding a confirmed sync floor:

1. Walk `state.json` newest-to-oldest.
2. Require the candidate snapshot to still exist in source `timeshift --list`.
3. Compare Btrfs UUID identity between the source candidate and the destination
   received subvolume metadata.
4. Skip normal sync candidates older than or equal to that confirmed floor.

If the newest state entry is not on the source, the search walks backward until
it finds a source/state/destination UUID match. Specific `--snapshot <name>`
runs bypass this normal high-watermark skip.

## Compression logic

Destination compression is intentionally not managed by this app. If compressed destination storage is wanted, mount the receiving Btrfs filesystem/subvolume with compression enabled before running the app.

`source.send_compressed_data` only changes the generated `btrfs send` command by adding `--compressed-data`; it does not set any destination compression property or mount option.


## Btrfs verbose output

`stream.btrfs_verbose = true` adds `-v` to both generated Btrfs stream commands:

```bash
btrfs send -v ...
btrfs receive -v ...
```

The pipeline lets that Btrfs verbose output pass through live to the terminal. This is operation/detail logging, not byte progress; mbuffer remains the main progress display for throughput and totals.


## 0.2.5 notes

- `sync.py` now includes interrupted receive cleanup controlled by `destination.cleanup_incomplete_receive`.
- `commands.py` mirrors all captured stderr to the terminal and `.err`, including expected negative probes.
- Source cache cleanup now prints a separator before the next transfer block.


## Timeshift on-demand tag workaround

Manual snapshot creation intentionally omits explicit `--tags O`. Timeshift defaults to tag `O` for manual/on-demand creates, and some versions reject explicit `O` because of a CLI validation bug.


## timeshift_btrfs_sync/mail.py

Contains all optional SMTP email notification logic. It uses Python standard library `smtplib` and `email.message`, builds success/failure status payloads, supports optional username/password or password_file, STARTTLS, implicit SMTP SSL, and sends plain-text status emails.


## 0.4.8 behavior notes

- `timeshift_btrfs_sync/cli.py` resolves dry-run before taking the lock. Dry-run sync/prune paths skip `FileLock` so they do not create lock/state directories.
- `timeshift_btrfs_sync/sync.py` calls `prepare_destination()` only for real sync runs. Strict dry-run prints the plan without creating destination folders or internal metadata directories.
- `timeshift_btrfs_sync/log.py` now tees normal app stdout to `.log` and normal app stderr to `.err` while a run logger is active. Transfer streams still bypass this tee for specialized logs, but their stderr is also copied to `.err`.


## 0.4.10 stderr logging audit

- `timeshift_btrfs_sync/commands.py` no longer suppresses stderr for expected negative probes.
- All captured command stderr is mirrored to the terminal and written to `.err` when file logging is enabled.
- Pipeline stderr from remote `btrfs send`, local `btrfs receive`, and `mbuffer` is written live to `.err`.
- `mbuffer` stderr is still also written to `.mbuffer`, and Btrfs verbose stderr is still also written to `.btrfs-out` when enabled.
