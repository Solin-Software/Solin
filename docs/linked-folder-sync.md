# Linked-folder collaboration

Meetings and linked playlists exchange immutable operations through the linked
folder. The cloud provider transports files; it does not provide a transaction,
a lock shared by all computers, or a reliable signal that synchronization has
finished. Convergence requires eventual delivery of the same valid operations
to every terminal.

## Shared state and conflict rules

Each linked document has a journal at
`.solin_sync/<namespace>/operations/<operation-sha256>.json`. The namespaces are
`meeting` and `playlist`. Protocol version 1 operations contain their namespace,
document identity, content-derived ID, causal dependencies, and either a migration baseline or an
edit. Edits identify their actor and carry field patches, removed fields,
occurrence deletions, and explicit restorations.

An edit is calculated against the state actually presented to its author.
Receiving another terminal's state does not itself create an edit. Operations
with missing dependencies wait until those dependencies arrive. Repeated files
are idempotent. Invalid JSON, failed integrity checks, unsupported versions, and
incompatible migration baselines are errors, not empty documents.

- Causally later field edits replace earlier edits. Concurrent changes to
  different fields survive; concurrent writers of one field use a stable
  operation-ID tie break. Wall-clock timestamps do not choose winners.
- Deletion survives concurrent edits and moves of the same occurrence. A
  deliberate new inclusion has a new occurrence identity. An explicit canonical
  restoration must acknowledge the deletion it restores.
- Tree projection assigns one parent to each occurrence. Sibling ordering uses
  fractional positions with stable ID tie breaks. Concurrent cycles are broken
  deterministically. Children whose parent disappeared use the nearest surviving
  ancestor recorded in their placement.
- Resource identity and occurrence identity are distinct. Multiple intentional
  occurrences may reference one file.

Local state lives below the application's data directory in
`linked_sync/documents/<namespace>/<document-id>/`. `operations/` archives
accepted operations and `outbox/` retains edits awaiting publication. Playlist
`intents/` retains UI edits before background media copying and publication.
Local paths identify this device's cache only; shared resource references use
portable relative paths or HTTP(S) URLs.

Immutable document descriptors live in
`.solin_sync/<namespace>/documents/<document-id>.json`. Folder bindings live
locally in `linked_sync/bindings/`; renaming a linked playlist through the app preserves its
storage identity and pending work. Explicit deletion retires the binding before
physical removal. Recreating that path therefore does not reuse its previous
document's operations.

Meeting sync has one additional authority file:
`.solin_sync/meeting/active.json`. Its existence means that the meeting is
linked, and its immutable `document_id` selects the only journal generation that
may be read or written. The marker contains the meeting identity, but no mutable
`enabled` flag. Operations, descriptors, resource archives, and advisory
snapshots can arrive late and never activate sync by themselves. A partial or
blocked marker is treated as unavailable transport, not as a disable.

An unknown folder initially has only local provisional observations and
intentions, stored under
`linked_sync/replicas/<local-storage-id>/provisional/<local-generation>/`. Discovering a file
does not initialize an authoritative document. Enabling meeting synchronization,
an explicit playlist save, or migration establishes the document identity.
Identical migration baselines establish the same identity deterministically.
An existing descriptor arriving later adopts the provisional operations with a
durable operation-ID translation, so already-rendered views remain valid causal
baselines. Unrelated simultaneous initializations require explicit resolution.

The linked playlist's **Reset synchronization** action establishes a new document
identity that supersedes the known previous identities, preserving the last
validated local content as the new baseline. Old history is retained separately, and stale
edits are rejected instead of being applied to the replacement. Missing metadata
never implies a reset. Replacement through Explorer requires an explicit reset
when a previously linked playlist path is intended to represent a new document.
Enabling a meeting publishes its initial journal before publishing `active.json`.
Disabling removes `active.json`, retires that local document generation, and then
removes the internal `.solin_sync` and `.solin_cache` trees. A later enable uses a
new document identity. Meetings do not expose the playlist reset action.

Local intent staging uses a separate lock from cloud reads and publication.
A provider waiting for hydration cannot hold the staging lock. Concurrent local
publishers archive an operation before removing its outbox copy; readers inspect
the outbox before the archive so that this transfer cannot hide accepted edits.

## Files arriving before organization

A file discovered directly in the folder has a deterministic automatic
occurrence. It appears in the meeting's default section, **Living as Christians**,
or at the end of the playlist while no explicit organization is known.

When organization from another terminal arrives for the same resource, that
explicit occurrence absorbs the automatic occurrence. Its intended section and
order appear without requiring a timeout, a failed save, or a second media
occurrence. Editing the automatic item records one intention register per field;
independent concurrent edits survive reconciliation. Subsequent edits of the
absorbed item update those same intentions, without copying a received projection
back into the original claim. A file that never receives explicit organization
continues to use its fallback position.

Removing an occurrence records durable resource suppression. Old bytes arriving
later cannot recreate an automatic occurrence. A deliberate new inclusion
acknowledges previously observed suppression. Repeated explicit occurrences
remain separate.

Meeting media records its portable size and SHA256 alongside the resource path.
A deletion suppresses that content version: different bytes copied to the same
name can be imported automatically, while previously deleted versions remain
blocked. Automatic meeting occurrences include the source content identity so
a replacement does not reuse a deleted occurrence. Pending removal and import
completion use the same content rule. This policy does not expire by time and
does not change linked playlist suppression.

Older meeting occurrences acquire their content identity when available media
is read in the background. A path-only deletion can be narrowed only when its
active document contains one complete, validated archived content version.
That version must match the occurrence or direct-source processing signature
in the causal view preceding its deletion. An archive from a cancelled copy
cannot establish that deletion's content identity.
Missing, incomplete, corrupt or ambiguous recovery evidence keeps that path
suppressed and preserves any visible replacement until the deleted version is
known. Binding is persisted in the journal and survives reopening.

Content comparison shares only file size and SHA256. Hashes are cached against
local filesystem revisions; inode, device, and timestamps are never shared as
content identity. Windows additionally checks the file's change time because
creation time cannot detect all same-size writes with a restored modification
time. On NTFS, its per-file update sequence number also distinguishes writes
within one clock tick. Windows filesystems without that signal are hashed on
each check rather than reusing a potentially stale hash. Hashing and directory
scans belong on background workers.

## Missing files and recoverable removal

An absent file in a synchronized document means unavailable media, not permission
to delete the occurrence or converted outputs. Its organization remains available
while transport catches up.

### Meeting folders with synchronization disabled

Turning off meeting synchronization preserves visible files and the local meeting
tree. The linked folder continues to accept new sources: midweek media appears in
**Living as Christians**, and weekend media appears in **Public Talk**. These
imports and their processing records are persisted locally; they do not activate
or publish a shared meeting journal.

A successful scan of an available meeting folder removes local manual occurrences
whose direct source files disappeared. It preserves official meeting content and
converted or extracted outputs when their original document disappears. An
unavailable root, a missing meeting folder, or a failed scan preserves the tree.

Removing a direct source through the UI accepts the local deletion before
attempting physical removal. If removal fails, the unchanged source stays
suppressed while the operation can be retried. Successful removal, or a later
scan observing that direct source's absence, clears its local processing record;
adding the same bytes again can therefore create a new local occurrence.

Activation discovery precedes source scanning. Detachment of shared cache files
occurs only when leaving active synchronization, and its result is accepted only
if the local tree has not changed during copying. Ordinary inactive refreshes
reconcile the current local tree instead of replacing it with an earlier copy.
Shared cache cleanup waits for successful local snapshot persistence and pending
insertions. Media prepared before deactivation finishes through local detachment;
late removals use the current activation marker and cannot remove a replacement
document's resources.

### Shared removal and recovery

Physical removal follows durable logical deletion. Retired resources are copied
and flushed into
`.solin_sync/resources/<document-id>/<resource-key-sha256>/<content-sha256>` with an accompanying
JSON metadata file before the visible source is removed. A deletion failure
leaves the original bytes and the recovery copy. Resource suppression prevents
reimport while cleanup retries.

Recovery is scoped to the active document, so reusing a folder path does not
restore another document's retired bytes. A playlist reset explicitly records
the previous archives needed by the content it preserves. Recoverable copies
made before any document identity exists omit the document-ID directory.

Recovery preserves a file that already exists and validates archived content
against its metadata. Metadata arriving before the archived bytes leaves the
resource unavailable. Multiple different archived versions require explicit
resolution; recovery does not select one by a machine-local timestamp. This
version has no automatic archive expiration or deletion-history compaction.
Cleanup and recovery errors are logged for retry; there is no dedicated conflict
resolution screen for archived content.

Cancelled meeting copies that were already visible are retained recoverably.
Cleanup preserves known active references; a later remote reference can recover
the archived bytes. Unpublished copy staging files are removed normally.

Before meeting deactivation, cache-backed references used by the local tree are
copied into durable application storage. The visible media files at the root of
the meeting folder remain in place. Generated cache files and that meeting's
shared journal and recovery archive are then removed. Cleanup failures are
retried, but the missing activation marker keeps the meeting in local mode while
residue remains.

Relative paths reject traversal, escaping symbolic links, Windows-reserved
names, and ambiguous case or Unicode filename collisions. Internal journal,
archive, and transaction staging files are excluded from media discovery.

## Migration and operational recovery

Update every collaborating terminal before using the new protocol. Older
versions do not understand the operation journal and must not continue writing
the same shared folder. There is no dual writer compatibility mode.

The local meeting cache preserves its linked folder, enabled state and causal
view. Reopening while the cloud folder is unavailable retains this linkage and
replays accepted local operations in the background, so edits continue into the
durable outbox. When the folder is accessible, a missing activation marker
switches the terminal back to local mode, cancels pending publication for that
generation, and scans visible media into the normal default sections.

Migration parses and backs up the same frozen bytes from one read, even if the
provider replaces the legacy manifest immediately afterward.
Playlist migration preserves the original manifest bytes under local
`migration/<manifest-sha256>.json` and imports the frozen manifest as an
idempotent baseline. Further playlist changes go through journal operations.
Meeting migration atomically preserves the original manifest bytes under
`.solin_sync/meeting/migration/<manifest-sha256>.json`, imports the old tree,
publishes `active.json`, removes `_solin_manifest.json`, and writes a schema
version 4 materialization to
`.solin_sync/meeting/snapshot.json`. The legacy meeting manifest is not maintained
as a second writable collaboration format. The snapshot is a view; operation
history defines collaboration state.

Different baseline contents are rejected rather than silently choosing one.
Keep both backups and all journal directories when investigating a conflict.
Resolve the intended baseline with all terminals stopped, then reconnect only
after their starting state is consistent. Playlist manifest deletion alone does
not erase retained journal history or local pending edits. For meetings,
`active.json` is the deliberate lifecycle boundary; deleting it deactivates the
meeting even if older journal files arrive afterward.

## Verification and performance

Automated tests use isolated replica directories and controlled file delivery
to cover delayed and repeated operations, stale edits, deletion, fallback
absorption, tree moves, migration, and filesystem failures. A simulator does not
replace validation against an actual cloud provider.

Run the local benchmark with:

```text
python scripts/benchmark_linked_folder_sync.py
```

It reports P50/P95 milliseconds for cold and unchanged reads, tree flattening and
reconstruction, single-field edits, and idle content-signature checks with
100, 1,000, and 10,000 entities over ten runs. Fixtures and replica state are
temporary. The idle hash counter should remain zero. Cached journal decoding and
materialization avoid repeated reconciliation when operation IDs are unchanged;
snapshot copying, directory checks, and tree projection still scale with document
size. The benchmark does not measure cloud latency or guarantee UI frame times.

A Windows development run on 2026-09-21 (10 samples per size, with other local
validation running) measured the following P50/P95 milliseconds. These are
observations, not performance budgets:

| Nodes | Cold read | Unchanged read | Single-field commit | Tree rebuild |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 14.9 / 23.7 | 8.7 / 10.2 | 43.5 / 54.7 | 1.0 / 2.1 |
| 1,000 | 54.0 / 65.1 | 13.3 / 25.1 | 73.5 / 93.2 | 17.1 / 25.1 |
| 10,000 | 565.8 / 628.4 | 63.2 / 208.8 | 449.4 / 492.2 | 165.2 / 181.2 |

All three size scenarios computed zero content hashes during their ten idle
checks. The 10,000-file signature check still took 2,108.8 / 2,729.0 ms because it opens files to validate local
revisions; these scans run outside the GUI thread.

Before release, perform this checklist on two independently synchronized
machines:

1. Open the same meeting and playlist on both machines; add media in a nondefault
   section, delete media, and edit or move another item concurrently.
2. Delay organization while delivering media first, then reverse delivery order.
   Verify the final section and order, with no extra occurrence.
3. Keep one machine offline during deletion, reopen it, and synchronize. Repeat
   open/close cycles and verify that item counts remain stable.
4. Pause the provider, lock a media file, and restart the application with pending
   edits. Resume transport and verify convergence and recoverable cleanup.
5. Exercise converted documents and intentional repeated media references;
   confirm that missing source files do not destroy their organization or cache.

The two-machine checklist must be recorded separately when performed. Automated
replica tests and the local benchmark do not establish that it has passed.
