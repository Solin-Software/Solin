"""Hosted meetings: Solin hosts the hybrid meeting itself on a Galene sidecar.

The hall PC runs Galene (media and permissions), a meeting web server (join page,
lobby, attendee client, stage page, Galene WebSocket proxy) and an operator client
that applies the meeting rules. A self-hostable hub gives the hall a public HTTPS
address (tunnel) and a TURN relay. See ``docs/hosted-meetings.md``.
"""
