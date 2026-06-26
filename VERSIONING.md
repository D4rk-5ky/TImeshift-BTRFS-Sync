# Versioning

This build is version `0.8.6`.

## Changelog

### 0.8.6

- Fixed retention-based source cache cleanup to pre-check which cache subvolumes still exist before deleting.
- Missing source cache paths are now skipped cleanly instead of producing noisy Btrfs delete/list errors.
- Removed duplicate stderr printing from cache cleanup failures; command stderr is already emitted by the command runner.

### 0.8.5

- Changed source cache cleanup to retention-based cleanup. `sync` now keeps every read-only cache snapshot it creates, and `prune` deletes matching source cache snapshots only for destination snapshots selected by the same retention delete plan.
- This preserves more common Btrfs UUID ground when short-lived snapshots are removed later.

### 0.8.4

- Refreshed `COMMENTED_CODE_MAP.md` to document only current commands, classes, and functions, and to add concise notes explaining safety-driven code paths.

### 0.8.3

- Added light CLI parser helpers for subparser creation, shared `--config`, shared run-mode flags, and shared delete-confirmation flags.
- Preserved command-specific help output and command flag visibility.

### 0.8.2

- Added shared `tags_text()` display helper and removed duplicate `_tags_text()` formatting helpers from sync/prune paths.

### 0.8.1

- Refactored safer config parsing patterns with shared table, optional-string, positive-integer, stripped-string, boolean, and integer helpers.
- Kept password/password_file pair validation explicit for a later, more focused refactor.

### 0.8.0

- Version-only bump from 0.7.10.

### 0.7.10

- Refactored pipeline stream reader setup into one compact stream-routing table.
- Preserved successful btrfs/mbuffer stderr routing to `.btrfs`/`.mbuffer` without polluting `.err`.

### 0.7.9

- Shared state metadata refresh/report/save logic between sync and prune without changing send/receive, retention, or parent UUID behavior.

### 0.7.3

- Consolidated source cache listing helpers around `remote_list_child_subvolumes`, `remote_cache_contains`, and cache child display formatting.
- Removed older overlapping cache list parsing/existence helpers while keeping the same cache parent cleanup behavior.

### 0.7.2

- Consolidated parent/source UUID matching into one shared helper, `match_source_path_to_destination_received_uuid` internally, so parent selection and sync-floor validation use the same Btrfs identity rule.
- Removed older overlapping helper code for parent/source UUID checks while keeping the same strict behavior: source path UUID must match destination received UUID or trusted state UUID history before it can be used.

### 0.6.11

- Reused one parsed Timeshift source snapshot index per sync stage.
- Manual snapshot creation still re-reads `timeshift --list` after creating a new snapshot, but metadata refresh, manual identity checks, sync-floor checks, parent selection, and the sync loop now share the same source index for that stage.

### 0.6.8

- Fixed successful transfer pipeline stderr handling. Successful `btrfs send` status lines such as `At subvol ...` and mbuffer progress no longer make `.err` non-empty.
- Transfer stderr is now buffered and copied to `.err` only if the send/mbuffer/receive pipeline fails.
- Btrfs transfer status is still written to `.btrfs`, and mbuffer progress is still written to `.mbuffer`.

### 0.6.7

- Added a separate `.succes` run log for readable sync and retention statistics.
- Sync summaries and retention delete plans are written to `.succes` and still shown in the terminal, instead of being mixed into the normal `.log` file.
- Email notifications use non-empty `.succes` text as the plain-text message body when present.
- Email log attachments are conditional and include only non-empty `.log`, `.err`, `.btrfs`, `.mbuffer`, and `.succes` files.
- Renamed the Btrfs verbose-output log suffix from `.btrfs-out` to `.btrfs`.

### 0.6.5

- Removed legacy config-option compatibility checks from the runtime config loader.
- Unknown removed config keys are no longer handled by special warning/error branches. The loader now only parses the active configuration needed by current functionality.
- Removed stale documentation text describing those old compatibility warnings.

### 0.6.4

- Destination retention uses only native Timeshift tags: H, D, W, M, B, and O.
- Non-native retention categories are not part of the active config model, examples, embedded `init-config` output, docs, or retention tag map.

### 0.6.3

- Added strict incremental parent source-path selection. For an existing destination parent, the app checks the saved state `send_path` first and requires its current source UUID to match the destination `received_uuid`.
- If the saved `send_path` is missing or does not match, the app tries the original Timeshift source path and only uses it if its UUID matches the destination `received_uuid`.
- Parent selection never creates a replacement cache snapshot. A recreated cache snapshot has a new UUID and cannot be a valid parent for an already received destination snapshot.

### 0.6.2

- Refreshed mutable Timeshift metadata in `state.json` from the latest `timeshift --list` during sync. Existing synced snapshots update `tags`, `comment`, `created`, and top-level target-relative `path` without re-sending data or changing UUID/parent/send identity fields.
- Improved Timeshift tag parsing so separated tag tokens such as `B H D W M` are recognized as tags instead of partly becoming the comment.

### 0.6.1

- Clarified and guarded the automatic manual/on-demand snapshot flow: the app may create a source-side Timeshift snapshot before syncing, but it never sends that snapshot directly or as a special priority target.
- After creating a manual snapshot, sync re-reads `timeshift --list`, reports newly detected snapshot names, and sends them only through the normal oldest-to-newest sync loop.

### 0.6.0

- Version-only bump.
