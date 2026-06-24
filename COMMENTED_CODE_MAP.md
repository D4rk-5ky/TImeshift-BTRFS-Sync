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
| `timeshift_btrfs_sync/btrfs.py` | Builds Btrfs commands for metadata, send, receive, cache snapshots, delete, and compression property. |
| `timeshift_btrfs_sync/sync.py` | Main sync loop: discover snapshots, choose parent, send/receive, update state. |
| `timeshift_btrfs_sync/commands.py` | Runs subprocess commands and manages the streaming pipeline with optional mbuffer. |
| `timeshift_btrfs_sync/state.py` | Reads/writes `state.json` and finds incremental parents. |
| `timeshift_btrfs_sync/retention.py` | Plans and applies destination pruning. |
| `timeshift_btrfs_sync/lock.py` | Prevents overlapping runs with a lock file. |
| `timeshift_btrfs_sync/models.py` | Dataclasses for snapshots and subvolumes. |

## Source-side commands

These are the only commands that need passwordless sudo on the source. In fast
discovery mode, the app does not run the btrfs metadata commands for every
snapshot up front; it delays them until send time.

```bash
sudo -n timeshift --list
sudo -n timeshift --create --scripted --tags O --comments "..."
sudo -n btrfs subvolume show <path>
sudo -n btrfs property get -ts <path> ro
sudo -n btrfs subvolume create <cache_root>/<snapshot>
sudo -n btrfs subvolume snapshot -r <source> <cache>
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

## Destination-side commands

These run locally on the backup machine:

```bash
sudo -n btrfs receive <snapshot_dir>
sudo -n btrfs subvolume show <received_path>
sudo -n btrfs property get -ts <received_path> ro
sudo -n btrfs property set <path> compression <zstd|lzo|zlib|none>
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

## Compression logic

Destination compression is best-effort:

1. Set compression property on target root/snapshots root.
2. Set compression on the per-snapshot receive directory before receive.
3. Optionally set compression on received subvolume after receive.

This should not break incremental sync because the incremental chain depends on
snapshot parent relationships and the received parent staying unchanged.
