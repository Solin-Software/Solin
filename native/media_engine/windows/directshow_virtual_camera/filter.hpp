#pragma once

#include "broker_client.hpp"
#include "frame_adapter.hpp"

#include <streams.h>

#include <chrono>
#include <cstdint>
#include <memory>
#include <vector>

namespace solin::media_engine::windows_virtual_camera {

class DirectShowCapturePin final : public CSourceStream,
                                   public IAMStreamConfig,
                                   public IKsPropertySet,
                                   public IAMPushSource {
  public:
    DECLARE_IUNKNOWN;

    DirectShowCapturePin(HRESULT* result, CSource* filter);
    ~DirectShowCapturePin() override = default;

    STDMETHODIMP NonDelegatingQueryInterface(REFIID interface_id,
                                               void** object) override;

    HRESULT FillBuffer(IMediaSample* sample) override;
    HRESULT DecideBufferSize(IMemAllocator* allocator,
                             ALLOCATOR_PROPERTIES* properties) override;
    HRESULT CheckMediaType(const CMediaType* media_type) override;
    HRESULT GetMediaType(int position, CMediaType* media_type) override;
    HRESULT SetMediaType(const CMediaType* media_type) override;
    HRESULT OnThreadCreate() override;

    STDMETHODIMP SetFormat(AM_MEDIA_TYPE* media_type) override;
    STDMETHODIMP GetFormat(AM_MEDIA_TYPE** media_type) override;
    STDMETHODIMP GetNumberOfCapabilities(int* count, int* size) override;
    STDMETHODIMP GetStreamCaps(int index, AM_MEDIA_TYPE** media_type,
                               BYTE* capabilities) override;

    STDMETHODIMP Set(REFGUID property_set, DWORD property_id,
                     LPVOID instance_data, DWORD instance_size,
                     LPVOID property_data, DWORD property_size) override;
    STDMETHODIMP Get(REFGUID property_set, DWORD property_id,
                     LPVOID instance_data, DWORD instance_size,
                     LPVOID property_data, DWORD property_size,
                     DWORD* returned_size) override;
    STDMETHODIMP QuerySupported(REFGUID property_set, DWORD property_id,
                                DWORD* support_type) override;

    STDMETHODIMP GetLatency(REFERENCE_TIME* latency) override;
    STDMETHODIMP GetPushSourceFlags(ULONG* flags) override;
    STDMETHODIMP SetPushSourceFlags(ULONG flags) override;
    STDMETHODIMP SetStreamOffset(REFERENCE_TIME offset) override;
    STDMETHODIMP GetStreamOffset(REFERENCE_TIME* offset) override;
    STDMETHODIMP GetMaxStreamOffset(REFERENCE_TIME* maximum_offset) override;
    STDMETHODIMP SetMaxStreamOffset(REFERENCE_TIME maximum_offset) override;

  private:
    HRESULT select_profile(const DirectShowMediaProfile& profile);

    DirectShowMediaProfile profile_{kMediaProfiles.front()};
    std::unique_ptr<DirectShowFrameAdapter> adapter_{};
    BrokerFrameProvider provider_{};
    std::vector<std::uint8_t> last_output_{};
    std::vector<std::uint8_t> staging_output_{};
    std::chrono::steady_clock::time_point deadline_epoch_{};
    std::uint64_t deadline_index_{0U};
    std::uint64_t frame_index_{0U};
    std::uint64_t last_generation_{0U};
    std::uint64_t last_input_sequence_{0U};
    bool last_frame_was_live_{false};
    bool have_last_input_sequence_{false};
    bool first_sample_{true};
};

class DirectShowVirtualCameraFilter final : public CSource,
                                            public IAMFilterMiscFlags {
  public:
    DECLARE_IUNKNOWN;

    static CUnknown* WINAPI CreateInstance(LPUNKNOWN outer, HRESULT* result);

    DirectShowVirtualCameraFilter(LPUNKNOWN outer, HRESULT* result);
    ~DirectShowVirtualCameraFilter() override = default;

    STDMETHODIMP NonDelegatingQueryInterface(REFIID interface_id,
                                               void** object) override;
    STDMETHODIMP_(ULONG) GetMiscFlags() override;
};

[[nodiscard]] HRESULT configure_media_type(
    const DirectShowMediaProfile& profile, CMediaType& media_type) noexcept;
[[nodiscard]] const DirectShowMediaProfile* profile_from_media_type(
    const AM_MEDIA_TYPE& media_type) noexcept;

} // namespace solin::media_engine::windows_virtual_camera
