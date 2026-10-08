#include "solin/media_engine/windows_virtual_camera_contract.hpp"
#include "solin/media_engine/shared_video_frame_channel.hpp"
#include "solin/media_engine/virtual_camera_broker_protocol.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <streams.h>
#include <dvdmedia.h>
#include <d3d9.h>
#include <dxva2api.h>
#include <ks.h>
#include <ksproxy.h>
#include <windows.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <iostream>
#include <mutex>
#include <new>
#include <span>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

using DllGetClassObjectFunction = HRESULT(STDAPICALLTYPE*)(REFCLSID, REFIID,
                                                           void**);
using DllInstallFunction = HRESULT(STDAPICALLTYPE*)(BOOL, LPCWSTR);

class ComApartment final {
  public:
    ComApartment() noexcept : result_(CoInitializeEx(nullptr, COINIT_MULTITHREADED)) {}
    ~ComApartment() {
        if (SUCCEEDED(result_)) {
            CoUninitialize();
        }
    }
    [[nodiscard]] HRESULT result() const noexcept { return result_; }

  private:
    HRESULT result_{E_FAIL};
};

class Module final {
  public:
    explicit Module(const wchar_t* path) : value_(LoadLibraryExW(path, nullptr, 0U)) {}
    ~Module() {
        if (value_ != nullptr) {
            static_cast<void>(FreeLibrary(value_));
        }
    }
    [[nodiscard]] HMODULE get() const noexcept { return value_; }

  private:
    HMODULE value_{nullptr};
};

template <typename Interface> class ComPtr final {
  public:
    ~ComPtr() {
        if (value_ != nullptr) {
            value_->Release();
        }
    }
    [[nodiscard]] Interface* get() const noexcept { return value_; }
    [[nodiscard]] Interface** put() noexcept { return &value_; }

  private:
    Interface* value_{nullptr};
};

void free_media_type(AM_MEDIA_TYPE* media_type) noexcept {
    if (media_type == nullptr) {
        return;
    }
    CoTaskMemFree(media_type->pbFormat);
    if (media_type->pUnk != nullptr) {
        media_type->pUnk->Release();
    }
    CoTaskMemFree(media_type);
}

[[nodiscard]] bool expect(const bool condition, const std::string& message) {
    if (!condition) {
        std::cerr << message << '\n';
    }
    return condition;
}

struct ExpectedProfile final {
    LONG width;
    LONG height;
    const GUID* subtype;
    ULONG sample_size;
};

constexpr REFERENCE_TIME kFrameDuration = 10'000'000LL / 30LL;
constexpr auto kCaptureTimeout = std::chrono::milliseconds{3000};
constexpr GUID kTestRendererClassId{
    0x44D4F119,
    0x2301,
    0x463F,
    {0xA2, 0x34, 0x15, 0x2D, 0x65, 0x18, 0x8A, 0x6B},
};

[[nodiscard]] std::uint64_t frame_checksum(
    const std::span<const std::uint8_t> bytes) noexcept {
    std::uint64_t checksum = 1'469'598'103'934'665'603ULL;
    for (const auto byte : bytes) {
        checksum ^= byte;
        checksum *= 1'099'511'628'211ULL;
    }
    return checksum;
}

[[nodiscard]] DWORD remaining_wait_ms(
    const std::chrono::steady_clock::time_point deadline) noexcept {
    const auto remaining = std::chrono::ceil<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now()).count();
    return static_cast<DWORD>((std::max)(std::int64_t{0}, remaining));
}

[[nodiscard]] DWORD complete_pipe_operation(
    const HANDLE pipe, OVERLAPPED& overlapped, const BOOL started,
    DWORD& transferred,
    const std::chrono::steady_clock::time_point deadline) noexcept {
    if (started != FALSE) {
        return ERROR_SUCCESS;
    }
    const auto error = GetLastError();
    if (error != ERROR_IO_PENDING) {
        return error;
    }
    if (WaitForSingleObject(overlapped.hEvent, remaining_wait_ms(deadline)) !=
        WAIT_OBJECT_0) {
        // Drain cancellation before the stack OVERLAPPED or buffer is destroyed.
        static_cast<void>(CancelIoEx(pipe, &overlapped));
        static_cast<void>(GetOverlappedResult(pipe, &overlapped, &transferred, TRUE));
        return ERROR_TIMEOUT;
    }
    return GetOverlappedResult(pipe, &overlapped, &transferred, FALSE) != FALSE
               ? ERROR_SUCCESS
               : GetLastError();
}

[[nodiscard]] DWORD transfer_pipe_bytes(
    const HANDLE pipe, const HANDLE event, const std::span<std::uint8_t> bytes,
    const bool write,
    const std::chrono::steady_clock::time_point deadline) noexcept {
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        if (std::chrono::steady_clock::now() >= deadline) {
            return ERROR_TIMEOUT;
        }
        static_cast<void>(ResetEvent(event));
        OVERLAPPED overlapped{};
        overlapped.hEvent = event;
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(bytes.size() - offset);
        const auto started =
            write ? WriteFile(pipe, bytes.data() + offset, remaining,
                              &transferred, &overlapped)
                  : ReadFile(pipe, bytes.data() + offset, remaining,
                             &transferred, &overlapped);
        const auto error = complete_pipe_operation(
            pipe, overlapped, started, transferred, deadline);
        if (error != ERROR_SUCCESS) {
            return error;
        }
        if (transferred == 0U) {
            return ERROR_HANDLE_EOF;
        }
        offset += transferred;
    }
    return ERROR_SUCCESS;
}

void serve_live_frame_endpoint(
    const HANDLE pipe,
    const solin::media_engine::SharedVideoFramePublisher& publisher,
    const std::chrono::steady_clock::time_point deadline,
    std::atomic_bool& completed, std::atomic_bool& valid_exchange) noexcept {
    using namespace solin::media_engine;
    const auto event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    try {
        if (event == nullptr) {
            throw std::runtime_error{"broker event creation failed"};
        }
        OVERLAPPED overlapped{};
        overlapped.hEvent = event;
        DWORD transferred = 0U;
        const auto started = ConnectNamedPipe(pipe, &overlapped);
        const auto error = complete_pipe_operation(
            pipe, overlapped, started, transferred, deadline);
        std::array<std::uint8_t, kVirtualCameraBrokerRequestSize> request_bytes{};
        if ((error != ERROR_SUCCESS && error != ERROR_PIPE_CONNECTED) ||
            transfer_pipe_bytes(pipe, event, request_bytes, false, deadline) !=
                ERROR_SUCCESS) {
            throw std::runtime_error{"broker read failed"};
        }
        const auto request =
            decode_virtual_camera_broker_request(request_bytes);
        const auto& configuration = publisher.configuration();
        auto response = encode_virtual_camera_broker_response({
            .status = VirtualCameraBrokerStatus::ok,
            .nonce = request.nonce,
            .mapping_file_path_utf8 =
                std::string{publisher.backing_file_path_utf8()},
            .mapping_size = publisher.mapping_size(),
            .generation = configuration.generation,
            .layout = configuration.layout,
            .fps_numerator = 30U,
            .fps_denominator = 1U,
        });
        if (transfer_pipe_bytes(pipe, event, response, true, deadline) !=
            ERROR_SUCCESS) {
            throw std::runtime_error{"broker write failed"};
        }
        std::array<std::uint8_t, 1U> presence_marker{};
        if (transfer_pipe_bytes(pipe, event, presence_marker, false, deadline) !=
                ERROR_SUCCESS ||
            presence_marker.front() != kVirtualCameraBrokerPresenceMarker) {
            throw std::runtime_error{"broker presence marker missing"};
        }
        valid_exchange.store(true);
        // Keep the current protocol's presence lease alive. The fixture leaves
        // its heartbeat unchanged so the filter must detect staleness, return
        // to standby, and close the lease itself.
        if (transfer_pipe_bytes(pipe, event, presence_marker, false, deadline) !=
            ERROR_BROKEN_PIPE) {
            throw std::runtime_error{"broker lease did not disconnect"};
        }
    } catch (...) {
        valid_exchange.store(false);
    }
    static_cast<void>(DisconnectNamedPipe(pipe));
    static_cast<void>(CloseHandle(pipe));
    if (event != nullptr) {
        static_cast<void>(CloseHandle(event));
    }
    completed.store(true);
}

struct CapturedSample final {
    LONG length{0};
    REFERENCE_TIME start{0};
    REFERENCE_TIME end{0};
    bool sync_point{false};
    bool discontinuity{false};
    std::uint64_t checksum{0U};
    std::chrono::steady_clock::time_point received_at{};
};

class SampleCaptureRenderer final : public CBaseRenderer {
  public:
    explicit SampleCaptureRenderer(
        HRESULT* result, const std::chrono::milliseconds consumer_delay)
        : CBaseRenderer(kTestRendererClassId, NAME("Solin Test Renderer"),
                        nullptr, result), consumer_delay_(consumer_delay) {}

    HRESULT CheckMediaType(const CMediaType* media_type) override {
        return media_type != nullptr && *media_type->Type() == MEDIATYPE_Video &&
                       *media_type->FormatType() == FORMAT_VideoInfo2
                   ? S_OK
                   : VFW_E_TYPE_NOT_ACCEPTED;
    }

    HRESULT DoRenderSample(IMediaSample* sample) override {
        BYTE* bytes = nullptr;
        REFERENCE_TIME start = 0;
        REFERENCE_TIME end = 0;
        if (sample == nullptr || FAILED(sample->GetPointer(&bytes)) ||
            bytes == nullptr || FAILED(sample->GetTime(&start, &end))) {
            return E_FAIL;
        }
        const auto length = sample->GetActualDataLength();
        if (length <= 0 || length > sample->GetSize()) {
            return E_FAIL;
        }
        const auto checksum = frame_checksum(
            {bytes, static_cast<std::size_t>(length)});
        {
            std::lock_guard lock{mutex_};
            samples_.push_back({
                .length = length,
                .start = start,
                .end = end,
                .sync_point = sample->IsSyncPoint() == S_OK,
                .discontinuity = sample->IsDiscontinuity() == S_OK,
                .checksum = checksum,
                .received_at = std::chrono::steady_clock::now(),
            });
        }
        samples_available_.notify_all();
        if (!stalled_once_) {
            stalled_once_ = true;
            std::this_thread::sleep_for(std::chrono::milliseconds{120});
        }
        std::this_thread::sleep_for(consumer_delay_);
        return S_OK;
    }

    [[nodiscard]] std::vector<CapturedSample> wait_for_samples(
        const std::chrono::steady_clock::time_point deadline,
        const std::function<bool(const std::vector<CapturedSample>&)>& ready) {
        std::unique_lock lock{mutex_};
        static_cast<void>(samples_available_.wait_until(
            lock, deadline, [this, &ready] { return ready(samples_); }));
        return samples_;
    }

  private:
    std::mutex mutex_{};
    std::condition_variable samples_available_{};
    std::vector<CapturedSample> samples_{};
    bool stalled_once_{false};
    const std::chrono::milliseconds consumer_delay_;
};

[[nodiscard]] bool validate_profile(const AM_MEDIA_TYPE& media_type,
                                    const ExpectedProfile& expected) {
    if (media_type.majortype != MEDIATYPE_Video ||
        media_type.subtype != *expected.subtype ||
        media_type.formattype != FORMAT_VideoInfo2 ||
        media_type.cbFormat < sizeof(VIDEOINFOHEADER2) ||
        media_type.pbFormat == nullptr ||
        media_type.bFixedSizeSamples == FALSE ||
        media_type.bTemporalCompression != FALSE ||
        media_type.lSampleSize != expected.sample_size) {
        return false;
    }
    const auto* info = reinterpret_cast<const VIDEOINFOHEADER2*>(media_type.pbFormat);
    DXVA2_ExtendedFormat color{};
    color.value = info->dwControlFlags & 0xFFFFFF00U;
    return info->AvgTimePerFrame == kFrameDuration &&
           info->dwInterlaceFlags == 0U &&
           info->dwPictAspectRatioX == static_cast<DWORD>(expected.width) &&
           info->dwPictAspectRatioY == static_cast<DWORD>(expected.height) &&
           (info->dwControlFlags &
            (AMCONTROL_USED | AMCONTROL_COLORINFO_PRESENT)) ==
               (AMCONTROL_USED | AMCONTROL_COLORINFO_PRESENT) &&
           color.VideoTransferMatrix == DXVA2_VideoTransferMatrix_BT709 &&
           color.NominalRange == DXVA2_NominalRange_16_235 &&
           color.VideoPrimaries == DXVA2_VideoPrimaries_BT709 &&
           color.VideoTransferFunction == DXVA2_VideoTransFunc_709 &&
           info->bmiHeader.biWidth == expected.width &&
           info->bmiHeader.biHeight == expected.height &&
           info->bmiHeader.biPlanes == 1U &&
           info->bmiHeader.biBitCount ==
               (*expected.subtype == MEDIASUBTYPE_NV12 ? 12U : 16U) &&
           info->bmiHeader.biCompression == expected.subtype->Data1 &&
           info->bmiHeader.biSizeImage == expected.sample_size;
}

} // namespace

int wmain(const int argument_count, wchar_t** arguments) {
    if (argument_count < 2 || argument_count > 3 ||
        (argument_count == 3 &&
         std::wstring{arguments[2]} != L"--slow-consumer")) {
        std::cerr << "filter path [--slow-consumer] required\n";
        return 2;
    }
    // Sustained backpressure exceeds a 30 fps slot. The graph must still make
    // the live/stale transitions without requiring the consumer to run at 30 fps.
    const auto consumer_delay = std::chrono::milliseconds{
        argument_count == 3 ? 80 : 0};
    ComApartment apartment;
    if (!expect(SUCCEEDED(apartment.result()), "COM initialization failed")) {
        return 1;
    }

    Module module{arguments[1]};
    if (!expect(module.get() != nullptr, "filter DLL failed to load")) {
        return 1;
    }
    const auto get_class_object = reinterpret_cast<DllGetClassObjectFunction>(
        GetProcAddress(module.get(), "DllGetClassObject"));
    if (!expect(get_class_object != nullptr, "DllGetClassObject is missing")) {
        return 1;
    }
    const auto install = reinterpret_cast<DllInstallFunction>(
        GetProcAddress(module.get(), "DllInstall"));
    if (!expect(install != nullptr, "DllInstall is missing") ||
        !expect(install(FALSE, nullptr) == E_INVALIDARG,
                "DllInstall accepted a missing scope") ||
        !expect(install(FALSE, L"invalid") == E_INVALIDARG,
                "DllInstall accepted an invalid scope")) {
        return 1;
    }
    ComPtr<IClassFactory> factory;
    auto result = get_class_object(
        solin::media_engine::windows_virtual_camera::kDirectShowFilterClassId,
        IID_IClassFactory, reinterpret_cast<void**>(factory.put()));
    ComPtr<IBaseFilter> filter;
    if (SUCCEEDED(result)) {
        result = factory.get()->CreateInstance(
            nullptr, IID_IBaseFilter, reinterpret_cast<void**>(filter.put()));
    }
    bool valid = expect(SUCCEEDED(result) && filter.get() != nullptr,
                        "filter creation failed");

    ComPtr<IAMFilterMiscFlags> misc_flags;
    if (valid) {
        valid = expect(SUCCEEDED(filter.get()->QueryInterface(
                           IID_IAMFilterMiscFlags,
                           reinterpret_cast<void**>(misc_flags.put()))) &&
                           misc_flags.get()->GetMiscFlags() ==
                               AM_FILTER_MISC_FLAGS_IS_SOURCE,
                       "filter does not identify as a source");
    }

    ComPtr<IEnumPins> pins;
    if (valid) {
        valid = expect(SUCCEEDED(filter.get()->EnumPins(pins.put())),
                       "pin enumeration failed");
    }
    IPin* raw_pin = nullptr;
    ULONG fetched = 0U;
    if (valid) {
        valid = expect(pins.get()->Next(1U, &raw_pin, &fetched) == S_OK &&
                           raw_pin != nullptr && fetched == 1U,
                       "capture pin is missing");
    }
    ComPtr<IPin> pin;
    if (raw_pin != nullptr) {
        *pin.put() = raw_pin;
    }
    if (valid) {
        raw_pin = nullptr;
        fetched = 0U;
        valid = expect(pins.get()->Next(1U, &raw_pin, &fetched) == S_FALSE,
                       "filter exposes more than one pin");
        if (raw_pin != nullptr) {
            raw_pin->Release();
        }
    }
    PIN_DIRECTION direction{};
    if (valid) {
        valid = expect(SUCCEEDED(pin.get()->QueryDirection(&direction)) &&
                           direction == PINDIR_OUTPUT,
                       "capture pin is not an output");
    }

    ComPtr<IAMStreamConfig> stream_config;
    if (valid) {
        valid = expect(SUCCEEDED(pin.get()->QueryInterface(
                           IID_IAMStreamConfig,
                           reinterpret_cast<void**>(stream_config.put()))),
                       "IAMStreamConfig is missing");
    }
    int capability_count = 0;
    int capability_size = 0;
    if (valid) {
        valid = expect(SUCCEEDED(stream_config.get()->GetNumberOfCapabilities(
                           &capability_count, &capability_size)) &&
                           capability_count == 8 &&
                           capability_size == sizeof(VIDEO_STREAM_CONFIG_CAPS),
                       "unexpected media capability table");
    }
    const std::array<ExpectedProfile, 8U> expected_profiles{{
        {1920, 1080, &MEDIASUBTYPE_NV12, 1920U * 1080U * 3U / 2U},
        {1280, 720, &MEDIASUBTYPE_NV12, 1280U * 720U * 3U / 2U},
        {640, 360, &MEDIASUBTYPE_NV12, 640U * 360U * 3U / 2U},
        {640, 480, &MEDIASUBTYPE_NV12, 640U * 480U * 3U / 2U},
        {1920, 1080, &MEDIASUBTYPE_YUY2, 1920U * 1080U * 2U},
        {1280, 720, &MEDIASUBTYPE_YUY2, 1280U * 720U * 2U},
        {640, 360, &MEDIASUBTYPE_YUY2, 640U * 360U * 2U},
        {640, 480, &MEDIASUBTYPE_YUY2, 640U * 480U * 2U},
    }};
    AM_MEDIA_TYPE* default_type = nullptr;
    if (valid) {
        result = stream_config.get()->GetFormat(&default_type);
        valid = expect(SUCCEEDED(result) && default_type != nullptr &&
                           validate_profile(*default_type, expected_profiles.front()),
                       "default format is not NV12 1920x1080 at 30 fps");
    }
    std::array<std::uint8_t, sizeof(VIDEO_STREAM_CONFIG_CAPS)> capabilities{};
    for (int index = 0; valid && index < capability_count; ++index) {
        AM_MEDIA_TYPE* media_type = nullptr;
        result = stream_config.get()->GetStreamCaps(index, &media_type,
                                                     capabilities.data());
        const auto* caps = reinterpret_cast<const VIDEO_STREAM_CONFIG_CAPS*>(
            capabilities.data());
        valid = expect(SUCCEEDED(result) && media_type != nullptr &&
                           validate_profile(
                               *media_type,
                               expected_profiles[static_cast<std::size_t>(index)]) &&
                           caps->MinFrameInterval == kFrameDuration &&
                           caps->MaxFrameInterval == kFrameDuration &&
                           caps->MinOutputSize.cx ==
                               expected_profiles[static_cast<std::size_t>(index)]
                                   .width &&
                           caps->MinOutputSize.cy ==
                               expected_profiles[static_cast<std::size_t>(index)]
                                   .height,
                       "invalid advertised media type");
        free_media_type(media_type);
    }
    AM_MEDIA_TYPE* out_of_range = reinterpret_cast<AM_MEDIA_TYPE*>(1);
    if (valid) {
        valid = expect(stream_config.get()->GetStreamCaps(
                           capability_count, &out_of_range,
                           capabilities.data()) == S_FALSE &&
                           out_of_range == nullptr,
                       "out-of-range media capability was not rejected");
    }
    if (valid && default_type != nullptr) {
        auto* info = reinterpret_cast<VIDEOINFOHEADER2*>(default_type->pbFormat);
        info->AvgTimePerFrame = 10'000'000LL / 60LL;
        valid = expect(stream_config.get()->SetFormat(default_type) ==
                           VFW_E_TYPE_NOT_ACCEPTED,
                       "an unadvertised 60 fps format was accepted");
    }
    free_media_type(default_type);

    AM_MEDIA_TYPE* selected_type = nullptr;
    if (valid) {
        result = stream_config.get()->GetStreamCaps(5, &selected_type,
                                                     capabilities.data());
        valid = expect(SUCCEEDED(result) && selected_type != nullptr &&
                           validate_profile(*selected_type, expected_profiles[5]) &&
                           SUCCEEDED(stream_config.get()->SetFormat(selected_type)),
                       "SetFormat rejected an advertised YUY2 720p profile");
    }
    AM_MEDIA_TYPE* configured_type = nullptr;
    if (valid) {
        result = stream_config.get()->GetFormat(&configured_type);
        valid = expect(SUCCEEDED(result) && configured_type != nullptr &&
                           validate_profile(*configured_type,
                                            expected_profiles[5]),
                       "GetFormat did not preserve the selected profile");
    }
    free_media_type(configured_type);
    ComPtr<IEnumMediaTypes> enumerated_types;
    if (valid) {
        valid = expect(SUCCEEDED(pin.get()->EnumMediaTypes(
                           enumerated_types.put())),
                       "media-type enumeration failed after SetFormat");
    }
    AM_MEDIA_TYPE* enumerated_type = nullptr;
    if (valid) {
        fetched = 0U;
        valid = expect(enumerated_types.get()->Next(
                           1U, &enumerated_type, &fetched) == S_OK &&
                           fetched == 1U && enumerated_type != nullptr &&
                           validate_profile(*enumerated_type,
                                            expected_profiles[5]),
                       "pin enumeration did not expose the selected profile");
    }
    free_media_type(enumerated_type);
    if (valid) {
        enumerated_type = nullptr;
        fetched = 0U;
        valid = expect(enumerated_types.get()->Next(
                           1U, &enumerated_type, &fetched) == S_FALSE &&
                           fetched == 0U,
                       "pin enumeration exposed profiles beyond SetFormat");
    }
    free_media_type(selected_type);

    ComPtr<IKsPropertySet> property_set;
    if (valid) {
        valid = expect(SUCCEEDED(pin.get()->QueryInterface(
                           IID_IKsPropertySet,
                           reinterpret_cast<void**>(property_set.put()))),
                       "IKsPropertySet is missing");
    }
    GUID category{};
    DWORD returned_size = 0U;
    if (valid) {
        valid = expect(SUCCEEDED(property_set.get()->Get(
                           AMPROPSETID_Pin, AMPROPERTY_PIN_CATEGORY, nullptr, 0U,
                           &category, sizeof(category), &returned_size)) &&
                           category == PIN_CATEGORY_CAPTURE,
                       "pin is not in the capture category");
    }

    ComPtr<IAMPushSource> push_source;
    ULONG push_flags = 0U;
    if (valid) {
        valid = expect(SUCCEEDED(pin.get()->QueryInterface(
                           IID_IAMPushSource,
                           reinterpret_cast<void**>(push_source.put()))) &&
                           SUCCEEDED(push_source.get()->GetPushSourceFlags(
                               &push_flags)) &&
                           push_flags == AM_PUSHSOURCECAPS_INTERNAL_RM,
                       "live-source cadence contract is missing");
    }
    REFERENCE_TIME stream_offset = -1;
    REFERENCE_TIME maximum_stream_offset = -1;
    if (valid) {
        valid = expect(
            push_source.get()->SetStreamOffset(1) == E_INVALIDARG &&
                SUCCEEDED(push_source.get()->SetStreamOffset(0)) &&
                SUCCEEDED(push_source.get()->GetStreamOffset(&stream_offset)) &&
                stream_offset == 0 &&
                SUCCEEDED(push_source.get()->GetMaxStreamOffset(
                    &maximum_stream_offset)) &&
                maximum_stream_offset == 0 &&
                push_source.get()->SetMaxStreamOffset(1) == E_INVALIDARG,
            "stream-offset limits are inconsistent");
    }

    ComPtr<IFilterGraph2> graph;
    ComPtr<IBaseFilter> sink;
    ComPtr<IMediaControl> control;
    SampleCaptureRenderer* capture_renderer = nullptr;
    std::unique_ptr<solin::media_engine::SharedVideoFramePublisher>
        live_publisher;
    std::uint64_t expected_live_checksum = 0U;
    try {
        const auto live_layout = solin::media_engine::packed_video_frame_layout(
            640U, 360U, solin::media_engine::VideoFramePixelFormat::nv12);
        live_publisher =
            solin::media_engine::make_cross_process_shared_video_frame_publisher(
                {.generation = 77U, .layout = live_layout});
        solin::media_engine::PackedVideoFrame live_frame{
            .width = live_layout.width,
            .height = live_layout.height,
            .pixel_format = live_layout.pixel_format,
            .plane_strides = live_layout.plane_strides,
            .plane_offsets = live_layout.plane_offsets,
            .bytes = std::vector<std::uint8_t>(
                static_cast<std::size_t>(live_layout.payload_size),
                std::uint8_t{210U}),
        };
        std::fill(live_frame.bytes.begin() +
                      static_cast<std::ptrdiff_t>(live_layout.plane_offsets[1]),
                  live_frame.bytes.end(), std::uint8_t{128U});
        expected_live_checksum = frame_checksum(live_frame.bytes);
        valid = expect(
            live_publisher->publish(
                solin::media_engine::video_frame_view(live_frame)) == 1U,
            "live broker fixture could not publish its NV12 frame");
    } catch (...) {
        valid = expect(false, "live broker fixture creation failed");
    }
    std::atomic_bool broker_completed{false};
    std::atomic_bool broker_exchange_valid{false};
    std::thread broker_thread;
    if (valid) {
        result = CoCreateInstance(CLSID_FilterGraph, nullptr,
                                  CLSCTX_INPROC_SERVER, IID_IFilterGraph2,
                                  reinterpret_cast<void**>(graph.put()));
        if (SUCCEEDED(result)) {
            capture_renderer =
                new (std::nothrow) SampleCaptureRenderer(&result, consumer_delay);
            if (capture_renderer == nullptr) {
                result = E_OUTOFMEMORY;
            }
        }
        if (SUCCEEDED(result)) {
            result = capture_renderer->QueryInterface(
                IID_IBaseFilter, reinterpret_cast<void**>(sink.put()));
        }
        if (SUCCEEDED(result)) {
            result = graph.get()->AddFilter(filter.get(), L"Solin Camera");
        }
        if (SUCCEEDED(result)) {
            result = graph.get()->AddFilter(sink.get(), L"Test Sink");
        }
        ComPtr<IEnumPins> sink_pins;
        IPin* raw_sink_pin = nullptr;
        if (SUCCEEDED(result)) {
            result = sink.get()->EnumPins(sink_pins.put());
        }
        if (SUCCEEDED(result)) {
            result = sink_pins.get()->Next(1U, &raw_sink_pin, &fetched);
            if (result != S_OK || raw_sink_pin == nullptr || fetched != 1U) {
                result = E_FAIL;
            }
        }
        if (SUCCEEDED(result)) {
            result = graph.get()->ConnectDirect(pin.get(), raw_sink_pin, nullptr);
        }
        if (raw_sink_pin != nullptr) {
            raw_sink_pin->Release();
        }
        AM_MEDIA_TYPE connection_type{};
        if (SUCCEEDED(result)) {
            result = pin.get()->ConnectionMediaType(&connection_type);
            if (SUCCEEDED(result) &&
                !validate_profile(connection_type, expected_profiles[5])) {
                result = VFW_E_INVALIDMEDIATYPE;
            }
            FreeMediaType(connection_type);
        }
        AM_MEDIA_TYPE* reconnect_type = nullptr;
        if (SUCCEEDED(result)) {
            result = stream_config.get()->GetStreamCaps(
                2, &reconnect_type, capabilities.data());
        }
        if (SUCCEEDED(result)) {
            result = stream_config.get()->SetFormat(reconnect_type);
        }
        free_media_type(reconnect_type);
        if (SUCCEEDED(result)) {
            result = pin.get()->ConnectionMediaType(&connection_type);
            if (SUCCEEDED(result) &&
                !validate_profile(connection_type, expected_profiles[2])) {
                result = VFW_E_INVALIDMEDIATYPE;
            }
            FreeMediaType(connection_type);
        }
        if (SUCCEEDED(result)) {
            result = graph.get()->QueryInterface(
                IID_IMediaControl, reinterpret_cast<void**>(control.put()));
        }
        ComPtr<IMediaFilter> media_filter;
        if (SUCCEEDED(result)) {
            result = graph.get()->QueryInterface(
                IID_IMediaFilter, reinterpret_cast<void**>(media_filter.put()));
        }
        if (SUCCEEDED(result)) {
            // A clocked renderer can hide a missing source wait. Capture without
            // a graph clock so this test exercises the filter's own pacing.
            result = media_filter.get()->SetSyncSource(nullptr);
        }
        if (SUCCEEDED(result)) {
            result = control.get()->Run();
        }
        if (SUCCEEDED(result) &&
            stream_config.get()->SetFormat(nullptr) != VFW_E_NOT_STOPPED) {
            result = E_FAIL;
        }
        const auto capture_started = std::chrono::steady_clock::now();
        const auto capture_deadline = capture_started + kCaptureTimeout;
        std::vector<CapturedSample> samples;
        if (SUCCEEDED(result)) {
            // Expose the broker only after the offline graph has delivered a
            // standby sample. Publishing earlier races the first FillBuffer and
            // can make the first sample live, reversing the transition oracle.
            samples = capture_renderer->wait_for_samples(
                capture_deadline,
                [](const auto& captured) { return !captured.empty(); });
            if (samples.empty() ||
                samples.front().checksum == expected_live_checksum) {
                result = E_FAIL;
            }
        }
        if (SUCCEEDED(result)) {
            const auto pipe = CreateNamedPipeW(
                solin::media_engine::windows_virtual_camera::
                    current_user_broker_pipe_name()
                        .c_str(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE |
                    FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
                    PIPE_REJECT_REMOTE_CLIENTS,
                1U,
                static_cast<DWORD>(
                    solin::media_engine::kVirtualCameraBrokerResponseSize),
                static_cast<DWORD>(
                    solin::media_engine::kVirtualCameraBrokerRequestSize),
                0U, nullptr);
            if (pipe == INVALID_HANDLE_VALUE || live_publisher == nullptr) {
                result = HRESULT_FROM_WIN32(GetLastError());
            } else {
                broker_thread = std::thread{
                    serve_live_frame_endpoint, pipe,
                    std::cref(*live_publisher), capture_deadline,
                    std::ref(broker_completed),
                    std::ref(broker_exchange_valid)};
            }
        }
        if (SUCCEEDED(result)) {
            // Both capture phases share the original three-second deadline.
            // Require the stale-producer transition before stopping the graph,
            // as Stop itself also closes the broker presence lease.
            // Two subsequent standby samples prove stable fallback. Throughput
            // under consumer backpressure is not a graph correctness condition;
            // exact scheduling is tested independently with controlled time.
            samples = capture_renderer->wait_for_samples(
                capture_deadline, [expected_live_checksum](const auto& captured) {
                    return captured.size() >= 4U &&
                           captured.back().checksum == captured.front().checksum &&
                           captured[captured.size() - 2U].checksum ==
                               captured.front().checksum &&
                           std::any_of(captured.begin(), captured.end(),
                                       [expected_live_checksum](const auto& sample) {
                                           return sample.checksum ==
                                                  expected_live_checksum;
                                       });
                });
        }
        if (SUCCEEDED(result) &&
            (WaitForSingleObject(broker_thread.native_handle(),
                                 remaining_wait_ms(capture_deadline)) !=
                 WAIT_OBJECT_0 ||
             !broker_completed.load() || !broker_exchange_valid.load())) {
            result = E_FAIL;
        }
        if (control.get() != nullptr) {
            const auto stop_result = control.get()->Stop();
            if (SUCCEEDED(result)) {
                result = stop_result;
            }
        }
        const auto capture_elapsed = std::chrono::steady_clock::now() -
                                     capture_started;
        if (SUCCEEDED(result)) {
            const auto expected_size =
                static_cast<LONG>(expected_profiles[2].sample_size);
            if (samples.size() < 4U || samples[0].length != expected_size ||
                !samples[0].sync_point || !samples[0].discontinuity ||
                samples[0].start != 0 ||
                samples[0].end != kFrameDuration ||
                samples[0].checksum == expected_live_checksum) {
                result = E_FAIL;
            }
            for (std::size_t index = 1U;
                 SUCCEEDED(result) && index < samples.size(); ++index) {
                if (samples[index].length != expected_size ||
                    !samples[index].sync_point ||
                    samples[index].start != samples[index - 1U].end ||
                    samples[index].start !=
                        static_cast<REFERENCE_TIME>(index * 10'000'000ULL / 30U) ||
                    samples[index].end !=
                        static_cast<REFERENCE_TIME>((index + 1U) * 10'000'000ULL / 30U)) {
                    result = E_FAIL;
                }
            }
            if (SUCCEEDED(result)) {
                const auto received_duration =
                    samples.back().received_at - samples.front().received_at;
                // Slow consumers may reduce throughput, but a clockless source
                // must never deliver more media time than elapsed wall time
                // (allow one slot for scheduling at the observation boundary).
                if (std::chrono::nanoseconds{samples.back().start * 100LL} >
                        received_duration + std::chrono::nanoseconds{
                            1'000'000'000LL / 30LL} ||
                    !samples[1].discontinuity) {
                    std::cerr << "source pacing or stall discontinuity failed\n";
                    result = E_FAIL;
                }
            }
            auto live_index = samples.size();
            for (std::size_t index = 1U; index < samples.size(); ++index) {
                if (samples[index].checksum != samples[0].checksum) {
                    live_index = index;
                    break;
                }
            }
            if (SUCCEEDED(result) &&
                (live_index == samples.size() ||
                 samples[live_index].checksum != expected_live_checksum ||
                 !samples[live_index].discontinuity)) {
                result = E_FAIL;
            }
            auto standby_index = samples.size();
            for (std::size_t index = live_index + 1U;
                 index < samples.size(); ++index) {
                if (samples[index].checksum == samples[0].checksum) {
                    standby_index = index;
                    break;
                }
                if (samples[index].checksum != samples[live_index].checksum) {
                    result = E_FAIL;
                    break;
                }
            }
            if (SUCCEEDED(result) &&
                (standby_index + 1U >= samples.size() ||
                 !samples[standby_index].discontinuity)) {
                result = E_FAIL;
            }
            for (std::size_t index = standby_index + 1U;
                 SUCCEEDED(result) && index < samples.size(); ++index) {
                if (samples[index].checksum != samples[0].checksum) {
                    result = E_FAIL;
                }
            }
        }
        if (broker_thread.joinable()) {
            broker_thread.join();
        }
        if (SUCCEEDED(result) &&
            (!broker_completed.load() || !broker_exchange_valid.load())) {
            result = E_FAIL;
        }
        if (FAILED(result)) {
            std::cerr << "graph validation HRESULT=0x" << std::hex
                      << static_cast<unsigned long>(result) << std::dec
                      << ", samples=" << samples.size()
                      << ", elapsed_ms="
                      << std::chrono::duration_cast<std::chrono::milliseconds>(
                             capture_elapsed)
                             .count()
                      << ", broker_completed=" << broker_completed.load()
                      << ", broker_valid=" << broker_exchange_valid.load()
                      << ", expected_live_checksum=" << expected_live_checksum
                      << '\n';
            for (std::size_t index = 0U; index < samples.size(); ++index) {
                std::cerr << "sample[" << index << "] length="
                          << samples[index].length << " time="
                          << samples[index].start << ':' << samples[index].end
                          << " discontinuity=" << samples[index].discontinuity
                          << " checksum=" << samples[index].checksum << '\n';
            }
        }
        valid = expect(SUCCEEDED(result),
                       "standby/live samples failed graph/cadence validation");
    }

    return valid ? 0 : 1;
}
