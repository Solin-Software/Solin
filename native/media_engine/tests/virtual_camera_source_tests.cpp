#include "solin/media_engine/windows_virtual_camera_contract.hpp"
#include "standby_resources.h"

#include <array>
#include <chrono>
#include <cstdint>
#include <d3d11.h>
#include <mfapi.h>
#include <mfidl.h>
#include <wrl/client.h>

#include <windows.h>

#include <iostream>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

class Library final {
  public:
    explicit Library(const wchar_t* path) : value_(LoadLibraryW(path)) {}
    ~Library() {
        if (value_ != nullptr) {
            static_cast<void>(FreeLibrary(value_));
        }
    }
    [[nodiscard]] HMODULE get() const noexcept { return value_; }

  private:
    HMODULE value_{nullptr};
};

} // namespace

int wmain(const int argc, const wchar_t* const argv[]) {
    if (argc != 2) {
        return 2;
    }
    const auto initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized)) {
        return 3;
    }
    const auto media_foundation_initialized =
        MFStartup(MF_VERSION, MFSTARTUP_FULL);
    if (FAILED(media_foundation_initialized)) {
        CoUninitialize();
        return 4;
    }
    const Library library{argv[1]};
    expect(library.get() != nullptr, "the virtual-camera source DLL loads standalone");
    if (library.get() != nullptr) {
        expect(FindResourceW(library.get(), MAKEINTRESOURCEW(SOLIN_STANDBY_ICON),
                             RT_GROUP_ICON) != nullptr,
               "the source embeds the official Solin standby icon");
        using GetClassObject = HRESULT(__stdcall*)(REFCLSID, REFIID, void**);
        using CanUnloadNow = HRESULT(__stdcall*)();
        const auto get_class_object = reinterpret_cast<GetClassObject>(
            GetProcAddress(library.get(), "DllGetClassObject"));
        const auto can_unload = reinterpret_cast<CanUnloadNow>(
            GetProcAddress(library.get(), "DllCanUnloadNow"));
        expect(get_class_object != nullptr && can_unload != nullptr,
               "the DLL exports the required COM entry points");
        if (get_class_object != nullptr && can_unload != nullptr) {
            Microsoft::WRL::ComPtr<IClassFactory> factory;
            auto status = get_class_object(
                solin::media_engine::windows_virtual_camera::kSourceClassId,
                IID_PPV_ARGS(&factory));
            expect(SUCCEEDED(status) && factory != nullptr,
                   "the registered CLSID resolves to an in-process class factory");
            Microsoft::WRL::ComPtr<IMFActivate> activation;
            if (factory != nullptr) {
                status = factory->CreateInstance(nullptr, IID_PPV_ARGS(&activation));
            }
            expect(SUCCEEDED(status) && activation != nullptr,
                   "the class factory creates an IMFActivate boundary object");
            if (activation != nullptr) {
                constexpr UINT32 marker = 42U;
                status = activation->SetUINT32(
                    solin::media_engine::windows_virtual_camera::
                        kBrokerPipeAttribute,
                    marker);
                UINT32 received = 0U;
                if (SUCCEEDED(status)) {
                    status = activation->GetUINT32(
                        solin::media_engine::windows_virtual_camera::
                            kBrokerPipeAttribute,
                        &received);
                }
                expect(SUCCEEDED(status) && received == marker,
                       "activation delegates its complete IMFAttributes contract");

                status = activation->SetString(
                    solin::media_engine::windows_virtual_camera::
                        kBrokerPipeAttribute,
                    solin::media_engine::windows_virtual_camera::
                        kBrokerPipeNameUtf16);
                if (SUCCEEDED(status)) {
                    status = activation->SetBlob(
                        solin::media_engine::windows_virtual_camera::
                            kBrokerTokenAttribute,
                        solin::media_engine::windows_virtual_camera::
                            kBrokerContractToken.data(),
                        static_cast<UINT32>(
                            solin::media_engine::windows_virtual_camera::
                                kBrokerContractToken.size()));
                }
                if (SUCCEEDED(status)) {
                    status = activation->SetUINT32(
                        solin::media_engine::windows_virtual_camera::
                            kFrameWidthAttribute,
                        1920U);
                }
                if (SUCCEEDED(status)) {
                    status = activation->SetUINT32(
                        solin::media_engine::windows_virtual_camera::
                            kFrameHeightAttribute,
                        1080U);
                }
                if (SUCCEEDED(status)) {
                    status = activation->SetUINT32(
                        solin::media_engine::windows_virtual_camera::
                            kFrameRateNumeratorAttribute,
                        30U);
                }
                if (SUCCEEDED(status)) {
                    status = activation->SetUINT32(
                        solin::media_engine::windows_virtual_camera::
                            kFrameRateDenominatorAttribute,
                        1U);
                }
                expect(SUCCEEDED(status),
                       "the activation accepts the complete runtime contract");

                Microsoft::WRL::ComPtr<IMFMediaSource> source;
                if (SUCCEEDED(status)) {
                    status = activation->ActivateObject(IID_PPV_ARGS(&source));
                }
                if (FAILED(status)) {
                    std::cerr << "source activation HRESULT: 0x" << std::hex
                              << static_cast<unsigned long>(status) << std::dec
                              << '\n';
                }
                expect(SUCCEEDED(status) && source != nullptr,
                       "source activation does not connect to the frame broker");
                Microsoft::WRL::ComPtr<IMFMediaSourceEx> source_ex;
                Microsoft::WRL::ComPtr<IMFAttributes> source_attributes;
                if (source != nullptr) {
                    status = source.As(&source_ex);
                }
                if (SUCCEEDED(status)) {
                    status = source_ex->GetSourceAttributes(&source_attributes);
                }
                UINT32 advertised_width = 0U;
                if (SUCCEEDED(status)) {
                    status = source_attributes->GetUINT32(
                        solin::media_engine::windows_virtual_camera::
                            kFrameWidthAttribute,
                        &advertised_width);
                }
                expect(SUCCEEDED(status) && advertised_width == 1920U,
                       "the source preserves all activation attributes");
                Microsoft::WRL::ComPtr<IMFPresentationDescriptor> descriptor;
                if (source != nullptr) {
                    status = source->CreatePresentationDescriptor(&descriptor);
                }
                DWORD stream_count = 0U;
                if (SUCCEEDED(status)) {
                    status = descriptor->GetStreamDescriptorCount(&stream_count);
                }
                expect(SUCCEEDED(status) && stream_count == 1U,
                       "the source advertises one video stream before broker I/O");
                BOOL stream_selected = FALSE;
                Microsoft::WRL::ComPtr<IMFStreamDescriptor> stream_descriptor;
                if (SUCCEEDED(status)) {
                    status = descriptor->GetStreamDescriptorByIndex(
                        0U, &stream_selected, &stream_descriptor);
                }
                Microsoft::WRL::ComPtr<IMFMediaTypeHandler> media_type_handler;
                if (SUCCEEDED(status)) {
                    status = stream_descriptor->GetMediaTypeHandler(
                        &media_type_handler);
                }
                DWORD media_type_count = 0U;
                if (SUCCEEDED(status)) {
                    status = media_type_handler->GetMediaTypeCount(
                        &media_type_count);
                }
                Microsoft::WRL::ComPtr<IMFMediaType> legacy_media_type;
                if (SUCCEEDED(status)) {
                    status = media_type_handler->GetMediaTypeByIndex(
                        1U, &legacy_media_type);
                }
                GUID legacy_subtype{};
                if (SUCCEEDED(status)) {
                    status = legacy_media_type->GetGUID(MF_MT_SUBTYPE,
                                                        &legacy_subtype);
                }
                expect(SUCCEEDED(status) && media_type_count == 2U &&
                           legacy_subtype == MFVideoFormat_YUY2,
                       "the stream exposes a YUY2 fallback for legacy consumers");
                Microsoft::WRL::ComPtr<IMFMediaType> media_type;
                if (SUCCEEDED(status)) {
                    status = media_type_handler->GetCurrentMediaType(&media_type);
                }
                UINT32 width = 0U;
                UINT32 height = 0U;
                UINT32 fps_numerator = 0U;
                UINT32 fps_denominator = 0U;
                if (SUCCEEDED(status)) {
                    status = MFGetAttributeSize(media_type.Get(), MF_MT_FRAME_SIZE,
                                                &width, &height);
                }
                if (SUCCEEDED(status)) {
                    status = MFGetAttributeRatio(
                        media_type.Get(), MF_MT_FRAME_RATE, &fps_numerator,
                        &fps_denominator);
                }
                expect(SUCCEEDED(status) && stream_selected == FALSE &&
                           width == 1920U && height == 1080U &&
                           fps_numerator == 30U && fps_denominator == 1U,
                       "the descriptor advertises the activation video format");
                if (SUCCEEDED(status)) {
                    status = media_type->DeleteItem(MF_MT_VIDEO_NOMINAL_RANGE);
                }
                expect(SUCCEEDED(status),
                       "a consumer may normalize optional media-type attributes");
                if (SUCCEEDED(status)) {
                    status = descriptor->SelectStream(0U);
                }
                Microsoft::WRL::ComPtr<IMFVideoSampleAllocatorEx> allocator;
                Microsoft::WRL::ComPtr<IMFSampleAllocatorControl>
                    allocator_control;
                Microsoft::WRL::ComPtr<ID3D11Device> d3d_device;
                Microsoft::WRL::ComPtr<ID3D11DeviceContext> d3d_context;
                Microsoft::WRL::ComPtr<IMFDXGIDeviceManager> device_manager;
                UINT device_reset_token = 0U;
                if (SUCCEEDED(status)) {
                    status = D3D11CreateDevice(
                        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
                        D3D11_CREATE_DEVICE_BGRA_SUPPORT |
                            D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
                        nullptr, 0U, D3D11_SDK_VERSION, &d3d_device, nullptr,
                        &d3d_context);
                }
                if (SUCCEEDED(status)) {
                    status = MFCreateDXGIDeviceManager(&device_reset_token,
                                                       &device_manager);
                }
                if (SUCCEEDED(status)) {
                    status = device_manager->ResetDevice(d3d_device.Get(),
                                                         device_reset_token);
                }
                if (SUCCEEDED(status)) {
                    status = MFCreateVideoSampleAllocatorEx(
                        IID_PPV_ARGS(&allocator));
                }
                if (SUCCEEDED(status)) {
                    status = allocator->SetDirectXManager(device_manager.Get());
                }
                if (SUCCEEDED(status)) {
                    status = source.As(&allocator_control);
                }
                if (SUCCEEDED(status)) {
                    status = allocator_control->SetDefaultAllocator(
                        0U, allocator.Get());
                }
                PROPVARIANT start_position{};
                if (SUCCEEDED(status)) {
                    status = source->Start(descriptor.Get(), nullptr,
                                           &start_position);
                }
                if (FAILED(status)) {
                    std::cerr << "offline source start HRESULT: 0x" << std::hex
                              << static_cast<unsigned long>(status) << std::dec
                              << '\n';
                }
                expect(SUCCEEDED(status),
                       "the source accepts a semantically compatible consumer media type");
                Microsoft::WRL::ComPtr<IMFMediaStream> stream;
                if (SUCCEEDED(status)) {
                    Microsoft::WRL::ComPtr<IMFMediaEvent> source_event;
                    status = source->GetEvent(0U, &source_event);
                    MediaEventType event_type = MEUnknown;
                    if (SUCCEEDED(status)) {
                        status = source_event->GetType(&event_type);
                    }
                    PROPVARIANT event_value{};
                    if (SUCCEEDED(status) && event_type == MENewStream) {
                        status = source_event->GetValue(&event_value);
                    } else if (SUCCEEDED(status)) {
                        status = E_UNEXPECTED;
                    }
                    if (SUCCEEDED(status) && event_value.vt == VT_UNKNOWN &&
                        event_value.punkVal != nullptr) {
                        status = event_value.punkVal->QueryInterface(
                            IID_PPV_ARGS(&stream));
                    } else if (SUCCEEDED(status)) {
                        status = E_UNEXPECTED;
                    }
                    static_cast<void>(PropVariantClear(&event_value));
                }
                Microsoft::WRL::ComPtr<IMFSample> sample;
                if (SUCCEEDED(status) && stream != nullptr) {
                    Microsoft::WRL::ComPtr<IMFMediaEvent> stream_started;
                    status = stream->GetEvent(0U, &stream_started);
                    MediaEventType event_type = MEUnknown;
                    if (SUCCEEDED(status)) {
                        status = stream_started->GetType(&event_type);
                    }
                    if (SUCCEEDED(status) && event_type != MEStreamStarted) {
                        status = E_UNEXPECTED;
                    }
                }
                if (SUCCEEDED(status)) {
                    status = stream->RequestSample(nullptr);
                }
                if (SUCCEEDED(status)) {
                    Microsoft::WRL::ComPtr<IMFMediaEvent> sample_event;
                    status = stream->GetEvent(0U, &sample_event);
                    MediaEventType event_type = MEUnknown;
                    HRESULT event_status = E_FAIL;
                    if (SUCCEEDED(status)) {
                        status = sample_event->GetType(&event_type);
                    }
                    if (SUCCEEDED(status)) {
                        status = sample_event->GetStatus(&event_status);
                    }
                    PROPVARIANT event_value{};
                    if (SUCCEEDED(status) && event_type == MEMediaSample &&
                        SUCCEEDED(event_status)) {
                        status = sample_event->GetValue(&event_value);
                    } else if (SUCCEEDED(status)) {
                        status = FAILED(event_status) ? event_status : E_UNEXPECTED;
                    }
                    if (SUCCEEDED(status) && event_value.vt == VT_UNKNOWN &&
                        event_value.punkVal != nullptr) {
                        status = event_value.punkVal->QueryInterface(
                            IID_PPV_ARGS(&sample));
                    } else if (SUCCEEDED(status)) {
                        status = E_UNEXPECTED;
                    }
                    static_cast<void>(PropVariantClear(&event_value));
                }
                if (FAILED(status)) {
                    std::cerr << "offline source sample HRESULT: 0x" << std::hex
                              << static_cast<unsigned long>(status) << std::dec
                              << '\n';
                }
                expect(SUCCEEDED(status) && sample != nullptr,
                       "the offline branded fallback produces a video sample");
                constexpr std::size_t burst_sample_count = 4U;
                const auto request_started = std::chrono::steady_clock::now();
                for (std::size_t index = 0U;
                     SUCCEEDED(status) && index < burst_sample_count; ++index) {
                    status = stream->RequestSample(nullptr);
                }
                const auto requests_completed = std::chrono::steady_clock::now();
                for (std::size_t index = 0U;
                     SUCCEEDED(status) && index < burst_sample_count; ++index) {
                    Microsoft::WRL::ComPtr<IMFMediaEvent> sample_event;
                    status = stream->GetEvent(0U, &sample_event);
                    MediaEventType event_type = MEUnknown;
                    if (SUCCEEDED(status)) {
                        status = sample_event->GetType(&event_type);
                    }
                    if (SUCCEEDED(status) && event_type != MEMediaSample) {
                        status = E_UNEXPECTED;
                    }
                }
                const auto burst_completed = std::chrono::steady_clock::now();
                const auto request_elapsed = requests_completed - request_started;
                const auto delivery_elapsed = burst_completed - request_started;
                expect(
                    SUCCEEDED(status) &&
                        request_elapsed < std::chrono::milliseconds{50},
                    "sample requests are queued without blocking the caller");
                expect(
                    SUCCEEDED(status) &&
                        delivery_elapsed >= std::chrono::milliseconds{75} &&
                        delivery_elapsed < std::chrono::seconds{1},
                    "sample delivery is paced to the negotiated frame rate");
                if (SUCCEEDED(status)) {
                    status = source->Stop();
                }
                expect(SUCCEEDED(status),
                       "an offline fallback stream stops cleanly");
                auto* const first_source = source.Get();
                expect(SUCCEEDED(activation->ShutdownObject()),
                       "IMFActivate shuts down and releases its active source");
                Microsoft::WRL::ComPtr<IMFMediaSource> reactivated_source;
                const auto reactivation_status = activation->ActivateObject(
                    IID_PPV_ARGS(&reactivated_source));
                expect(SUCCEEDED(reactivation_status) &&
                           reactivated_source.Get() != first_source,
                       "activation after shutdown creates a fresh media source");
                expect(SUCCEEDED(activation->DetachObject()),
                       "IMFActivate detaches its cached media source");
                if (reactivated_source != nullptr) {
                    static_cast<void>(reactivated_source->Shutdown());
                }
                reactivated_source.Reset();
                source_attributes.Reset();
                source_ex.Reset();
                descriptor.Reset();
                source.Reset();
            }
            activation.Reset();
            factory.Reset();
            expect(can_unload() == S_OK,
                   "the DLL becomes unloadable after all COM objects are released");
        }
    }
    static_cast<void>(MFShutdown());
    CoUninitialize();
    return failures == 0 ? 0 : 1;
}
