> [!WARNING]
> **Work in progress — not ready for real use.**
>
> This project is experimental and still being tested. Do **not** rely on it as your only backup system. It may contain bugs that can cause failed backups, broken incremental chains, or data loss. Test only on non-critical data or keep separate verified backups before using it.

# timeshift-btrfs-sync v0.1.8

Destination-pull sync for Timeshift Btrfs snapshots over SSH.

This build keeps fast discovery, but adds an incremental parent guard so the app
does not accidentally use destination snapshots from another OS/source as parents.

## Version

This is the 18th zip build in the corrected sequence, so the version is:

```text
0.1.8
```

See `VERSIONING.md` for the count.

## What this version adds

A dedicated file, `COMMENTED_CODE_MAP.md`, explains each source file, major function area, and generated command.

- Source cache cleanup that deletes superseded read-only cache snapshots after a newer successful send.
- Keeps the newest source cache snapshot per subvolume so future incremental sends still have a valid parent.
- Human-readable transfer output with blank lines and separators between snapshots/subvolumes.
- Prints `REMOTE SEND`, optional `STREAM BUFFER`, and `LOCAL RECEIVE` as separate blocks before each transfer.
- Lets `mbuffer` progress/summary output display live during transfers.
- Optional `mbuffer` in the send/receive pipeline.
- SSH compression choice with `ssh -C`.
- SSH cipher choice with `ssh -c <cipher>`.
- Destination Btrfs compression property setting.
- Optional `btrfs send --compressed-data`.
- Fast discovery mode that avoids per-snapshot Btrfs checks during planning.
- Incremental parent guard that compares current source UUID with destination received_uuid before using a parent.
- Parent-guard cache: verify the first incremental parent per subvolume in a run, then trust the chain created by this process.
- Faster state updates: after receive, update metadata from local destination `Received UUID` instead of remote source UUID checks for every current send.
- Comments/docstrings explaining sections, functions, commands, and code paths.
- Clear documentation that `target_root` creates both `snapshots/` and `.ts-btrfs-sync/`, and both must be cleaned when fully resetting a backup.

## Source sudo remains minimal

The source still only needs passwordless sudo for Btrfs and Timeshift:

```sudoers
ts-btrfs-sync-user ALL=(root) NOPASSWD: /usr/bin/btrfs *
ts-btrfs-sync-user ALL=(root) NOPASSWD: /usr/bin/timeshift *
```

What those lines allow:

- `sudo -n timeshift --list` for snapshot discovery.
- `sudo -n timeshift --create --scripted --tags O ...` for manual snapshots.
- `sudo -n btrfs subvolume show ...` for UUID metadata when needed, mainly the first incremental parent per subvolume per run.
- `sudo -n btrfs property get -ts ... ro` for read-only checks when a subvolume is actually going to be sent.
- `sudo -n btrfs subvolume create ...` for send-cache snapshot parents.
- `sudo -n btrfs subvolume snapshot -r ...` for read-only send-cache snapshots.
- `sudo -n btrfs subvolume delete ...` for deleting superseded send-cache snapshots after successful sends.
- `sudo -n btrfs send ...` for full/incremental streams.

What those lines do **not** directly allow:

- `sudo mkdir`
- `sudo cat`
- `sudo find`
- `sudo python`
- source-side helper scripts


## Fast discovery for many snapshots

Older builds used to do Btrfs metadata checks for every snapshot/subvolume during
discovery:

```bash
sudo -n btrfs subvolume show <snapshot>/@
sudo -n btrfs property get -ts <snapshot>/@ ro
sudo -n btrfs subvolume show <snapshot>/@home
sudo -n btrfs property get -ts <snapshot>/@home ro
```

With 24 snapshots and `@` + `@home`, that can easily become around 96 remote
Btrfs commands before anything is transferred. On some systems that can take
minutes.

The new default is fast discovery:

```toml
[source]
verify_subvolumes_at_discovery = false
```

What fast discovery does:

- Runs `sudo -n timeshift --list` once.
- Parses snapshot names/tags from Timeshift output.
- Constructs expected paths from `snapshot_root` and `subvolumes`.
- Delays Btrfs checks until an actual subvolume is going to be sent.
- Makes `sync --dry-run` much faster because dry-run does not need read-only
  checks for every old snapshot.

If you want the old behavior where discovery verifies every subvolume up front,
set:

```toml
[source]
verify_subvolumes_at_discovery = true
```

Your example permissions show `info.json` as `-rw-r--r--` and the snapshot
directories as `drwxr-xr-x`, so a normal SSH user should be able to read
`info.json` without sudo, as long as all parent directories are searchable.
However, the app does not need to read `info.json` for syncing; it can transfer
subvolumes based on the normal Timeshift timestamp names and configured
subvolume names, then save its own metadata in `state.json` after each receive.


## Incremental parent guard

Fast discovery skips bulk metadata checks, but real incremental sends are still
protected. Before the app uses an existing destination snapshot as a parent for:

```bash
btrfs send -p <parent> <current>
```

it verifies the selected parent only. It does **not** verify every snapshot.

The guard reads metadata for the chosen source parent:

```bash
sudo -n btrfs subvolume show <source-parent-send-path>
```

and metadata for the matching local destination parent:

```bash
sudo -n btrfs subvolume show <target_root>/snapshots/<same-date-name>/<subvolume>
```

Then it compares:

```text
source parent UUID == destination parent Received UUID
```

That is the important safety check. If the destination contains snapshots from
another OS/source, the UUIDs should not match, so the app refuses the
incremental send instead of mixing backups.

The default config is:

```toml
[source]
verify_subvolumes_at_discovery = false
verify_incremental_parent = true
verify_incremental_parent_once_per_run = true
allow_incremental_without_parent_match = false
```

Meaning:

- discovery stays fast,
- the first real incremental parent for each subvolume name is checked,
- later incrementals in the same run trust the chain this process just created,
- unsafe/unproven parents are refused.

If the destination has no snapshots yet, the first send is full. After a full
send succeeds, the app saves source and destination metadata to its own
`state.json`. Later snapshots can then be sent incrementally.

If `state.json` is missing but destination snapshots exist, the app can still
look for matching date-named snapshots on disk and compare source UUID against
destination `Received UUID`. If no valid match can be proven, it stops and asks
you to use an empty/separate `target_root`.

### Parent guard is now once per subvolume per run

Older guarded builds verified every incremental parent. With many incrementals,
that still caused repeated remote/local metadata commands. This build verifies
only the first incremental parent for each subvolume name during one run:

```text
@ first incremental      -> verify source UUID vs destination received_uuid
@ later incrementals     -> trust chain created by this process
@home first incremental  -> verify source UUID vs destination received_uuid
@home later incrementals -> trust chain created by this process
```

After every receive, the app still reads local destination metadata and updates
`state.json`. The source UUID for the just-received snapshot is inferred from the
local destination `Received UUID`, so the app no longer needs a remote
`btrfs subvolume show` for every current snapshot merely to refresh state.


## Source cache cleanup

When source Timeshift snapshots are writable (`ro=false`), the app creates temporary read-only cache snapshots under:

```toml
[source]
cache_root = "/media/darkyere/OS-Root/timeshift-btrfs/.ts-btrfs-sync/send-cache"
create_readonly_cache = true
cleanup_superseded_cache = true
```

The cache is needed because `btrfs send` requires a read-only source. But those cache snapshots should not pile up forever.

The important rule is:

```text
Do not delete the current/latest cache snapshot immediately.
Delete the previous cache snapshot only after a newer snapshot has been sent successfully.
```

Why: the latest cache snapshot is the parent for the next incremental send. If it is deleted immediately after sending, the next run may have no valid read-only parent and may be forced into a full send or fail the parent guard.

So the app now does this after a successful incremental send:

```text
old parent cache    -> safe to delete
current cache       -> keep as next incremental parent
```

Example with `@`:

```text
2026-06-23_07-10-24/@ sent successfully
  keep its cache, because it is needed as parent

2026-06-23_09-00-01/@ sent incrementally from 07-10-24/@
  now delete 07-10-24/@ cache
  keep 09-00-01/@ cache as the newest parent
```

Cleanup uses only source-side Btrfs:

```bash
sudo -n btrfs subvolume delete <old-cache-subvolume>
```

The app also tries to delete the now-empty per-snapshot cache parent folder. If `@home` or another cached subvolume still exists inside it, that parent delete fails harmlessly and is ignored until the remaining child cache is deleted later.


## Destination `target_root` layout and full reset cleanup

The destination setting:

```toml
[destination]
target_root = "/media/darkyere/btrbk/KubuntuBTRFSRAID0/"
```

means the app owns this backup job folder:

```text
/media/darkyere/btrbk/KubuntuBTRFSRAID0/
```

Inside that folder, the app creates **two important folders**:

```text
/media/darkyere/btrbk/KubuntuBTRFSRAID0/
├── snapshots/
└── .ts-btrfs-sync/
```

What they are:

- `snapshots/` contains the received Btrfs backup snapshots, for example `snapshots/2026-06-23_07-10-24/@` and `snapshots/2026-06-23_07-10-24/@home`.
- `.ts-btrfs-sync/` contains the app metadata, especially `state.json`, the lock file, and logs.

If you want to completely start over with a new full sync, you must clean **both**:

```text
snapshots/
.ts-btrfs-sync/
```

Do **not** delete only `.ts-btrfs-sync/state.json` while leaving old received snapshots in `snapshots/`. That makes the destination contain real backup snapshots but no matching state file, so the safety guard may refuse to continue because it cannot prove those snapshots belong to the same source OS.

Also do **not** delete only `snapshots/` while leaving `.ts-btrfs-sync/state.json`. Then the state file may claim snapshots exist even though they were removed.

A full reset means the destination backup folder should look empty again, except for the parent `target_root` itself.

Important: received `@` and `@home` are Btrfs subvolumes, so they should be deleted with `btrfs subvolume delete`, not plain `rm -rf`.

Example reset shape, adjust paths before using:

```bash
# 1. Inspect what is there first.
sudo btrfs subvolume list /media/darkyere/btrbk/KubuntuBTRFSRAID0

# 2. Delete received subvolumes under snapshots/.
# Example only. Delete the exact paths that exist on your destination.
sudo btrfs subvolume delete /media/darkyere/btrbk/KubuntuBTRFSRAID0/snapshots/2026-06-23_07-10-24/@home
sudo btrfs subvolume delete /media/darkyere/btrbk/KubuntuBTRFSRAID0/snapshots/2026-06-23_07-10-24/@

# 3. After all Btrfs subvolumes below snapshots/ are gone,
# remove the ordinary folders and app metadata.
sudo rm -rf /media/darkyere/btrbk/KubuntuBTRFSRAID0/snapshots
sudo rm -rf /media/darkyere/btrbk/KubuntuBTRFSRAID0/.ts-btrfs-sync
```

After that, the next real sync starts as a new backup chain and the first send is full.

## SSH options

Example:

```toml
[ssh]
compression = true
cipher = "chacha20-poly1305@openssh.com"
```

What it does:

- `compression = true` adds `-C` to SSH.
- `cipher = "..."` adds `-c <cipher>` to SSH.
- Leave `cipher` unset for OpenSSH defaults.

Resulting command shape:

```bash
ssh -C -c chacha20-poly1305@openssh.com ts-btrfs-sync-user@source 'sudo -n btrfs send ...'
```

## SSH password or identity file

Recommended key-based auth:

```toml
[ssh]
host = "source-machine.example.lan"
user = "ts-btrfs-sync-user"
identity_file = "/root/.ssh/timeshift-btrfs-sync"
extra_args = ["-o", "BatchMode=yes"]
```

What it does:

- `identity_file` adds `ssh -i /root/.ssh/timeshift-btrfs-sync`.
- `BatchMode=yes` makes SSH fail instead of hanging on prompts.

Optional password auth through `sshpass` on the destination:

```toml
[ssh]
host = "source-machine.example.lan"
user = "ts-btrfs-sync-user"
password_file = "/root/.ssh/timeshift-btrfs-sync.password"
extra_args = ["-o", "StrictHostKeyChecking=accept-new"]
```

What it does:

- Runs `sshpass -e ssh ...` on the destination.
- Passes the password through the `SSHPASS` environment variable.
- Requires `sshpass` installed on the destination.
- Must not be combined with `BatchMode=yes`.

## mbuffer

Example:

```toml
[stream]
use_mbuffer = true
mbuffer_command = "mbuffer"
mbuffer_size = "512M"
# mbuffer_rate = "100M"
```

What it does:

- Inserts `mbuffer` between SSH and local Btrfs receive.
- Helps smooth out network/disk speed bursts.
- Runs on the destination machine.

Without mbuffer:

```text
ssh source 'btrfs send ...' | btrfs receive ...
```

With mbuffer:

```text
ssh source 'btrfs send ...' | mbuffer -m 512M | btrfs receive ...
```

Install on the destination if enabled:

```bash
sudo apt install mbuffer
```

## Destination Btrfs compression

Example:

```toml
[destination]
compression = "zstd"
set_compression_before_receive = true
set_compression_after_receive = true
```

What the app tries to run:

```bash
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs compression zstd
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs/snapshots compression zstd
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs/snapshots/2026-06-22_18-00-01 compression zstd
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs/snapshots/2026-06-22_18-00-01/@ compression zstd
```

Important notes:

- This is best-effort.
- This should not break incremental send/receive.
- Incremental send still depends on a valid unchanged parent existing on both source and destination.
- `compression = "zstd:3"` is normalized to `zstd` because `btrfs property set ... compression` does not set levels.
- For exact compression levels, use destination mount options such as `compress=zstd:3`, or run a defrag/recompress later.

## Optional source compressed-data send

Example:

```toml
[source]
send_compressed_data = true
send_proto = 2
```

What it does:

- Adds `--compressed-data` to `btrfs send`.
- Optionally adds `--proto 2` if configured.
- Attempts to preserve compressed source extents when supported.
- This is separate from destination compression.

## Usual test flow

```bash
ts-btrfs test-ssh --config ./config.toml
ts-btrfs list-source --config ./config.toml
# Optional slower verification:
# ts-btrfs list-source --config ./config.toml --verify-btrfs
ts-btrfs sync --config ./config.toml --dry-run
ts-btrfs sync --config ./config.toml --run --limit 1
```

What the commands do:

- `test-ssh` verifies SSH and minimal source sudo.
- `list-source` is now fast by default: it parses Timeshift snapshots and constructs expected subvolume paths without probing every subvolume with Btrfs.
- `list-source --verify-btrfs` does the slower full Btrfs verification.
- `sync --dry-run` prints the plan without writing data.
- `sync --run --limit 1` performs one real subvolume transfer for safe testing.

## Changelog

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

## Disclaimer

Test with throwaway data first. You are responsible for verifying that backups
and restores work on your systems.
