#include "media_source.hpp"

#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#include <cstdint>
#include <limits>
#include <stdexcept>
#include <utility>

namespace solin::media_engine::windows_virtual_camera {
namespace {

constexpr DWORD kStreamId = 0U;

[[nodiscard]] HRESULT read_stream_configuration(
    IMFAttributes* attributes, MediaStreamConfiguration& configuration) {
    if (attributes == nullptr) {
        return E_INVALIDARG;
    }
    UINT32 width = 0U;
    UINT32 height = 0U;
    UINT32 fps_numerator = 0U;
    UINT32 fps_denominator = 0U;
    auto status = attributes->GetUINT32(kFrameWidthAttribute, &width);
    if (SUCCEEDED(status)) {
        status = attributes->GetUINT32(kFrameHeightAttribute, &height);
    }
    if (SUCCEEDED(status)) {
        status = attributes->GetUINT32(kFrameRateNumeratorAttribute,
                                       &fps_numerator);
    }
    if (SUCCEEDED(status)) {
        status = attributes->GetUINT32(kFrameRateDenominatorAttribute,
                                       &fps_denominator);
    }
    if (FAILED(status)) {
        return status;
    }
    if (fps_numerator == 0U || fps_denominator == 0U ||
        static_cast<std::uint64_t>(fps_numerator) >
            240ULL * fps_denominator) {
        return E_INVALIDARG;
    }
    try {
        configuration = {
            .layout = packed_video_frame_layout(
                width, height, VideoFramePixelFormat::nv12),
            .fps_numerator = fps_numerator,
            .fps_denominator = fps_denominator,
        };
        return S_OK;
    } catch (const std::invalid_argument&) {
        return E_INVALIDARG;
    } catch (...) {
        return E_FAIL;
    }
}

[[nodiscard]] HRESULT create_source_attributes(
    IMFAttributes* activation_attributes, IMFAttributes** attributes) {
    if (activation_attributes == nullptr || attributes == nullptr) {
        return E_POINTER;
    }
    *attributes = nullptr;
    Microsoft::WRL::ComPtr<IMFAttributes> result;
    UINT32 activation_attribute_count = 0U;
    auto status = activation_attributes->GetCount(&activation_attribute_count);
    if (FAILED(status)) {
        return status;
    }
    if (activation_attribute_count >
        (std::numeric_limits<UINT32>::max)() - 2U) {
        return E_INVALIDARG;
    }
    status = MFCreateAttributes(&result, activation_attribute_count + 2U);
    if (FAILED(status)) {
        return status;
    }
    status = activation_attributes->CopyAllItems(result.Get());
    if (FAILED(status)) {
        return status;
    }
    status = result->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                             MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    if (FAILED(status)) {
        return status;
    }
    Microsoft::WRL::ComPtr<IMFSensorProfileCollection> profiles;
    status = MFCreateSensorProfileCollection(&profiles);
    if (FAILED(status)) {
        return status;
    }
    Microsoft::WRL::ComPtr<IMFSensorProfile> legacy_profile;
    status = MFCreateSensorProfile(KSCAMERAPROFILE_Legacy, 0U, nullptr,
                                   &legacy_profile);
    if (FAILED(status)) {
        return status;
    }
    status = legacy_profile->AddProfileFilter(
        kStreamId, L"((RES==;FRT<=240,1;SUT==))");
    if (FAILED(status)) {
        return status;
    }
    status = profiles->AddProfile(legacy_profile.Get());
    if (FAILED(status)) {
        return status;
    }
    status = result->SetUnknown(MF_DEVICEMFT_SENSORPROFILE_COLLECTION,
                                profiles.Get());
    if (FAILED(status)) {
        return status;
    }
    return result.CopyTo(attributes);
}

} // namespace

HRESULT SolinMediaSource::Initialize(IMFAttributes* activation_attributes) {
    std::scoped_lock lock{mutex_};
    if (initialized_ || activation_attributes == nullptr) {
        return initialized_ ? MF_E_ALREADY_INITIALIZED : E_INVALIDARG;
    }
    auto status = validate_frame_broker_activation(activation_attributes);
    if (FAILED(status)) {
        return status;
    }
    MediaStreamConfiguration configuration;
    status = read_stream_configuration(activation_attributes, configuration);
    if (FAILED(status)) {
        return status;
    }
    Microsoft::WRL::ComPtr<IMFAttributes> source_attributes;
    status = create_source_attributes(activation_attributes, &source_attributes);
    if (FAILED(status)) {
        return status;
    }
    Microsoft::WRL::ComPtr<IMFMediaEventQueue> event_queue;
    status = MFCreateEventQueue(&event_queue);
    if (FAILED(status)) {
        return status;
    }
    auto stream = Microsoft::WRL::Make<SolinMediaStream>();
    if (stream == nullptr) {
        return E_OUTOFMEMORY;
    }
    status = stream->Initialize(this, configuration);
    if (FAILED(status)) {
        static_cast<void>(stream->Shutdown());
        return status;
    }
    Microsoft::WRL::ComPtr<IMFStreamDescriptor> stream_descriptor;
    status = stream->GetStreamDescriptor(&stream_descriptor);
    if (FAILED(status)) {
        static_cast<void>(stream->Shutdown());
        return status;
    }
    IMFStreamDescriptor* descriptors[]{stream_descriptor.Get()};
    Microsoft::WRL::ComPtr<IMFPresentationDescriptor> descriptor;
    status = MFCreatePresentationDescriptor(1U, descriptors, &descriptor);
    if (FAILED(status)) {
        static_cast<void>(stream->Shutdown());
        return status;
    }
    attributes_ = std::move(source_attributes);
    event_queue_ = std::move(event_queue);
    stream_ = std::move(stream);
    descriptor_ = std::move(descriptor);
    initialized_ = true;
    state_ = State::stopped;
    return S_OK;
}

HRESULT SolinMediaSource::check_shutdown_locked() const noexcept {
    return !initialized_ || state_ == State::shutdown || event_queue_ == nullptr
               ? MF_E_SHUTDOWN
               : S_OK;
}

STDMETHODIMP SolinMediaSource::BeginGetEvent(IMFAsyncCallback* callback,
                                             IUnknown* state) {
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    return FAILED(status) ? status : event_queue_->BeginGetEvent(callback, state);
}

STDMETHODIMP SolinMediaSource::EndGetEvent(IMFAsyncResult* result,
                                           IMFMediaEvent** event) {
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    return FAILED(status) ? status : event_queue_->EndGetEvent(result, event);
}

STDMETHODIMP SolinMediaSource::GetEvent(DWORD flags, IMFMediaEvent** event) {
    Microsoft::WRL::ComPtr<IMFMediaEventQueue> queue;
    {
        std::scoped_lock lock{mutex_};
        const auto status = check_shutdown_locked();
        if (FAILED(status)) {
            return status;
        }
        queue = event_queue_;
    }
    return queue->GetEvent(flags, event);
}

STDMETHODIMP SolinMediaSource::QueueEvent(MediaEventType type,
                                          REFGUID extended_type,
                                          HRESULT status,
                                          const PROPVARIANT* event_value) {
    std::scoped_lock lock{mutex_};
    const auto state = check_shutdown_locked();
    return FAILED(state)
               ? state
               : event_queue_->QueueEventParamVar(type, extended_type, status,
                                                  event_value);
}

STDMETHODIMP SolinMediaSource::GetCharacteristics(DWORD* characteristics) {
    if (characteristics == nullptr) {
        return E_POINTER;
    }
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    *characteristics = MFMEDIASOURCE_IS_LIVE;
    return S_OK;
}

STDMETHODIMP SolinMediaSource::CreatePresentationDescriptor(
    IMFPresentationDescriptor** descriptor) {
    if (descriptor == nullptr) {
        return E_POINTER;
    }
    *descriptor = nullptr;
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    return FAILED(status) ? status : descriptor_->Clone(descriptor);
}

STDMETHODIMP SolinMediaSource::Start(
    IMFPresentationDescriptor* descriptor, const GUID* time_format,
    const PROPVARIANT* start_position) {
    if (descriptor == nullptr || start_position == nullptr) {
        return E_INVALIDARG;
    }
    if (time_format != nullptr && *time_format != GUID_NULL) {
        return MF_E_UNSUPPORTED_TIME_FORMAT;
    }
    std::scoped_lock lock{mutex_};
    auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    DWORD stream_count = 0U;
    status = descriptor->GetStreamDescriptorCount(&stream_count);
    if (FAILED(status) || stream_count != 1U) {
        return E_INVALIDARG;
    }
    BOOL selected = FALSE;
    Microsoft::WRL::ComPtr<IMFStreamDescriptor> stream_descriptor;
    status = descriptor->GetStreamDescriptorByIndex(0U, &selected,
                                                     &stream_descriptor);
    if (FAILED(status)) {
        return status;
    }
    DWORD stream_id = 0U;
    status = stream_descriptor->GetStreamIdentifier(&stream_id);
    if (FAILED(status) || stream_id != kStreamId) {
        return E_INVALIDARG;
    }
    BOOL was_selected = FALSE;
    Microsoft::WRL::ComPtr<IMFStreamDescriptor> current_stream_descriptor;
    status = descriptor_->GetStreamDescriptorByIndex(
        0U, &was_selected, &current_stream_descriptor);
    if (FAILED(status)) {
        return status;
    }
    DWORD current_stream_id = 0U;
    status = current_stream_descriptor->GetStreamIdentifier(&current_stream_id);
    if (FAILED(status) || current_stream_id != kStreamId) {
        return E_UNEXPECTED;
    }
    if (selected != FALSE) {
        Microsoft::WRL::ComPtr<IMFMediaTypeHandler> handler;
        status = stream_descriptor->GetMediaTypeHandler(&handler);
        if (FAILED(status)) {
            return status;
        }
        Microsoft::WRL::ComPtr<IMFMediaType> media_type;
        status = handler->GetCurrentMediaType(&media_type);
        if (FAILED(status)) {
            return status;
        }
        if (provider_ == nullptr) {
            status = connect_to_frame_broker(attributes_.Get(), provider_);
            if (FAILED(status)) {
                return status;
            }
        }
        status = stream_->PrepareStart(media_type.Get(), provider_);
        if (FAILED(status)) {
            return status;
        }
        status = descriptor_->SelectStream(0U);
        if (FAILED(status)) {
            return status;
        }
        Microsoft::WRL::ComPtr<IUnknown> stream_unknown;
        status = stream_.As(&stream_unknown);
        if (FAILED(status)) {
            return status;
        }
        status = event_queue_->QueueEventParamUnk(
            was_selected != FALSE ? MEUpdatedStream : MENewStream, GUID_NULL,
            S_OK, stream_unknown.Get());
        if (FAILED(status)) {
            return status;
        }
        status = stream_->Start(media_type.Get(), provider_);
        if (FAILED(status)) {
            return status;
        }
    } else if (was_selected != FALSE) {
        status = stream_->Stop(false);
        if (FAILED(status)) {
            return status;
        }
        status = descriptor_->DeselectStream(0U);
        if (FAILED(status)) {
            return status;
        }
    }
    PROPVARIANT start_time{};
    start_time.vt = VT_I8;
    start_time.hVal.QuadPart = MFGetSystemTime();
    status = event_queue_->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK,
                                              &start_time);
    if (FAILED(status)) {
        return status;
    }
    state_ = State::started;
    return S_OK;
}

STDMETHODIMP SolinMediaSource::Stop() {
    std::scoped_lock lock{mutex_};
    auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    if (state_ != State::started) {
        return MF_E_INVALID_STATE_TRANSITION;
    }
    status = stream_->Stop(true);
    if (FAILED(status)) {
        return status;
    }
    status = descriptor_->DeselectStream(0U);
    if (FAILED(status)) {
        return status;
    }
    PROPVARIANT stop_time{};
    stop_time.vt = VT_I8;
    stop_time.hVal.QuadPart = MFGetSystemTime();
    status = event_queue_->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK,
                                              &stop_time);
    if (SUCCEEDED(status)) {
        state_ = State::stopped;
    }
    return status;
}

STDMETHODIMP SolinMediaSource::Pause() {
    return MF_E_INVALID_STATE_TRANSITION;
}

STDMETHODIMP SolinMediaSource::Shutdown() {
    std::scoped_lock lock{mutex_};
    if (state_ == State::shutdown) {
        return S_OK;
    }
    state_ = State::shutdown;
    if (stream_ != nullptr) {
        static_cast<void>(stream_->Shutdown());
    }
    if (event_queue_ != nullptr) {
        static_cast<void>(event_queue_->Shutdown());
    }
    stream_.Reset();
    provider_.reset();
    descriptor_.Reset();
    attributes_.Reset();
    event_queue_.Reset();
    return S_OK;
}

STDMETHODIMP SolinMediaSource::GetSourceAttributes(IMFAttributes** attributes) {
    if (attributes == nullptr) {
        return E_POINTER;
    }
    *attributes = nullptr;
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    return FAILED(status) ? status : attributes_.CopyTo(attributes);
}

STDMETHODIMP SolinMediaSource::GetStreamAttributes(
    const DWORD stream_identifier, IMFAttributes** attributes) {
    if (attributes == nullptr) {
        return E_POINTER;
    }
    *attributes = nullptr;
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    if (stream_identifier != kStreamId || stream_->attributes() == nullptr) {
        return MF_E_INVALIDSTREAMNUMBER;
    }
    stream_->attributes()->AddRef();
    *attributes = stream_->attributes();
    return S_OK;
}

STDMETHODIMP SolinMediaSource::SetD3DManager(IUnknown* manager) {
    static_cast<void>(manager);
    return E_NOTIMPL;
}

STDMETHODIMP SolinMediaSource::GetService(REFGUID service,
                                          REFIID interface_id,
                                          LPVOID* object) {
    static_cast<void>(service);
    static_cast<void>(interface_id);
    if (object == nullptr) {
        return E_POINTER;
    }
    *object = nullptr;
    return MF_E_UNSUPPORTED_SERVICE;
}

STDMETHODIMP SolinMediaSource::KsProperty(PKSPROPERTY property,
                                          ULONG property_length,
                                          LPVOID property_data,
                                          ULONG data_length,
                                          ULONG* bytes_returned) {
    static_cast<void>(property);
    static_cast<void>(property_length);
    static_cast<void>(property_data);
    static_cast<void>(data_length);
    if (bytes_returned != nullptr) {
        *bytes_returned = 0U;
    }
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

STDMETHODIMP SolinMediaSource::KsMethod(PKSMETHOD method, ULONG method_length,
                                        LPVOID method_data, ULONG data_length,
                                        ULONG* bytes_returned) {
    static_cast<void>(method);
    static_cast<void>(method_length);
    static_cast<void>(method_data);
    static_cast<void>(data_length);
    if (bytes_returned != nullptr) {
        *bytes_returned = 0U;
    }
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

STDMETHODIMP SolinMediaSource::KsEvent(PKSEVENT event, ULONG event_length,
                                       LPVOID event_data, ULONG data_length,
                                       ULONG* bytes_returned) {
    static_cast<void>(event);
    static_cast<void>(event_length);
    static_cast<void>(event_data);
    static_cast<void>(data_length);
    if (bytes_returned != nullptr) {
        *bytes_returned = 0U;
    }
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

STDMETHODIMP SolinMediaSource::SetDefaultAllocator(
    const DWORD output_stream_id, IUnknown* allocator) {
    if (allocator == nullptr || output_stream_id != kStreamId) {
        return E_INVALIDARG;
    }
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    Microsoft::WRL::ComPtr<IMFVideoSampleAllocator> video_allocator;
    const auto query = allocator->QueryInterface(IID_PPV_ARGS(&video_allocator));
    return FAILED(query) ? query
                         : stream_->SetSampleAllocator(video_allocator.Get());
}

STDMETHODIMP SolinMediaSource::GetAllocatorUsage(
    const DWORD output_stream_id, DWORD* input_stream_id,
    MFSampleAllocatorUsage* usage) {
    if (input_stream_id == nullptr || usage == nullptr) {
        return E_POINTER;
    }
    std::scoped_lock lock{mutex_};
    const auto status = check_shutdown_locked();
    if (FAILED(status)) {
        return status;
    }
    if (output_stream_id != kStreamId) {
        return MF_E_INVALIDSTREAMNUMBER;
    }
    *input_stream_id = kStreamId;
    *usage = MFSampleAllocatorUsage_UsesProvidedAllocator;
    return S_OK;
}

} // namespace solin::media_engine::windows_virtual_camera
