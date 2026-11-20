# ADR-0003: Host isolated remote web apps on one local TLS listener

- **Status:** Accepted
- **Date:** 2026-11-18

## Context

Solin's local remote-control subsystem originally served one Progressive Web App (PWA) on a
selected private IPv4 interface over HTTPS. That service combined local PKI trust, static shell
delivery, authentication, session management, and domain-specific projection state in a single
application surface.

Adding remote viewing and control for the meeting timer raised two questions about what the
applications should share.

- **Network listener.** A dedicated timer service would leave the media service in place and
  add a second HTTPS listener, with another port to configure and allow through the firewall.
  Extending the existing service into a shared host lets both apps use the same listener,
  private network interface, and device trust setup.
- **Authentication.** Projection operators and timekeepers need access to different controls,
  while public displays need only read-only state. A session shared across modules would couple
  revocation. Granting that session control over both workflows would also give operators
  access beyond their own role. Per-module credentials and sessions let each workflow enforce
  its own access and revocation policies.

## Decision drivers

- **Least privilege and role separation:** Credentials, sessions, CSRF tokens, and device
  revocation for one operator role must never grant access to or disrupt another role.
- **Single network and trust ceremony:** Operators should select one private LAN interface,
  allow one TCP port, and trust one installation-local Root CA per client device regardless of
  how many remote apps are enabled.
- **Independent PWA installability:** Browsers distinguish installed PWAs on the same origin by
  manifest `id`, `scope`, `start_url`, and icon set; sibling path scopes allow each tool to be
  installed independently on the same phone or tablet.
- **Support for public display surfaces alongside authenticated control:** A remote app may
  expose a strictly sanitized read-only stage without login while gating state-mutating controls
  behind scoped authentication on the same path prefix.
- **Zero-downtime module reconfiguration and TLS renewal:** Enabling, disabling, or rotating
  credentials for one remote module must not drop active connections on sibling modules, and
  short-lived leaf certificate renewal must hot-reload in place.
- **Clean vertical boundaries:** Desktop domain state remains authoritative on the Qt main
  thread; the network host owns only transport, security headers, and module lifecycle.

## Decision

Solin hosts all local remote web applications on one profile-scoped HTTPS listener bound to the
selected RFC 1918 IPv4 interface on a single fixed port, multiplexing isolated route modules by
URL path prefix:

- **Shared transport and PKI host:** One local HTTPS server and HTTP host application own the TLS
  listener, strict `Host` and same-origin `Origin` validation, HTTP security headers, Content
  Security Policy (`script-src 'self'; style-src 'self'`), bounded password-hashing concurrency,
  leaf-certificate hot reload, and the public Root CA download endpoint.
- **Scoped route modules:** Each remote surface mounts under a dedicated `/remote/<scope>/` prefix
  behind a uniform module boundary with its own:
  - enablement toggle and persisted credential record;
  - in-memory session store and login rate limiter;
  - `__Host-`-prefixed `Secure; HttpOnly; SameSite=Strict` session cookie and per-session
    in-memory CSRF secret;
  - WebSocket connection pools and monotonic event sequence(s);
  - PWA manifest (`id`, `scope`, `start_url`), Service Worker scope, and distinct application
    icons;
  - revisioned static asset bundle combining shared primitives with module-specific UI assets.
- **Optional public read-only tier per module:** A module may expose an unauthenticated read-only
  bootstrap and WebSocket stream carrying only display-safe state (such as live clock and timer
  anchors) on its own event sequence, while keeping schedule details and command endpoints
  strictly behind its scoped session and CSRF boundary.
- **Unified desktop supervision:** One desktop controller coordinates network-interface recovery,
  TLS renewal, dynamic module activation without dropping sibling scopes, and aggregated device
  inventory so operators can inspect active services and revoke individual or all sessions per
  scope.

## Consequences

### Positive

- **Zero extra ports or certificate prompts:** Trusting the installation Root CA once enables
  HTTPS and standalone PWA installation for every remote module on that Solin installation.
- **Independent session and service lifecycles.** Each module authorizes access using its own
  sessions. Rotating credentials or revoking sessions in one module leaves sibling sessions
  active. Disabling a module closes only its own connections.
- **Extensible vertical slices:** New remote surfaces can be added as independent modules without
  duplicating TLS, CSP, static-asset versioning, or session primitives.
- **Low network overhead for real-time displays:** Time-based views synchronize epoch anchors on
  discrete state transitions and heartbeats rather than broadcasting high-frequency UI ticks.

### Costs and trade-offs

- **Shared browser origin.** Each URL prefix is part of the same origin, so frontend code in
  every module runs with that origin's authority. The IETF draft
  [`draft-ietf-httpbis-rfc6265bis`](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-rfc6265bis-22#section-4.1.3.2)
  requires `Path=/` for `__Host-` cookies, so a request to one module can also carry cookies
  for others. Each module reads only its own session cookie and validates the token against
  its own session store.
- **Shared listener lifecycle:** A master switch or network-interface change stops the listener
  for all hosted modules together, while per-module switches toggle individual modules in place.

## Reconsideration triggers

A superseding ADR is required if:

- different remote surfaces require distinct network-interface bindings, transport protocols, or
  separate OS process privileges that cannot share one local HTTPS listener; or
- the applications need separate browser origins to meet requirements for frontend isolation,
  independent PWA installation, or per-module session controls.
