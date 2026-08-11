#pragma once

#include <unknwn.h>
#include <ks.h>
#include <ksmedia.h>
#include <ksproxy.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <wrl.h>
#include <wrl/client.h>
#include <wrl/implements.h>

#include "broker_client.hpp"
#include "media_stream.hpp"

#include <cstdint>
#include <memory>
#include <mutex>

namespace solin::media_engine::windows_virtual_camera {

class SolinMediaSource final
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          Microsoft::WRL::ChainInterfaces<IMFMediaSourceEx, IMFMediaSource,
                                          IMFMediaEventGenerator>,
          IMFGetService, IKsControl, IMFSampleAllocatorControl,
          Microsoft::WRL::FtmBase> {
  public:
    HRESULT Initialize(IMFAttributes* activation_attributes);

    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* callback,
                               IUnknown* state) override;
    STDMETHODIMP EndGetEvent(IMFAsyncResult* result,
                             IMFMediaEvent** event) override;
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** event) override;
    STDMETHODIMP QueueEvent(MediaEventType type, REFGUID extended_type,
                            HRESULT status,
                            const PROPVARIANT* event_value) override;

    STDMETHODIMP GetCharacteristics(DWORD* characteristics) override;
    STDMETHODIMP CreatePresentationDescriptor(
        IMFPresentationDescriptor** descriptor) override;
    STDMETHODIMP Start(IMFPresentationDescriptor* descriptor,
                       const GUID* time_format,
                       const PROPVARIANT* start_position) override;
    STDMETHODIMP Stop() override;
    STDMETHODIMP Pause() override;
    STDMETHODIMP Shutdown() override;

    STDMETHODIMP GetSourceAttributes(IMFAttributes** attributes) override;
    STDMETHODIMP GetStreamAttributes(DWORD stream_identifier,
                                     IMFAttributes** attributes) override;
    STDMETHODIMP SetD3DManager(IUnknown* manager) override;

    STDMETHODIMP GetService(REFGUID service, REFIID interface_id,
                            LPVOID* object) override;

    STDMETHODIMP KsProperty(PKSPROPERTY property, ULONG property_length,
                            LPVOID property_data, ULONG data_length,
                            ULONG* bytes_returned) override;
    STDMETHODIMP KsMethod(PKSMETHOD method, ULONG method_length,
                          LPVOID method_data, ULONG data_length,
                          ULONG* bytes_returned) override;
    STDMETHODIMP KsEvent(PKSEVENT event, ULONG event_length,
                         LPVOID event_data, ULONG data_length,
                         ULONG* bytes_returned) override;

    STDMETHODIMP SetDefaultAllocator(DWORD output_stream_id,
                                     IUnknown* allocator) override;
    STDMETHODIMP GetAllocatorUsage(DWORD output_stream_id,
                                   DWORD* input_stream_id,
                                   MFSampleAllocatorUsage* usage) override;

  private:
    enum class State : std::uint8_t {
        stopped,
        started,
        shutdown,
    };

    [[nodiscard]] HRESULT check_shutdown_locked() const noexcept;

    mutable std::mutex mutex_{};
    State state_{State::stopped};
    bool initialized_{false};
    Microsoft::WRL::ComPtr<IMFMediaEventQueue> event_queue_{};
    Microsoft::WRL::ComPtr<IMFAttributes> attributes_{};
    Microsoft::WRL::ComPtr<IMFPresentationDescriptor> descriptor_{};
    Microsoft::WRL::ComPtr<SolinMediaStream> stream_{};
    std::shared_ptr<BrokerFrameProvider> provider_{};
};

} // namespace solin::media_engine::windows_virtual_camera
