# timeshift-btrfs-sync

`timeshift-btrfs-sync` pulls Timeshift Btrfs snapshots from a source machine to a Btrfs backup destination over SSH.

This version uses the **minimal source sudo** model:

- no source-side helper script
- no source-side Python package
- no source-side `sudo mkdir`
- no source-side `sudo cat`
- no source-side `sudo find`
- no source-side root shell script
- source-side passwordless sudo only for `timeshift` and `btrfs`

The backup/destination side orchestrates the job over SSH, similar in spirit to tools like btrbk/syncoid.

## Source sudoers

On the source, create a dedicated SSH user, for example `btrbk-source`, then allow only Timeshift and Btrfs:

```sudoers
# edit with: sudo visudo -f /etc/sudoers.d/ts-btrfs-source
btrbk-source ALL=(root) NOPASSWD: /usr/bin/btrfs *
btrbk-source ALL=(root) NOPASSWD: /usr/bin/timeshift *
```

What those sudoers lines mean:

- `/usr/bin/timeshift *` allows snapshot listing and manual snapshot creation.
- `/usr/bin/btrfs *` allows metadata reads, read-only cache snapshots, send streams, and cache parent subvolume creation.
- It does **not** directly allow `sudo mkdir`, `sudo cat`, `sudo find`, `sudo python`, or a source-side script.

Check command paths on the source with:

```bash
command -v btrfs
command -v timeshift
```

## How discovery works

The destination runs this on the source:

```bash
ssh source 'sudo -n timeshift --list'
```

What it does:

- SSH connects to the source.
- `sudo -n` uses sudo without prompting for a password.
- `timeshift --list` prints known Timeshift snapshots.
- The app parses snapshot names and tags from that output.

Then the app constructs paths like:

```text
/timeshift-btrfs/snapshots/2026-06-22_18-00-01/@
/timeshift-btrfs/snapshots/2026-06-22_18-00-01/@home
```

It verifies each configured subvolume with:

```bash
sudo -n btrfs subvolume show <path>
sudo -n btrfs property get -ts <path> ro
```

What those do:

- `btrfs subvolume show <path>` confirms the path is a Btrfs subvolume and reads UUID metadata.
- `btrfs property get -ts <path> ro` checks whether the subvolume is read-only.
- `ro=true` means it can be sent directly with `btrfs send`.
- `ro=false` means the app needs a read-only cache snapshot unless cache creation is disabled.

## Writable Timeshift snapshots and cache layout

`btrfs send` needs read-only subvolumes. If the original Timeshift snapshot is writable, the app can create a read-only source cache using only `btrfs`.

Create the top-level cache root manually once on the source:

```bash
sudo mkdir -p /timeshift-btrfs/.ts-btrfs-sync/send-cache
sudo chmod 700 /timeshift-btrfs/.ts-btrfs-sync/send-cache
```

The app does not run those commands. They are a one-time admin setup step.

During sync, the app creates the per-snapshot parent with Btrfs, not mkdir:

```bash
sudo -n btrfs subvolume create /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01
```

What it does:

- Creates a Btrfs subvolume used as a recognizable per-snapshot cache folder.
- Keeps the layout close to Timeshift's normal snapshot layout.
- Still only requires source-side passwordless `btrfs`.

Then the app creates read-only cache snapshots:

```bash
sudo -n btrfs subvolume snapshot -r \
  /timeshift-btrfs/snapshots/2026-06-22_18-00-01/@ \
  /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
```

and:

```bash
sudo -n btrfs subvolume snapshot -r \
  /timeshift-btrfs/snapshots/2026-06-22_18-00-01/@home \
  /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@home
```

What `btrfs subvolume snapshot -r` does:

- Creates a read-only snapshot of the original writable snapshot.
- Leaves the original Timeshift snapshot untouched.
- Provides a valid source path for `btrfs send`.

Resulting source cache layout:

```text
/timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
/timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@home
```

To require source snapshots to already be read-only, disable cache creation:

```toml
[source]
create_readonly_cache = false
```

## Install destination app

On the backup/destination machine:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

What those commands do:

- `python3 -m venv .venv` creates a local Python virtual environment.
- `. .venv/bin/activate` activates it for the current shell.
- `pip install -e .` installs the project in editable mode so the `ts-btrfs` command is available.

Check the app:

```bash
ts-btrfs --version
```

## Create config

```bash
ts-btrfs init-config --path ./kubuntu.toml
nano ./kubuntu.toml
```

What those commands do:

- `init-config` writes a commented starting config.
- `nano` opens it for editing.

Important fields:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"

# Recommended: key-based auth. Store the key path in TOML.
identity_file = "/root/.ssh/timeshift-btrfs-sync"

# Optional: password auth through sshpass on the destination.
# Use either password OR password_file, not both.
# password = "your-ssh-password"
# password_file = "/root/.ssh/timeshift-btrfs-sync.password"

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


## SSH authentication options

The `[ssh]` section supports both private keys and password authentication.

Recommended key-based auth:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
identity_file = "/root/.ssh/timeshift-btrfs-sync"
extra_args = ["-o", "BatchMode=yes"]
```

What it does:

- `identity_file` tells SSH which private key to use.
- `BatchMode=yes` makes SSH fail instead of hanging on a prompt.
- This is the best option for systemd timers and unattended backup jobs.

Optional password stored directly in TOML:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
password = "your-ssh-password"
extra_args = ["-o", "StrictHostKeyChecking=accept-new"]
```

Slightly safer password stored in a separate file:

```toml
[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
password_file = "/root/.ssh/timeshift-btrfs-sync.password"
extra_args = ["-o", "StrictHostKeyChecking=accept-new"]
```

What password mode does:

- Uses `sshpass -e ssh ...` on the destination machine.
- Passes the password through the `SSHPASS` environment variable, not as a command-line argument.
- Requires `sshpass` installed on the destination machine.
- Must not be used with `BatchMode=yes`, because that disables password prompting.

Install sshpass on Debian/Ubuntu/Kubuntu if you choose password mode:

```bash
sudo apt install sshpass
```

Password mode is convenient, but key-based auth is still safer. If you use `password_file`, protect it like this:

```bash
sudo chmod 600 /root/.ssh/timeshift-btrfs-sync.password
```

## Usage

Test SSH and minimal source sudo:

```bash
ts-btrfs test-ssh --config ./kubuntu.toml
```

What it does:

- Verifies SSH connectivity.
- Runs `sudo -n timeshift --list` on the source.
- Runs `sudo -n btrfs --version` on the source.

List source snapshots:

```bash
ts-btrfs list-source --config ./kubuntu.toml
```

What it does:

- Parses `timeshift --list`.
- Checks configured subvolumes with `btrfs subvolume show`.
- Prints snapshot name, tags, subvolumes, and comments.

Dry-run sync:

```bash
ts-btrfs sync --config ./kubuntu.toml --dry-run
```

What it does:

- Shows what would be sent.
- Shows whether each send would be full or incremental.
- Does not create cache snapshots.
- Does not receive or delete anything.

First real sync, limited to one subvolume:

```bash
ts-btrfs sync --config ./kubuntu.toml --run --limit 1
```

What it does:

- Performs only one subvolume transfer.
- Good first live test before syncing everything.

Full real sync:

```bash
ts-btrfs sync --config ./kubuntu.toml --run
```

Create a Timeshift on-demand/manual snapshot on the source:

```bash
ts-btrfs create-manual --config ./kubuntu.toml --comment "Before upgrade"
```

What it does:

- Runs `sudo -n timeshift --create --scripted --tags O --comments ...` on the source.
- The `O` tag means on-demand/manual.

Prune destination backups with dry-run first:

```bash
ts-btrfs prune --config ./kubuntu.toml --dry-run
```

Real prune requires explicit delete confirmation:

```bash
ts-btrfs prune --config ./kubuntu.toml --run --yes-delete
```

What pruning does:

- Applies retention rules only on the destination backup store.
- Deletes old received Btrfs subvolumes with local `btrfs subvolume delete`.
- Does not delete source Timeshift snapshots.

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

### 0.3.3

- Added optional SSH password authentication using `sshpass -e`.
- Added `ssh.password` and `ssh.password_file` TOML options.
- Kept `ssh.identity_file` support and documented it as the recommended method.
- Added environment handling so streamed `btrfs send` also works with password auth.

### 0.3.2

- Added detailed comments/docstrings throughout the Python source.
- Added explanations to the example config, sudoers file, systemd units, and README command examples.
- No intended behavior change from 0.3.1.

### 0.3.1

- Changed the source read-only send-cache from a flat name layout to a Timeshift-like layout.
- Per-snapshot cache parents are created with `btrfs subvolume create`, not `mkdir`.
- Source-side sudo remains limited to `btrfs` and `timeshift`.

### 0.3.0

- Removed the source helper design.
- Source-side passwordless sudo reduced to `btrfs` and `timeshift`.
