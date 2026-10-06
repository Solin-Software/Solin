"""Persisted update-channel preference."""

from enum import StrEnum


class UpdateChannel(StrEnum):
    STABLE = "stable"
    BETA = "beta"
