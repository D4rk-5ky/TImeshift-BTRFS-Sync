> [!WARNING]
> **Work in progress — not ready for real use.**
>
> This project is experimental and still being tested. Do **not** rely on it as your only backup system. It may contain bugs that can cause failed backups, broken incremental chains, or data loss. Test only on non-critical data or keep separate verified backups before using it.

# timeshift-btrfs-sync v0.2.7

Destination-pull sync for Timeshift Btrfs snapshots over SSH.

This build keeps fast discovery, but adds an incremental parent guard so the app
does not accidentally use destination snapshots from another OS/source as parents. This build also adds optional MQTT status notifications for Home Assistant.

## Version

This is the 27th zip build in the corrected sequence, so the version is:

```text
0.2.7
```

See `VERSIONING.md` for the count.

## What this version adds

A dedicated file, `COMMENTED_CODE_MAP.md`, explains each source file, major function area, and generated command.

- Prune-safe high-watermark sync: after destination pruning, the app skips older source snapshots at or below the newest UUID-confirmed state/source match instead of sending them again.
- If the newest state snapshot is no longer listed on the source, the app walks backward through `state.json` until it finds a source snapshot that still exists and matches by Btrfs UUID.
- Destination compression is no longer applied to read-only received subvolumes; after-receive compression is disabled by default and skipped if the subvolume is read-only.
- New state entries store both the original Timeshift source UUID and the exact send-path UUID, which matters when writable snapshots are sent through a read-only cache.
- Optional MQTT success/failure notifications using `paho-mqtt`.
- Adds `timeshift_btrfs_sync/mqtt.py` so MQTT logic is kept in one file.
- MQTT failure JSON includes the config `name`, command, exit code, error text, and latest stderr tail.
- Better stderr handling: captured command stderr is mirrored to the terminal unless it is an expected quiet probe.
- Recovery for interrupted receives: incomplete destination subvolumes can be deleted and retried automatically.
- Clear separator after source cache cleanup before the next send/receive block.
- Optional split logging controlled by top-level `log_dir`.
- Adds `timeshift_btrfs_sync/log.py` so logging logic is kept in one file.
- Creates per-run `.log`, `.mbuffer`, `.btrfs-out`, and `.err` files when logging is enabled.
- Keeps mbuffer progress out of `.log` and writes it to `.mbuffer`.
- Writes Btrfs send/receive command blocks to `.log`, `.mbuffer`, and `.btrfs-out` so each stream log is readable by itself.
- Source cache cleanup that deletes superseded read-only cache snapshots after a newer successful send.
- Keeps the newest source cache snapshot per subvolume so future incremental sends still have a valid parent.
- Human-readable transfer output with blank lines and separators between snapshots/subvolumes.
- Prints `REMOTE SEND`, optional `STREAM BUFFER`, and `LOCAL RECEIVE` as separate blocks before each transfer.
- Lets `mbuffer` progress/summary output display live during transfers.
- Optional Btrfs verbose output with `btrfs send -v` and `btrfs receive -v`.
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
- Clear documentation that pruning needs `--yes-delete` before any real deletion happens.

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


## Optional split logging

File logging is optional and controlled by top-level `log_dir` in `config.toml`:

```toml
log_dir = "/media/darkyere/btrbk/KubuntuBTRFSRAID0/.ts-btrfs-sync/logs"
```

If `log_dir` is blank or omitted, the app only prints to the terminal. If it is
set, the directory is created automatically and each run creates four files:

```text
YYYY-MM-DD_HH-MM-SS_<job-name>_<pid>.log
YYYY-MM-DD_HH-MM-SS_<job-name>_<pid>.mbuffer
YYYY-MM-DD_HH-MM-SS_<job-name>_<pid>.btrfs-out
YYYY-MM-DD_HH-MM-SS_<job-name>_<pid>.err
```

The files are split like this:

```text
.log = normal app command logging, return codes, and captured normal command output
.mbuffer = mbuffer progress/summary and the transfer command header
.btrfs-out = Btrfs send/receive verbose output and send/receive command lines
.err = stderr/error output
```

The send/receive command blocks are written to `.log`, `.mbuffer`, and `.btrfs-out`, so either stream log can be read by itself when debugging a transfer:

```text
REMOTE SEND: ssh ... 'sudo -n btrfs send ...'
STREAM BUFFER: mbuffer -m 256M
LOCAL RECEIVE: sudo -n btrfs receive ...
```

`mbuffer` progress is intentionally written to `.mbuffer`, not `.log`, so the normal
log does not get flooded during large transfers. Btrfs verbose output is written to
`.btrfs-out`, so it does not mix with mbuffer progress.

## Optional MQTT notifications

MQTT notifications are optional and controlled by the `[mqtt]` section in
`config.toml`. If `mqtt.enabled = false`, the app does not import `paho-mqtt` and
no MQTT message is sent.

Install the optional MQTT dependency in the same virtual environment as the app:

```bash
python3 -m pip install -e '.[mqtt]'
```

Minimal example:

```toml
[mqtt]
enabled = true
host = "homeassistant.local"
port = 1883
topic = "timeshift-btrfs-sync/kubuntu-timeshift/status"
username = "mqtt-user"
password_file = "/root/.config/ts-btrfs-mqtt.password"
qos = 0
retain = false
notify_on_success = true
notify_on_failure = true
```

A success payload is JSON and includes the human-readable `name` from the config
file so it is easy to recognize in Home Assistant:

```json
{
  "state": "success",
  "status": "success",
  "success": true,
  "job": "kubuntu-timeshift",
  "name": "kubuntu-timeshift",
  "command": "sync",
  "exit_code": 0,
  "error": "",
  "stderr": "",
  "timestamp": "2026-06-24T10:40:00+00:00",
  "host": "backup-host",
  "app": "timeshift-btrfs-sync",
  "version": "0.2.7"
}
```

A failure payload uses the same topic and includes the exit code plus the newest
stderr text that the app captured:

```json
{
  "state": "failure",
  "status": "failure",
  "success": false,
  "job": "kubuntu-timeshift",
  "name": "kubuntu-timeshift",
  "command": "sync",
  "exit_code": 1,
  "error": "Command failed (1): ...",
  "stderr": "ERROR: last stderr output here",
  "timestamp": "2026-06-24T10:41:00+00:00",
  "host": "backup-host",
  "app": "timeshift-btrfs-sync",
  "version": "0.2.7"
}
```

Home Assistant can consume this using an MQTT sensor or an automation trigger on
the configured topic. This build does not create MQTT discovery entities yet; it
only publishes simple JSON status messages.

## Interrupted receive recovery

If a transfer is cancelled during `btrfs receive`, the destination can contain a
partial subvolume that is not recorded in `state.json`. On the next run, the app
now detects that situation before sending again.

Default config:

```toml
[destination]
cleanup_incomplete_receive = true
```

When enabled, the app does this for an unrecorded destination path such as
`snapshots/<snapshot>/@`:

```text
1. Check whether the path is a Btrfs subvolume.
2. If it is a Btrfs subvolume, delete it with local `btrfs subvolume delete`.
3. If it is only an empty normal directory, remove the empty directory.
4. If it is a non-empty normal directory, stop and ask for manual cleanup.
5. Recreate the receive directory and retry `btrfs receive`.
```

This avoids treating a partial receive as a valid backup, while also avoiding a
dangerous automatic `rm -rf`.

The terminal output will show a block like:

```text
  @: found incomplete destination receive not recorded in state.json

LOCAL INCOMPLETE DELETE: /path/to/target_root/snapshots/2026-06-24_07-53-05/@

  incomplete destination receive removed; retrying transfer

---
```

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

## Pruning and `--yes-delete` safety

The app has two separate switches for pruning:

```toml
prune_after_sync = true
```

and:

```bash
ts-btrfs sync --config ./config.toml --run --prune
```

Both mean: **run the prune/retention step after sync**. They do **not** by
themselves allow real deletion.

Real destination deletion requires all of these to be true:

```text
1. the command is not dry-run
2. prune is enabled with either prune_after_sync=true or --prune
3. --yes-delete is passed
```

So if your config has:

```toml
prune_after_sync = true
```

this command will still refuse to delete old snapshots:

```bash
ts-btrfs sync --config ./config.toml --run
```

because it is missing:

```bash
--yes-delete
```

Use this for a real sync plus real prune:

```bash
ts-btrfs sync --config ./config.toml --run --yes-delete
```

Or, when `prune_after_sync = false`, explicitly enable pruning like this:

```bash
ts-btrfs sync --config ./config.toml --run --prune --yes-delete
```

To preview pruning without deleting anything:

```bash
ts-btrfs prune --config ./config.toml --dry-run
```

To run only pruning for real:

```bash
ts-btrfs prune --config ./config.toml --run --yes-delete
```

Why this extra flag exists: pruning deletes destination Btrfs subvolumes. The
extra `--yes-delete` flag prevents a config mistake or forgotten
`prune_after_sync = true` from deleting backups unexpectedly.

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


## Btrfs verbose output

Btrfs send/receive do not provide a clean percentage progress bar like some copy tools. The useful throughput/total display still comes from `mbuffer`.

The app can optionally add `-v` to both Btrfs commands:

```toml
[stream]
btrfs_verbose = true
```

This changes the generated commands to include:

```bash
sudo -n btrfs send -v ...
sudo -n btrfs receive -v ...
```

What it does:

- Shows Btrfs operation/detail output live in the terminal when Btrfs prints it.
- Does not replace `mbuffer` for byte progress, speed, totals, or elapsed time.
- Can be noisy on large sends, so the default is `false`.

## Destination Btrfs compression

Example:

```toml
[destination]
compression = "zstd"
set_compression_before_receive = true
set_compression_after_receive = false
```

What the app normally tries to run before receive:

```bash
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs compression zstd
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs/snapshots compression zstd
sudo -n btrfs property set /Backups/Kubuntu/timeshift-btrfs/snapshots/2026-06-22_18-00-01 compression zstd
```

Important notes:

- This is best-effort.
- The default is now `set_compression_after_receive = false` because received Btrfs snapshots are normally read-only. Trying to set compression on the received `@` or `@home` subvolume itself can fail with a read-only-property error.
- If `set_compression_after_receive = true` is enabled, the app checks the received subvolume read-only property first and skips the after-receive compression change when it is read-only.
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

## Prune-safe high-watermark sync

When destination pruning deletes old received snapshots, the same old snapshots may still exist on the source in `timeshift --list`. The app should **not** send those old pruned snapshots again.

Instead of storing a long tombstone list, the app now finds a confirmed sync floor from `state.json`:

```text
1. Walk state.json newest-to-oldest.
2. Find the newest state snapshot that still exists in source `timeshift --list`.
3. Confirm Btrfs UUID identity between source and destination.
4. Skip source snapshots older than or equal to that confirmed floor.
```

If the newest state entry is no longer present on the source, the app automatically walks backward until it finds an older snapshot that is still present and UUID-confirmed. If no UUID-confirmed floor can be found, the app refuses to use the high-watermark skip and continues with the normal parent guard behavior.

This means if you keep 6 hourly snapshots on the destination but the source still has 12, the next normal sync continues from the newest confirmed received snapshot instead of re-sending the 6 older pruned ones.

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


## Complete CLI command reference

All command flags below are shown by the built-in help commands:

```bash
python3 -m timeshift_btrfs_sync --help
python3 -m timeshift_btrfs_sync init-config --help
python3 -m timeshift_btrfs_sync test-ssh --help
python3 -m timeshift_btrfs_sync list-source --help
python3 -m timeshift_btrfs_sync sync --help
python3 -m timeshift_btrfs_sync prune --help
python3 -m timeshift_btrfs_sync create-manual --help
python3 -m timeshift_btrfs_sync show-state --help
```

### Global command

| Flag | Meaning |
|---|---|
| `-h`, `--help` | Show help for the main command or a subcommand. |
| `--version` | Print the program version and exit. |

### `init-config`

Writes a complete commented TOML config template.

| Flag | Meaning |
|---|---|
| `--path PATH` | Where to write the example config. Default: `./ts-btrfs.toml`. |
| `--force` | Overwrite the destination config if it already exists. |

### `test-ssh`

Tests SSH and the two required source sudo commands.

| Flag | Meaning |
|---|---|
| `--config CONFIG`, `-c CONFIG` | Path to `config.toml`. |

### `list-source`

Lists source Timeshift snapshots.

| Flag | Meaning |
|---|---|
| `--config CONFIG`, `-c CONFIG` | Path to `config.toml`. |
| `--verify-btrfs` | Slow mode. Verify every configured source subvolume with Btrfs during listing. Without this, listing uses fast discovery. |

### `sync`

Pulls missing snapshot subvolumes from source to destination.

| Flag | Meaning |
|---|---|
| `--config CONFIG`, `-c CONFIG` | Path to `config.toml`. |
| `--dry-run` | Preview planned sends/prunes. Does not receive or delete anything. |
| `--run` | Perform real send/receive work. Required for actual changes. |
| `--limit LIMIT` | Transfer at most this many subvolumes. Useful for first live testing. |
| `--snapshot SNAPSHOT` | Sync only one Timeshift snapshot name, for example `2026-06-23_07-10-24`. |
| `--resend` | Try even if `state.json` says the subvolume was already synced. Use carefully. |
| `--prune` | Run destination retention pruning after sync. Real delete also requires `--run --yes-delete`. |
| `--yes-delete` | Allow real pruning deletes when used with `--run` and `--prune`, or with `prune_after_sync = true`. |

### `prune`

Applies destination retention rules without syncing first.

| Flag | Meaning |
|---|---|
| `--config CONFIG`, `-c CONFIG` | Path to `config.toml`. |
| `--dry-run` | Show what would be deleted. Does not delete anything. |
| `--run` | Perform real pruning if `--yes-delete` is also present. |
| `--yes-delete` | Explicit safety confirmation required before real prune deletes. |

### `create-manual`

Creates a source Timeshift on-demand/manual snapshot with tag `O`.

| Flag | Meaning |
|---|---|
| `--config CONFIG`, `-c CONFIG` | Path to `config.toml`. |
| `--comment COMMENT` | Comment passed to `timeshift --create --comments`. |

### `show-state`

Shows the local `state.json` tracking file.

| Flag | Meaning |
|---|---|
| `--config CONFIG`, `-c CONFIG` | Path to `config.toml`. |
| `--json` | Print raw `state.json` instead of a short table. |

## Complete config option reference

Every option below is also present in `config.example.toml`. Options commented out there are optional but still documented.

### Top-level options

| Option | Meaning |
|---|---|
| `name` | Human-readable job name. Also used in log filenames. |
| `default_dry_run` | If true, commands preview changes unless `--run` is passed. |
| `prune_after_sync` | If true, `sync` runs pruning after sync. Real delete still requires `--run --yes-delete`. |
| `log_dir` | Optional directory for per-run `.log`, `.mbuffer`, `.btrfs-out`, and `.err` files. Blank/omitted disables file logging. |
| `state_file` | Optional path to state tracking file. Default: `<target_root>/.ts-btrfs-sync/state.json`. |
| `lock_file` | Optional path to lock file. Default: `<target_root>/.ts-btrfs-sync/lock`. |

### `[mqtt]`

| Option | Meaning |
|---|---|
| `enabled` | Enable optional MQTT status notifications. If false, `paho-mqtt` is not required. |
| `host` | MQTT broker hostname or IP. Required when `enabled = true`. |
| `port` | MQTT broker port, normally `1883`. |
| `topic` | MQTT topic used for JSON status messages. Required when enabled. |
| `username` | Optional MQTT username. Anonymous MQTT is used if omitted. |
| `password` | Optional MQTT password. Use either `password` or `password_file`, not both. |
| `password_file` | Optional file containing the MQTT password. Safer than storing the password directly in config.toml. |
| `client_id` | Optional fixed MQTT client id. If omitted, one is generated from the local hostname. |
| `qos` | MQTT publish QoS. Must be `0`, `1`, or `2`. |
| `retain` | If true, retain the last status message on the broker. Useful for HA sensors, but can show stale status. |
| `timeout` | MQTT connect/publish timeout in seconds. |
| `notify_on_success` | If true, publish success JSON after a successful command. |
| `notify_on_failure` | If true, publish failure JSON after a failed command. |

### `[ssh]`

| Option | Meaning |
|---|---|
| `host` | Source hostname or IP address. Required. |
| `user` | Source SSH user, normally `ts-btrfs-sync-user`. Optional; SSH default user is used if omitted. |
| `port` | Optional SSH port. |
| `identity_file` | Optional SSH private key path. Recommended for unattended jobs. |
| `compression` | If true, adds `ssh -C`. |
| `cipher` | Optional SSH cipher, adds `ssh -c <cipher>`. |
| `password` | Optional SSH password for `sshpass -e`. Less safe than key auth. |
| `password_file` | Optional file containing SSH password for `sshpass -e`. Use either `password` or `password_file`, not both. |
| `extra_args` | Extra SSH arguments as a TOML string list. Do not use `BatchMode=yes` with password/password_file. |

### `[source]`

| Option | Meaning |
|---|---|
| `sudo` | Source sudo prefix, normally `sudo -n`. |
| `btrfs_command` | Source Btrfs command name/path. |
| `timeshift_command` | Source Timeshift command name/path. |
| `snapshot_root` | Source Timeshift snapshot root. The app builds `<snapshot_root>/<snapshot>/<subvolume>`. Required. |
| `subvolumes` | Subvolume names expected inside each snapshot, normally `@` and `@home`. |
| `verify_subvolumes_at_discovery` | Slow discovery safety check. If true, list-source verifies every subvolume with Btrfs. Default false for speed. |
| `verify_incremental_parent` | If true, verify selected incremental parent metadata before using it. Recommended true. |
| `verify_incremental_parent_once_per_run` | If true, verify the first incremental parent per subvolume name per run, then trust the chain this run creates. |
| `allow_incremental_without_parent_match` | Dangerous escape hatch. If true, may continue even when parent match cannot be proven. Keep false. |
| `cache_root` | Source-side read-only cache root for writable Timeshift snapshots. |
| `create_readonly_cache` | If true, create read-only cache snapshots when source snapshots are writable. |
| `cleanup_superseded_cache` | If true, delete older source cache snapshots after a newer successful send supersedes them. |
| `send_compressed_data` | If true, add `btrfs send --compressed-data`. |
| `send_proto` | Optional Btrfs send protocol version, for example `2` adds `--proto 2`. |

### `[destination]`

| Option | Meaning |
|---|---|
| `target_root` | Local backup root. The app uses/creates `snapshots/` and `.ts-btrfs-sync/` inside it. Required. |
| `sudo` | Destination sudo prefix for Btrfs receive/delete/property commands. |
| `btrfs_command` | Destination Btrfs command name/path. |
| `create_target_root` | If true, create `target_root` and app metadata folders if missing. |
| `cleanup_incomplete_receive` | If true, delete and retry incomplete destination receives that are not recorded in state.json. Only Btrfs subvolumes or empty directories are auto-deleted. |
| `compression` | Destination Btrfs compression property: `zstd`, `lzo`, `zlib`, `none`, or blank. `zstd:3` is normalized to `zstd`. |
| `set_compression_before_receive` | If true, set compression on the receive parent before `btrfs receive`. |
| `set_compression_after_receive` | If true, try to set compression on the received subvolume after receive, but skip when it is read-only. Default false because received snapshots are normally read-only. |

### `[stream]`

| Option | Meaning |
|---|---|
| `use_mbuffer` | If true, insert `mbuffer` between SSH send and local receive. |
| `mbuffer_command` | mbuffer command name/path. |
| `mbuffer_size` | mbuffer memory buffer size, for example `256M`. |
| `mbuffer_rate` | Optional mbuffer rate limit, for example `100M`. |
| `mbuffer_extra_args` | Extra mbuffer arguments as a TOML string list. |
| `btrfs_verbose` | If true, add `-v` to `btrfs send` and `btrfs receive`. This is verbose operation output, not progress. |

### `[retention]`

| Option | Meaning |
|---|---|
| `hourly` | Number of newest `H` snapshots to keep. |
| `daily` | Number of newest `D` snapshots to keep. |
| `weekly` | Number of newest `W` snapshots to keep. |
| `monthly` | Number of newest `M` snapshots to keep. |
| `boot` | Number of newest `B` snapshots to keep. |
| `ondemand` | Number of newest `O` snapshots to keep. |
| `yearly` | Optional non-native `Y` retention count. |
| `keep_latest` | Always keep newest synced snapshot. |
| `keep_latest_common_parent` | Keep newest likely common parent for incremental safety. |
| `protected_snapshots` | Snapshot names that are never pruned. |

## Changelog

### 0.2.7

- Stopped trying to set destination compression on read-only received subvolumes.
- Changed `destination.set_compression_after_receive` default to `false`.
- If after-receive compression is explicitly enabled, read-only received subvolumes are detected and skipped safely.
- Added prune-safe high-watermark sync: after pruning old destination snapshots, normal sync uses the newest UUID-confirmed state/source match as a floor and skips older source snapshots instead of re-sending them.
- If the newest state snapshot is not present on the source, the app walks backward in `state.json` until it finds a source snapshot that exists and matches by Btrfs UUID.
- New state entries store both `original_source_uuid` and `send_source_uuid`, so writable Timeshift snapshots sent through read-only cache can be verified correctly later.

### 0.2.6

- Added optional MQTT status notifications using `paho-mqtt`.
- Added `timeshift_btrfs_sync/mqtt.py` so MQTT logic is isolated in one file.
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

## Disclaimer

Test with throwaway data first. You are responsible for verifying that backups
and restores work on your systems.
