from pathlib import Path

from pyqttoast import ToastPosition, ToastPreset

from app.core.ui.notifications import NotificationCenter, _SynchronizedToast


class _FakeToast:
    instances = []
    position_relative_to = None
    static_values = {}
    reset_count = 0

    def __init__(self, parent):
        self.parent = parent
        self.values = {}
        self.shown = False
        self.__class__.instances.append(self)

    def __getattr__(self, name):
        if name == "applyPreset" or name.startswith("set"):
            return lambda *args: self.values.__setitem__(name, args)
        raise AttributeError(name)

    def show(self):
        self.shown = True

    @classmethod
    def setPositionRelativeToWidget(cls, widget):
        cls.position_relative_to = widget

    @classmethod
    def getPositionRelativeToWidget(cls):
        return cls.position_relative_to

    @classmethod
    def reset(cls):
        cls.position_relative_to = None
        cls.reset_count += 1

    @classmethod
    def setMovePositionWithWidget(cls, value):
        cls.static_values["move_with_widget"] = value

    @classmethod
    def setPosition(cls, value):
        cls.static_values["position"] = value

    @classmethod
    def setOffset(cls, x, y):
        cls.static_values["offset"] = (x, y)

    @classmethod
    def setSpacing(cls, value):
        cls.static_values["spacing"] = value

    @classmethod
    def setMaximumOnScreen(cls, value):
        cls.static_values["maximum"] = value


def _reset_fake_toast():
    _FakeToast.instances = []
    _FakeToast.position_relative_to = None
    _FakeToast.static_values = {}
    _FakeToast.reset_count = 0


def test_notification_center_configures_bottom_right_relative_to_anchor():
    _reset_fake_toast()
    anchor = object()

    NotificationCenter(anchor, toast_type=_FakeToast)

    assert _FakeToast.position_relative_to is anchor
    assert _FakeToast.static_values == {
        "move_with_widget": True,
        "position": ToastPosition.BOTTOM_RIGHT,
        "offset": (20, 64),
        "spacing": 10,
        "maximum": 3,
    }


def test_notification_center_applies_severity_theme_and_optional_detail():
    _reset_fake_toast()
    center = NotificationCenter(object(), toast_type=_FakeToast)

    assert center.error("Network unavailable", title="Download failed") is True

    toast = _FakeToast.instances[-1]
    assert toast.shown is True
    assert toast.values["applyPreset"] == (ToastPreset.ERROR_DARK,)
    assert toast.values["setTitle"] == ("Download failed",)
    assert toast.values["setText"] == ("Network unavailable",)
    assert toast.values["setDuration"] == (7000,)
    assert toast.values["setFixedWidth"] == (360,)
    assert toast.values["setStayOnTop"] == (False,)


def test_notification_center_deduplicates_only_within_configured_window():
    _reset_fake_toast()
    timestamps = iter((10.0, 11.0, 14.1))
    center = NotificationCenter(
        object(),
        toast_type=_FakeToast,
        clock=lambda: next(timestamps),
        dedupe_window_seconds=3.0,
    )

    assert center.warning("First", dedupe_key="same") is True
    assert center.warning("Duplicate", dedupe_key="same") is False
    assert center.warning("Later", dedupe_key="same") is True
    assert len(_FakeToast.instances) == 2


def test_notification_center_shutdown_resets_owned_toasts():
    _reset_fake_toast()
    anchor = object()
    center = NotificationCenter(anchor, toast_type=_FakeToast)

    center.shutdown()

    assert _FakeToast.position_relative_to is None
    assert _FakeToast.reset_count == 1


def test_nuitka_builds_include_pyqttoast_runtime_data():
    for path in (
        Path(".github/workflows/build-solin-windows.yml"),
        Path(".github/workflows/build-solin-macos.yml"),
        Path("build/scripts/build_solin.bat"),
    ):
        assert "--include-package-data=pyqttoast" in path.read_text(encoding="utf-8")


def test_synchronized_toast_bar_uses_elapsed_time_instead_of_callback_count():
    assert _SynchronizedToast._remaining_bar_width(360, 4000, 0) == 360
    assert _SynchronizedToast._remaining_bar_width(360, 4000, 1000) == 270
    assert _SynchronizedToast._remaining_bar_width(360, 4000, 3999) == 1
    assert _SynchronizedToast._remaining_bar_width(360, 4000, 4000) == 0
