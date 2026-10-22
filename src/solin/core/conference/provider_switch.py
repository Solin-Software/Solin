"""One :class:`~solin.core.conference.provider.MeetingProvider` that forwards to the chosen one.

The meeting workflow and the toolbar hold this object, so switching between Zoom and a
Solin-hosted meeting in settings needs no re-wiring: the switch re-routes calls, forwards
the active provider's signals and, on a switch, announces the new provider's state.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QObject, Signal

from .provider import PROVIDER_SIGNALS, MeetingProvider


class MeetingProviderSwitch(QObject):
    enabled_changed = Signal(bool)
    connection_changed = Signal(bool)
    busy_changed = Signal(bool)
    operation_finished = Signal(object)
    error = Signal(str)
    active_changed = Signal(str)

    def __init__(self, providers: Mapping[str, MeetingProvider], active: str,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        if active not in providers:
            raise ValueError(f"Unknown meeting provider {active!r}")
        self._providers = dict(providers)
        self._active_key = ""
        self._active: MeetingProvider | None = None
        self.set_active(active)

    @property
    def active(self) -> str:
        return self._active_key

    @property
    def provider(self) -> MeetingProvider:
        assert self._active is not None
        return self._active

    def set_active(self, key: str) -> None:
        if key == self._active_key:
            return
        provider = self._providers[key]
        previous = self._active
        if previous is not None:
            for name in PROVIDER_SIGNALS:
                getattr(previous, name).disconnect(getattr(self, name))
        self._active_key, self._active = key, provider
        for name in PROVIDER_SIGNALS:
            getattr(provider, name).connect(getattr(self, name))
        if previous is not None:
            self.enabled_changed.emit(provider.is_enabled)
            self.connection_changed.emit(provider.is_connected)
            self.busy_changed.emit(provider.is_busy)
        self.active_changed.emit(key)

    # -- MeetingProvider ----------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return self.provider.is_available

    @property
    def is_enabled(self) -> bool:
        return self.provider.is_enabled

    @property
    def is_connected(self) -> bool:
        return self.provider.is_connected

    @property
    def is_busy(self) -> bool:
        return self.provider.is_busy

    def start(self) -> None:
        self.provider.start()

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        self.provider.stop(wait=wait, timeout=timeout)

    def request_pre_meeting(self) -> bool:
        return self.provider.request_pre_meeting()

    def request_post_meeting(self) -> bool:
        return self.provider.request_post_meeting()

    def cancel(self) -> None:
        """Abandon a running sequence, where the provider supports it (Zoom does)."""
        cancel = getattr(self.provider, "cancel", None)
        if callable(cancel):
            cancel()
