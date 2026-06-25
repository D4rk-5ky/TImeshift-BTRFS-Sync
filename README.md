# timeshift-btrfs-sync v0.4.2

> ⚠️ AI-assisted / vibe-coded experimental software. Use at your own risk.

## Disclaimer

This project is AI-assisted / vibe-coded software created as a hobby project.

It has not been professionally audited, and it may contain bugs, unsafe behavior,
data-loss issues, security problems, or incorrect assumptions. Use it at your own risk.

You are responsible for reviewing the code, testing it in a safe environment,
making backups, and understanding what it does before using it on real data.

The author is not responsible for any damage, data loss, broken systems,
security issues, or other problems caused by using this software.

## Data Loss Warning

This application can perform destructive operations, including deleting files,
snapshots, or backup data.

Before using these features, make sure you have tested the program, understand
the configuration, and have a working backup. The author is not responsible for
lost or damaged data.

## License

MIT License. See [`LICENSE`](LICENSE).

Destination-pull sync for Timeshift Btrfs snapshots over SSH.

This build keeps fast discovery, but adds an incremental parent guard so the app
does not accidentally use destination snapshots from another OS/source as parents. This build also adds optional MQTT and email status notifications, including optional email attachments for the split run logs.

## Version

This build version is:

```text
0.4.2
```

See `VERSIONING.md` for the count.


## Mail log attachments

Email notifications can now attach the split run log files when top-level `log_dir` is enabled.

The attached files are, if they exist for the run:

```text
*.log
*.err
*.mbuffer
*.btrfs-out
```

This keeps the email body short while still giving you the detailed normal log,
error log, mbuffer progress log, and Btrfs verbose-output log for debugging.


## Source sudo remains minimal

The source still only needs passwordless sudo for Btrfs and Timeshift:

```sudoers
ts-btrfs-sync-user ALL=(root) NOPASSWD: /usr/bin/btrfs *
ts-btrfs-sync-user ALL=(root) NOPASSWD: /usr/bin/timeshift *
```

What those lines allow:

- `sudo -n timeshift --list` for snapshot discovery.
- `sudo -n timeshift --create --scripted --comments ...` for manual snapshots.
- `sudo -n btrfs subvolume show ...` for UUID metadata when needed, mainly the first incremental parent per subvolume per run.
- `sudo -n btrfs property get -ts ... ro` for read-only checks when a subvolume is actually going to be sent.
- `sudo -n btrfs subvolume create ...` for send-cache snapshot parents.
- `sudo -n btrfs subvolume snapshot -r ...` for read-only send-cache snapshots.
- `sudo -n btrfs subvolume delete ...` for deleting superseded send-cache snapshots after successful sends.
- `sudo -n btrfs send ...` for full/incremental streams.


## Optional automatic on-demand snapshot before sync

You can let `sync` create a normal Timeshift on-demand/manual snapshot on the
source before it reads the source snapshot list:

```toml
[manual_snapshot]
# Create one app-tagged Timeshift on-demand/tag O snapshot before normal sync.
# The app intentionally omits explicit --tags O because Timeshift defaults to O.
enabled = true

# Keep true for safety: verify the configured source against state.json by UUID
# before creating a new source-side Timeshift snapshot.
require_verified_source = true

# Independently prune app-created on-demand snapshots by marker.
cleanup_enabled = true

comment = "ts-btrfs-sync automatic on-demand snapshot"
marker = "ts-btrfs-sync"
retention_count = 10

[retention]
# Independently decide whether normal/user-created Timeshift tag O snapshots
# may be pruned. Default false keeps them all.
cleanup_ondemand = false
ondemand = 10
```

Real run behavior with the default safety guard enabled:

```text
1. Connect to source over SSH.
2. Run: sudo -n timeshift --list
3. Verify the configured source against state.json using Btrfs UUID metadata.
4. Run: sudo -n timeshift --create --scripted --comments <comment>
5. Run: sudo -n timeshift --list again.
6. Sync the newly created snapshot like any other Timeshift snapshot.
```

If the UUID verification cannot find a trusted source anchor, the app refuses to
create the manual snapshot. This protects against accidentally creating a stale
snapshot on the wrong mounted OS.

Dry-run behavior only prints that the manual snapshot would be created. It does
not create anything on the source.

If `--snapshot <name>` is used, automatic manual creation is skipped because the
command is a targeted sync of one existing snapshot.

The comment marker is used by destination pruning when the comment is available
in `timeshift --list` and saved in `state.json`. This lets the app recognize
its own on-demand snapshots separately from your normal/manual Timeshift
on-demand snapshots.

The cleanup switches are independent:

```text
manual_snapshot.enabled          create a new app-tagged source snapshot before sync
manual_snapshot.cleanup_enabled  prune only app-created on-demand snapshots by marker
retention.cleanup_ondemand       prune normal/user-created Timeshift tag O snapshots
```

Default safety behavior keeps normal/user-created on-demand snapshots unless you
explicitly set `cleanup_ondemand = true`. Real deletion still requires prune to
run in non-dry-run mode and `--yes-delete`.

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
cache_root = "/media/<UserName>/OS-Root/timeshift-btrfs/.ts-btrfs-sync/send-cache"
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
log_dir = "/media/<UserName>/btrbk/KubuntuBTRFSRAID0/.ts-btrfs-sync/logs"
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
  "version": "0.4.2"
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
  "version": "0.4.2"
}
```

Home Assistant can consume this using an MQTT sensor or an automation trigger on
the configured topic. This build does not create MQTT discovery entities yet; it
only publishes simple JSON status messages.

### Home Assistant Pushover automation example

This example uses the same Home Assistant automation YAML shape as the UI/exported automation format:

- top-level `triggers:` instead of legacy `trigger:`
- top-level `actions:` instead of legacy `action:`
- MQTT topic under `options:` for the MQTT trigger

Important: `trigger.payload_json` only exists when the automation is actually started by an MQTT message containing valid JSON. If you press **Run actions** manually from the automation editor, there may be no MQTT payload. For real testing, publish a test MQTT message to the same topic from **Developer Tools -> MQTT**.

```yaml
alias: Timeshift Btrfs Sync - MQTT Pushover
description: >-
  Send Pushover notification when timeshift-btrfs-sync reports success or
  failure over MQTT.
triggers:
  - trigger: mqtt
    options:
      topic: homeassistant/timeshift-btrfs-sync/kubuntu-timeshift/status
actions:
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ trigger.payload_json.success | default(false) | bool }}"
        sequence:
          - action: notify.pushover
            data:
              title: ✅ Btrfs sync successful
              message: >-
                {{ trigger.payload_json.name | default(trigger.payload_json.job
                | default('timeshift-btrfs-sync')) }} finished successfully.

                Command: {{ trigger.payload_json.command | default('unknown') }}
                Exit code: {{ trigger.payload_json.exit_code | default(0) }}
              data:
                priority: 0
                sound: pushover
    default:
      - action: notify.pushover
        data:
          title: ❌ Btrfs sync failed
          message: >-
            {{ trigger.payload_json.name | default(trigger.payload_json.job |
            default('timeshift-btrfs-sync')) }} failed.

            Command: {{ trigger.payload_json.command | default('unknown') }}
            Exit code: {{ trigger.payload_json.exit_code | default('unknown') }}

            Error: {{ trigger.payload_json.error | default('No error message')
            }}

            Last stderr: {{ trigger.payload_json.stderr | default('No stderr
            captured') }}
          data:
            priority: 1
            sound: siren
mode: queued
```

Success test payload:

```json
{
  "success": true,
  "name": "kubuntu-timeshift",
  "command": "sync",
  "exit_code": 0
}
```

Failure test payload:

```json
{
  "success": false,
  "name": "kubuntu-timeshift",
  "command": "sync",
  "exit_code": 1,
  "error": "Test failure",
  "stderr": "Test stderr"
}
```

## Optional email notifications

Email notifications are optional and controlled by the `[mail]` section in
`config.toml`. This uses Python standard library `smtplib` and `email.message`,
so no extra Python dependency is required. If `mail.enabled = false`, no email is
sent.

Minimal STARTTLS example:

```toml
[mail]
enabled = true
smtp_host = "smtp.example.com"
smtp_port = 587
smtp_ssl = false
starttls = true
username = "smtp-user@example.com"
password_file = "/root/.config/ts-btrfs-mail.password"
from_addr = "timeshift-btrfs-sync@example.com"
to_addrs = ["admin@example.com"]
subject_prefix = "[timeshift-btrfs-sync]"
notify_on_success = true
notify_on_failure = true
include_json = true
attach_logs = true
max_attachment_bytes = 0
```

For implicit SMTP SSL, use port 465 and set:

```toml
smtp_ssl = true
starttls = false
```

Success mail includes the job name from the top-level `name` config, the command,
exit code, host, timestamp, and optional JSON payload. Failure mail includes the
same fields plus the error text and latest captured stderr tail.

When `mail.attach_logs = true` and top-level `log_dir` is set, the email also
attaches the split run log files that exist for that run:

```text
.log
.err
.mbuffer
.btrfs-out
```

Set `mail.max_attachment_bytes` to a positive byte count to skip very large
attachments, for example if `btrfs_verbose = true` creates a huge `.btrfs-out`
file. The default `0` means no size cap.

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
target_root = "/media/<UserName>/btrbk/KubuntuBTRFSRAID0/"
```

means the app owns this backup job folder:

```text
/media/<UserName>/btrbk/KubuntuBTRFSRAID0/
```

Inside that folder, the app creates **two important folders**:

```text
/media/<UserName>/btrbk/KubuntuBTRFSRAID0/
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
sudo btrfs subvolume list /media/<UserName>/btrbk/KubuntuBTRFSRAID0

# 2. Delete received subvolumes under snapshots/.
# Example only. Delete the exact paths that exist on your destination.
sudo btrfs subvolume delete /media/<UserName>/btrbk/KubuntuBTRFSRAID0/snapshots/2026-06-23_07-10-24/@home
sudo btrfs subvolume delete /media/<UserName>/btrbk/KubuntuBTRFSRAID0/snapshots/2026-06-23_07-10-24/@

# 3. After all Btrfs subvolumes below snapshots/ are gone,
# remove the ordinary folders and app metadata.
sudo rm -rf /media/<UserName>/btrbk/KubuntuBTRFSRAID0/snapshots
sudo rm -rf /media/<UserName>/btrbk/KubuntuBTRFSRAID0/.ts-btrfs-sync
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

Creates a source Timeshift on-demand/manual snapshot. Timeshift assigns tag `O` by default when no other tag is given.

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

### `[mail]`

| Option | Meaning |
|---|---|
| `enabled` | Enable optional email status notifications. If false, no email is sent. |
| `smtp_host` | SMTP server hostname or IP. Required when `enabled = true`. |
| `smtp_port` | SMTP server port. Common values: `587` for STARTTLS, `465` for implicit SSL. |
| `smtp_ssl` | If true, connect with implicit SMTP SSL using `smtplib.SMTP_SSL`. |
| `starttls` | If true and `smtp_ssl = false`, upgrade the SMTP connection using STARTTLS. |
| `username` | Optional SMTP username. If omitted, SMTP login is skipped. |
| `password` | Optional SMTP password. Use either `password` or `password_file`, not both. |
| `password_file` | Optional file containing the SMTP password. Safer than storing the password directly in config.toml. |
| `from_addr` | Sender address. Required when enabled. |
| `to_addrs` | List of recipient addresses. Required when enabled. |
| `subject_prefix` | Prefix for success/failure email subjects. |
| `timeout` | SMTP connect/send timeout in seconds. |
| `include_json` | If true, append the JSON status payload to the plain-text email body. |
| `attach_logs` | If true, attach the run `.log`, `.err`, `.mbuffer`, and `.btrfs-out` files when `log_dir` is enabled and the files exist. |
| `max_attachment_bytes` | Optional per-file attachment size cap in bytes. `0` means no size cap. |
| `notify_on_success` | If true, send email after a successful command. |
| `notify_on_failure` | If true, send email after a failed command. |

### `[manual_snapshot]`

| Option | Meaning |
|---|---|
| `enabled` | If true, `sync` creates a source Timeshift on-demand/tag `O` snapshot before reading the source list. The command intentionally omits explicit `--tags O` because Timeshift defaults to `O` and some versions reject explicit `O`. Dry-run only previews it. This only controls creation. |
| `cleanup_enabled` | If true, destination prune may delete old app-created on-demand snapshots recognized by marker. This does not affect normal/user-created on-demand snapshots. Default `true`. |
| `require_verified_source` | If true, automatic manual snapshot creation first requires a UUID-confirmed match between the configured source and existing `state.json` history. Default `true`. |
| `comment` | Comment passed to `timeshift --create --comments`. Keep the marker text inside this comment. |
| `marker` | Case-insensitive text used to recognize app-created manual snapshots in saved state comments. |
| `retention_count` | Number of newest app-created/manual snapshots to keep by marker during destination prune. Default `10`; set `0` to keep none except global safety keeps. Set `cleanup_enabled = false` to keep all app-created snapshots. |

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
| `ondemand` | Number of newest normal/user-created Timeshift tag `O` snapshots to keep when `cleanup_ondemand = true`. Default `10`. |
| `cleanup_ondemand` | If true, destination prune may delete old normal/user-created Timeshift tag `O` snapshots. Default `false` for safety. App-created on-demand cleanup is controlled separately by `[manual_snapshot].cleanup_enabled`. |
| `yearly` | Optional non-native `Y` retention count. |
| `keep_latest` | Always keep newest synced snapshot. |
| `keep_latest_common_parent` | Keep newest likely common parent for incremental safety. |
| `protected_snapshots` | Snapshot names that are never pruned. |



### Mail attachment empty-file guard

Mail notifications skip 0-byte `.log`, `.err`, `.mbuffer`, and `.btrfs-out` attachments. Skipped empty files are listed in the email body.
