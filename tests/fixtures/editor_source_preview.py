"""Deterministic editor-preview port for bridge and QML interaction tests."""

from collections import deque
from concurrent.futures import Future

from solin.core.scenes.engine import SceneSourcePreview


class EditorSourcePreview:
    def __init__(self, width: int = 1920, height: int = 1080) -> None:
        self.width = width
        self.height = height
        self.requests: list[tuple[str, str | None]] = []
        self.responses: deque[Future[SceneSourcePreview]] = deque()

    def __call__(self, scene_id: str, layer_id: str | None) -> Future[SceneSourcePreview]:
        self.requests.append((scene_id, layer_id))
        if layer_id is not None and self.responses:
            return self.responses.popleft()
        future: Future[SceneSourcePreview] = Future()
        future.set_result(
            SceneSourcePreview(self.width, self.height)
            if layer_id is not None else SceneSourcePreview()
        )
        return future
