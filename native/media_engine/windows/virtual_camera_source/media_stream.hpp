#pragma once

#include "broker_client.hpp"

#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <wrl.h>
#include <wrl/client.h>
#include <wrl/implements.h>

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace solin::media_engine::windows_virtual_camera {

struct MediaStreamConfiguration final {
    PackedVideoFrameLayout layout{};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{0U};
};

class SolinMediaStream final
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          Microsoft::WRL::ChainInterfaces<IMFMediaStream2, IMFMediaStream,
                                          IMFMediaEventGenerator>,
          Microsoft::WRL::FtmBase> {
  public:
    ~SolinMediaStream();

    HRESULT Initialize(IMFMediaSource* parent,
                       const MediaStreamConfiguration& configuration);
    HRESULT PrepareStart(IMFMediaType* media_type,
                         std::shared_ptr<BrokerFrameProvider> provider);
    HRESULT Start(IMFMediaType* media_type,
                  std::shared_ptr<BrokerFrameProvider> provider);
    HRESULT Stop(bool send_event);
    HRESULT Shutdown();
    HRESULT SetSampleAllocator(IMFVideoSampleAllocator* allocator);
    [[nodiscard]] IMFAttributes* attributes() const noexcept;

    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* callback,
                               IUnknown* state) override;
    STDMETHODIMP EndGetEvent(IMFAsyncResult* result,
                             IMFMediaEvent** event) override;
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** event) override;
    STDMETHODIMP QueueEvent(MediaEventType type, REFGUID extended_type,
                            HRESULT status,
                            const PROPVARIANT* event_value) override;

    STDMETHODIMP GetMediaSource(IMFMediaSource** source) override;
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** descriptor) override;
    STDMETHODIMP RequestSample(IUnknown* token) override;

    STDMETHODIMP SetStreamState(MF_STREAM_STATE state) override;
    STDMETHODIMP GetStreamState(MF_STREAM_STATE* state) override;

  private:
    [[nodiscard]] HRESULT check_shutdown_locked() const noexcept;
    HRESULT ensure_allocator_locked();
    HRESULT prepare_start_locked(IMFMediaType* media_type,
                                 std::shared_ptr<BrokerFrameProvider> provider);
    HRESULT start_locked(bool send_event, IMFMediaType* media_type,
                         std::shared_ptr<BrokerFrameProvider> provider);
    HRESULT stop_locked(bool send_event, bool deselect);
    HRESULT produce_sample_locked(IUnknown* token);
    HRESULT write_sample_locked(IMFSample* sample,
                                const VideoFrameView& frame);
    void sample_worker_loop(std::stop_token stop_token) noexcept;

    mutable std::mutex mutex_{};
    std::mutex shutdown_mutex_{};
    std::condition_variable sample_wakeup_{};
    std::deque<Microsoft::WRL::ComPtr<IUnknown>> pending_sample_tokens_{};
    std::jthread sample_worker_{};
    std::chrono::steady_clock::time_point next_sample_at_{};
    bool shutdown_{false};
    bool selected_{false};
    bool first_sample_{true};
    bool output_yuy2_{false};
    std::uint64_t last_discontinuity_sequence_{0U};
    MF_STREAM_STATE state_{MF_STREAM_STATE_STOPPED};
    LONGLONG sample_duration_hns_{333'333LL};
    MediaStreamConfiguration configuration_{};
    std::shared_ptr<BrokerFrameProvider> provider_{};
    Microsoft::WRL::ComPtr<IMFMediaSource> parent_{};
    Microsoft::WRL::ComPtr<IMFMediaEventQueue> event_queue_{};
    Microsoft::WRL::ComPtr<IMFAttributes> attributes_{};
    Microsoft::WRL::ComPtr<IMFStreamDescriptor> descriptor_{};
    Microsoft::WRL::ComPtr<IMFMediaType> nv12_media_type_{};
    Microsoft::WRL::ComPtr<IMFMediaType> yuy2_media_type_{};
    Microsoft::WRL::ComPtr<IMFMediaType> media_type_{};
    Microsoft::WRL::ComPtr<IMFVideoSampleAllocator> allocator_{};
    bool allocator_initialized_{false};
    std::vector<std::uint8_t> yuy2_scratch_{};
};

} // namespace solin::media_engine::windows_virtual_camera
