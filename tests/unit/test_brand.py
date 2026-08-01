"""Tests for the shared JW brand mark."""

from __future__ import annotations

from solin.projection.brand import render_idle_logo


def test_idle_logo_fills_requested_canvas():
    img = render_idle_logo(1280, 720)
    assert not img.isNull()
    assert (img.width(), img.height()) == (1280, 720)


def test_idle_logo_clamps_degenerate_sizes():
    img = render_idle_logo(0, -5)
    assert not img.isNull()
    assert img.width() >= 1 and img.height() >= 1
