# Solin Remote web app

This directory is the self-contained PWA shell served at `/remote/`. It has no
runtime dependency on a CDN, a package manager, or browser storage. All media and
projection state remains authoritative in the desktop process.

## Static hosting contract

Serve this directory below `/remote/`, with `index.html` for both `/remote/` and
`/remote/index.html`. `service-worker.js` must be served with
`Service-Worker-Allowed: /remote/` and revalidation enabled.
Static shell assets use HTTP revalidation rather than freshness windows; the
versioned Service Worker cache is the offline source of truth and always reloads
the shell from the server while installing a replacement worker.

The HTTP server is responsible for the security policy that cannot be enforced
reliably by markup alone, especially:

- HTTPS only, exact `Host` and same-origin `Origin` validation;
- `Content-Security-Policy` with `frame-ancestors 'none'`, scripts/styles from
  `'self'`, and `connect-src 'self'`;
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and a
  restrictive `Permissions-Policy`;
- `Cache-Control: no-store` on HTML and every API response.

The service worker caches only the explicit static shell allowlist. It never
intercepts `/remote/api/`, commands, authentication, WebSocket traffic,
thumbnails, or other runtime data.

## Local certificate trust

`GET /remote/trust-certificate.cer` is the only unauthenticated dynamic file
download besides the static shell (the localization read model returns JSON).
It returns the public DER certificate for Solin's
installation-local authority with `Cache-Control: no-store`; no private key is
ever exposed. The authority has `pathLenConstraint=0` and a critical IP
`nameConstraints` extension limited to the selected address as a `/32`. It signs
the leaf HTTPS certificate for that exact IP.

Users must compare the authority SHA-256 fingerprint with the value displayed in
desktop settings before trusting it. Trust is a one-device setup step required
for browsers to provide a reliable secure context for Service Worker and PWA
installation. Selecting another IP rotates both the constrained authority and
the server certificate, so devices must explicitly trust the replacement.

## Authentication

All endpoints use `application/json`, same-origin credentials, and the prefix
`/remote/api`.

### `POST /remote/api/auth/login`

Request:

```json
{"username":"operator","password":"a user supplied password"}
```

Success creates a `Secure; HttpOnly; SameSite=Strict; Path=/` session cookie and
returns the bootstrap fields below, plus `csrfToken`. The CSRF token is retained
only in JavaScript memory and sent as `X-CSRF-Token` on mutations. It is never
written to `localStorage`, `sessionStorage`, IndexedDB, Cache Storage, or a URL.

### `POST /remote/api/logout`

Requires the session cookie, exact same origin, and `X-CSRF-Token`. It revokes
the server-side session and expires the cookie.

### Errors

The preferred error body is:

```json
{"error":{"code":"rate_limited","message":"Try again later.","retryable":true}}
```

The client also handles an empty or plain-text HTTP error by mapping the HTTP
status. `401` returns to login; login `429` remains on the form and preserves the
username.

## Read models

### `GET /remote/api/localization`

This is the only unauthenticated JSON endpoint. It returns only the active BCP
47 locale and the translated `_RemoteControlWeb` message map needed by the login
screen. It never includes profile identity, credentials, session data, or other
runtime state, and is always served with `Cache-Control: no-store`. English
sources live in `messages.en.json`; the desktop resolves them through the active
Qt `.qm` catalog before publishing them.

### `GET /remote/api/bootstrap`

```json
{
  "authenticated": true,
  "serverInstanceId": "runtime-uuid",
  "eventSequence": 42,
  "csrfToken": "opaque-csrf-secret",
  "principal": "operator",
  "currentMeetingWeekStart": "2026-07-13",
  "profile": {
    "id": "main-hall",
    "name": "Salão principal",
    "locale": "pt-BR",
    "messages": {
      "app.remoteControl": "Controle remoto"
    },
    "theme": "dark"
  },
  "catalogRevision": 17,
  "playbackRevision": 91,
  "playback": {}
}
```

`locale` is the active Solin interface language converted from its locale
code to a BCP 47 browser tag (`pt_BR` becomes `pt-BR`). `messages` comes from
the same installed `QTranslator` used by the desktop, not from locale metadata.
`currentMeetingWeekStart` is the Solin-authoritative Monday for the current
meeting week and is refreshed in WebSocket heartbeats. `theme` may be omitted
while the desktop adapter is using the PWA default (`dark`) and accepts only
`dark` or `light`.

### `GET /remote/api/catalog`

```json
{
  "catalogRevision": 17,
  "collections": [
    {
      "id": "playlist-id",
      "kind": "playlist",
      "title": "Reunião de fim de semana",
      "subtitle": "12 mídias",
      "thumbnailId": null,
      "weekStart": null,
      "meetingType": null,
      "nodes": []
    }
  ]
}
```

Meeting collections carry the semantic start of their Monday-to-Sunday range as
`weekStart` (`YYYY-MM-DD`) and a structured `meetingType`: `midweek`, `weekend`,
`memorial`, or `other`. Other collection kinds use `null` for both fields. The
meeting collection's `thumbnailId` identifies the persisted publication cover
without exposing its bytes, source path, or storage representation. The
PWA never renders `weekStart` as if it were an event date. It groups the current,
future, and previous weeks and formats each complete range with
`Intl.DateTimeFormat.formatRange`; Qt date-pattern syntax is not duplicated in
the web client.

Collection `kind` is `playlist`, `linked_folder`, or `meeting`. A catalog node
has this public-only shape:

```json
{
  "id": "stable-node-id",
  "kind": "media",
  "title": "Vídeo de abertura",
  "children": [],
  "color": null,
  "collapsed": false,
  "mediaKind": "video",
  "durationMs": 125000,
  "thumbnailId": "opaque-thumbnail-id",
  "available": true,
  "unavailableReason": null
}
```

Node `kind` is `section`, `subsection`, `marker`, or `media`. The UI never needs
or accepts a media URL, filesystem path, persisted reference, or original file
metadata. Section and subsection nodes carry the same authoritative `collapsed`
state used by Solin's native meeting/playlist tree. The PWA uses it as the
initial state, preserves local expansion across playback-only renders, and
reconciles it whenever a newer catalog snapshot arrives.
Canonical generated meeting section/subsection titles are resolved from
`section_code` (or the legacy `meeting_source_key`) through the active Qt
translator. Non-generated and user-overridden titles remain untouched.

### `GET /remote/api/playback`

This optional diagnostic endpoint returns the same playback object included in
bootstrap and WebSocket snapshots:

```json
{
  "playbackRevision": 91,
  "playbackSessionId": "session-id",
  "state": "playing",
  "origin": {
    "source": "playlist",
    "collectionId": "playlist-id",
    "nodeId": "stable-node-id"
  },
  "title": "Vídeo de abertura",
  "mediaKind": "video",
  "queue": [
    {
      "origin": {
        "source": "playlist",
        "collectionId": "playlist-id",
        "nodeId": "stable-node-id"
      },
      "title": "Vídeo de abertura",
      "mediaKind": "video",
      "thumbnailId": "opaque-thumbnail-id"
    }
  ],
  "currentIndex": 0,
  "positionMs": 24000,
  "durationMs": 125000,
  "volume": 80,
  "capabilities": {
    "pause": true,
    "resume": false,
    "seek": true,
    "setVolume": true,
    "previous": false,
    "next": true,
    "stop": true
  },
  "error": null
}
```

Playback `state` is `idle`, `loading`, `playing`, `paused`, or `error`. The dock
renders only controls allowed by `capabilities`; remote UI code never overrides
desktop playback protection. The canonical `mediaKind` determines the
presentation: idle has its own control-free state, images and other visual
media never show seek/time/volume, and only `audio` or `video` may expose timed
controls. Timed media keeps its timeline and multi-item queue navigation in a
stable layout; playback protection, offline state, queue boundaries, and
in-flight commands disable the affected controls instead of removing them.
The current queue item is the single source of truth for the dock artwork, so
the player does not need to rediscover the media inside a concurrently changing
catalog tree.

### `GET /remote/api/thumbnails/{source}/{collectionId}/{nodeId}`

Returns a small authenticated image only when the corresponding node contains a
non-null `thumbnailId`. The server re-resolves all three public IDs and must not
accept a path or URL. Responses should use a restrictive image content type,
`nosniff`, size limits, and private revalidation. For local image nodes without
a pre-generated desktop thumbnail, the adapter renders a bounded JPEG directly.
For video and audio it reuses Solin's bounded Qt media-info queue to capture a
representative frame or embedded cover; stored HTTP(S) media uses the same
bounded metadata fetch and established media-stream fallback. Meeting nodes
prefer their already-resolved `thumbnail_url`; when artwork is
absent, `resolved_url` is used to extract a representative media frame. Both
references remain private and are re-resolved from the current catalog IDs.
Concurrent requests are deduplicated and real extracted results are cached
atomically. If
media has no visual data, a deterministic type-specific placeholder uses a
bounded expiring memory cache so a transient decoder or network failure cannot
poison the persistent thumbnail store. Generation is fenced by catalog revision.
The PWA never receives the source path or URL and does not persist thumbnails.

### `GET /remote/api/collection-thumbnails/{source}/{collectionId}`

Returns a meeting publication cover only when the current sanitized collection
contains a non-null `thumbnailId`. The server re-resolves the collection against
the same catalog revision, reads its persisted `MeetingTreeOverview` cover, and
normalizes the bounded input to JPEG before responding. The route is authenticated
and rejects paths, unknown sources, stale collections, missing covers, and invalid
image data.

The shell intentionally contains no inline scripts or styles. A browser console
warning that names an injected file such as `content.js` means an extension
attempted inline page injection and was blocked by the CSP; third-party hashes,
nonces, or `unsafe-inline` must not be added to accommodate it.

## Commands

`POST /remote/api/commands` requires the session cookie, exact same origin, and
`X-CSRF-Token`. `commandId` is a canonical UUID and makes retries idempotent.
The parser rejects unknown fields.

Play:

```json
{
  "commandId": "9c0936e5-c96a-45ce-8662-00515a57b466",
  "type": "play",
  "catalogRevision": 17,
  "origin": {
    "source": "meeting",
    "collectionId": "meeting-id",
    "nodeId": "stable-node-id"
  },
  "startPaused": false
}
```

`pause`, `resume`, `previous`, and `next` add only `playbackSessionId`. `seek`
also adds integer `positionMs`; `set_volume` adds integer `volume` from 0 to 100.
Stop is deliberately unconditional:

```json
{"commandId":"9c0936e5-c96a-45ce-8662-00515a57b466","type":"stop"}
```

Result:

```json
{
  "commandId": "9c0936e5-c96a-45ce-8662-00515a57b466",
  "ok": true,
  "catalogRevision": 17,
  "playbackRevision": 92,
  "error": null
}
```

Known command error codes are `catalog_stale`, `playback_stale`, `blocked`,
`not_found`, `unavailable`, `failed`, `invalid`, `command_id_conflict`, and
`command_in_progress`. A stale response triggers a full bootstrap/catalog
recovery instead of an optimistic merge.

## WebSocket synchronization

`GET /remote/api/ws` authenticates with the same cookie. The server sends
initial catalog, playback, and profile snapshots, then revisioned events:

```json
{
  "type": "playback.snapshot",
  "serverInstanceId": "runtime-uuid",
  "sequence": 43,
  "catalogRevision": 17,
  "playbackRevision": 92,
  "payload": {}
}
```

Event types are `catalog.snapshot`, `playback.snapshot`, `profile.snapshot`,
`session.revoked`, and `heartbeat`. Heartbeats repeat
`currentMeetingWeekStart`, allowing an open PWA to cross into a new week without
using the phone clock as its authority. The three initial snapshots may share
the current bootstrap sequence. They form one authoritative baseline and are all
applied before controls become available; contiguity checks begin only after
that baseline. Language changes publish a profile snapshot before a localized
catalog snapshot, so translated UI chrome and semantic section titles change as
one ordered stream. Subsequent state events increase the sequence monotonically. The
server sends a heartbeat at most every 25 seconds. After 55 seconds without any
frame, the client closes the socket and reconnects with exponential backoff and
jitter.

The client performs a complete bootstrap/catalog recovery when it sees a
sequence gap, a new `serverInstanceId`, a stale command result, the network
returning, or a foregrounded disconnected app. Position-only snapshots update
the player without rebuilding the catalog tree.

Because browsers do not expose an HTTP status for every failed WebSocket
handshake, a pre-upgrade failure is verified through bootstrap. A command-side
`403` follows the same proof: bootstrap refreshes the in-memory CSRF token and
the client retries the exact same idempotent `commandId` once. Bootstrap `401`
always returns to login.
