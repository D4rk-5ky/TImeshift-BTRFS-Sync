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
24th zip -> 0.2.4
25th zip -> 0.2.5
26th zip -> 0.2.6
28th zip -> 0.2.9
29th zip -> 0.2.10
```

This build is the 29th zip in the corrected sequence, so its version is `0.2.10`.

The next zip should be `0.3.0` or `0.2.10`, depending on whether the next change is a larger feature or a patch.

## 0.2.9

- Stopped trying to set destination compression on read-only received subvolumes.
- Changed `destination.set_compression_after_receive` default to `false`.
- If after-receive compression is explicitly enabled, read-only received subvolumes are detected and skipped safely.
- Added prune-safe high-watermark sync: after pruning old destination snapshots, normal sync uses the newest UUID-confirmed state/source match as a floor and skips older source snapshots instead of re-sending them.
- If the newest state snapshot is not present on the source, the app walks backward in `state.json` until it finds a source snapshot that exists and matches by Btrfs UUID.
- New state entries store both `original_source_uuid` and `send_source_uuid`, so writable Timeshift snapshots sent through read-only cache can be verified correctly later.

## 0.2.6

- Added optional MQTT status notifications using `paho-mqtt`.
- Added `timeshift_btrfs_sync/mqtt.py`; all MQTT publishing logic lives there.
- Added `[mqtt]` config section with optional username/password/password_file.
- Added Home-Assistant-friendly JSON payloads for success and failure.
- Failure JSON includes exit code, error text, and latest captured stderr tail.
- Added optional dependency extra: `python3 -m pip install -e '.[mqtt]'.`

## 0.2.5

- Mirrored captured command stderr to the terminal, while suppressing expected probe stderr.
- Added `destination.cleanup_incomplete_receive = true` to recover from interrupted receives.
- Automatically deletes incomplete destination Btrfs subvolumes that are not recorded in state.json, then retries the transfer.
- Added a separator after superseded source cache cleanup before the next send/receive block.
- Suppressed expected `Directory not empty` stderr when trying to delete a cache parent that still contains another cached subvolume.

## 0.2.4

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

## 0.2.9 - Home Assistant MQTT template docs fix

- Added a safer Home Assistant MQTT + Pushover automation example.
- Fixed the example trigger syntax by placing `topic` directly under `trigger: mqtt`.
- Added fallback handling for manual **Run actions** tests where `trigger.payload_json` does not exist.
- Added success and failure MQTT test payload examples for Home Assistant Developer Tools.

## 0.2.10 - Home Assistant YAML block/parser fix

Docs-only update.

- Replaced the Home Assistant MQTT/Pushover example with a parser-friendlier YAML block.
- Uses quoted one-line `description:` instead of a folded multiline description.
- Uses the older singular `trigger:` / `action:` syntax for compatibility with more Home Assistant YAML editors.
- Keeps the MQTT JSON fallback logic for manual "Run actions" tests.

## 0.2.9 - Home Assistant YAML indentation fix

- Fixed indentation and structure of the README Home Assistant MQTT Pushover automation example.
- Reworked template variables so manual action tests do not fail when `trigger.payload_json` is absent.
