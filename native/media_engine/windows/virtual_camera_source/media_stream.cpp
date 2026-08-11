#include "media_stream.hpp"

#include <evr.h>
#include <unknwn.h>
#include <ks.h>
#include <ksmedia.h>

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <span>
#include <utility>
#include <vector>

namespace solin::media_engine::windows_virtual_camera {
namespace {

constexpr DWORD kStreamId = 0U;
// Match the Windows Camera reference source. Four samples are insufficient for
// consumers that pipeline several outstanding requests (Zoom is a common case).
constexpr DWORD kAllocatorSampleCount = 10U;
// Consumers may pipeline requests faster than the negotiated capture rate.
// Keep tokens bounded and let the stream worker release exactly one sample per
// frame interval instead of turning RequestSample into an unbounded copy loop.
constexpr std::size_t kMaximumPendingSampleRequests = 32U;

class BufferLock final {
  public:
    explicit BufferLock(IMFMediaBuffer* buffer) : buffer_(buffer) {}
    ~BufferLock() {
        if (locked_) {
            static_cast<void>(buffer_->Unlock());
        }
    }
    void mark_locked() noexcept { locked_ = true; }

  private:
    IMFMediaBuffer* buffer_{nullptr};
    bool locked_{false};
};

class Buffer2DLock final {
  public:
    explicit Buffer2DLock(IMF2DBuffer2* buffer) : buffer_(buffer) {}
    ~Buffer2DLock() {
        if (locked_) {
            static_cast<void>(buffer_->Unlock2D());
        }
    }
    void mark_locked() noexcept { locked_ = true; }

  private:
    IMF2DBuffer2* buffer_{nullptr};
    bool locked_{false};
};

[[nodiscard]] HRESULT configure_media_type(
    IMFMediaType* media_type, const MediaStreamConfiguration& configuration,
    const GUID& subtype) {
    if (media_type == nullptr ||
        configuration.layout.pixel_format != VideoFramePixelFormat::nv12 ||
        (subtype != MFVideoFormat_NV12 && subtype != MFVideoFormat_YUY2) ||
        configuration.fps_numerator == 0U ||
        configuration.fps_denominator == 0U) {
        return E_INVALIDARG;
    }
    const auto& layout = configuration.layout;
    const auto yuy2 = subtype == MFVideoFormat_YUY2;
    const auto sample_size =
        yuy2 ? static_cast<std::uint64_t>(layout.width) * layout.height * 2U
             : layout.payload_size;
    const auto stride = yuy2 ? static_cast<std::uint64_t>(layout.width) * 2U
                             : layout.plane_strides[0];
    if (sample_size > (std::numeric_limits<UINT32>::max)() ||
        stride > (std::numeric_limits<UINT32>::max)()) {
        return E_INVALIDARG;
    }
    auto result = media_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetGUID(MF_MT_SUBTYPE, subtype);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_INTERLACE_MODE,
                                   MFVideoInterlace_Progressive);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(
        MF_MT_SAMPLE_SIZE, static_cast<UINT32>(sample_size));
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_DEFAULT_STRIDE,
                                   static_cast<UINT32>(stride));
    if (FAILED(result)) {
        return result;
    }
    result = MFSetAttributeSize(media_type, MF_MT_FRAME_SIZE, layout.width,
                                layout.height);
    if (FAILED(result)) {
        return result;
    }
    result = MFSetAttributeRatio(media_type, MF_MT_FRAME_RATE,
                                 configuration.fps_numerator,
                                 configuration.fps_denominator);
    if (FAILED(result)) {
        return result;
    }
    result = MFSetAttributeRatio(media_type, MF_MT_PIXEL_ASPECT_RATIO, 1U, 1U);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_VIDEO_PRIMARIES,
                                   MFVideoPrimaries_BT709);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_TRANSFER_FUNCTION,
                                   MFVideoTransFunc_709);
    if (FAILED(result)) {
        return result;
    }
    result = media_type->SetUINT32(MF_MT_YUV_MATRIX,
                                   MFVideoTransferMatrix_BT709);
    if (FAILED(result)) {
        return result;
    }
    return media_type->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE,
                                 MFNominalRange_16_235);
}

[[nodiscard]] HRESULT validate_output_media_type(
    IMFMediaType* media_type, const MediaStreamConfiguration& configuration,
    GUID& subtype) {
    if (media_type == nullptr) {
        return E_INVALIDARG;
    }
    GUID major_type{};
    auto result = media_type->GetGUID(MF_MT_MAJOR_TYPE, &major_type);
    if (FAILED(result) || major_type != MFMediaType_Video) {
        return MF_E_INVALIDMEDIATYPE;
    }
    result = media_type->GetGUID(MF_MT_SUBTYPE, &subtype);
    if (FAILED(result) ||
        (subtype != MFVideoFormat_NV12 && subtype != MFVideoFormat_YUY2)) {
        return MF_E_INVALIDMEDIATYPE;
    }
    UINT32 width = 0U;
    UINT32 height = 0U;
    result = MFGetAttributeSize(media_type, MF_MT_FRAME_SIZE, &width, &height);
    if (FAILED(result) || width != configuration.layout.width ||
        height != configuration.layout.height) {
        return MF_E_INVALIDMEDIATYPE;
    }
    UINT32 fps_numerator = 0U;
    UINT32 fps_denominator = 0U;
    result = MFGetAttributeRatio(media_type, MF_MT_FRAME_RATE, &fps_numerator,
                                 &fps_denominator);
    if (FAILED(result) || fps_denominator == 0U ||
        static_cast<std::uint64_t>(fps_numerator) *
                configuration.fps_denominator !=
            static_cast<std::uint64_t>(configuration.fps_numerator) *
                fps_denominator) {
        return MF_E_INVALIDMEDIATYPE;
    }
    return S_OK;
}

[[nodiscard]] HRESULT copy_yuy2_to_2d(
    IMF2DBuffer2* destination, const std::span<const std::uint8_t> bytes,
    const std::uint32_t width, const std::uint32_t height) {
    if (destination == nullptr || bytes.empty() || width == 0U || height == 0U ||
        (width & 1U) != 0U || (height & 1U) != 0U) {
        return E_INVALIDARG;
    }
    BYTE* first_scanline = nullptr;
    LONG pitch = 0;
    BYTE* buffer_start = nullptr;
    DWORD buffer_length = 0U;
    Buffer2DLock lock{destination};
    auto result = destination->Lock2DSize(
        MF2DBuffer_LockFlags_Write, &first_scanline, &pitch, &buffer_start,
        &buffer_length);
    if (FAILED(result)) {
        return result;
    }
    lock.mark_locked();
    if (first_scanline == nullptr || buffer_start == nullptr || pitch <= 0) {
        return E_UNEXPECTED;
    }
    const auto row_bytes = static_cast<std::uint64_t>(width) * 2U;
    const auto source_stride = static_cast<LONG>(row_bytes);
    if (row_bytes > static_cast<std::uint64_t>((std::numeric_limits<DWORD>::max)()) ||
        row_bytes > static_cast<std::uint64_t>(pitch)) {
        return MF_E_BUFFERTOOSMALL;
    }
    const auto destination_bytes = static_cast<std::uint64_t>(pitch) * height;
    const auto source_bytes = row_bytes * height;
    if (destination_bytes > buffer_length || source_bytes != bytes.size()) {
        return MF_E_BUFFERTOOSMALL;
    }
    result = MFCopyImage(first_scanline, pitch, bytes.data(), source_stride,
                         static_cast<DWORD>(row_bytes), height);
    return result;
}

[[nodiscard]] HRESULT copy_nv12_to_2d(
    IMF2DBuffer2* destination, const VideoFrameView& frame) {
    if (destination == nullptr ||
        frame.pixel_format != VideoFramePixelFormat::nv12 ||
        frame.plane_strides[0] <= 0 || frame.plane_strides[1] <= 0) {
        return E_INVALIDARG;
    }
    BYTE* first_scanline = nullptr;
    LONG pitch = 0;
    BYTE* buffer_start = nullptr;
    DWORD buffer_length = 0U;
    Buffer2DLock lock{destination};
    auto result = destination->Lock2DSize(
        MF2DBuffer_LockFlags_Write, &first_scanline, &pitch, &buffer_start,
        &buffer_length);
    if (FAILED(result)) {
        return result;
    }
    lock.mark_locked();
    if (first_scanline == nullptr || buffer_start == nullptr || pitch <= 0) {
        return E_UNEXPECTED;
    }
    const auto required =
        static_cast<std::uint64_t>(pitch) * frame.height * 3U / 2U;
    if (static_cast<std::uint64_t>(pitch) < frame.width ||
        required > buffer_length) {
        return MF_E_BUFFERTOOSMALL;
    }
    result = MFCopyImage(first_scanline, pitch, frame.planes[0].data(),
                         frame.plane_strides[0], frame.width, frame.height);
    if (FAILED(result)) {
        return result;
    }
    auto* destination_uv =
        first_scanline + static_cast<std::size_t>(pitch) * frame.height;
    return MFCopyImage(destination_uv, pitch, frame.planes[1].data(),
                       frame.plane_strides[1], frame.width, frame.height / 2U);
}

[[nodiscard]] HRESULT copy_to_contiguous(
    IMFMediaBuffer* destination, const std::span<const std::uint8_t> source) {
    BYTE* bytes = nullptr;
    DWORD maximum_length = 0U;
    DWORD current_length = 0U;
    BufferLock lock{destination};
    auto result = destination->Lock(&bytes, &maximum_length, &current_length);
    if (FAILED(result)) {
        return result;
    }
    lock.mark_locked();
    if (bytes == nullptr || source.size() > maximum_length) {
        return MF_E_BUFFERTOOSMALL;
    }
    std::memcpy(bytes, source.data(), source.size());
    return destination->SetCurrentLength(static_cast<DWORD>(source.size()));
}

[[nodiscard]] HRESULT copy_nv12_to_contiguous(
    IMFMediaBuffer* destination, const VideoFrameView& frame,
    const PackedVideoFrameLayout& layout) {
    if (destination == nullptr) {
        return E_INVALIDARG;
    }
    BYTE* bytes = nullptr;
    DWORD maximum_length = 0U;
    DWORD current_length = 0U;
    BufferLock lock{destination};
    auto result = destination->Lock(&bytes, &maximum_length, &current_length);
    if (FAILED(result)) {
        return result;
    }
    lock.mark_locked();
    if (bytes == nullptr || layout.payload_size > maximum_length) {
        return MF_E_BUFFERTOOSMALL;
    }
    try {
        copy_video_frame_pixels(
            frame, std::span<std::uint8_t>{
                       bytes, static_cast<std::size_t>(layout.payload_size)});
    } catch (...) {
        return E_INVALIDARG;
    }
    return destination->SetCurrentLength(static_cast<DWORD>(layout.payload_size));
}

void convert_nv12_to_yuy2(const VideoFrameView& frame,
                          std::vector<std::uint8_t>& destination) {
    const auto required = static_cast<std::size_t>(frame.width) * frame.height * 2U;
    destination.resize(required);
    const auto* y_plane = frame.planes[0].data();
    const auto* uv_plane = frame.planes[1].data();
    for (std::uint32_t row = 0U; row < frame.height; ++row) {
        const auto* y = y_plane + static_cast<std::size_t>(row) *
                                     static_cast<std::size_t>(frame.plane_strides[0]);
        const auto* uv = uv_plane + static_cast<std::size_t>(row / 2U) *
                                         static_cast<std::size_t>(frame.plane_strides[1]);
        auto* output = destination.data() +
                       static_cast<std::size_t>(row) * frame.width * 2U;
        for (std::uint32_t column = 0U; column < frame.width; column += 2U) {
            output[column * 2U] = y[column];
            output[column * 2U + 1U] = uv[column];
            output[column * 2U + 2U] = y[column + 1U];
            output[column * 2U + 3U] = uv[column + 1U];
        }
    }
}

} // namespace

HRESULT SolinMediaStream::Initialize(
    IMFMediaSource* parent, const MediaStreamConfiguration& configuration) {
    std::scoped_lock lock{mutex_};
    if (parent == nullptr || event_queue_ != nullptr ||
        configuration.layout.pixel_format != VideoFramePixelFormat::nv12 ||
        configuration.fps_numerator == 0U ||
        configuration.fps_denominator == 0U) {
        return E_INVALIDARG;
    }
    parent_ = parent;
    configuration_ = configuration;
    auto result = MFCreateEventQueue(&event_queue_);
    if (FAILED(result)) {
        return result;
    }
    result = MFCreateAttributes(&attributes_, 5U);
    if (FAILED(result)) {
        return result;
    }
    result = attributes_->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY,
                                  PINNAME_VIDEO_CAPTURE);
    if (FAILED(result)) {
        return result;
    }
    result = attributes_->SetUINT32(MF_DEVICESTREAM_STREAM_ID, kStreamId);
    if (FAILED(result)) {
        return result;
    }
    result = attributes_->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1U);
    if (FAILED(result)) {
        return result;
    }
    result = attributes_->SetUINT32(
        MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES,
        MFFrameSourceTypes::MFFrameSourceTypes_Color);
    if (FAILED(result)) {
        return result;
    }
    result = MFCreateMediaType(&nv12_media_type_);
    if (FAILED(result)) {
        return result;
    }
    result = configure_media_type(nv12_media_type_.Get(), configuration_,
                                  MFVideoFormat_NV12);
    if (FAILED(result)) {
        return result;
    }
    result = MFCreateMediaType(&yuy2_media_type_);
    if (FAILED(result)) {
        return result;
    }
    result = configure_media_type(yuy2_media_type_.Get(), configuration_,
                                  MFVideoFormat_YUY2);
    if (FAILED(result)) {
        return result;
    }
    media_type_ = nv12_media_type_;
    IMFMediaType* types[]{nv12_media_type_.Get(), yuy2_media_type_.Get()};
    result = MFCreateStreamDescriptor(kStreamId, 2U, types, &descriptor_);
    if (FAILED(result)) {
        return result;
    }
    Microsoft::WRL::ComPtr<IMFMediaTypeHandler> handler;
    result = descriptor_->GetMediaTypeHandler(&handler);
    if (FAILED(result)) {
        return result;
    }
    result = handler->SetCurrentMediaType(media_type_.Get());
    if (FAILED(result)) {
        return result;
    }
    Microsoft::WRL::ComPtr<IMFAttributes> descriptor_attributes;
    result = descriptor_.As(&descriptor_attributes);
    if (FAILED(result)) {
        return result;
    }
    result = attributes_->CopyAllItems(descriptor_attributes.Get());
    if (FAILED(result)) {
        return result;
    }
    const auto duration = 10'000'000ULL * configuration_.fps_denominator /
                          configuration_.fps_numerator;
    if (duration == 0U || duration >
                              static_cast<std::uint64_t>(
                                  (std::numeric_limits<LONGLONG>::max)())) {
        return E_INVALIDARG;
    }
    sample_duration_hns_ = static_cast<LONGLONG>(duration);
    try {
        sample_worker_ = std::jthread(
            [this](const std::stop_token stop_token) noexcept {
                sample_worker_loop(stop_token);
            });
    } catch (...) {
        return E_OUTOFMEMORY;
    }
    return S_OK;
}

HRESULT SolinMediaStream::check_shutdown_locked() const noexcept {
    return shutdown_ || event_queue_ == nullptr ? MF_E_SHUTDOWN : S_OK;
}

STDMETHODIMP SolinMediaStream::BeginGetEvent(IMFAsyncCallback* callback,
                                             IUnknown* state) {
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    return FAILED(result) ? result : event_queue_->BeginGetEvent(callback, state);
}

STDMETHODIMP SolinMediaStream::EndGetEvent(IMFAsyncResult* result,
                                           IMFMediaEvent** event) {
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    return FAILED(status) ? status : event_queue_->EndGetEvent(result, event);
}

STDMETHODIMP SolinMediaStream::GetEvent(DWORD flags, IMFMediaEvent** event) {
    Microsoft::WRL::ComPtr<IMFMediaEventQueue> queue;
    {
        std::scoped_lock lock{mutex_};
        const auto result = check_shutdown_locked();
        if (FAILED(result)) {
            return result;
        }
        queue = event_queue_;
    }
    return queue->GetEvent(flags, event);
}

STDMETHODIMP SolinMediaStream::QueueEvent(MediaEventType type,
                                          REFGUID extended_type,
                                          HRESULT status,
                                          const PROPVARIANT* event_value) {
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    return FAILED(result)
               ? result
               : event_queue_->QueueEventParamVar(type, extended_type, status,
                                                  event_value);
}

STDMETHODIMP SolinMediaStream::GetMediaSource(IMFMediaSource** source) {
    if (source == nullptr) {
        return E_POINTER;
    }
    *source = nullptr;
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    return FAILED(result) ? result : parent_.CopyTo(source);
}

STDMETHODIMP SolinMediaStream::GetStreamDescriptor(
    IMFStreamDescriptor** descriptor) {
    if (descriptor == nullptr) {
        return E_POINTER;
    }
    *descriptor = nullptr;
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    return FAILED(result) ? result : descriptor_.CopyTo(descriptor);
}

HRESULT SolinMediaStream::ensure_allocator_locked() {
    if (allocator_ == nullptr) {
        return MF_E_NOT_INITIALIZED;
    }
    if (allocator_initialized_) {
        return S_OK;
    }
    const auto result = allocator_->InitializeSampleAllocator(
        kAllocatorSampleCount, media_type_.Get());
    if (SUCCEEDED(result)) {
        allocator_initialized_ = true;
    }
    return result;
}

HRESULT SolinMediaStream::write_sample_locked(IMFSample* sample,
                                               const VideoFrameView& frame) {
    if (sample == nullptr || frame.pixel_format != VideoFramePixelFormat::nv12) {
        return E_INVALIDARG;
    }
    try {
        validate_video_frame_view(frame, configuration_.layout);
    } catch (...) {
        return E_INVALIDARG;
    }
    Microsoft::WRL::ComPtr<IMFMediaBuffer> buffer;
    auto result = sample->GetBufferByIndex(0U, &buffer);
    if (FAILED(result)) {
        return result;
    }
    if (output_yuy2_) {
        if (frame.plane_strides[0] <= 0 || frame.plane_strides[1] <= 0) {
            return E_INVALIDARG;
        }
        convert_nv12_to_yuy2(frame, yuy2_scratch_);
        Microsoft::WRL::ComPtr<IMF2DBuffer2> buffer_2d;
        if (SUCCEEDED(buffer.As(&buffer_2d))) {
            return copy_yuy2_to_2d(buffer_2d.Get(), yuy2_scratch_, frame.width,
                                   frame.height);
        }
        return copy_to_contiguous(buffer.Get(), yuy2_scratch_);
    }
    Microsoft::WRL::ComPtr<IMF2DBuffer2> buffer_2d;
    if (SUCCEEDED(buffer.As(&buffer_2d))) {
        return copy_nv12_to_2d(buffer_2d.Get(), frame);
    }
    return copy_nv12_to_contiguous(buffer.Get(), frame,
                                   configuration_.layout);
}

HRESULT SolinMediaStream::produce_sample_locked(IUnknown* token) {
    if (provider_ == nullptr) {
        return MF_E_NOT_INITIALIZED;
    }
    auto result = ensure_allocator_locked();
    if (FAILED(result)) {
        return result;
    }
    Microsoft::WRL::ComPtr<IMFSample> sample;
    result = allocator_->AllocateSample(&sample);
    if (FAILED(result)) {
        return result;
    }
    std::uint64_t frame_sequence = 0U;
    bool frame_discontinuity = false;
    const auto stable_frame = provider_->visit_frame(
        [this, &sample, &result, &frame_sequence,
         &frame_discontinuity](const VideoFrameView& frame) {
            result = write_sample_locked(sample.Get(), frame);
            frame_sequence = frame.sequence;
            frame_discontinuity = frame.discontinuity;
        });
    if (!stable_frame) {
        return S_FALSE;
    }
    if (FAILED(result)) {
        return result;
    }
    result = sample->SetSampleTime(MFGetSystemTime());
    if (FAILED(result)) {
        return result;
    }
    result = sample->SetSampleDuration(sample_duration_hns_);
    if (FAILED(result)) {
        return result;
    }
    result = sample->SetUINT32(MFSampleExtension_CleanPoint, TRUE);
    if (FAILED(result)) {
        return result;
    }
    if (first_sample_ ||
        (frame_discontinuity &&
         frame_sequence != last_discontinuity_sequence_)) {
        result = sample->SetUINT32(MFSampleExtension_Discontinuity, TRUE);
        if (FAILED(result)) {
            return result;
        }
        first_sample_ = false;
        last_discontinuity_sequence_ = frame_sequence;
    }
    if (token != nullptr) {
        result = sample->SetUnknown(MFSampleExtension_Token, token);
        if (FAILED(result)) {
            return result;
        }
    }
    return event_queue_->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK,
                                            sample.Get());
}

void SolinMediaStream::sample_worker_loop(
    const std::stop_token stop_token) noexcept {
    try {
        std::unique_lock lock{mutex_};
        const auto sample_interval = std::chrono::duration_cast<
            std::chrono::steady_clock::duration>(
            std::chrono::duration<LONGLONG, std::ratio<1, 10'000'000>>{
                sample_duration_hns_});
        while (!stop_token.stop_requested()) {
            sample_wakeup_.wait(lock, [this, &stop_token] {
                return stop_token.stop_requested() || shutdown_ ||
                       (state_ == MF_STREAM_STATE_RUNNING &&
                        !pending_sample_tokens_.empty());
            });
            if (stop_token.stop_requested() || shutdown_) {
                return;
            }
            if (state_ != MF_STREAM_STATE_RUNNING ||
                pending_sample_tokens_.empty()) {
                continue;
            }
            const auto now = std::chrono::steady_clock::now();
            if (next_sample_at_ > now) {
                sample_wakeup_.wait_until(lock, next_sample_at_,
                                          [this, &stop_token] {
                                              return stop_token.stop_requested() ||
                                                     shutdown_ ||
                                                     state_ !=
                                                         MF_STREAM_STATE_RUNNING;
                                          });
                continue;
            }
            auto token = std::move(pending_sample_tokens_.front());
            pending_sample_tokens_.pop_front();
            const auto result = produce_sample_locked(token.Get());
            const auto completed_at = std::chrono::steady_clock::now();
            const auto scheduled_next = next_sample_at_ + sample_interval;
            next_sample_at_ = scheduled_next > completed_at
                                  ? scheduled_next
                                  : completed_at + sample_interval;
            if (result == S_FALSE || result == MF_E_SAMPLEALLOCATOR_EMPTY) {
                pending_sample_tokens_.push_front(std::move(token));
            } else if (FAILED(result) && event_queue_ != nullptr) {
                static_cast<void>(event_queue_->QueueEventParamVar(
                    MEError, GUID_NULL, result, nullptr));
            }
        }
    } catch (...) {
    }
}

STDMETHODIMP SolinMediaStream::RequestSample(IUnknown* token) {
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    if (!selected_ || provider_ == nullptr ||
        state_ == MF_STREAM_STATE_STOPPED) {
        return MF_E_MEDIA_SOURCE_WRONGSTATE;
    }
    if (pending_sample_tokens_.size() >= kMaximumPendingSampleRequests) {
        return S_OK;
    }
    Microsoft::WRL::ComPtr<IUnknown> retained_token;
    if (token != nullptr) {
        retained_token = token;
    }
    pending_sample_tokens_.push_back(std::move(retained_token));
    sample_wakeup_.notify_one();
    return S_OK;
}

STDMETHODIMP SolinMediaStream::SetStreamState(const MF_STREAM_STATE state) {
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    if (state_ == state) {
        return S_OK;
    }
    if (!selected_) {
        return MF_E_INVALID_STATE_TRANSITION;
    }
    if (state == MF_STREAM_STATE_RUNNING) {
        return start_locked(false, media_type_.Get(), nullptr);
    }
    if (state == MF_STREAM_STATE_STOPPED) {
        return stop_locked(false, false);
    }
    if (state == MF_STREAM_STATE_PAUSED) {
        if (state_ != MF_STREAM_STATE_RUNNING) {
            return MF_E_INVALID_STATE_TRANSITION;
        }
        state_ = MF_STREAM_STATE_PAUSED;
        sample_wakeup_.notify_all();
        return S_OK;
    }
    return MF_E_INVALID_STATE_TRANSITION;
}

STDMETHODIMP SolinMediaStream::GetStreamState(MF_STREAM_STATE* state) {
    if (state == nullptr) {
        return E_POINTER;
    }
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    if (FAILED(result)) {
        return result;
    }
    *state = state_;
    return S_OK;
}

HRESULT SolinMediaStream::prepare_start_locked(
    IMFMediaType* media_type, std::shared_ptr<BrokerFrameProvider> provider) {
    GUID subtype{};
    auto result =
        validate_output_media_type(media_type, configuration_, subtype);
    if (FAILED(result)) {
        return result;
    }
    if (provider != nullptr &&
        (provider->layout() != configuration_.layout ||
         provider->fps_numerator() != configuration_.fps_numerator ||
         provider->fps_denominator() != configuration_.fps_denominator)) {
        return MF_E_INVALIDMEDIATYPE;
    }
    if (provider == nullptr) {
        provider = provider_;
    }
    if (provider == nullptr) {
        return MF_E_NOT_INITIALIZED;
    }
    DWORD equality_flags = 0U;
    const auto equality = media_type_ != nullptr
                              ? media_type_->IsEqual(media_type, &equality_flags)
                              : S_FALSE;
    if (equality != S_OK) {
        if (allocator_initialized_) {
            const auto uninitialized = allocator_->UninitializeSampleAllocator();
            if (FAILED(uninitialized)) {
                return uninitialized;
            }
            allocator_initialized_ = false;
        }
        media_type_ = media_type;
    }
    output_yuy2_ = subtype == MFVideoFormat_YUY2;
    result = ensure_allocator_locked();
    if (FAILED(result)) {
        return result;
    }
    provider_ = std::move(provider);
    return S_OK;
}

HRESULT SolinMediaStream::start_locked(
    const bool send_event, IMFMediaType* media_type,
    std::shared_ptr<BrokerFrameProvider> provider) {
    const auto prepared =
        prepare_start_locked(media_type, std::move(provider));
    if (FAILED(prepared)) {
        return prepared;
    }
    first_sample_ = true;
    last_discontinuity_sequence_ = 0U;
    state_ = MF_STREAM_STATE_RUNNING;
    next_sample_at_ = std::chrono::steady_clock::now();
    sample_wakeup_.notify_all();
    return send_event ? event_queue_->QueueEventParamVar(
                            MEStreamStarted, GUID_NULL, S_OK, nullptr)
                      : S_OK;
}

HRESULT SolinMediaStream::PrepareStart(
    IMFMediaType* media_type, std::shared_ptr<BrokerFrameProvider> provider) {
    if (media_type == nullptr || provider == nullptr) {
        return E_INVALIDARG;
    }
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    return FAILED(status)
               ? status
               : prepare_start_locked(media_type, std::move(provider));
}

HRESULT SolinMediaStream::Start(
    IMFMediaType* media_type, std::shared_ptr<BrokerFrameProvider> provider) {
    if (media_type == nullptr || provider == nullptr) {
        return E_INVALIDARG;
    }
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    const auto started = start_locked(true, media_type, std::move(provider));
    if (SUCCEEDED(started)) {
        selected_ = true;
    }
    return started;
}

HRESULT SolinMediaStream::stop_locked(const bool send_event,
                                      const bool deselect) {
    state_ = MF_STREAM_STATE_STOPPED;
    pending_sample_tokens_.clear();
    if (deselect) {
        selected_ = false;
    }
    sample_wakeup_.notify_all();
    return send_event ? event_queue_->QueueEventParamVar(
                            MEStreamStopped, GUID_NULL, S_OK, nullptr)
                      : S_OK;
}

HRESULT SolinMediaStream::Stop(const bool send_event) {
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    if (FAILED(result)) {
        return result;
    }
    return stop_locked(send_event, true);
}

SolinMediaStream::~SolinMediaStream() { static_cast<void>(Shutdown()); }

HRESULT SolinMediaStream::Shutdown() {
    std::scoped_lock shutdown_lock{shutdown_mutex_};
    {
        std::scoped_lock lock{mutex_};
        if (shutdown_) {
            return S_OK;
        }
        shutdown_ = true;
        selected_ = false;
        state_ = MF_STREAM_STATE_STOPPED;
        pending_sample_tokens_.clear();
    }
    sample_worker_.request_stop();
    sample_wakeup_.notify_all();
    if (sample_worker_.joinable()) {
        sample_worker_.join();
    }
    {
        std::scoped_lock lock{mutex_};
        if (event_queue_ != nullptr) {
            static_cast<void>(event_queue_->Shutdown());
        }
        event_queue_.Reset();
        attributes_.Reset();
        descriptor_.Reset();
        nv12_media_type_.Reset();
        yuy2_media_type_.Reset();
        media_type_.Reset();
        if (allocator_initialized_ && allocator_ != nullptr) {
            static_cast<void>(allocator_->UninitializeSampleAllocator());
        }
        allocator_initialized_ = false;
        allocator_.Reset();
        yuy2_scratch_.clear();
        provider_.reset();
        parent_.Reset();
    }
    return S_OK;
}

HRESULT SolinMediaStream::SetSampleAllocator(
    IMFVideoSampleAllocator* allocator) {
    if (allocator == nullptr) {
        return E_INVALIDARG;
    }
    std::scoped_lock lock{mutex_};
    const auto result = check_shutdown_locked();
    if (FAILED(result)) {
        return result;
    }
    if (state_ == MF_STREAM_STATE_RUNNING) {
        return MF_E_INVALIDREQUEST;
    }
    if (allocator_initialized_ && allocator_ != nullptr) {
        const auto uninitialized = allocator_->UninitializeSampleAllocator();
        if (FAILED(uninitialized)) {
            return uninitialized;
        }
    }
    allocator_ = allocator;
    allocator_initialized_ = false;
    return S_OK;
}

IMFAttributes* SolinMediaStream::attributes() const noexcept {
    return attributes_.Get();
}

} // namespace solin::media_engine::windows_virtual_camera
