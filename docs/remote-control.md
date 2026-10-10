# Remote-control architecture

This reference describes the local service, device trust, and exposed
capabilities. For step-by-step setup and operation, see the
[user guide](https://solinav.vercel.app/guide/).

Solin can expose a profile-scoped remote-control web application on one selected
private network interface. The service is disabled by default, never binds to
every interface, and must not be forwarded through a router or exposed to the
public Internet.

## Device trust

The first setup installs the public certificate of a Root CA private to that
Solin installation. Its private key never leaves the desktop. Trusting the
public certificate gives the browser a persistent HTTPS identity and the secure
context needed for reliable PWA installation.

Device setup exposes a verification code and the Root CA fingerprint so the
device's certificate identity can be checked against the desktop before trust
is granted.

Changing the selected private IPv4 address rotates the short-lived server
certificate without replacing the trusted Root CA. An expired Root CA or
unrecoverable loss of its private key requires a new device setup; Solin fails
closed instead of silently changing trust.

## Exposed capabilities

The service exposes read-only catalogs for playlists, linked folders, and
meeting trees, along with current playback state. It accepts playback, seek,
volume, and stop commands; WebSocket events keep connected clients up to date.
Filesystem paths, media URLs, editing, and reordering operations are not exposed.

## Authentication and sessions

Authentication uses profile-local password hashes, bounded login attempts,
server-side sessions, secure cookies, CSRF protection, exact Host/Origin checks,
and HTTPS WebSockets. The toolbar can inspect and revoke active devices without
interrupting projection.

## Protocol reference

The complete HTTP, WebSocket, caching, and security contract is documented in
[`src/solin/resources/remote_control/README.md`](../src/solin/resources/remote_control/README.md).
