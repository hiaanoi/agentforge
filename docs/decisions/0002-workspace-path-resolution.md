# ADR 0002: Resolve paths before workspace containment checks

## Status

Accepted for Milestone 2.

## Decision

Reject absolute, drive-qualified, and UNC input; normalize both path separators; resolve the
candidate; then compare normalized workspace and candidate paths with `commonpath`. Verify the
requested file type only after containment. Repository traversal does not follow symlink
directories and skips symlink entries. Explicit internal symlinks are canonicalized, while any
external final target is denied.

## Consequences

String tricks, `..`, Windows drive changes, case differences, symlinks, and junctions cannot
bypass the workspace boundary. Canonical output may use a symlink target's relative path rather
than the user-supplied alias.
