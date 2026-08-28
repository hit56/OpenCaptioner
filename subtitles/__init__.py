from subtitles._runtime import configure
from subtitles.artifact_sync import sync_session_artifacts_to_gateway


async def run_video_subtitle_pipeline(*args, **kwargs):
    from subtitles.pipeline import run_video_subtitle_pipeline as _impl
    return await _impl(*args, **kwargs)


def run_video_subtitle_pipeline_sync(*args, **kwargs):
    from subtitles.pipeline import run_video_subtitle_pipeline_sync as _impl
    return _impl(*args, **kwargs)


def auto_generate_subtitle_video_sync(*args, **kwargs):
    from subtitles.burn_in import auto_generate_subtitle_video_sync as _impl
    return _impl(*args, **kwargs)


__all__ = [
    "configure",
    "run_video_subtitle_pipeline",
    "run_video_subtitle_pipeline_sync",
    "sync_session_artifacts_to_gateway",
    "auto_generate_subtitle_video_sync",
]
