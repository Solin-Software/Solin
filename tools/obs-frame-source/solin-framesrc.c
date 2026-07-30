/*
 * solin-framesrc — a minimal libobs async video source that displays frames
 * pushed into it from the application via obs_source_output_video().
 *
 * Why: Solin projects the live browser tab on the second monitor. Rather than
 * capture the browser's native X window (xcomposite), which goes black when the
 * operator switches panels (the window gets unmapped), we reuse the browser's
 * existing off-screen frame stream (WebKitGTK / CDP screencast — works while
 * hidden) and push those frames into THIS source. libobs then composites and
 * crossfades it like any other scene, and a future virtual-camera/recording
 * would capture it too — with none of the window-mapping fragility.
 *
 * The source itself is intentionally empty: OBS_SOURCE_ASYNC_VIDEO makes libobs
 * display whatever frames are pushed via obs_source_output_video(); width/height
 * come from the last pushed frame. All frame production stays in Python.
 *
 * GPL-2.0-or-later (links libobs).
 */
#include <obs-module.h>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("solin-framesrc", "en-US")

MODULE_EXPORT const char *obs_module_description(void)
{
	return "Solin async frame-injection source (frames pushed via obs_source_output_video)";
}

static const char *solin_frame_source_get_name(void *unused)
{
	UNUSED_PARAMETER(unused);
	return "Solin Frame Source";
}

static void *solin_frame_source_create(obs_data_t *settings, obs_source_t *source)
{
	UNUSED_PARAMETER(settings);
	UNUSED_PARAMETER(source);
	/* No per-source state is needed; return a non-NULL opaque handle so libobs
	 * treats creation as successful. */
	return bzalloc(1);
}

static void solin_frame_source_destroy(void *data)
{
	bfree(data);
}

struct obs_source_info solin_frame_source = {
	.id = "solin_frame_source",
	.type = OBS_SOURCE_TYPE_INPUT,
	.output_flags = OBS_SOURCE_ASYNC_VIDEO,
	.get_name = solin_frame_source_get_name,
	.create = solin_frame_source_create,
	.destroy = solin_frame_source_destroy,
	.icon_type = OBS_ICON_TYPE_CUSTOM,
};

bool obs_module_load(void)
{
	obs_register_source(&solin_frame_source);
	return true;
}
