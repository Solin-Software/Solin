from __future__ import annotations

import sys
from types import SimpleNamespace

from solin.core.foundation import settings_store
import solin.core.profiles.manager as profile_manager
from solin.bootstrap import application as main
from solin.bootstrap import profile_flow
from solin.bootstrap.runtime_args import parse_runtime_args


def test_parse_runtime_args_handles_profile_flags_and_existing_files(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"")

    runtime_args = parse_runtime_args([
        "solin",
        "--profile",
        "profile-1",
        "--create-profile",
        str(media_file),
        str(tmp_path / "missing.mp4"),
    ])

    assert runtime_args.requested_profile_id == "profile-1"
    assert runtime_args.create_profile is True
    assert runtime_args.media_files == (str(media_file),)


def test_parse_runtime_args_supports_profile_equals_form(tmp_path):
    csv_file = tmp_path / "poll.csv"
    csv_file.write_bytes(b"")

    runtime_args = parse_runtime_args([
        "solin",
        "--profile=profile-2",
        str(csv_file),
    ])

    assert runtime_args.requested_profile_id == "profile-2"
    assert runtime_args.create_profile is False
    assert runtime_args.media_files == (str(csv_file),)


def test_launch_main_window_schedules_startup_media_after_window_is_shown(monkeypatch):
    events = []
    file_args = ["clip.mp4", "song.mp3"]
    runtime_paths = object()
    profile_paths = object()

    class _MainWindow:
        def __init__(
            self,
            lang_manager,
            received_runtime_paths,
            received_profile_paths,
        ):
            self.lang_manager = lang_manager
            self.runtime_paths = received_runtime_paths
            self.profile_paths = received_profile_paths

        def show(self):
            events.append("show")

        def open_media_files(self, paths):
            events.append(("open_media_files", paths))

    monkeypatch.setitem(
        sys.modules,
        "solin.main_window",
        SimpleNamespace(MainWindow=_MainWindow),
    )
    monkeypatch.setattr(
        "solin.core.ui.titlebar.apply_titlebar_color",
        lambda window, color: events.append(("titlebar", window, color)),
    )
    monkeypatch.setattr(
        main,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda ms, callback: events.append(("timer", ms)) or callback()
        ),
    )

    window = main._launch_main_window(
        app=object(),
        lang_manager="lang",
        file_args=file_args,
        runtime_paths=runtime_paths,
        profile_paths=profile_paths,
    )

    assert window.lang_manager == "lang"
    assert window.runtime_paths is runtime_paths
    assert window.profile_paths is profile_paths
    assert events == [
        "show",
        ("titlebar", window, "#1A231F"),
        ("timer", 200),
        ("open_media_files", file_args),
    ]


def test_launch_main_window_without_startup_media_does_not_schedule_open(monkeypatch):
    events = []
    runtime_paths = object()
    profile_paths = object()

    class _MainWindow:
        def __init__(
            self,
            lang_manager,
            received_runtime_paths,
            received_profile_paths,
        ):
            self.lang_manager = lang_manager
            self.runtime_paths = received_runtime_paths
            self.profile_paths = received_profile_paths

        def show(self):
            events.append("show")

    monkeypatch.setitem(
        sys.modules,
        "solin.main_window",
        SimpleNamespace(MainWindow=_MainWindow),
    )
    monkeypatch.setattr(
        "solin.core.ui.titlebar.apply_titlebar_color",
        lambda window, color: events.append(("titlebar", color)),
    )
    monkeypatch.setattr(
        main,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda ms, callback: events.append(("timer", ms)) or callback()
        ),
    )

    main._launch_main_window(
        app=object(),
        lang_manager="lang",
        file_args=[],
        runtime_paths=runtime_paths,
        profile_paths=profile_paths,
    )

    assert events == ["show", ("titlebar", "#1A231F")]


def test_run_zoom_poll_standalone_keeps_window_alive_until_event_loop(monkeypatch):
    events = []
    window = object()

    class _App:
        def exec(self):
            events.append(("exec", getattr(self, "_solin_zoom_poll_window", None)))
            return 7

    def _launch(filepath, lang_manager):
        events.append(("launch", filepath, lang_manager))
        return window

    monkeypatch.setattr(main, "_launch_zoom_poll_window", _launch)

    app = _App()

    assert main._run_zoom_poll_standalone(app, "poll.csv", "lang") == 7
    assert app._solin_zoom_poll_window is window
    assert events == [
        ("launch", "poll.csv", "lang"),
        ("exec", window),
    ]


def test_relaunch_with_profile_persists_target_profile_starts_new_process_and_quits(
    monkeypatch,
):
    settings = []
    launches = []
    stopped = []

    class _Settings:
        def __init__(self, org_name, app_name):
            self.org_name = org_name
            self.app_name = app_name
            self.values = {}
            self.synced = False
            settings.append(self)

        def setValue(self, key, value):
            self.values[key] = value

        def sync(self):
            self.synced = True

    class _ProfileManager:
        def get_profile(self, profile_id):
            return object() if profile_id == "profile-b" else None

    class _App:
        def __init__(self):
            self.events = []

        def processEvents(self):
            self.events.append("processEvents")

        def quit(self):
            self.events.append("quit")

    class _Window:
        def __init__(self):
            self.closed = 0
            self.enabled = []
            self.shown = 0

        def close(self):
            self.closed += 1

        def setEnabled(self, enabled):
            self.enabled.append(enabled)

        def show(self):
            self.shown += 1

    app = _App()
    window = _Window()
    monkeypatch.setattr(profile_manager, "get", lambda: _ProfileManager())
    monkeypatch.setattr(settings_store, "QSettings", _Settings)
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda app, window: stopped.append((app, window)) or "ipc",
    )
    monkeypatch.setattr(profile_flow.single_instance, "restart_process_ipc", lambda *args: None)
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(
            startDetached=lambda program, args, cwd: launches.append(
                (program, args, cwd)
            )
            or True
        ),
    )
    monkeypatch.setattr(profile_flow.sys, "executable", "python.exe")
    monkeypatch.delattr(profile_flow.sys, "frozen", raising=False)

    profile_flow.relaunch_with_profile(app, "profile-b", window)

    assert settings[0].values == {"last_active_profile": "profile-b"}
    assert settings[0].synced is True
    assert stopped == [(app, window)]
    assert launches[0][0] == "python.exe"
    assert launches[0][1][-2:] == ["--profile", "profile-b"]
    assert launches[0][1][0].endswith("main.py")
    assert window.closed == 1
    assert window.enabled == []
    assert window.shown == 0
    assert app.events == ["processEvents", "quit"]


def test_relaunch_with_profile_restores_window_and_ipc_when_new_process_fails(
    monkeypatch,
):
    restored = []

    class _Settings:
        def __init__(self, *_args):
            pass

        def setValue(self, *_args):
            pass

        def sync(self):
            pass

    class _ProfileManager:
        def get_profile(self, profile_id):
            return object()

    class _App:
        def __init__(self):
            self.events = []

        def processEvents(self):
            self.events.append("processEvents")

        def quit(self):
            self.events.append("quit")

    class _Window:
        def __init__(self):
            self.closed = 0
            self.enabled = []
            self.shown = 0

        def close(self):
            self.closed += 1

        def setEnabled(self, enabled):
            self.enabled.append(enabled)

        def show(self):
            self.shown += 1

    app = _App()
    window = _Window()
    monkeypatch.setattr(profile_manager, "get", lambda: _ProfileManager())
    monkeypatch.setattr(settings_store, "QSettings", _Settings)
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda app, window: "ipc-server",
    )
    monkeypatch.setattr(
        profile_flow.single_instance,
        "restart_process_ipc",
        lambda app, server, window: restored.append((app, server, window)),
    )
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(startDetached=lambda *_args: False),
    )

    profile_flow.relaunch_with_profile(app, "profile-b", window)

    assert window.enabled == [True]
    assert window.shown == 1
    assert window.closed == 0
    assert restored == [(app, "ipc-server", window)]
    assert app.events == []


def test_relaunch_with_profile_ignores_unknown_profile(monkeypatch):
    launches = []

    class _ProfileManager:
        def get_profile(self, profile_id):
            return None

    monkeypatch.setattr(profile_manager, "get", lambda: _ProfileManager())
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda *_args: launches.append("stop"),
    )
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(startDetached=lambda *_args: launches.append("launch")),
    )

    profile_flow.relaunch_with_profile(app=object(), profile_id="missing", window=object())

    assert launches == []


def test_relaunch_to_profile_creator_persists_bootstrap_language_and_quits(
    monkeypatch,
):
    settings = []
    launches = []
    stopped = []

    class _Settings:
        def __init__(self, org_name, app_name):
            self.org_name = org_name
            self.app_name = app_name
            self.values = {}
            self.synced = False
            settings.append(self)

        def setValue(self, key, value):
            self.values[key] = value

        def sync(self):
            self.synced = True

    class _ProfileManager:
        active_id = "main_hall"

    class _App:
        def __init__(self):
            self.events = []

        def processEvents(self):
            self.events.append("processEvents")

        def quit(self):
            self.events.append("quit")

    class _Window:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    app = _App()
    window = _Window()
    monkeypatch.setattr(profile_manager, "get", lambda: _ProfileManager())
    monkeypatch.setattr(
        profile_flow.profile_settings,
        "app_settings",
        lambda: SimpleNamespace(app_language=lambda: "pt_BR"),
    )
    monkeypatch.setattr(settings_store, "QSettings", _Settings)
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda app, window: stopped.append((app, window)) or "ipc",
    )
    monkeypatch.setattr(profile_flow.single_instance, "restart_process_ipc", lambda *args: None)
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(
            startDetached=lambda program, args, cwd: launches.append(
                (program, args, cwd)
            )
            or True
        ),
    )
    monkeypatch.setattr(profile_flow.sys, "executable", "python.exe")
    monkeypatch.delattr(profile_flow.sys, "frozen", raising=False)

    profile_flow.relaunch_to_profile_creator(app, window)

    assert settings[0].values == {"bootstrap_language": "pt_BR"}
    assert settings[0].synced is True
    assert stopped == [(app, window)]
    assert launches[0][0] == "python.exe"
    assert launches[0][1][-1:] == ["--create-profile"]
    assert launches[0][1][0].endswith("main.py")
    assert window.closed == 1
    assert app.events == ["processEvents", "quit"]
