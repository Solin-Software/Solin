# Local remote control

Solin can expose a profile-scoped remote-control web application on one selected
private network interface. The service is disabled by default, never binds to
every interface, and must not be forwarded through a router or exposed to the
public Internet.

## Enabling the service

1. Open Solin settings.
2. Select the private LAN interface.
3. Save a username and a password of at least 12 characters.
4. Enable **Remote control**.
5. Use **Set up a device** to configure each phone, tablet, or computer.

The assistant displays the remote address, a QR code, and a verification code.
Compare the code shown by Solin with the one shown on the device before trusting
the certificate.

## Device trust

The first setup installs the public certificate of a Root CA private to that
Solin installation. Its private key never leaves the desktop. Trusting the
public certificate gives the browser a persistent HTTPS identity and the secure
context needed for reliable PWA installation.

Changing the selected private IPv4 address rotates the short-lived server
certificate without replacing the trusted Root CA. An expired Root CA or
unrecoverable loss of its private key requires a new device setup; Solin fails
closed instead of silently changing trust.

## Exposed capabilities

The remote interface can synchronize playlists, linked folders, meeting trees,
now-playing state, transport, seek, volume, and stop. Its catalog is read-only:
filesystem paths, media URLs, editing, and reordering operations are not
exposed.

Authentication uses profile-local password hashes, bounded login attempts,
server-side sessions, secure cookies, CSRF protection, exact Host/Origin checks,
and HTTPS WebSockets. The toolbar can inspect and revoke active devices without
interrupting projection.

The complete HTTP, WebSocket, caching, and security contract is documented in
[`src/solin/resources/remote_control/README.md`](../src/solin/resources/remote_control/README.md).
