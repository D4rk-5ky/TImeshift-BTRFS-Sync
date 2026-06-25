# Installation

This project runs on the backup/destination machine. The source machine is reached over SSH and only needs `btrfs`, `timeshift`, SSH access, and the minimal sudo rules described in the README.

The executable or Python install does **not** include system tools such as `btrfs`, `timeshift`, `ssh`, `sudo`, `mbuffer`, or `sshpass`. Those must be installed on the relevant machines.

## System packages

On a Debian/Ubuntu style destination machine, install the tools you plan to use:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip openssh-client btrfs-progs
```

Optional tools:

```bash
sudo apt install mbuffer
sudo apt install sshpass
```

Use `mbuffer` only if `[stream].use_mbuffer = true`. Use `sshpass` only if `[ssh].password` or `[ssh].password_file` is configured. Key-based SSH is recommended.

## Install from source in a virtual environment

From the project folder:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
ts-btrfs --version
```

With MQTT notification support:

```bash
python3 -m pip install -e '.[mqtt]'
```

Email notifications use Python standard library modules, so they do not need an extra Python package.

## Create a config

Copy the example config and edit it:

```bash
cp config.example.toml config.toml
nano config.toml
```

Or generate it with the installed command:

```bash
ts-btrfs init-config --path ./config.toml
```

Recommended first checks:

```bash
ts-btrfs test-ssh --config ./config.toml
ts-btrfs list-source --config ./config.toml
ts-btrfs sync --config ./config.toml --dry-run
ts-btrfs sync --config ./config.toml --run --limit 1
```

## PyInstaller builds

PyInstaller can create a Linux executable for the machine/distro where the build is run. Build on the same OS family and CPU architecture where you expect to run the executable.

PyInstaller bundles the Python app. It does **not** bundle external system commands. The destination still needs commands like `btrfs`, `ssh`, `sudo`, optional `mbuffer`, and optional `sshpass` installed. The source still needs `timeshift` and `btrfs`.

### Install PyInstaller build dependency

From the project folder:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[pyinstaller]'
```

If MQTT support should work inside the executable, install both extras before building:

```bash
python3 -m pip install -e '.[mqtt,pyinstaller]'
```

### Folder-style executable

This creates `dist/ts-btrfs/ts-btrfs` plus supporting files in the same folder.

```bash
python3 scripts/build_pyinstaller.py --mode onedir
./dist/ts-btrfs/ts-btrfs --version
```

Use this when you want easier inspection/debugging and usually faster startup. Copy the whole `dist/ts-btrfs/` folder to the destination machine.

### One-file executable

This creates one executable at `dist/ts-btrfs`.

```bash
python3 scripts/build_pyinstaller.py --mode onefile
./dist/ts-btrfs --version
```

Use this when you want one file that is easy to copy. Startup can be slower because the executable extracts itself to a temporary directory when it starts.

### Build both formats

```bash
python3 scripts/build_pyinstaller.py --mode both
```

With MQTT support included:

```bash
python3 scripts/build_pyinstaller.py --mode both --with-mqtt
```

### Direct PyInstaller commands

The helper script above is recommended, but these are the direct commands it wraps.

Folder-style executable:

```bash
python3 -m PyInstaller --clean --name ts-btrfs --console --paths . tools/pyinstaller_entry.py
```

One-file executable:

```bash
python3 -m PyInstaller --clean --onefile --name ts-btrfs --console --paths . tools/pyinstaller_entry.py
```

With MQTT support, add:

```bash
--hidden-import paho.mqtt.client
```

### Running the executable

The executable uses the same config file and command flags as the Python module.

Folder-style build:

```bash
./dist/ts-btrfs/ts-btrfs test-ssh --config ./config.toml
./dist/ts-btrfs/ts-btrfs sync --config ./config.toml --dry-run
```

One-file build:

```bash
./dist/ts-btrfs test-ssh --config ./config.toml
./dist/ts-btrfs sync --config ./config.toml --dry-run
```

### Cleanup build artifacts

PyInstaller creates `build/`, `dist/`, and a `.spec` file. Remove them when you want a clean rebuild:

```bash
rm -rf build dist ts-btrfs.spec
```
