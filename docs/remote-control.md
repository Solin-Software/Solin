# Remote-control architecture

This reference describes Solin's local remote-control host, device trust, and isolated
web applications. For step-by-step setup and operation, see the
[user guide](https://solinav.vercel.app/guide/). For the underlying architectural rationale,
see [ADR-0003](adr/0003-host-isolated-remote-web-apps-on-one-local-tls-listener.md).

Solin can expose profile-scoped Progressive Web Apps (PWAs) on one selected private RFC 1918
IPv4 interface over HTTPS (`8765`). Remote control is disabled by default, never binds to
wildcard interfaces, and must not be forwarded through a router or exposed to the public
Internet.

## Hosted remote applications

One shared HTTPS listener hosts independent, path-scoped web applications behind a master
remote-control switch and per-service toggles:

- **Media (`/remote/media/` — Solin Media):** Authenticated browsing of meeting trees,
  playlists, and linked folders, with live playback state and transport controls (play, pause,
  resume, seek, volume, previous/next, and stop). Filesystem paths, media URLs, and catalog
  editing are never exposed.
- **Timer (`/remote/timer/` — Solin Timer):** Fullscreen-first live clock and active part
  accessible without signing in. The authenticated workspace runs and edits meeting and
  custom schedules according to server-authoritative capabilities. The authenticated
  controller workspace shows the pacing forecast and schedule window when available.

Each application has its own PWA manifest (`id`, `scope`, `start_url`) and distinct icon set so
both can be installed side by side on the same phone or tablet. Enabling or disabling one
service in Settings updates the running host in place without dropping sessions on the other.

## Device trust

First-time device setup downloads the public DER certificate of a Root CA private to that Solin
installation (`/remote/trust-certificate.cer`). Its private key never leaves the desktop.
Trusting the Root CA once on a device establishes a persistent HTTPS secure context for all
remote apps on that Solin installation, enabling Service Workers and standalone PWA installation.

Device setup displays a short verification code and the Root CA SHA-256 fingerprint so operators
can verify the certificate against Solin before trusting it. Renewing the 90-day server leaf
hot-reloads TLS in place without replacing the Root CA or dropping active sessions; changing the
selected private IPv4 address restarts the listener on the new address and revokes active sessions
while preserving the Root CA so trusted devices need not reinstall it. Root CA expiry or
unrecoverable key loss fails closed and requires explicit trust replacement.

## Authentication, isolation, and supervision

Media and Timer maintain strictly separate credentials, `scrypt` password verifiers, login rate
limiters, server-side session stores, `__Host-`-prefixed `Secure; HttpOnly; SameSite=Strict`
cookies, in-memory CSRF tokens, and WebSocket pools. Authenticating on one app never grants
access to the other, and rotating credentials in one scope revokes only that scope's sessions.

The desktop quick-access toolbar surfaces active services, public Timer viewer counts, and
authenticated devices grouped by scope, allowing per-device or global session revocation without
interrupting projection or the running timer.

## Protocol reference

Custom schedule management is enabled for authenticated timer controllers by default.
Changing pacing settings remotely requires a separate capability.

The base HTTP, WebSocket, caching, CSP, and command contract for both web applications is
documented in
[`src/solin/resources/remote_control/README.md`](../src/solin/resources/remote_control/README.md).
