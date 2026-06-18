import solin.widgets.projection.bar as projection_bar
from solin.widgets.projection.audio import ProjectionAudioMixin


def test_projection_bar_uses_audio_mixin():
    assert projection_bar.ProjectionAudioMixin is ProjectionAudioMixin
    assert issubclass(projection_bar.ProjectionBar, ProjectionAudioMixin)
    assert projection_bar.ProjectionBar.set_cover_art is ProjectionAudioMixin.set_cover_art
    assert (
        projection_bar.ProjectionBar._render_wave_frame
        is ProjectionAudioMixin._render_wave_frame
    )
