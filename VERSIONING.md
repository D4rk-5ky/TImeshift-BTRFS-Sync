# Versioning

This build is version `0.6.5`.

## Changelog

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
