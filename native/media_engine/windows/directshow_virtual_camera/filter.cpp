#include "filter.hpp"

#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <dvdmedia.h>
#include <d3d9.h>
#include <dxva2api.h>
#include <ks.h>
#include <ksmedia.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <limits>
#include <new>
#include <thread>

namespace solin::media_engine::windows_virtual_camera {
namespace {

constexpr REFERENCE_TIME kUnitsPerSecond = 10'000'000LL;
constexpr REFERENCE_TIME kNominalFrameDuration = kUnitsPerSecond / 30LL;

[[nodiscard]] DWORD color_control_flags() noexcept {
    DXVA2_ExtendedFormat color{};
    color.VideoTransferMatrix = DXVA2_VideoTransferMatrix_BT709;
    color.NominalRange = DXVA2_NominalRange_16_235;
    color.VideoChromaSubsampling = DXVA2_VideoChromaSubsampling_MPEG2;
    color.VideoPrimaries = DXVA2_VideoPrimaries_BT709;
    color.VideoTransferFunction = DXVA2_VideoTransFunc_709;
    return AMCONTROL_USED | AMCONTROL_COLORINFO_PRESENT |
           (color.value & 0xFFFFFF00U);
}

[[nodiscard]] const GUID& subtype_for(
    const DirectShowPixelFormat pixel_format) noexcept {
    return pixel_format == DirectShowPixelFormat::nv12 ? MEDIASUBTYPE_NV12
                                                        : MEDIASUBTYPE_YUY2;
}

[[nodiscard]] const DirectShowPixelFormat* pixel_format_for(
    const GUID& subtype) noexcept {
    static constexpr DirectShowPixelFormat nv12 = DirectShowPixelFormat::nv12;
    static constexpr DirectShowPixelFormat yuy2 = DirectShowPixelFormat::yuy2;
    if (subtype == MEDIASUBTYPE_NV12) {
        return &nv12;
    }
    if (subtype == MEDIASUBTYPE_YUY2) {
        return &yuy2;
    }
    return nullptr;
}

[[nodiscard]] REFERENCE_TIME frame_time(const std::uint64_t index) noexcept {
    return directshow_frame_time_100ns(index);
}

[[nodiscard]] std::chrono::steady_clock::time_point frame_due_time(
    const std::chrono::steady_clock::time_point started_at,
    const std::uint64_t index) noexcept {
    return started_at + std::chrono::seconds{index / 30U} +
           std::chrono::nanoseconds{static_cast<std::int64_t>(
               (index % 30U) * 1'000'000'000ULL / 30U)};
}

} // namespace

HRESULT configure_media_type(const DirectShowMediaProfile& profile,
                             CMediaType& media_type) noexcept {
    try {
        if (find_media_profile(profile.width, profile.height,
                               profile.pixel_format, profile.fps_numerator,
                               profile.fps_denominator) == nullptr ||
            profile.sample_size() >
                static_cast<std::size_t>((std::numeric_limits<LONG>::max)())) {
            return E_INVALIDARG;
        }
        media_type.InitMediaType();
        media_type.SetType(&MEDIATYPE_Video);
        const auto& subtype = subtype_for(profile.pixel_format);
        media_type.SetSubtype(&subtype);
        media_type.SetFormatType(&FORMAT_VideoInfo2);
        media_type.SetTemporalCompression(FALSE);
        media_type.SetSampleSize(static_cast<ULONG>(profile.sample_size()));
        auto* video_info = reinterpret_cast<VIDEOINFOHEADER2*>(
            media_type.AllocFormatBuffer(sizeof(VIDEOINFOHEADER2)));
        if (video_info == nullptr) {
            return E_OUTOFMEMORY;
        }
        std::memset(video_info, 0, sizeof(*video_info));
        video_info->AvgTimePerFrame = kNominalFrameDuration;
        video_info->dwInterlaceFlags = 0U;
        video_info->dwPictAspectRatioX = profile.width;
        video_info->dwPictAspectRatioY = profile.height;
        video_info->dwControlFlags = color_control_flags();
        auto& bitmap = video_info->bmiHeader;
        bitmap.biSize = sizeof(BITMAPINFOHEADER);
        bitmap.biWidth = static_cast<LONG>(profile.width);
        bitmap.biHeight = static_cast<LONG>(profile.height);
        bitmap.biPlanes = 1U;
        bitmap.biBitCount = profile.pixel_format == DirectShowPixelFormat::nv12
                                ? 12U
                                : 16U;
        bitmap.biCompression = subtype.Data1;
        bitmap.biSizeImage = static_cast<DWORD>(profile.sample_size());
        return S_OK;
    } catch (...) {
        return E_FAIL;
    }
}

const DirectShowMediaProfile* profile_from_media_type(
    const AM_MEDIA_TYPE& media_type) noexcept {
    if (media_type.majortype != MEDIATYPE_Video ||
        media_type.formattype != FORMAT_VideoInfo2 ||
        media_type.cbFormat < sizeof(VIDEOINFOHEADER2) ||
        media_type.pbFormat == nullptr || media_type.bFixedSizeSamples == FALSE ||
        media_type.bTemporalCompression != FALSE) {
        return nullptr;
    }
    const auto* pixel_format = pixel_format_for(media_type.subtype);
    if (pixel_format == nullptr) {
        return nullptr;
    }
    const auto* video_info =
        reinterpret_cast<const VIDEOINFOHEADER2*>(media_type.pbFormat);
    const auto& bitmap = video_info->bmiHeader;
    const auto expected_bit_count =
        *pixel_format == DirectShowPixelFormat::nv12 ? 12U : 16U;
    if (bitmap.biWidth <= 0 || bitmap.biHeight <= 0 || bitmap.biPlanes != 1U ||
        bitmap.biSize != sizeof(BITMAPINFOHEADER) ||
        bitmap.biBitCount != expected_bit_count ||
        video_info->AvgTimePerFrame != kNominalFrameDuration ||
        video_info->dwInterlaceFlags != 0U ||
        video_info->dwPictAspectRatioX !=
            static_cast<DWORD>(bitmap.biWidth) ||
        video_info->dwPictAspectRatioY !=
            static_cast<DWORD>(bitmap.biHeight) ||
        video_info->dwControlFlags != color_control_flags()) {
        return nullptr;
    }
    const auto* profile = find_media_profile(
        static_cast<std::uint32_t>(bitmap.biWidth),
        static_cast<std::uint32_t>(bitmap.biHeight), *pixel_format, 30U, 1U);
    if (profile == nullptr || bitmap.biCompression != media_type.subtype.Data1 ||
        bitmap.biSizeImage != profile->sample_size() ||
        media_type.lSampleSize != profile->sample_size()) {
        return nullptr;
    }
    return profile;
}

DirectShowCapturePin::DirectShowCapturePin(HRESULT* result, CSource* filter)
    : CSourceStream(NAME("Solin Virtual Camera Capture Pin"), result, filter,
                    L"Capture") {
    if (result == nullptr || FAILED(*result)) {
        return;
    }
    *result = select_profile(kMediaProfiles.front());
}

STDMETHODIMP DirectShowCapturePin::NonDelegatingQueryInterface(
    REFIID interface_id, void** object) {
    if (interface_id == IID_IAMStreamConfig) {
        return GetInterface(static_cast<IAMStreamConfig*>(this), object);
    }
    if (interface_id == IID_IKsPropertySet) {
        return GetInterface(static_cast<IKsPropertySet*>(this), object);
    }
    if (interface_id == IID_IAMPushSource || interface_id == IID_IAMLatency) {
        return GetInterface(static_cast<IAMPushSource*>(this), object);
    }
    return CSourceStream::NonDelegatingQueryInterface(interface_id, object);
}

HRESULT DirectShowCapturePin::FillBuffer(IMediaSample* sample) {
    if (sample == nullptr || adapter_ == nullptr) {
        return E_POINTER;
    }
    const auto now = std::chrono::steady_clock::now();
    bool cadence_discontinuity = false;
    auto due = frame_due_time(deadline_epoch_, deadline_index_);
    constexpr auto maximum_lateness =
        std::chrono::nanoseconds{1'000'000'000LL / 30LL};
    if (now > due + maximum_lateness) {
        deadline_epoch_ = now;
        deadline_index_ = 0U;
        due = now;
        cadence_discontinuity = true;
    }
    std::this_thread::sleep_until(due);

    BYTE* bytes = nullptr;
    auto result = sample->GetPointer(&bytes);
    if (FAILED(result) || bytes == nullptr ||
        sample->GetSize() < static_cast<LONG>(last_output_.size())) {
        return FAILED(result) ? result : VFW_E_BUFFER_OVERFLOW;
    }

    struct VisitState final {
        std::uint64_t generation_before{0U};
        std::uint64_t visited_sequence{0U};
        bool adapted{false};
        bool frame_changed{false};
    } visit_state;
    const auto generation_before = provider_.generation();
    visit_state.generation_before = generation_before;
    const auto visited = provider_.visit_live_frame(
        [this, &visit_state](const VideoFrameView& frame) {
            visit_state.adapted = false;
            visit_state.frame_changed = false;
            visit_state.visited_sequence = frame.sequence;
            if (have_last_input_sequence_ &&
                visit_state.generation_before == last_generation_ &&
                frame.sequence == last_input_sequence_) {
                visit_state.adapted = true;
                return;
            }
            visit_state.adapted = adapter_->adapt(frame, staging_output_);
            visit_state.frame_changed = visit_state.adapted;
        });
    const auto generation = provider_.generation();
    bool discontinuity = first_sample_ || cadence_discontinuity ||
                         generation != last_generation_;
    const auto stable_live_frame = visited && visit_state.adapted &&
                                   generation != 0U &&
                                   generation_before == generation;
    if (!stable_live_frame) {
        if (generation == 0U || !last_frame_was_live_) {
            if (!adapter_->write_standby(last_output_)) {
                return E_FAIL;
            }
            discontinuity = discontinuity || last_frame_was_live_;
            last_frame_was_live_ = false;
            have_last_input_sequence_ = false;
        }
    } else {
        if (visit_state.frame_changed) {
            last_output_.swap(staging_output_);
            last_input_sequence_ = visit_state.visited_sequence;
            have_last_input_sequence_ = true;
        }
        discontinuity = discontinuity || !last_frame_was_live_ ||
                        generation_before != generation;
        last_frame_was_live_ = true;
    }
    last_generation_ = generation;
    std::memcpy(bytes, last_output_.data(), last_output_.size());
    sample->SetActualDataLength(static_cast<LONG>(last_output_.size()));
    auto start = frame_time(frame_index_);
    auto end = frame_time(frame_index_ + 1U);
    ++frame_index_;
    ++deadline_index_;
    sample->SetTime(&start, &end);
    sample->SetSyncPoint(TRUE);
    sample->SetDiscontinuity(discontinuity ? TRUE : FALSE);
    first_sample_ = false;
    return S_OK;
}

HRESULT DirectShowCapturePin::DecideBufferSize(
    IMemAllocator* allocator, ALLOCATOR_PROPERTIES* properties) {
    if (allocator == nullptr || properties == nullptr) {
        return E_POINTER;
    }
    properties->cBuffers = (std::max)(properties->cBuffers, 3L);
    properties->cbBuffer = static_cast<LONG>(profile_.sample_size());
    properties->cbAlign = 1L;
    properties->cbPrefix = 0L;
    ALLOCATOR_PROPERTIES actual{};
    const auto result = allocator->SetProperties(properties, &actual);
    if (FAILED(result)) {
        return result;
    }
    return actual.cbBuffer >= properties->cbBuffer &&
                   actual.cBuffers >= properties->cBuffers
               ? S_OK
               : E_FAIL;
}

HRESULT DirectShowCapturePin::CheckMediaType(const CMediaType* media_type) {
    return media_type != nullptr && profile_from_media_type(*media_type) != nullptr
               ? S_OK
               : VFW_E_TYPE_NOT_ACCEPTED;
}

HRESULT DirectShowCapturePin::GetMediaType(const int position,
                                           CMediaType* media_type) {
    if (media_type == nullptr) {
        return E_POINTER;
    }
    if (position < 0) {
        return E_INVALIDARG;
    }
    if (position > 0) {
        return VFW_S_NO_MORE_ITEMS;
    }
    CAutoLock lock{m_pFilter->pStateLock()};
    return configure_media_type(profile_, *media_type);
}

HRESULT DirectShowCapturePin::SetMediaType(const CMediaType* media_type) {
    if (media_type == nullptr) {
        return E_POINTER;
    }
    CAutoLock lock{m_pFilter->pStateLock()};
    const auto* profile = profile_from_media_type(*media_type);
    if (profile == nullptr) {
        return VFW_E_TYPE_NOT_ACCEPTED;
    }
    auto result = CSourceStream::SetMediaType(media_type);
    return SUCCEEDED(result) ? select_profile(*profile) : result;
}

HRESULT DirectShowCapturePin::OnThreadCreate() {
    deadline_epoch_ = std::chrono::steady_clock::now();
    deadline_index_ = 0U;
    frame_index_ = 0U;
    last_generation_ = 0U;
    last_input_sequence_ = 0U;
    last_frame_was_live_ = false;
    have_last_input_sequence_ = false;
    first_sample_ = true;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::SetFormat(AM_MEDIA_TYPE* media_type) {
    CAutoLock lock{m_pFilter->pStateLock()};
    FILTER_STATE state = State_Stopped;
    const auto state_result = m_pFilter->GetState(0U, &state);
    if (FAILED(state_result)) {
        return state_result;
    }
    if (state != State_Stopped) {
        return VFW_E_NOT_STOPPED;
    }
    const auto* profile = media_type == nullptr
                              ? &kMediaProfiles.front()
                              : profile_from_media_type(*media_type);
    if (profile == nullptr) {
        return VFW_E_TYPE_NOT_ACCEPTED;
    }

    const auto previous_profile = profile_;
    CMediaType previous_media_type{m_mt};
    auto previous_adapter = std::move(adapter_);
    auto previous_output = std::move(last_output_);
    auto previous_staging = std::move(staging_output_);
    auto result = select_profile(*profile);
    if (FAILED(result)) {
        profile_ = previous_profile;
        m_mt = previous_media_type;
        adapter_ = std::move(previous_adapter);
        last_output_ = std::move(previous_output);
        staging_output_ = std::move(previous_staging);
        return result;
    }
    IncrementTypeVersion();
    if (!IsConnected()) {
        return S_OK;
    }
    result = m_pFilter->ReconnectPin(this, &m_mt);
    if (SUCCEEDED(result)) {
        return result;
    }
    profile_ = previous_profile;
    m_mt = previous_media_type;
    adapter_ = std::move(previous_adapter);
    last_output_ = std::move(previous_output);
    staging_output_ = std::move(previous_staging);
    IncrementTypeVersion();
    static_cast<void>(m_pFilter->ReconnectPin(this, &m_mt));
    return result;
}

STDMETHODIMP DirectShowCapturePin::GetFormat(AM_MEDIA_TYPE** media_type) {
    if (media_type == nullptr) {
        return E_POINTER;
    }
    CAutoLock lock{m_pFilter->pStateLock()};
    *media_type = CreateMediaType(&m_mt);
    return *media_type != nullptr ? S_OK : E_OUTOFMEMORY;
}

STDMETHODIMP DirectShowCapturePin::GetNumberOfCapabilities(int* count,
                                                           int* size) {
    if (count == nullptr || size == nullptr) {
        return E_POINTER;
    }
    *count = static_cast<int>(kMediaProfiles.size());
    *size = sizeof(VIDEO_STREAM_CONFIG_CAPS);
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::GetStreamCaps(const int index,
                                                 AM_MEDIA_TYPE** media_type,
                                                 BYTE* capabilities) {
    if (media_type == nullptr || capabilities == nullptr) {
        return E_POINTER;
    }
    *media_type = nullptr;
    if (index < 0 || index >= static_cast<int>(kMediaProfiles.size())) {
        return S_FALSE;
    }
    CMediaType type;
    const auto& profile = kMediaProfiles[static_cast<std::size_t>(index)];
    auto result = configure_media_type(profile, type);
    if (FAILED(result)) {
        return result;
    }
    *media_type = CreateMediaType(&type);
    if (*media_type == nullptr) {
        return E_OUTOFMEMORY;
    }
    auto* caps = reinterpret_cast<VIDEO_STREAM_CONFIG_CAPS*>(capabilities);
    std::memset(caps, 0, sizeof(*caps));
    caps->guid = FORMAT_VideoInfo2;
    caps->InputSize = {static_cast<LONG>(profile.width),
                       static_cast<LONG>(profile.height)};
    caps->MinCroppingSize = caps->InputSize;
    caps->MaxCroppingSize = caps->InputSize;
    caps->CropGranularityX = 2;
    caps->CropGranularityY = 2;
    caps->MinOutputSize = caps->InputSize;
    caps->MaxOutputSize = caps->InputSize;
    caps->OutputGranularityX = 2;
    caps->OutputGranularityY = 2;
    caps->MinFrameInterval = kNominalFrameDuration;
    caps->MaxFrameInterval = kNominalFrameDuration;
    const auto bits_per_second = static_cast<LONG>(
        (std::min)(profile.sample_size() * 8U * 30U,
                   static_cast<std::size_t>((std::numeric_limits<LONG>::max)())));
    caps->MinBitsPerSecond = bits_per_second;
    caps->MaxBitsPerSecond = bits_per_second;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::Set(REFGUID, DWORD, LPVOID, DWORD, LPVOID,
                                       DWORD) {
    return E_NOTIMPL;
}

STDMETHODIMP DirectShowCapturePin::Get(
    REFGUID property_set, const DWORD property_id, LPVOID, DWORD,
    LPVOID property_data, const DWORD property_size, DWORD* returned_size) {
    if (property_set != AMPROPSETID_Pin ||
        property_id != AMPROPERTY_PIN_CATEGORY) {
        return E_PROP_SET_UNSUPPORTED;
    }
    if (returned_size == nullptr) {
        return E_POINTER;
    }
    *returned_size = sizeof(GUID);
    if (property_data == nullptr && property_size == 0U) {
        return S_OK;
    }
    if (property_data == nullptr) {
        return E_POINTER;
    }
    if (property_size < sizeof(GUID)) {
        return E_UNEXPECTED;
    }
    std::memcpy(property_data, &PIN_CATEGORY_CAPTURE, sizeof(GUID));
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::QuerySupported(
    REFGUID property_set, const DWORD property_id, DWORD* support_type) {
    if (support_type == nullptr) {
        return E_POINTER;
    }
    if (property_set != AMPROPSETID_Pin ||
        property_id != AMPROPERTY_PIN_CATEGORY) {
        return E_PROP_SET_UNSUPPORTED;
    }
    *support_type = KSPROPERTY_SUPPORT_GET;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::GetLatency(REFERENCE_TIME* latency) {
    if (latency == nullptr) {
        return E_POINTER;
    }
    *latency = kNominalFrameDuration;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::GetPushSourceFlags(ULONG* flags) {
    if (flags == nullptr) {
        return E_POINTER;
    }
    *flags = AM_PUSHSOURCECAPS_INTERNAL_RM;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::SetPushSourceFlags(const ULONG flags) {
    return flags == AM_PUSHSOURCECAPS_INTERNAL_RM ? S_OK : E_INVALIDARG;
}

STDMETHODIMP DirectShowCapturePin::SetStreamOffset(const REFERENCE_TIME offset) {
    return offset == 0 ? S_OK : E_INVALIDARG;
}

STDMETHODIMP DirectShowCapturePin::GetStreamOffset(REFERENCE_TIME* offset) {
    if (offset == nullptr) {
        return E_POINTER;
    }
    *offset = 0;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::GetMaxStreamOffset(
    REFERENCE_TIME* maximum_offset) {
    if (maximum_offset == nullptr) {
        return E_POINTER;
    }
    *maximum_offset = 0;
    return S_OK;
}

STDMETHODIMP DirectShowCapturePin::SetMaxStreamOffset(
    const REFERENCE_TIME maximum_offset) {
    return maximum_offset == 0 ? S_OK : E_INVALIDARG;
}

HRESULT DirectShowCapturePin::select_profile(
    const DirectShowMediaProfile& profile) {
    try {
        CMediaType media_type;
        const auto result = configure_media_type(profile, media_type);
        if (FAILED(result)) {
            return result;
        }
        auto adapter = std::make_unique<DirectShowFrameAdapter>(profile);
        std::vector<std::uint8_t> output(profile.sample_size());
        std::vector<std::uint8_t> staging(profile.sample_size());
        if (!adapter->write_standby(output)) {
            return E_FAIL;
        }
        profile_ = profile;
        m_mt = media_type;
        adapter_ = std::move(adapter);
        last_output_ = std::move(output);
        staging_output_ = std::move(staging);
        return S_OK;
    } catch (const std::bad_alloc&) {
        return E_OUTOFMEMORY;
    } catch (...) {
        return E_FAIL;
    }
}

CUnknown* WINAPI DirectShowVirtualCameraFilter::CreateInstance(
    LPUNKNOWN outer, HRESULT* result) {
    if (result == nullptr) {
        return nullptr;
    }
    auto* filter = new (std::nothrow)
        DirectShowVirtualCameraFilter(outer, result);
    if (filter == nullptr) {
        *result = E_OUTOFMEMORY;
    }
    return filter;
}

DirectShowVirtualCameraFilter::DirectShowVirtualCameraFilter(
    LPUNKNOWN outer, HRESULT* result)
    : CSource(NAME("Solin Virtual Camera"), outer,
              windows_virtual_camera::kDirectShowFilterClassId, result) {
    if (result != nullptr && SUCCEEDED(*result)) {
        static_cast<void>(new (std::nothrow) DirectShowCapturePin(result, this));
        if (SUCCEEDED(*result) && GetPinCount() != 1) {
            *result = E_OUTOFMEMORY;
        }
    }
}

STDMETHODIMP DirectShowVirtualCameraFilter::NonDelegatingQueryInterface(
    REFIID interface_id, void** object) {
    if (interface_id == IID_IAMFilterMiscFlags) {
        return GetInterface(static_cast<IAMFilterMiscFlags*>(this), object);
    }
    return CSource::NonDelegatingQueryInterface(interface_id, object);
}

STDMETHODIMP_(ULONG) DirectShowVirtualCameraFilter::GetMiscFlags() {
    return AM_FILTER_MISC_FLAGS_IS_SOURCE;
}

} // namespace solin::media_engine::windows_virtual_camera
