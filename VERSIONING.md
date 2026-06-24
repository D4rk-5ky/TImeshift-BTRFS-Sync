# Versioning

The early experimental artifacts jumped version numbers too quickly. From the
commented/performance build onward, artifact versions are counted by zip number.

Corrected sequence:

```text
9th zip  -> 0.0.9
10th zip -> 0.1.0
11th zip -> 0.1.1
12th zip -> 0.1.2
13th zip -> 0.1.3
14th zip -> 0.1.4
15th zip -> 0.1.5
16th zip -> 0.1.6
17th zip -> 0.1.7
18th zip -> 0.1.8
19th zip -> 0.1.9
20th zip -> 0.2.0
21st zip -> 0.2.1
22nd zip -> 0.2.2
23rd zip -> 0.2.3
24th zip -> 0.2.5
```

This build is the 24th zip in the corrected sequence, so its version is `0.2.5`.

The next zip should be `0.2.5`.

## 0.2.5

- Audited command flags, config options, README documentation, and `config.example.toml`.
- Expanded argparse `--help` text for every command flag.
- Added `state_file` and `lock_file` to `config.example.toml` and README config reference.
- Updated `init-config` so it writes the same complete example config as `config.example.toml`.
- Added `CONFIG_AND_CLI_AUDIT.md` with the audit checklist.

## 0.2.3

- Documentation-only update.
- Added README and config comments explaining that `prune_after_sync = true` or `--prune` only enables the prune step.
- Clarified that real deletion still requires both `--run` and `--yes-delete`.

## 0.2.2

- Split the old combined `.out` transfer log into `.mbuffer` and `.btrfs-out`.
- `.mbuffer` stores mbuffer progress/summary and the transfer command header.
- `.btrfs-out` stores Btrfs send/receive verbose output and send/receive commands.

## 0.2.1

- Fixed mbuffer live progress output after Btrfs verbose/logging support.
- Stream readers now use chunked `os.read()` so carriage-return progress lines are not hidden until transfer end.

## 0.2.0

- Added optional split logging with `.log`, `.out`, and `.err` files.
- Added `timeshift_btrfs_sync/log.py`; all file logging logic lives there.
- `log_dir` controls file logging and creates the directory automatically.
