# Remote-control web applications

This directory contains the self-contained Progressive Web App (PWA) shells served by Solin's
local HTTPS remote-control host:

- `shared/` — shared CSS design tokens/base rules and zero-storage ES modules (`dom.js`,
  `i18n.js`, `api.js`, `pwa.js`, `setup.js`).
- `media/` — **Solin Media** (`/remote/media/`), for browsing meeting/playlist media and
  controlling projection playback.
- `timer/` — **Solin Timer** (`/remote/timer/`), a fullscreen-first meeting clock/timer display
  with authenticated part and schedule control.

Neither app depends on external CDNs, bundlers, or browser storage (`localStorage`,
`sessionStorage`, or `indexedDB`). All domain state remains authoritative in the desktop Qt
process.

## Static hosting and Service Worker contract

Each module mounts under its own `/remote/<scope>/` prefix (`/remote/media/` and
`/remote/timer/`):

- `GET /remote/<scope>` redirects (`302`) to `/remote/<scope>/`, preserving query parameters
  such as `?setup=1`.
- `GET /remote/<scope>/` and `/remote/<scope>/index.html` return the rendered HTML shell with
  `Cache-Control: no-store`.
- `GET /remote/<scope>/service-worker.js` is served with `Service-Worker-Allowed: /remote/<scope>/`
  and `Cache-Control: no-cache, no-store, must-revalidate`.
- `RemoteControlWebAssets` computes an installation-scoped SHA-256 shell revision over the
  installation ID, `index.html`, `service-worker.js`, all module assets (`app/…`), and all
  shared assets (`shared/…`). Rendered shells rewrite `./…` and `../shared/…` references to
  immutable `/remote/<scope>/_assets/<revision>/…` URLs (`public, max-age=31536000, immutable`).
- Service Workers cache only the explicit revisioned static shell allowlist (`solin-remote-media-shell-*`
  and `solin-remote-timer-shell-*`) and never intercept `/remote/<scope>/api/`, authentication,
  commands, thumbnails, WebSockets, or `/remote/trust-certificate.cer`.

### Security headers and CSP

Every response enforces:

- HTTPS only (TLS 1.2+), exact `Host` matching, and exact same-origin `Origin` validation on
  mutations and WebSocket upgrades;
- `Content-Security-Policy`: `default-src 'self'; base-uri 'none'; form-action 'self';
  frame-ancestors 'none'; object-src 'none'; script-src 'self'; style-src 'self';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self';
  worker-src 'self'` (no inline scripts, no inline `style` attributes, no `element.style.*`
  mutations, and no HTML string injection);
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`, and a
  restrictive `Permissions-Policy`;
- `Cache-Control: no-store` on HTML, certificate downloads, and all API responses.

## Shared certificate trust and onboarding

`GET /remote/trust-certificate.cer` returns the public DER certificate for Solin's
installation-local Root CA (`application/pkix-cert`, `Cache-Control: no-store`). A 10-year Root
CA (`pathLenConstraint=1`) signs a 3-year private-network intermediate CA (`pathLenConstraint=0`,
critical RFC 1918 `nameConstraints`), which signs the 90-day server leaf for the selected IPv4
address. Leaf renewals hot-reload the live `SSLContext` without restarting the listener or
revoking sessions.

`GET /remote/<scope>/api/setup` is unauthenticated and returns only the localized setup/login
catalog (`locale` + translated `messages` from Qt `.qm` catalogs) and public `tls` metadata
(`installationId`, `authoritySha256`, `verificationCode`, `authorityCertificateUrl`,
`authorityNotValidAfter`). Visiting `/remote/<scope>/?setup=1` opens the dedicated 3-step
certificate trust and PWA installation guide.

## Scoped authentication and session isolation

Media and Timer never share credentials, rate limiters, sessions, cookies, or CSRF secrets:

| Property | Media (`RemoteScope.MEDIA`) | Timer (`RemoteScope.TIMER`) |
| --- | --- | --- |
| Prefix | `/remote/media` | `/remote/timer` |
| Cookie | `__Host-solin_remote_media` | `__Host-solin_remote_timer` |
| Manifest `id` / `scope` | `/remote/media/` | `/remote/timer/` |

- `POST /remote/<scope>/api/auth/login` validates `{"username", "password"}` against that
  scope's credential store, sets `Secure; HttpOnly; SameSite=Strict; Path=/`, and returns the
  authenticated bootstrap payload including `csrfToken`.
- `csrfToken` is kept strictly in JavaScript memory and sent as `X-CSRF-Token` on `POST`
  mutations (`/logout` and `/commands`).
- `POST /remote/<scope>/api/logout` revokes the server session, closes its authenticated
  WebSockets with `4401`, and expires that scope's cookie.
- Desktop revocation accepts `<scope>:<managementId>` to revoke a single device or revokes all
  sessions in one or all scopes.

---

## Media contract (`/remote/media/api`)

All Media runtime routes require an authenticated `RemoteScope.MEDIA` session:

- `GET /remote/media/api/bootstrap` — returns `serverInstanceId`, `eventSequence`, `csrfToken`,
  `principal`, `currentMeetingWeekStart`, `profile` (`id`, `name`, `theme`, `locale`,
  `messages`), `catalogRevision`, `playbackRevision`, and `playback`.
- `GET /remote/media/api/catalog` — returns sanitized `collections` (`playlist`,
  `linked_folder`, `meeting`) and `nodes` (`section`, `subsection`, `marker`, `media`) without
  exposing local paths or upstream URLs.
- `GET /remote/media/api/playback` — returns `playbackRevision`, `playbackSessionId`, `state`,
  `origin`, `title`, `mediaKind`, `queue`, `positionMs`, `durationMs`, `volume`,
  `capabilities`, and `error`.
- `GET /remote/media/api/thumbnails/{source}/{collectionId}/{nodeId}` and
  `GET /remote/media/api/collection-thumbnails/{source}/{collectionId}` — re-resolve opaque
  public catalog IDs to bounded JPEG artwork (`Cache-Control: private, max-age=300`).
- `POST /remote/media/api/commands` — idempotent UUID `commandId` envelope supporting `play`
  (fenced by `catalogRevision`), `pause`, `resume`, `seek`, `set_volume`, `previous`, `next`
  (fenced by `playbackSessionId`), and unconditional `stop`. Error codes: `catalog_stale`,
  `playback_stale`, `blocked`, `not_found`, `unavailable`, `failed`, `invalid`,
  `command_id_conflict`, `command_in_progress`.
- `GET /remote/media/api/ws` — unidirectional server-to-client WebSocket emitting atomic
  `session.snapshot` baseline followed by `catalog.snapshot`, `playback.snapshot`,
  `profile.snapshot`, `session.revoked`, and 20 s `heartbeat` frames.

---

## Timer contract (`/remote/timer/api`)

### Public fullscreen stage (no login required)

Opening `/remote/timer/` enters the fullscreen timer stage immediately and acquires a browser
Screen Wake Lock (`navigator.wakeLock`) while visible:

- `GET /remote/timer/api/public` — unauthenticated read model returning `authenticated`,
  `controlAvailable`, `serverInstanceId`, `eventSequence`, `profile` (`id`, `name`,
  `displayName`, `theme`, `locale`, `messages`), `tls`, `liveRevision`, and `live`. It never
  includes the meeting schedule or `scheduleRevision`.
- `GET /remote/timer/api/ws/public` — unauthenticated, origin-checked, IP-bounded WebSocket
  emitting `public.snapshot`, `timer.live`, `profile.snapshot`, and `heartbeat`.

### Zero-tick-flooding live synchronization

`live` (`TimerLiveWireSnapshot`) carries `liveRevision`, `serverEpochMs`, `utcOffsetMinutes`,
`active`, `state` (`idle` | `running` | `stopped`), `direction` (`down` | `up`), `activePartId`,
`activePartTitle`, `activeSection`, `plannedSeconds`, `elapsedSeconds`,
and `clockConfig` (`mode`, `analogStyle`, `hourFormat24h`, `showAmpm`, `showSeconds`,
`direction`, `partTimerDisplay`, `textScalePct`, `freezeSeconds`).

The desktop emits `timer.live` only on discrete transitions (start, stop, reset, freeze revert,
`±1 s` nudge, or clock configuration changes)—never on 250 ms UI ticks. Clients extrapolate
elapsed and wall-clock time locally from a monotonic clock anchor and render Digital, Analog
(*Signature* and *Classic*), Analog + Digital, *Clock + Timer* split, and duration-sector washes
with exact parity to `solin.core.timer.render.build_render_model`.

### Authenticated controller workspace

Tapping the fullscreen stage reveals an auto-hiding HUD with **Sign in** (when
`controlAvailable` is `true`), opening a modal login dialog over the live timer, or **Control
parts** when already authenticated:

- `GET /remote/timer/api/bootstrap` — authenticated baseline returning `csrfToken`, `principal`,
  `profile`, `liveRevision`, `scheduleRevision`, `live`, and `schedule`.
- `GET /remote/timer/api/schedule` — authenticated `TimerScheduleWireSnapshot` containing
  `scheduleRevision`, `weekMonday`, `weekLabel`, `isCurrentWeek`, `meetingType`, section-styled
  `parts` (including per-part `durationEditable`), and server-authoritative `capabilities`
  (`controlParts`, `adjustPlannedMinutes`, `setPlannedSeconds`, `setSectionCount`,
  `switchMeetingType`, `navigateWeeks`, `manageCustomSchedules`). The last capability is
  false by default. With it enabled, `customSchedule` describes the active custom schedule,
  including its ID, title, revision, and ordered root parts and sections, including empty sections.
- `GET /remote/timer/api/custom-schedules` — authenticated, on-demand custom schedule catalog;
  unavailable when the capability or adapter is disabled. The Qt adapter obtains each snapshot on
  the main thread. Part progress contains `state`, accumulated `elapsedSeconds`,
  `startedAtEpoch`, and `firstStartedEpoch`. Catalog details are not sent to public viewers.
- `GET /remote/timer/api/ws` — authenticated WebSocket emitting `session.snapshot` (`live` +
  `schedule` + `profile`), `timer.live`, `timer.schedule`, `profile.snapshot`,
  `session.revoked`, and `heartbeat`.
- `POST /remote/timer/api/commands` — requires `__Host-solin_remote_timer`, same `Origin`, and
  `X-CSRF-Token`. Rejects unknown fields and enforces idempotency (`commandId`) plus optimistic
  concurrency (`scheduleRevision` on schedule-bound mutations):
  - `start_part` (`scheduleRevision`, `partId`)
  - `stop_part` (optional `partId`)
  - `reset_part` (`scheduleRevision`, `partId`)
  - `adjust_part` (`scheduleRevision`, `partId`, `deltaSeconds` in `{-60, -1, 1, 60}`)
  - `set_part_seconds` (`scheduleRevision`, `partId`, `plannedSeconds` `1..86399`; meeting
    operations enforce their own minimum)
  - `set_section_count` (`scheduleRevision`, `section` in `{"ministry", "living"}`, `count` `1..8`)
  - `set_meeting_type` (`meetingType` in `{"midweek", "weekend"}`)
  - `navigate_week` (`target` in `{"previous", "next", "current"}`)
  - `create_custom_schedule` (`scheduleRevision`, `title`)
  - `duplicate_custom_schedule` (`scheduleRevision`, `scheduleId`, `customRevision`, `title`)
  - `delete_custom_schedule`, `select_custom_schedule` (`scheduleRevision`, `scheduleId`, `customRevision`)
  - `select_meeting_schedule`, `reset_all` (`scheduleRevision`)
  - `rename_part` (`scheduleRevision`, `partId`, nullable `title`)
  - `edit_custom_schedule` (`scheduleRevision`, `scheduleId`, `customRevision`, `action`, `values`)

  These additional commands require `manageCustomSchedules`. Successful create and duplicate
  results include `createdScheduleId`, including on replay. Edit `values` use the snake-case
  operation arguments. Actions are `add_part` (`title`, `planned_seconds`, optional `section_id`,
  `before_id`); `add_section` (`title`, optional `before_id`); `rename_part` (`part_id`, `title`);
  `rename_section` (`section_id`, `title`); `rename_schedule` (`title`); `move_part` (`part_id`,
  optional `section_id`, `before_id`); `move_section` (`section_id`, optional `before_id`);
  `delete_part` (`part_id`); `delete_section` (`section_id`); and `set_part_seconds` (`part_id`,
  `planned_seconds`). Null or omitted optional positions append at the root or section. Each edit
  is one operation; clients refresh the catalog on demand. The adapter revalidates the custom
  revision on the Qt thread. While a custom schedule is active with this capability disabled,
  timer mutations against it are blocked, while public live viewing remains available.
  Changing the active week, meeting type, or schedule requires stopping the running part first.

  Error codes: `schedule_stale`, `blocked`, `not_found`, `unavailable`, `failed`, `invalid`,
  `command_id_conflict`, `command_in_progress`. On `schedule_stale`, the client refreshes via
  `/remote/timer/api/schedule` rather than merging stale schedule state; on `401` or
  `session.revoked`, the client drops smoothly back to the live public fullscreen stage without
  blanking the timer.
