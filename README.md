# timeshift-btrfs-sync v0.0.9

Destination-pull sync for Timeshift Btrfs snapshots over SSH.

This build keeps the project at a more realistic early version number and adds
comments/explanations for the new performance options.

## Version correction

I previously used large experimental numbers such as `0.4.0`. Based on the zip
artifacts created in this conversation, this is the 9th zip build, so the
corrected version is:

```text
0.0.9
```

The next zip build should be:

```text
0.1.0
```

See `VERSIONING.md` for the count.

## What this version adds

A dedicated file, `COMMENTED_CODE_MAP.md`, explains each source file, major function area, and generated command.

- Optional `mbuffer` in the send/receive pipeline.
- SSH compression choice with `ssh -C`.
- SSH cipher choice with `ssh -c <cipher>`.
- Destination Btrfs compression property setting.
- Optional `btrfs send --compressed-data`.
- Comments/docstrings explaining sections, functions, commands, and code paths.

## Source sudo remains minimal

The source still only needs passwordless sudo for Btrfs and Timeshift:

```sudoers
btrbk-source ALL=(root) NOPASSWD: /usr/bin/btrfs *
btrbk-source ALL=(root) NOPASSWD: /usr/bin/timeshift *
```

What those lines allow:

- `sudo -n timeshift --list` for snapshot discovery.
- `sudo -n timeshift --create --scripted --tags O ...` for manual snapshots.
- `sudo -n btrfs subvolume show ...` for UUID metadata.
- `sudo -n btrfs property get -ts ... ro` for read-only checks.
- `sudo -n btrfs subvolume create ...` for send-cache snapshot parents.
- `sudo -n btrfs subvolume snapshot -r ...` for read-only send-cache snapshots.
- `sudo -n btrfs send ...` for full/incremental streams.

What those lines do **not** directly allow:

- `sudo mkdir`
- `sudo cat`
- `sudo find`
- `sudo python`
- source-side helper scripts

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
ssh -C -c chacha20-poly1305@openssh.com btrbk-source@source 'sudo -n btrfs send ...'
```

## SSH password or identity file

Recommended key-based auth:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
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
user = "btrbk-source"
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
ts-btrfs sync --config ./config.toml --dry-run
ts-btrfs sync --config ./config.toml --run --limit 1
```

What the commands do:

- `test-ssh` verifies SSH and minimal source sudo.
- `list-source` parses Timeshift snapshots and Btrfs subvolumes.
- `sync --dry-run` prints the plan without writing data.
- `sync --run --limit 1` performs one real subvolume transfer for safe testing.

## Changelog

### 0.0.9

- Corrected project version number from the over-large experimental `0.4.0`.
- Added more explanatory comments/docstrings around functions, commands, config sections, and performance options.
- Added `VERSIONING.md` explaining the zip count and corrected version sequence.

## Disclaimer

Test with throwaway data first. You are responsible for verifying that backups
and restores work on your systems.
