# Meeting content lifecycle

`MeetingPreparationService` owns weekly preparation, saved trees, maintenance,
and optional audio/video prefetch. `PublicationContentRequest` distinguishes
prepare, check, update, and repair operations and selects the meeting publication.
The widget and destination picker use this coordinator rather than downloading
publications themselves.

## Preparation and maintenance

The profile's automatic-download preference controls initial preparation and
audio/video prefetch. When disabled, an absent meeting offers **Download**.
Selecting it prepares that meeting's structure, covers, and images without
prefetching audio or video. Navigation does not prepare absent meetings.
Audio/video authorization remains scoped to the requested publications through
metadata checks and updates. Downloads retain their own batch owners across
metadata generations; canceling one consumer preserves other consumers of the
same URL.

Saved meetings remain available offline. Maintenance checks remote checksums
for the parent publication and referenced publications. Observed checksums are
stored separately from the applied revision. Checks cannot download archives.
Explicit updates preserve operator edits through the existing canonical merge.
Preparation serves available cached content before remote revalidation. The
midweek base is available before referenced publications finish loading, and
each reference is loaded independently. Missing or malformed references cannot
prevent healthy references from loading.
Partial results merge healthy content while retaining official nodes whose
absence is not yet confirmed. Known canonical content and operator deletions
remain durable; the full canonical hash is cleared until preparation completes.
Normal maintenance retries incomplete content when automatic updates are
enabled; otherwise the existing Update action completes it manually. A complete
result reconciles removals and advances the applied revision and dependencies.
Unsynced saved trees reconnect missing image sources to available local canonical
images without changing content or operator edits. This also prevents pending
edits from overwriting recovered source bindings; active sync retains ownership
of its portable paths.
Image availability assessment and source normalization run in coalesced background
work, including before and after physical repair. UI state uses assessments for
the exact saved revision; controller recovery publishes the current eligible
snapshot even when no new write is needed. Delivery rejects retired contexts and
shutdown work. Publication failures are isolated, reported, and release pending
maintenance so a later request can retry. Post-repair assessment does not start
another physical repair.
Image repair checks the compressed local cache before downloading and accepts
only the saved image's SHA-256. If that revision cannot be restored, automatic
updates rebuild the saved meeting using the available publication, even without
evidence of a remote change. When automatic updates are disabled, the existing
Update action authorizes that rebuild.

The immutable `MeetingContentPolicy` reads these application feature flags from
`core/foundation/constants.py` at composition time:

| Flag | Default | Behavior |
| --- | --- | --- |
| `KEEP_MEETING_IMAGES_FOR_MANUAL_CLEANUP` | `False` | When enabled, retain images automatically and expose them in Library > Downloads for manual deletion. |
| `AUTO_RECOVER_PREPARED_MEETING_IMAGES` | `True` | Restore missing official images through normal meeting maintenance. When disabled, show a manual retry action. |
| `AUTO_UPDATE_PREPARED_MEETINGS` | `True` | Apply publication changes and rebuild saved meetings when exact image repair is unavailable. When disabled, expose a separate update action. |

These policies are independent of initial automatic preparation. A manual image
deletion invalidates availability; normal recovery may restore a referenced
image when enabled. Active projection and publication handoffs pin their files.
Manual deletion keeps a small metadata tombstone so repair preserves the saved
path even when another publication names identical bytes differently. Collection
reclaims that tombstone after its final reference disappears, including when
automatic image deletion is disabled.

## Storage and ownership

| Data | Owner and lifetime |
| --- | --- |
| Original extracted images | `PublicationAssetStore` in the shared data directory's `media_assets` folder. Immutable content addressed objects, deduplicated by SHA-256, with filename/title sidecars. |
| Compressed JWPUBs | Shared publication cache, with a 512 MiB budget and 30-day expiry. Least recently used entries are removed first; current/next-week dependencies are preferred. Active operations and unresolved legacy sources can temporarily exceed the budget. |
| Extracted databases and members | Scoped scratch directories, removed after materialization. Extraction is serialized per cache root. Validation and extraction each enforce a shared 2 GiB expanded-byte budget and 100,000-member limit across the outer and inner archives. |
| Meeting structures and provenance | Per-profile meeting store. Images record publication, language, issue, archive checksum, member, and expected image digest. |
| Audio and video | Existing shared HTTP media cache, with its existing manual deletion workflow. |

Automatic image collection retains references from every profile's visible,
canonical, and hidden meeting nodes, playlists, scene documents, effective
linked snapshots, pending journal work, and in-flight pins. Expiry uses the
same two-previous-week navigation boundary. An image shared by retained content
survives until its final consumer disappears. Imported local files are outside
this collector's ownership. Linked folders keep portable copies.

Startup validates all owners, migrates provable legacy image references before
removing extracts, and expires old trees before mutable profile repositories
are loaded. Unproven sources remain protected. During a session, the active
preparation coordinator expires its own trees; background maintenance expires
only unowned profiles and coalesces collection requests. Unreadable or malformed
owners defer collection and protect publication sources.

## Validation

Unit and integration coverage lives in the publication asset, archive/cache,
meeting content operation, preparation, lifecycle, destination, and projection
tests. The filesystem benchmark uses isolated synthetic profiles and reports
P50/P95 for cold publication, warm reuse, and collection across two profiles:

```text
python scripts/benchmark_meeting_assets.py --images 128 --size-kib 256 --runs 15
```
