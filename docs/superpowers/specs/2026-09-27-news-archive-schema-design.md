# News archive schema and transaction design

The archive combines a seven-day article cache with durable Telegram notification state. A schema change must never silently discard pending or sent state. This change keeps the current v3 JSON format and single-writer ownership, adds explicit handling for known legacy input, and refuses unknown or corrupt input without modifying the original file.

## Data boundary

- Current v3 and unversioned legacy files load with all notification fields preserved.
- The known broad-RSS v2 archive can move to v3 only after a byte-exact local backup. Its articles remain invalidated under the existing collection policy. The v2 initialization flag resets so the first new collection establishes a baseline without flooding alerts; v2 did not contain an outbox. Any unexpected durable notification fields are preserved when representable or migration is refused.
- Unknown schema versions, malformed JSON, and invalid top-level values stop read-modify-write operations. The original file remains untouched, and an actionable error is logged without including article content.
- Existing malformed outbox entries continue to be ignored with a warning under the current compatibility contract; an invalid pending event can therefore be lost. Archive errors are logged, but the scheduler's collection status and `/health` do not currently expose them.
- Article expiry remains seven days. Notification outbox, digest timestamps, and initialization state are not treated as regenerable cache.

## Writer and rollback boundary

- The crawler retains its existing in-process read-modify-write lock and atomic replacement. The host maintenance pruner remains outside this change because the repository change harness blocks `scripts/maintenance.py`; its concurrent or in-place write behavior is a known unresolved risk. Whether it targets the same live path must be checked before deployment.
- Migration backup is created before the first rewrite and verified against the bytes read. Existing backups are never overwritten.
- No current-schema bump or automatic downgrade is introduced. Rollback to an older image requires stopping the sole writer and restoring a compatible archive snapshot. N100 data and deployment are outside this implementation.

## Verification

Tests cover v3/legacy field preservation, known v2 migration and backup, unsupported/corrupt fail-closed behavior, and unchanged notification retry behavior. Local tests do not establish the schema or backup state of the live PVC or safety against the host pruner.
