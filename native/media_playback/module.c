/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <obs-module.h>
#include <libavcodec/avcodec.h>
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libswscale/swscale.h>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("solin-media-source", "en-US")

extern struct obs_source_info ffmpeg_source;

static bool compatible_library(unsigned runtime, unsigned compiled)
{
	return AV_VERSION_MAJOR(runtime) == AV_VERSION_MAJOR(compiled) && runtime >= compiled;
}

bool obs_module_load(void)
{
	if (obs_get_version() != LIBOBS_API_VER ||
	    !compatible_library(avcodec_version(), LIBAVCODEC_VERSION_INT) ||
	    !compatible_library(avdevice_version(), LIBAVDEVICE_VERSION_INT) ||
	    !compatible_library(avformat_version(), LIBAVFORMAT_VERSION_INT) ||
	    !compatible_library(avutil_version(), LIBAVUTIL_VERSION_INT) ||
	    !compatible_library(swscale_version(), LIBSWSCALE_VERSION_INT)) {
		blog(LOG_ERROR, "Solin media source: incompatible OBS or FFmpeg runtime");
		return false;
	}
	obs_register_source(&ffmpeg_source);
	return true;
}

MODULE_EXPORT const char *obs_module_name(void)
{
	return "Solin media source";
}

MODULE_EXPORT const char *obs_module_description(void)
{
	return "OBS 32.1.2 playback with attached-picture selection and seek EOF fixes";
}
