# timeshift-btrfs-sync

`timeshift-btrfs-sync` is a first-version Python CLI app for pulling Timeshift Btrfs snapshots from a source machine to a Btrfs backup destination over SSH.

It does **not** reimplement Btrfs send/receive. It safely orchestrates:

```bash
btrfs send
btrfs receive
ssh
btrfs subvolume show
btrfs property get
btrfs subvolume snapshot -r
btrfs subvolume delete
timeshift --create --tags O
```

The first version is **destination-pull only**:

```text
backup/destination machine -> SSH -> source machine -> btrfs send stream -> local btrfs receive
```

That means the source machine does not need write access to the backup location.

## Status

This is an MVP/first version. Use `--dry-run` first. Test on non-critical snapshots before trusting it.

This commented build adds explanatory docstrings and inline comments throughout the Python source so the control flow is easier to follow and modify.

Implemented:

- Destination-pull over SSH
- Full Btrfs send/receive
- Incremental Btrfs send/receive using the newest synced source snapshot still present on the source
- Timeshift snapshot discovery
- `@` and `@home` subvolume support, configurable
- On-demand/manual Timeshift snapshot creation using tag `O`
- Local sync state file
- Dry-run mode
- Retention pruning by Timeshift-like tags: `H`, `D`, `W`, `M`, `B`, `O`
- Optional `Y` yearly retention extension
- Local destination pruning with explicit `--yes-delete`
- Lock file to prevent multiple simultaneous runs

Not implemented yet:

- GUI
- Push mode
- Automatic SSH/sudo setup
- Snapshot restore workflow
- Full validation of every possible Timeshift `info.json` variant
- Automatic detection of all nested subvolumes

## Install

On the backup/destination machine:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Then test:

```bash
ts-btrfs --version
```

## Create config

```bash
ts-btrfs init-config --path ./kubuntu.toml
```

Edit the file:

```bash
nano ./kubuntu.toml
```

Important fields:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
identity_file = "/root/.ssh/timeshift-btrfs-sync"

[source]
snapshot_root = "/timeshift-btrfs/snapshots"
subvolumes = ["@", "@home"]
sudo = "sudo -n"

[destination]
target_root = "/Backups/Kubuntu/timeshift-btrfs"
sudo = "sudo -n"
```

Common source snapshot roots:

```text
/timeshift-btrfs/snapshots
/run/timeshift/backup/timeshift-btrfs/snapshots
```

## Required permissions

The backup machine must be able to SSH into the source machine.

The source SSH user needs passwordless sudo for commands like:

```bash
btrfs subvolume show
btrfs property get
btrfs subvolume snapshot -r
btrfs send
timeshift --create
mkdir
cat
```

The destination machine needs permission to run:

```bash
btrfs receive
btrfs subvolume show
btrfs property get
btrfs subvolume delete
```

The example config uses:

```toml
sudo = "sudo -n"
```

`sudo -n` means sudo will fail instead of asking for a password. This is usually what you want for scheduled backup jobs.

## Basic usage

Test SSH:

```bash
ts-btrfs test-ssh --config ./kubuntu.toml
```

List source Timeshift snapshots:

```bash
ts-btrfs list-source --config ./kubuntu.toml
```

Fast list without Btrfs metadata:

```bash
ts-btrfs list-source --config ./kubuntu.toml --fast
```

Dry-run sync:

```bash
ts-btrfs sync --config ./kubuntu.toml --dry-run
```

Actually sync:

```bash
ts-btrfs sync --config ./kubuntu.toml --run
```

Sync only one snapshot:

```bash
ts-btrfs sync --config ./kubuntu.toml --run --snapshot 2026-06-22_18-00-01
```

Limit the first test to one subvolume transfer:

```bash
ts-btrfs sync --config ./kubuntu.toml --run --limit 1
```

Show local sync state:

```bash
ts-btrfs show-state --config ./kubuntu.toml
```

## Manual / On-demand Timeshift snapshots

Create a Timeshift on-demand snapshot on the source machine:

```bash
ts-btrfs create-manual --config ./kubuntu.toml --comment "Before upgrade"
```

This uses Timeshift tag `O`.

## Retention / cleanup

Dry-run pruning:

```bash
ts-btrfs prune --config ./kubuntu.toml --dry-run
```

Actually delete old backup snapshots:

```bash
ts-btrfs prune --config ./kubuntu.toml --run --yes-delete
```

Run sync and then prune:

```bash
ts-btrfs sync --config ./kubuntu.toml --run --prune --yes-delete
```

Retention config:

```toml
[retention]
hourly = 6
daily = 7
weekly = 4
monthly = 6
boot = 5
ondemand = 10
yearly = 0
keep_latest = true
keep_latest_common_parent = true
protected_snapshots = []
```

`yearly` is an optional extension. Timeshift itself normally uses `H`, `D`, `W`, `M`, `B`, and `O`.

## Destination layout

The app writes to:

```text
/Backups/Kubuntu/timeshift-btrfs/
├── snapshots/
│   ├── 2026-06-22_18-00-01/
│   │   ├── @
│   │   ├── @home
│   │   └── info.json
│   └── 2026-06-22_19-00-01/
│       ├── @
│       ├── @home
│       └── info.json
└── .ts-btrfs-sync/
    ├── state.json
    ├── lock
    └── logs/
```

## Read-only source snapshots

Btrfs send needs read-only snapshots.

If a source Timeshift snapshot subvolume is already read-only, the app sends it directly.

If it is writable, the app creates a read-only source-side cache snapshot here by default:

```text
<parent-of-snapshot_root>/.ts-btrfs-sync/send-cache/<snapshot>/<subvolume>
```

Example:

```text
/timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
```

The original Timeshift snapshot is not modified.

## Safety notes

- Always test with `--dry-run` first.
- Do not run pruning until you have verified the destination paths.
- Do not manually modify received destination subvolumes.
- Do not delete source snapshots too aggressively, or the next run may need a full send instead of incremental send.
- The destination must be Btrfs.
- This software can delete backup snapshots when pruning is run with `--run --yes-delete`.

## Disclaimer

You are responsible for any damage, data loss, broken backups, or restore failure caused by using this software. Review the code, test with throwaway data, and keep separate backups before relying on it.


## Changelog

### 0.1.1

- Added comments/docstrings throughout the Python source files.
- Added more comments to the example config and systemd unit files.
- No intended logic change from 0.1.0.
