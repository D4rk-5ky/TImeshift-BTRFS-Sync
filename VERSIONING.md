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
27th zip -> 0.2.7
28th zip -> 0.2.8
29th zip -> 0.2.9
30th zip -> 0.2.10
31st zip -> 0.2.11
32nd zip -> 0.2.12
33rd zip -> 0.2.13
34th zip -> 0.2.14
35th zip -> 0.2.15
36th zip -> 0.2.16
37th zip -> 0.2.17
38th zip -> 0.2.18
```

This build is version `0.2.18`.

The next zip should normally be `0.2.19` for a patch or `0.3.0` for a larger feature.


## 0.2.18 - README warning/disclaimer order

Docs-only update.

- Reordered the README front matter as project name, AI-assisted warning, disclaimer, data-loss warning, and license.
- Expanded the disclaimer and data-loss warning.
- Added a top-level MIT license section pointing to `LICENSE`.

## 0.2.17 - Timeshift on-demand tag workaround

- Manual snapshot creation no longer passes explicit `--tags O`.
- Timeshift defaults manual creates to on-demand/tag `O`, and some versions reject explicit `O` despite listing it as valid.
- The generated command is now `timeshift --create --scripted --comments <comment>`.

## 0.2.16 - Cleaner manual snapshot comment quoting

- Manual Timeshift snapshot creation now quotes the `--comments` value with remote-safe double quotes.
- This keeps logged SSH commands readable and avoids the noisy nested single-quote escape pattern.
- The quoting still escapes characters that are special inside remote shell double quotes, including double quotes, `$`, backticks, backslashes, and line breaks.

## 0.2.14 - Verified manual snapshot source guard

- Added `manual_snapshot.require_verified_source`, default `true`.
- Before automatic manual snapshot creation, the app now reads `timeshift --list` first.
- The configured source must match existing `state.json` history by Btrfs UUID / destination received_uuid before Timeshift is asked to create a new tag `O` snapshot.
- If the newest state snapshot is not present on the source, the app walks backward in state until it finds a UUID-confirmed source anchor.
- If no trusted source anchor exists, the app refuses to create a manual snapshot to avoid writing to the wrong mounted OS/source.

## 0.2.13 - Independent on-demand retention controls

- Split app-created on-demand cleanup from normal/user-created Timeshift on-demand cleanup.
- Added `manual_snapshot.cleanup_enabled` for pruning only app-created tag `O` snapshots recognized by the configured marker.
- Added `retention.cleanup_ondemand` for pruning normal/user-created Timeshift tag `O` snapshots.
- Default safety behavior keeps normal/user-created on-demand snapshots unless `cleanup_ondemand = true`.

## 0.2.12 - Manual snapshot config

- Added optional `[manual_snapshot]` config section.
- `sync --run` can create a source Timeshift tag `O` snapshot before source discovery.
- Created snapshots use a configurable comment and marker.
- Added marker-based retention for app-created on-demand snapshots, default count 10.

## 0.2.11 - Home Assistant UI YAML style fix

Docs-only update.

- Restored the Home Assistant MQTT/Pushover example to the UI/exported automation style using `triggers:`, `actions:`, and MQTT `options:`.
- Removed the legacy singular `trigger:` / `action:` example from the README.
- Kept the note that `trigger.payload_json` requires a real MQTT JSON trigger; manual **Run actions** tests may not provide it.

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

## 0.2.16 - Manual snapshot create diagnostics

This build improves failure visibility for source Timeshift manual snapshot
creation.

If `timeshift --create` exits non-zero and Timeshift writes the useful reason to
stdout instead of stderr, the app now mirrors that stdout to the terminal and
includes both stdout and stderr in the raised command error. This is especially
useful for debugging source-side Timeshift failures where the SSH command itself
works, the source identity check passes, but Timeshift refuses to create the
snapshot.
