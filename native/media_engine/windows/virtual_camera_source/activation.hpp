#pragma once

#include "media_source.hpp"

#include <mfidl.h>
#include <mfobjects.h>
#include <wrl.h>
#include <wrl/client.h>
#include <wrl/implements.h>

#include <mutex>

namespace solin::media_engine::windows_virtual_camera {

class SolinVirtualCameraActivate final
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IMFActivate, Microsoft::WRL::FtmBase> {
  public:
    HRESULT Initialize();

    STDMETHODIMP ActivateObject(REFIID interface_id, void** object) override;
    STDMETHODIMP ShutdownObject() override;
    STDMETHODIMP DetachObject() override;

    STDMETHODIMP GetItem(REFGUID key, PROPVARIANT* item_value) override;
    STDMETHODIMP GetItemType(REFGUID key, MF_ATTRIBUTE_TYPE* type) override;
    STDMETHODIMP CompareItem(REFGUID key, REFPROPVARIANT item_value,
                             BOOL* result) override;
    STDMETHODIMP Compare(IMFAttributes* theirs,
                         MF_ATTRIBUTES_MATCH_TYPE match_type,
                         BOOL* result) override;
    STDMETHODIMP GetUINT32(REFGUID key, UINT32* item_value) override;
    STDMETHODIMP GetUINT64(REFGUID key, UINT64* item_value) override;
    STDMETHODIMP GetDouble(REFGUID key, double* item_value) override;
    STDMETHODIMP GetGUID(REFGUID key, GUID* item_value) override;
    STDMETHODIMP GetStringLength(REFGUID key, UINT32* length) override;
    STDMETHODIMP GetString(REFGUID key, LPWSTR item_value, UINT32 value_size,
                           UINT32* length) override;
    STDMETHODIMP GetAllocatedString(REFGUID key, LPWSTR* item_value,
                                    UINT32* length) override;
    STDMETHODIMP GetBlobSize(REFGUID key, UINT32* size) override;
    STDMETHODIMP GetBlob(REFGUID key, UINT8* buffer, UINT32 buffer_size,
                         UINT32* blob_size) override;
    STDMETHODIMP GetAllocatedBlob(REFGUID key, UINT8** buffer,
                                  UINT32* size) override;
    STDMETHODIMP GetUnknown(REFGUID key, REFIID interface_id,
                            LPVOID* object) override;
    STDMETHODIMP SetItem(REFGUID key, REFPROPVARIANT item_value) override;
    STDMETHODIMP DeleteItem(REFGUID key) override;
    STDMETHODIMP DeleteAllItems() override;
    STDMETHODIMP SetUINT32(REFGUID key, UINT32 item_value) override;
    STDMETHODIMP SetUINT64(REFGUID key, UINT64 item_value) override;
    STDMETHODIMP SetDouble(REFGUID key, double item_value) override;
    STDMETHODIMP SetGUID(REFGUID key, REFGUID item_value) override;
    STDMETHODIMP SetString(REFGUID key, LPCWSTR item_value) override;
    STDMETHODIMP SetBlob(REFGUID key, const UINT8* buffer,
                         UINT32 buffer_size) override;
    STDMETHODIMP SetUnknown(REFGUID key, IUnknown* item_value) override;
    STDMETHODIMP LockStore() override;
    STDMETHODIMP UnlockStore() override;
    STDMETHODIMP GetCount(UINT32* item_count) override;
    STDMETHODIMP GetItemByIndex(UINT32 index, GUID* key,
                                PROPVARIANT* item_value) override;
    STDMETHODIMP CopyAllItems(IMFAttributes* destination) override;

  private:
    Microsoft::WRL::ComPtr<IMFAttributes> attributes_{};
    std::mutex source_mutex_{};
    Microsoft::WRL::ComPtr<SolinMediaSource> source_{};
};

} // namespace solin::media_engine::windows_virtual_camera
