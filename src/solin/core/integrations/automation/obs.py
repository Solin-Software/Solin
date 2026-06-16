"""
OBSWebSocketService — integração com OBS Studio via WebSocket Protocol v5.

Thread-safety
─────────────
  Worker roda em threading.Thread daemon.
  Toda comunicação de volta para a main thread é via Qt Signals internos
  (QueuedConnection automático) — nunca via QMetaObject.invokeMethod.

Eventos OBS suportados
──────────────────────
  SceneListChanged → atualiza lista de cenas automaticamente (sem botão manual)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
import uuid
from enum import Enum, auto

from PySide6.QtCore import QObject, Signal, QTimer

from solin.core.integrations.automation.settings import OBSConnectionSettings

log = logging.getLogger(__name__)


class OBSConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING   = auto()
    CONNECTED    = auto()
    ERROR        = auto()


# ── OBS WebSocket v5 opcodes ──────────────────────────────────────────────────

_OP_HELLO      = 0
_OP_IDENTIFY   = 1
_OP_IDENTIFIED = 2
_OP_REQUEST    = 6
_OP_RESPONSE   = 7
_OP_EVENT      = 5

# Event types we care about
_EVT_SCENE_LIST_CHANGED    = "SceneListChanged"
_EVT_SCENE_CHANGED         = "CurrentProgramSceneChanged"
_EVT_RECORD_STATE_CHANGED  = "RecordStateChanged"


def _make_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()
    ).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()
    ).decode()


# ── OBSWebSocketService ───────────────────────────────────────────────────────

class OBSWebSocketService(QObject):
    """
    Gerencia conexão persistente com OBS Studio WebSocket v5.

    Public Signals
    ──────────────
    state_changed(OBSConnectionState, str)
        Emitido na main thread a cada mudança de estado.

    scenes_updated(list[str])
        Emitido na main thread após conexão ou quando OBS muda suas cenas.
        A UI deve sempre reagir a este signal para manter os selects atualizados.
    """

    state_changed  = Signal(object, str)   # (OBSConnectionState, message)
    scenes_updated = Signal(list)          # list[str]
    current_scene_changed = Signal(str)    # nome da cena ativa no OBS
    recording_state_changed = Signal(bool) # True = recording, False = stopped

    # ── Sinais internos bridge thread → main thread ───────────────────────
    _sig_connected = Signal(int, list)
    _sig_disconnected = Signal(int, str)
    _sig_error = Signal(int, str)
    _sig_scenes = Signal(int, list)
    _sig_scene_changed = Signal(int, str)
    _sig_record_state = Signal(int, bool)
    _sig_worker_stopped = Signal(int)

    _BACKOFF_BASE = 2
    _BACKOFF_MAX  = 30

    def __init__(self, settings: OBSConnectionSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._state    = OBSConnectionState.DISCONNECTED
        self._scenes: list[str] = []
        self._current_scene: str | None = None   # cena ativa atualmente no OBS
        self._is_recording: bool = False
        self._ws       = None
        self._ws_lock  = threading.Lock()
        self._thread: threading.Thread | None = None
        self._run_stop_evt: threading.Event | None = None
        self._generation = 0
        self._active = False
        self._restart_requested = False

        # Flag to request a scene refresh from the event loop thread.
        # This avoids the race condition of two threads calling ws.recv().
        self._refresh_scenes_evt = threading.Event()

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._attempt_connect)
        self._backoff = self._BACKOFF_BASE

        # Periodic retry timer: re-fetches scenes when connected but list is empty
        self._scenes_retry_timer = QTimer(self)
        self._scenes_retry_timer.setInterval(2000)
        self._scenes_retry_timer.timeout.connect(self._on_scenes_retry_tick)
        self._scenes_retry_count = 0
        self._SCENES_RETRY_MAX   = 5

        # Scene-change debounce: collapses rapid successive calls into one
        self._scene_timer = QTimer(self)
        self._scene_timer.setSingleShot(True)
        self._scene_timer.setInterval(400)
        self._scene_timer.timeout.connect(self._flush_pending_scene)
        self._pending_scene: str | None = None

        # Wire internal bridge signals → main-thread slots
        self._sig_connected.connect(self._on_connected_main)
        self._sig_disconnected.connect(self._on_disconnected_main)
        self._sig_error.connect(self._on_error_main)
        self._sig_scenes.connect(self._on_scenes_updated_main)
        self._sig_scene_changed.connect(self._on_scene_changed_main)
        self._sig_record_state.connect(self._on_record_state_main)
        self._sig_worker_stopped.connect(self._on_worker_stopped)

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def state(self) -> OBSConnectionState:
        return self._state

    @property
    def scenes(self) -> list[str]:
        return list(self._scenes)

    @property
    def is_connected(self) -> bool:
        return self._state == OBSConnectionState.CONNECTED

    @property
    def current_scene(self) -> str | None:
        """Nome da cena ativa no momento no OBS (None se desconectado ou desconhecido)."""
        return self._current_scene

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def start(self):
        """Inicia a integração se port estiver configurada."""
        if not self._config_ok():
            return
        self._active = True
        if self._thread is not None and self._thread.is_alive():
            self._restart_requested = True
            self._request_stop()
            return
        self._attempt_connect()

    def stop(self, *, wait: bool = False, timeout: float = 7.0):
        """Para completamente; não tenta reconectar."""
        self._active = False
        self._restart_requested = False
        self._retry_timer.stop()
        self._scene_timer.stop()
        self._scenes_retry_timer.stop()
        self._request_stop()
        self._close_ws()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
            if thread.is_alive():
                log.warning("OBS worker did not stop within %.1f seconds", timeout)
            else:
                self._finish_run(self._generation)
        self._current_scene = None
        self._is_recording = False
        self._set_state(OBSConnectionState.DISCONNECTED, "Disconnected")

    def request_scene_change(self, scene_name: str):
        """
        Solicita troca de cena com debounce de 400 ms.
        Chamadas rápidas em sequência colapsam na última.
        """
        if not self.is_connected or not scene_name:
            return
        self._pending_scene = scene_name
        if not self._scene_timer.isActive():
            self._scene_timer.start()

    def toggle_recording(self):
        """Toggle OBS recording on/off."""
        if not self.is_connected:
            return
        self._send_request("ToggleRecord")

    def _send_request(self, request_type: str, request_data: dict | None = None):
        with self._ws_lock:
            ws = self._ws
        if not ws:
            return
        try:
            d = {
                "requestType": request_type,
                "requestId": str(uuid.uuid4())[:8],
            }
            if request_data:
                d["requestData"] = request_data
            ws.send(json.dumps({"op": _OP_REQUEST, "d": d}))
        except Exception as exc:  # noqa: BLE001 - websocket-client request boundary
            log.warning("OBS %s failed: %s", request_type, exc)

    # ── Internals ─────────────────────────────────────────────────────────

    def _config_ok(self) -> bool:
        return self._settings.is_configured()

    def _attempt_connect(self):
        if self._state == OBSConnectionState.CONNECTED:
            return
        if not self._active or not self._config_ok():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._set_state(OBSConnectionState.CONNECTING, "Connecting to OBS…")
        self._generation += 1
        generation = self._generation
        stop_event = threading.Event()
        self._run_stop_evt = stop_event
        self._refresh_scenes_evt.clear()
        self._thread = threading.Thread(
            target=self._worker_entry,
            args=(generation, stop_event),
            daemon=True,
            name="obs-ws",
        )
        self._thread.start()

    def _request_stop(self) -> None:
        stop_event = self._run_stop_evt
        if stop_event is not None:
            stop_event.set()

    def _close_ws(self):
        with self._ws_lock:
            ws, self._ws = self._ws, None
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - websocket-client cleanup boundary
                log.debug("Failed to close OBS websocket", exc_info=True)

    def _set_state(self, state: OBSConnectionState, msg: str):
        self._state = state
        self.state_changed.emit(state, msg)

    def _schedule_retry(self):
        if not self._active:
            return
        self._retry_timer.start(self._backoff * 1000)
        self._backoff = min(self._backoff * 2, self._BACKOFF_MAX)

    def _flush_pending_scene(self):
        scene, self._pending_scene = self._pending_scene, None
        if scene:
            self._send_set_scene(scene)

    def _send_set_scene(self, scene_name: str):
        with self._ws_lock:
            ws = self._ws
        if not ws:
            return
        try:
            ws.send(json.dumps({
                "op": _OP_REQUEST,
                "d": {
                    "requestType": "SetCurrentProgramScene",
                    "requestId":   str(uuid.uuid4())[:8],
                    "requestData": {"sceneName": scene_name},
                }
            }))
        except Exception as exc:  # noqa: BLE001 - websocket-client request boundary
            log.warning("OBS SetCurrentProgramScene failed: %s", exc)

    # ── Worker: connect + event loop ──────────────────────────────────────

    def _worker_entry(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        try:
            self._worker_connect(generation, stop_event)
        except Exception as exc:  # noqa: BLE001 - automation worker thread boundary
            if not stop_event.is_set():
                try:
                    self._sig_error.emit(generation, str(exc))
                except RuntimeError:
                    log.debug("OBS service was destroyed before worker error delivery")
        finally:
            try:
                self._sig_worker_stopped.emit(generation)
            except RuntimeError:
                return

    def _worker_connect(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        try:
            import websocket
        except ImportError:
            self._sig_error.emit(
                generation,
                "websocket-client not installed.\nRun: pip install websocket-client"
            )
            return

        port = self._settings.websocket_port()
        password = self._settings.password()

        try:
            ws = websocket.create_connection(f"ws://localhost:{port}", timeout=6)
        except Exception as exc:  # noqa: BLE001 - websocket-client connection boundary
            self._sig_disconnected.emit(generation, str(exc))
            return

        if stop_event.is_set():
            ws.close()
            return
        with self._ws_lock:
            self._ws = ws

        try:
            scenes = self._do_handshake(ws, password, generation)
        except Exception as exc:  # noqa: BLE001 - OBS websocket protocol boundary
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - websocket-client cleanup boundary
                log.debug("Failed to close OBS websocket after handshake error", exc_info=True)
            with self._ws_lock:
                if self._ws is ws:
                    self._ws = None
            self._sig_error.emit(generation, str(exc))
            return

        if stop_event.is_set():
            ws.close()
            with self._ws_lock:
                if self._ws is ws:
                    self._ws = None
            return

        self._sig_connected.emit(generation, scenes)

        # ── Event loop ────────────────────────────────────────────────────
        ws.settimeout(2.0)
        while not stop_event.is_set():
            # Check if the main thread requested a scene refresh
            if self._refresh_scenes_evt.is_set():
                self._refresh_scenes_evt.clear()
                try:
                    fresh = self._request_scenes(ws)
                    if fresh:
                        self._sig_scenes.emit(generation, fresh)
                except Exception as exc:  # noqa: BLE001 - OBS websocket protocol boundary
                    log.debug("OBS scene refresh failed: %s", exc)

            try:
                raw = ws.recv()
                if raw:
                    self._handle_event(json.loads(raw), generation)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001 - websocket-client receive boundary
                if not stop_event.is_set():
                    self._sig_disconnected.emit(generation, str(exc))
                break

        with self._ws_lock:
            if self._ws is ws:
                self._ws = None

    def _handle_event(self, msg: dict, generation: int):
        """
        Processa mensagens recebidas do OBS durante o event loop.
        Trata: SceneListChanged, CurrentProgramSceneChanged.
        """
        if msg.get("op") != _OP_EVENT:
            return
        event_type = msg.get("d", {}).get("eventType", "")

        if event_type == _EVT_SCENE_LIST_CHANGED:
            # Re-fetch the updated scene list and notify main thread.
            # We are already on the event-loop thread, so it is safe to call
            # _request_scenes directly — no concurrent recv() race.
            with self._ws_lock:
                ws = self._ws
            if ws:
                try:
                    scenes = self._request_scenes(ws)
                    self._sig_scenes.emit(generation, scenes)
                except Exception as exc:  # noqa: BLE001 - OBS websocket protocol boundary
                    log.warning("OBS SceneListChanged re-fetch failed: %s", exc)

        elif event_type == _EVT_SCENE_CHANGED:
            scene_name = msg.get("d", {}).get("eventData", {}).get("sceneName", "")
            if scene_name:
                self._sig_scene_changed.emit(generation, scene_name)

        elif event_type == _EVT_RECORD_STATE_CHANGED:
            state = msg.get("d", {}).get("eventData", {}).get("outputState", "")
            # OBS states: OBS_WEBSOCKET_OUTPUT_STARTED, OBS_WEBSOCKET_OUTPUT_STOPPED, etc.
            is_rec = state in ("OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_RESUMED")
            self._sig_record_state.emit(generation, is_rec)

    def _do_handshake(self, ws, password: str, generation: int) -> list[str]:
        """Executa Hello→Identify→Identified. Retorna lista de cenas."""
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("op") != _OP_HELLO:
            raise ValueError(f"Expected Hello (op=0), got op={msg.get('op')}")

        hello_d  = msg["d"]
        identify = {"op": _OP_IDENTIFY, "d": {"rpcVersion": hello_d.get("rpcVersion", 1)}}

        auth_info = hello_d.get("authentication")
        if auth_info:
            if not password:
                raise ValueError(
                    "OBS requires a password but none was provided."
                )
            identify["d"]["authentication"] = _make_auth(
                password, auth_info["salt"], auth_info["challenge"]
            )

        ws.send(json.dumps(identify))

        raw2 = ws.recv()
        if raw2 == '' or raw2 == None:
            raise ValueError(
                "Incorrect password."
            )
        msg2 = json.loads(raw2)
        if msg2.get("op") != _OP_IDENTIFIED:
            raise ValueError(
                f"Authentication failed (op={msg2.get('op')}). Check your password."
            )

        scenes = self._request_scenes(ws)
        current = self._request_current_scene(ws)
        if current:
            # Entrega já pelo sig_scene_changed para popular _current_scene
            # antes do sig_connected ser processado na main thread.
            self._sig_scene_changed.emit(generation, current)

        # Fetch initial recording status
        rec_status = self._request_record_status(ws)
        self._sig_record_state.emit(generation, rec_status)

        return scenes

    def _request_scenes(self, ws) -> list[str]:
        """Envia GetSceneList e retorna nomes em ordem de criação."""
        import websocket
        req_id = str(uuid.uuid4())[:8]
        ws.send(json.dumps({
            "op": _OP_REQUEST,
            "d":  {"requestType": "GetSceneList", "requestId": req_id},
        }))
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            m = json.loads(raw)
            if m.get("op") == _OP_RESPONSE and m["d"].get("requestId") == req_id:
                raw_scenes = m["d"].get("responseData", {}).get("scenes", [])
                return [s["sceneName"] for s in reversed(raw_scenes)]
        return []

    def _request_current_scene(self, ws) -> str:
        """Envia GetCurrentProgramScene e retorna o nome da cena ativa."""
        import websocket
        req_id = str(uuid.uuid4())[:8]
        ws.send(json.dumps({
            "op": _OP_REQUEST,
            "d":  {"requestType": "GetCurrentProgramScene", "requestId": req_id},
        }))
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            m = json.loads(raw)
            if m.get("op") == _OP_RESPONSE and m["d"].get("requestId") == req_id:
                return m["d"].get("responseData", {}).get("currentProgramSceneName", "")
        return ""

    def _request_record_status(self, ws) -> bool:
        """Envia GetRecordStatus e retorna se está gravando."""
        import websocket
        req_id = str(uuid.uuid4())[:8]
        ws.send(json.dumps({
            "op": _OP_REQUEST,
            "d":  {"requestType": "GetRecordStatus", "requestId": req_id},
        }))
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            m = json.loads(raw)
            if m.get("op") == _OP_RESPONSE and m["d"].get("requestId") == req_id:
                return bool(m["d"].get("responseData", {}).get("outputActive", False))
        return False

    # ── Main-thread slots (via QueuedConnection) ──────────────────────────

    def _accept_generation(self, generation: int) -> bool:
        return (
            self._active
            and generation == self._generation
            and self._run_stop_evt is not None
            and not self._run_stop_evt.is_set()
        )

    def _on_connected_main(self, generation: int, scenes: list):
        if not self._accept_generation(generation):
            return
        self._scenes  = list(scenes)
        self._backoff = self._BACKOFF_BASE
        self._set_state(OBSConnectionState.CONNECTED, "Connected to OBS Studio")
        self.scenes_updated.emit(self._scenes)
        if not self._scenes:
            self._start_scenes_retry()

    def _start_scenes_retry(self):
        """Begin periodic re-fetches until scenes arrive (up to _SCENES_RETRY_MAX)."""
        self._scenes_retry_count = 0
        self._scenes_retry_timer.start()

    def _on_scenes_retry_tick(self):
        """Called every 2 s while scenes are still empty after connection."""
        self._scenes_retry_count += 1
        if not self.is_connected or self._scenes or self._scenes_retry_count > self._SCENES_RETRY_MAX:
            self._scenes_retry_timer.stop()
            return
        # Signal the event-loop thread to do the actual WebSocket I/O
        self._refresh_scenes_evt.set()

    def request_scenes_refresh(self):
        """Public API: re-fetch scene list from OBS (async, emits scenes_updated)."""
        if self.is_connected:
            self._refresh_scenes_evt.set()

    def _on_disconnected_main(self, generation: int, reason: str):
        if not self._accept_generation(generation):
            return
        if self._state == OBSConnectionState.DISCONNECTED:
            return
        self._is_recording = False
        self.recording_state_changed.emit(False)
        self._set_state(OBSConnectionState.DISCONNECTED, reason)
        self._schedule_retry()

    def _on_error_main(self, generation: int, message: str):
        if not self._accept_generation(generation):
            return
        self._is_recording = False
        self.recording_state_changed.emit(False)
        self._set_state(OBSConnectionState.ERROR, message)
        # Auth errors: wait for user to fix config before retrying
        if "password" in message.lower() or "auth" in message.lower():
            return
        self._schedule_retry()

    def _on_scenes_updated_main(self, generation: int, scenes: list):
        """Chamado quando OBS emite SceneListChanged."""
        if not self._accept_generation(generation):
            return
        self._scenes = list(scenes)
        self.scenes_updated.emit(self._scenes)

    def _on_scene_changed_main(self, generation: int, scene_name: str):
        """Chamado quando OBS troca de cena (CurrentProgramSceneChanged ou handshake)."""
        if not self._accept_generation(generation):
            return
        self._current_scene = scene_name
        self.current_scene_changed.emit(scene_name)

    def _on_record_state_main(self, generation: int, is_recording: bool):
        """Chamado quando OBS muda estado de gravação."""
        if not self._accept_generation(generation):
            return
        self._is_recording = is_recording
        self.recording_state_changed.emit(is_recording)

    def _on_worker_stopped(self, generation: int) -> None:
        self._finish_run(generation)

    def _finish_run(self, generation: int) -> None:
        if (
            generation != self._generation
            or (self._thread is None and self._run_stop_evt is None)
        ):
            return
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.25)
            if thread.is_alive():
                QTimer.singleShot(10, lambda: self._finish_run(generation))
                return
        self._thread = None
        self._run_stop_evt = None
        if self._restart_requested and self._active:
            self._restart_requested = False
            self._attempt_connect()
