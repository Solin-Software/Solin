from __future__ import annotations

import pytest
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTranslator,
    Signal,
)
from PySide6.QtGui import QAccessible, QContextMenuEvent, QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from solin.core.profiles.models import ProfileInfo
from solin.styles.theme import PALETTE
from solin.ui.profile_selector import ProfileSelector
from solin.ui.profile_switch_overlay import ProfileSwitchOverlay
from solin.ui.profile_widgets import AddProfileCard, ProfileCard
from tests._translation_catalog import catalog_translation
from tests._qt import wait_until


@pytest.fixture
def own_widget():
    widgets = []

    def own(widget):
        widgets.append(widget)
        return widget

    yield own
    for widget in reversed(widgets):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def embedded_selector(own_widget):
    host = own_widget(QWidget())
    host.resize(460, 600)
    # No host layout: the child keeps its requested viewport even when the
    # desktop constrains the native window's initial size.
    selector = ProfileSelector(host)
    host.show()
    return selector


def _profiles(count: int) -> list[ProfileInfo]:
    return [ProfileInfo(f"profile-{index}", f"Profile {index}") for index in range(count)]


def _settle_layout() -> None:
    # Scrollbar visibility changes the viewport width, requiring another layout pass.
    for _ in range(5):
        QCoreApplication.processEvents()


@pytest.mark.parametrize("width,count", [(460, 3), (960, 3), (460, 30), (960, 30)])
def test_overlay_fades_complete_frames_instead_of_revealing_body_at_completion(
    own_widget,
    monkeypatch,
    width,
    count,
):
    from solin.ui import helpers

    underlay = QColor("#b01866")
    host = own_widget(QWidget())
    host.setStyleSheet(f"background: {underlay.name()};")
    host.resize(width, 640)
    host.show()
    _settle_layout()
    frames = []
    settled_frames = []
    original_paint = ProfileCard.paintEvent

    def observe_settled_frame(card, event):
        original_paint(card, event)
        if card.window() is host and overlay.graphicsEffect() is None:
            image = host.backingStore().paintDevice()
            assert isinstance(image, QImage)
            settled_frames[:] = [image.copy()]

    monkeypatch.setattr(ProfileCard, "paintEvent", observe_settled_frame)

    class ObservedOpacityEffect(QGraphicsOpacityEffect):
        def draw(self, painter):
            super().draw(painter)
            # Observe after the effect has composited its source into a valid
            # backing store. Rendering/grabbing the widget would hide omissions.
            image = host.backingStore().paintDevice()
            assert isinstance(image, QImage)
            frames[:] = [image.copy()]

    monkeypatch.setattr(helpers, "QGraphicsOpacityEffect", ObservedOpacityEffect)
    overlay = own_widget(
        ProfileSwitchOverlay(host, f"profile-{count - 1}", profiles=_profiles(count))
    )
    effect = overlay.graphicsEffect()
    animation = effect.findChild(QPropertyAnimation)
    animation.pause()
    overlay.show()
    overlay.raise_()
    _settle_layout()
    selector = overlay.findChild(ProfileSelector)
    points = [
        QPoint(12, 20),
        selector._scroll.viewport().mapTo(host, QPoint(4, 4)),
        selector._cards[-1].mapTo(host, QPoint(40, 70)),
    ]

    def sampled_colors(image):
        scale = image.devicePixelRatio()
        return [image.pixelColor(round(p.x() * scale), round(p.y() * scale)) for p in points]

    samples = []
    for progress in (0.1, 0.3, 0.6, 0.9):
        frames.clear()
        animation.setCurrentTime(round(animation.duration() * progress))
        wait_until(lambda: bool(frames), description="composited profile transition frame")
        samples.append((effect.opacity(), sampled_colors(frames[-1])))

    final_opacity, final_colors = samples[-1]
    assert all(color != underlay for color in final_colors), "The whole selector must fade in"
    for opacity, colors in samples[:-1]:
        for color, final_color in zip(colors, final_colors, strict=True):
            for channel in (QColor.red, QColor.green, QColor.blue):
                expected = channel(underlay) + (channel(final_color) - channel(underlay)) * (
                    opacity / final_opacity
                )
                assert abs(channel(color) - expected) <= 3, (
                    "Header, body and cards must fade together"
                )

    animation.setCurrentTime(animation.duration())
    wait_until(lambda: overlay.graphicsEffect() is None, description="profile transition cleanup")
    wait_until(lambda: bool(settled_frames), description="profile frame after effect removal")
    for color, final_color in zip(sampled_colors(settled_frames[-1]), final_colors, strict=True):
        for channel in (QColor.red, QColor.green, QColor.blue):
            assert abs(channel(color) - channel(final_color)) <= 3, (
                "Cleanup must not reveal new content"
            )


def test_overlay_paints_background_and_cards_without_hover_or_forced_render(
    own_widget, monkeypatch
):
    host = own_widget(QWidget())
    host.setStyleSheet("background: #b01866;")
    host.resize(960, 640)
    host.show()
    _settle_layout()
    frames = []
    original_paint = ProfileCard.paintEvent

    def observe_painted_frame(card, event):
        original_paint(card, event)
        if card.window() is host and overlay.graphicsEffect() is None:
            # Read within a paint event, while QBackingStore's paint device is
            # valid. A copy preserves that completed portion of the real frame.
            device = host.backingStore().paintDevice()
            assert isinstance(device, QImage)
            frames[:] = [device.copy()]

    monkeypatch.setattr(ProfileCard, "paintEvent", observe_painted_frame)

    def paints_full_surface(overlay):
        # grab()/render() would repaint the widget and conceal stale backing
        # store pixels. Inspect only the frame Qt has already painted.
        if not frames:
            return False
        image = frames[-1]

        def color_at(widget, point):
            position = widget.mapTo(host, point)
            scale = image.devicePixelRatio()
            return image.pixelColor(round(position.x() * scale), round(position.y() * scale))

        selector = overlay.findChild(ProfileSelector)
        viewport = selector._scroll.viewport()
        if color_at(viewport, QPoint(4, 4)) != QColor(PALETTE.bg0):
            return False
        visible_cards = [
            card
            for card in overlay.findChildren(ProfileCard)
            if viewport.rect().contains(card.mapTo(viewport, QPoint(40, 70)))
        ]
        return bool(visible_cards) and all(
            color_at(card, QPoint(40, 70)) not in (QColor("#b01866"), QColor(PALETTE.bg0))
            for card in visible_cards
        )

    for _ in range(2):
        frames.clear()
        overlay = own_widget(ProfileSwitchOverlay(host, "profile-1", profiles=_profiles(12)))
        overlay.show()
        overlay.raise_()
        wait_until(
            lambda overlay=overlay: overlay.graphicsEffect() is None,
            description="profile fade completion",
        )
        wait_until(
            lambda overlay=overlay: paints_full_surface(overlay),
            description="complete profile selector frame",
        )
        selector = overlay.findChild(ProfileSelector)
        frames.clear()
        selector._scroll.ensureWidgetVisible(selector._cards[-1])
        wait_until(
            lambda overlay=overlay: paints_full_surface(overlay),
            description="painted scrolled profile cards",
        )
        overlay.hide()


@pytest.mark.parametrize("count", [1, 3, 8, 30])
@pytest.mark.parametrize("width", [460, 860, 960, 1500])
def test_profiles_reflow_without_widening_window_and_remain_reachable(
    embedded_selector, count, width
):
    selector = embedded_selector
    selector.set_profiles(_profiles(count))
    selector.resize(width, 600)
    selector.show()
    _settle_layout()

    assert selector.width() == width
    cards = [*selector.findChildren(ProfileCard), *selector.findChildren(AddProfileCard)]
    assert len(cards) == count + 1
    assert selector._scroll.horizontalScrollBar().maximum() == 0
    for index, card in enumerate(cards):
        for other in cards[index + 1 :]:
            assert not card.geometry().intersects(other.geometry())
        selector._scroll.ensureWidgetVisible(card, 0, 0)
        _settle_layout()
        position = card.mapTo(selector._scroll.viewport(), QPoint())
        assert (
            selector._scroll.viewport()
            .rect()
            .contains(
                card.rect().translated(position),
            )
        )


def test_resize_reuses_cards_and_reflows_in_both_directions(embedded_selector):
    selector = embedded_selector
    selector.set_profiles(_profiles(12))
    selector.resize(1500, 600)
    selector.show()
    _settle_layout()
    assert selector.width() == 1500
    cards = tuple(selector.findChildren(ProfileCard))
    initial_rows = {card.y() for card in cards}

    selector.resize(460, 600)
    _settle_layout()
    assert selector.width() == 460
    assert tuple(selector.findChildren(ProfileCard)) == cards
    assert len({card.y() for card in cards}) > len(initial_rows)

    selector.resize(1500, 600)
    _settle_layout()
    assert selector.width() == 1500
    assert {card.y() for card in cards} == initial_rows


def test_resize_keeps_focused_card_in_view(embedded_selector):
    selector = embedded_selector
    selector.set_profiles(_profiles(30))
    selector.resize(1500, 1000)
    selector.show()
    selector.window().activateWindow()
    _settle_layout()
    assert (selector.width(), selector.height()) == (1500, 1000)
    selector._add_card.setFocus()
    wait_until(
        lambda: QApplication.focusWidget() is selector._add_card,
        description="focused profile creation card before resize",
    )

    selector.resize(460, 600)
    _settle_layout()

    assert QApplication.focusWidget() is selector._add_card
    position = selector._add_card.mapTo(selector._scroll.viewport(), QPoint())
    assert (
        selector._scroll.viewport()
        .rect()
        .contains(
            selector._add_card.rect().translated(position),
        )
    )


def test_short_window_scrolls_title_and_cards_but_keeps_header_visible(own_widget):
    selector = own_widget(ProfileSelector(allow_cancel=True, allow_logout=True))
    selector.set_profiles(_profiles(8))
    selector.resize(460, 320)
    selector.show()
    _settle_layout()
    selector._scroll.ensureWidgetVisible(selector._add_card, 0, 0)
    _settle_layout()

    assert selector.height() == 320
    for button in (selector._logout_button, selector._close_button):
        assert selector.rect().contains(button.rect().translated(button.mapTo(selector, QPoint())))


@pytest.mark.parametrize("allow_management", [False, True])
def test_context_actions_are_dispatched_only_when_allowed(own_widget, allow_management):
    selector = own_widget(ProfileSelector(allow_management=allow_management))
    selector.set_profiles(_profiles(2))
    selector.show()
    _settle_layout()
    requested = []
    selector.context_requested.connect(lambda *args: requested.append(args))
    card = selector.findChildren(ProfileCard)[0]
    for reason in (QContextMenuEvent.Reason.Mouse, QContextMenuEvent.Reason.Keyboard):
        event = QContextMenuEvent(
            reason, card.rect().center(), card.mapToGlobal(card.rect().center())
        )
        QApplication.sendEvent(card, event)
    assert len(requested) == (2 if allow_management else 0)
    if requested:
        assert all(profile_id == card.profile.id for profile_id, _position in requested)


def test_profile_and_creation_buttons_use_keyboard_and_native_mouse_semantics(own_widget):
    selector = own_widget(ProfileSelector())
    profiles = _profiles(2)
    selector.set_profiles(profiles)
    selector.show()
    _settle_layout()
    selected = []
    created = []
    selector.profile_selected.connect(selected.append)
    selector.create_profile_requested.connect(lambda: created.append(True))
    card = selector.findChildren(ProfileCard)[0]
    card.setFocus()
    QTest.keyClick(card, Qt.Key.Key_Return)
    QTest.keyClick(card, Qt.Key.Key_Space)
    QTest.mousePress(card, Qt.MouseButton.LeftButton, pos=card.rect().center())
    QTest.mouseRelease(card, Qt.MouseButton.LeftButton, pos=QPoint(-2, -2))
    QTest.keyClick(selector._add_card, Qt.Key.Key_Return)
    assert selected == [profiles[0].id, profiles[0].id]
    assert created == [True]


def test_focus_scrolls_into_view_and_stays_inside_selector(own_widget):
    selector = own_widget(ProfileSelector(allow_cancel=True, allow_logout=True))
    selector.set_profiles(_profiles(30))
    selector.resize(460, 600)
    selector.show()
    selector.activateWindow()
    _settle_layout()
    selector.focus_initial_card()
    for _ in range(30):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is selector._add_card
    position = selector._add_card.mapTo(selector._scroll.viewport(), QPoint())
    assert (
        selector._scroll.viewport()
        .rect()
        .contains(
            selector._add_card.rect().translated(position),
        )
    )
    QTest.keyClick(selector._add_card, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is selector._logout_button


def test_switch_overlay_uses_shared_selector_and_distinguishes_exit_from_cancel(own_widget):
    host = own_widget(QWidget())
    layout = QVBoxLayout(host)
    previous = QPushButton("Profile", host)
    layout.addWidget(previous)
    host.resize(960, 640)
    host.show()
    previous.setFocus()
    _settle_layout()
    profiles = _profiles(3)
    overlay = ProfileSwitchOverlay(host, profiles[1].id, profiles=profiles)
    overlay.show()
    _settle_layout()
    selector = overlay.findChild(ProfileSelector)
    cancelled, logout, selected = [], [], []
    overlay.cancelled.connect(lambda: cancelled.append(True))
    overlay.logout_requested.connect(lambda: logout.append(True))
    overlay.profile_selected.connect(selected.append)
    current = next(
        card for card in selector.findChildren(ProfileCard) if card.profile.id == profiles[1].id
    )
    assert current.accessibleDescription()
    current.click()
    selector._close_button.click()
    QTest.keyClick(selector._add_card, Qt.Key.Key_Escape)
    selector._logout_button.click()
    assert len(cancelled) == 3
    assert logout == [True]
    assert selected == []
    host.resize(460, 600)
    _settle_layout()
    assert overlay.geometry() == host.rect()


def test_destroyed_previous_focus_does_not_prevent_overlay_cancellation(own_widget):
    host = own_widget(QWidget())
    layout = QVBoxLayout(host)
    previous = QPushButton("Profile", host)
    layout.addWidget(previous)
    host.show()
    host.activateWindow()
    previous.setFocus()
    _settle_layout()
    overlay = ProfileSwitchOverlay(host, "profile-0", profiles=_profiles(1))
    overlay.show()
    cancelled = []
    overlay.cancelled.connect(lambda: cancelled.append(True))
    previous.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    overlay._cancel()

    assert cancelled == [True]


def test_profile_names_are_literal_and_do_not_register_shortcuts(own_widget):
    selector = own_widget(ProfileSelector())
    profile = ProfileInfo("north-south", "North &South && East")
    selector.set_profiles([profile])
    selector.show()
    _settle_layout()
    card = selector.findChild(ProfileCard)
    accessible = QAccessible.queryAccessibleInterface(card)
    assert card.shortcut().isEmpty()
    assert accessible.text(QAccessible.Text.Name) == profile.name
    assert card.toolTip() == profile.name


def test_profile_refresh_preserves_focus_and_exposes_full_long_name(own_widget):
    selector = own_widget(ProfileSelector())
    profiles = _profiles(3)
    selector.set_profiles(profiles)
    selector.show()
    selector.activateWindow()
    _settle_layout()
    selector.findChildren(ProfileCard)[1].setFocus()
    renamed = profiles[1].renamed("A very long profile name " * 6)
    selector.set_profiles([profiles[0], renamed, profiles[2]])
    _settle_layout()
    focused = QApplication.focusWidget()
    assert isinstance(focused, ProfileCard)
    assert focused.profile == renamed
    assert focused.toolTip() == renamed.name
    assert focused.accessibleName() == renamed.name


def test_live_language_change_uses_existing_selector_contexts(
    own_widget,
    compiled_translation_catalogs,
):
    profiles = _profiles(2)
    selector = own_widget(
        ProfileSelector(
            current_profile_id=profiles[0].id,
            allow_cancel=True,
            allow_logout=True,
        )
    )
    selector.set_profiles(profiles)
    translator = QTranslator()
    assert translator.load(str(compiled_translation_catalogs / "solin_pt_BR.qm"))
    app = QApplication.instance()
    assert app.installTranslator(translator)
    try:
        _settle_layout()
        assert selector._title.text() == catalog_translation(
            "pt_BR",
            "ProfileScreen",
            "Who is using Solin?",
        )
        assert selector._add_card.text() == catalog_translation(
            "pt_BR",
            "ProfileScreen",
            "New Profile",
        )
        assert selector._close_button.toolTip() == catalog_translation(
            "pt_BR",
            "ProfileSwitchOverlay",
            "Cancel profile switch",
        )
        assert selector._logout_button.accessibleName() == catalog_translation(
            "pt_BR",
            "ProfileSelector",
            "Sign out of profile",
        )
        assert selector._cards[0].accessibleDescription() == catalog_translation(
            "pt_BR",
            "ProfileSelector",
            "Current profile",
        )
    finally:
        app.removeTranslator(translator)


@pytest.mark.parametrize("count,force_selector", [(1, False), (1, True), (3, False)])
def test_startup_screen_shares_selector_and_preserves_single_profile_fast_path(
    own_widget,
    monkeypatch,
    count,
    force_selector,
):
    from solin.ui import profile_screen

    class OnboardingHost(QWidget):
        completed = Signal(str)
        cancelled = Signal()

        def __init__(self, **kwargs):
            super().__init__(kwargs["parent"])

        def shutdown(self):
            pass

    class Profiles:
        profiles = _profiles(count)
        active = None

        def has_profiles(self):
            return bool(self.profiles)

        def set_active(self, profile_id):
            self.active = profile_id

    monkeypatch.setattr(profile_screen, "OnboardingQmlHost", OnboardingHost)
    profiles = Profiles()
    screen = own_widget(
        profile_screen.ProfileScreen(
            profile_service=profiles,
            profile_settings_for=lambda _id: None,
            onboarding_service=None,
            obs_probe=None,
            congregation_lookup=None,
        )
    )
    ready = []
    screen.profile_ready.connect(ready.append)
    screen.resize(960, 640)
    screen.show()
    screen.start(force_selector=force_selector)
    _settle_layout()

    if count == 1 and not force_selector:
        assert ready == [profiles.profiles[0].id]
        assert profiles.active == profiles.profiles[0].id
    else:
        assert ready == []
        assert screen._stack.currentWidget() is screen.findChild(ProfileSelector)
        assert len(screen.findChildren(ProfileCard)) == count
        assert screen.width() == 960
