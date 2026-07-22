import pytest

from solin.core.media.operations import (
    MediaOperationPresentation,
    MediaOperationProgress,
    MediaOperationRecord,
    MediaOperationSpec,
    MediaOperationState,
)


def test_operation_progress_supports_determinate_and_indeterminate_work() -> None:
    indeterminate = MediaOperationProgress(MediaOperationState.PREPARING)
    determinate = MediaOperationProgress(
        MediaOperationState.COPYING,
        completed=25,
        total=100,
    )

    assert indeterminate.ratio == -1.0
    assert determinate.ratio == 0.25


@pytest.mark.parametrize(
    ("completed", "total"),
    [(-1, 0), (0, -1), (True, 1)],
)
def test_operation_progress_rejects_invalid_counts(completed, total) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        MediaOperationProgress(
            MediaOperationState.PROCESSING,
            completed=completed,
            total=total,
        )


def test_operation_spec_requires_a_stable_conflict_identity() -> None:
    with pytest.raises(ValueError, match="conflict_key"):
        MediaOperationSpec(
            operation_id="operation-1",
            scope_id="playlist:1",
            operation_type="copy",
            conflict_key="",
            presentation=MediaOperationPresentation.TREE_LOCAL,
            runner=lambda _progress, _cancellation: None,
            commit=lambda _result: None,
        )


def test_operation_record_progress_is_bounded() -> None:
    record = MediaOperationRecord(
        operation_id="operation-1",
        scope_id="playlist:1",
        operation_type="copy",
        presentation=MediaOperationPresentation.TREE_LOCAL,
        state=MediaOperationState.COPYING,
        completed=150,
        total=100,
    )

    assert record.progress == 1.0
