# timeshift-btrfs-sync

!!! NOTE !!! THIS IS A WORK IN PROGRESS AND IS NOT READY FOR USE !!! NOTE !!!

`timeshift-btrfs-sync` pulls Timeshift Btrfs snapshots from a source machine to a Btrfs backup destination over SSH.

This version is rewritten so the **source machine only needs passwordless sudo for `timeshift` and `btrfs`**. v0.3.1 also keeps the source send-cache layout close to normal Timeshift snapshot layout.

There is:

- no source-side helper script
- no source-side Python package
- no source-side `sudo mkdir`
- no source-side `sudo cat`
- no source-side `sudo find`
- no source-side root shell script

The model is similar in spirit to tools like btrbk/syncoid: the backup side orchestrates commands over SSH, and the source-side sudoers policy is kept to the filesystem tool plus the snapshot tool.

## Source sudoers

On the source, create a dedicated SSH user, for example `btrbk-source`, then allow only Timeshift and Btrfs:

```sudoers
# edit with: sudo visudo -f /etc/sudoers.d/ts-btrfs-source
btrbk-source ALL=(root) NOPASSWD: /usr/bin/btrfs *
btrbk-source ALL=(root) NOPASSWD: /usr/bin/timeshift *
```

If your commands are in different paths, check with:

```bash
command -v btrfs
command -v timeshift
```

The destination config can still use command names like `btrfs` and `timeshift`, but the safest sudoers rule uses absolute paths.

## How source discovery works without helper scripts

The destination runs:

```bash
ssh source 'sudo -n timeshift --list'
```

It parses snapshot names/tags from Timeshift output. Then it constructs snapshot paths from:

```text
source.snapshot_root + snapshot_name + configured subvolume name
```

Example:

```text
/timeshift-btrfs/snapshots/2026-06-22_18-00-01/@
/timeshift-btrfs/snapshots/2026-06-22_18-00-01/@home
```

Each configured subvolume is verified with:

```bash
sudo -n btrfs subvolume show <path>
sudo -n btrfs property get -ts <path> ro
```

## Writable Timeshift snapshots and cache root

`btrfs send` needs read-only subvolumes. If a source Timeshift snapshot is already read-only, the app sends it directly.

If it is writable, the app can create a read-only send-cache snapshot using **only `btrfs`**:

```bash
sudo -n btrfs subvolume snapshot -r <original> <cache-path>
```

The app does **not** create the top-level cache directory. Create it once manually on the source:

```bash
sudo mkdir -p /timeshift-btrfs/.ts-btrfs-sync/send-cache
sudo chmod 700 /timeshift-btrfs/.ts-btrfs-sync/send-cache
```

After that, the app can keep a Timeshift-like cache layout without needing `mkdir`.
It creates the per-snapshot parent with **btrfs itself**:

```bash
sudo -n btrfs subvolume create /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01
sudo -n btrfs subvolume snapshot -r /timeshift-btrfs/snapshots/2026-06-22_18-00-01/@ /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
sudo -n btrfs subvolume snapshot -r /timeshift-btrfs/snapshots/2026-06-22_18-00-01/@home /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@home
```

Resulting source cache layout:

```text
/timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
/timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@home
```

If you do not want the app to create cache snapshots, set:

```toml
[source]
create_readonly_cache = false
```

Then source snapshots must already be read-only.

## Install destination app

On the backup/destination machine:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Test:

```bash
ts-btrfs --version
```

## Create config

```bash
ts-btrfs init-config --path ./kubuntu.toml
nano ./kubuntu.toml
```

Important fields:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
identity_file = "/root/.ssh/timeshift-btrfs-sync"

[source]
sudo = "sudo -n"
btrfs_command = "btrfs"
timeshift_command = "timeshift"
snapshot_root = "/timeshift-btrfs/snapshots"
subvolumes = ["@", "@home"]
cache_root = "/timeshift-btrfs/.ts-btrfs-sync/send-cache"
create_readonly_cache = true

[destination]
target_root = "/Backups/Kubuntu/timeshift-btrfs"
sudo = "sudo -n"
```

## Usage

Test SSH and passwordless source sudo:

```bash
ts-btrfs test-ssh --config ./kubuntu.toml
```

List source snapshots:

```bash
ts-btrfs list-source --config ./kubuntu.toml
```

Dry-run sync:

```bash
ts-btrfs sync --config ./kubuntu.toml --dry-run
```

First real sync, limited to one subvolume:

```bash
ts-btrfs sync --config ./kubuntu.toml --run --limit 1
```

Full real sync:

```bash
ts-btrfs sync --config ./kubuntu.toml --run
```

Create a Timeshift on-demand/manual snapshot on the source:

```bash
ts-btrfs create-manual --config ./kubuntu.toml --comment "Before upgrade"
```

Prune destination backups with dry-run first:

```bash
ts-btrfs prune --config ./kubuntu.toml --dry-run
```

Real prune requires explicit delete confirmation:

```bash
ts-btrfs prune --config ./kubuntu.toml --run --yes-delete
```

## Destination layout

```text
/Backups/Kubuntu/timeshift-btrfs/
├── snapshots/
│   ├── 2026-06-22_18-00-01/
│   │   ├── @
│   │   └── @home
│   └── 2026-06-22_19-00-01/
│       ├── @
│       └── @home
└── .ts-btrfs-sync/
    ├── state.json
    ├── lock
    └── logs/
```

## Limitations of minimal-sudo mode

Because this version does not use `cat` or a source helper, it does not copy Timeshift's `info.json` from the source. Tags and comments come from `timeshift --list` parsing instead.

If Timeshift's list output changes format, snapshot names should still parse as long as the normal timestamp folder name appears in the output.

## Disclaimer

You are responsible for any damage, data loss, broken backups, or restore failure caused by using this software. Review the code, test with throwaway data, and keep separate backups before relying on it.


## Changelog

### 0.3.1

- Changed the source read-only send-cache from a flat name layout to a Timeshift-like layout.
- Per-snapshot cache parents are created with `btrfs subvolume create`, not `mkdir`.
- Source-side sudo remains limited to `btrfs` and `timeshift`.

### 0.3.0

- Removed the source helper design.
- Source-side passwordless sudo reduced to `btrfs` and `timeshift`.
