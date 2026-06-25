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
39th zip -> 0.2.19
40th zip -> 0.2.20
41st zip -> 0.4.1
42nd zip -> 0.4.2
```

This build is version `0.4.2`.

The version line was intentionally bumped to `0.4.0` at user request. Normal patch releases now continue from the 0.4.x line.


## 0.4.2 - Safe config defaults

- Updated `config.example.toml` and `init-config` output to the user-provided safe default baseline.
- Kept destructive actions guarded by dry-run and `--yes-delete`; normal on-demand cleanup remains disabled unless explicitly enabled.

## 0.4.1 - Mail attachment empty-file guard

- Mail notifications now skip log attachments whose file size is 0 bytes.
- This avoids mail client/download problems with empty `.err`, `.mbuffer`, or `.btrfs-out` files.
- Skipped empty files are listed in the email body under "Log files not attached".

## 0.4.1 - Version bump

- Version bump from `0.2.20` to `0.4.1`.
- No functional changes from `0.2.20`; this is the same mail-log-attachment build under the new version number.

## 0.2.20 - Mail log attachments

- Added optional email attachments for split run log files.
- When top-level `log_dir` is enabled and `[mail].attach_logs = true`, mail can attach `.log`, `.err`, `.mbuffer`, and `.btrfs-out` files from the current run.
- Added `[mail].max_attachment_bytes` as an optional per-file size cap. `0` means no cap.

## 0.2.19 - Mail notifications

- Added optional email status notifications using Python standard library `smtplib` and `email.message`.
- Added `timeshift_btrfs_sync/mail.py` so mail logic is isolated.

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

- Manual Timeshift snapshot creation uses readable remote-safe double-quote escaping for the `--comments` value.

## 0.2.14 - Verified manual snapshot creation

- Added `manual_snapshot.require_verified_source`, default `true`.
- Automatic manual snapshot creation runs `timeshift --list` first and verifies the configured source against `state.json` with Btrfs UUID metadata before creating a new Timeshift snapshot.

## 0.2.13 - Independent on-demand retention

- Added independent cleanup controls for app-created and normal/user-created on-demand snapshots.

## 0.2.12 - Manual snapshot config

- Added `[manual_snapshot]` config section.
- `sync --run` can create a source Timeshift on-demand snapshot before sync.
