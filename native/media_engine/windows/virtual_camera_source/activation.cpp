#include "activation.hpp"

#include <memory>

namespace solin::media_engine::windows_virtual_camera {

HRESULT SolinVirtualCameraActivate::Initialize() {
    return MFCreateAttributes(&attributes_, 4U);
}

STDMETHODIMP SolinVirtualCameraActivate::ActivateObject(REFIID interface_id,
                                                        void** object) {
    if (object == nullptr) {
        return E_POINTER;
    }
    *object = nullptr;
    std::scoped_lock lock{source_mutex_};
    if (source_ == nullptr) {
        auto source = Microsoft::WRL::Make<SolinMediaSource>();
        if (source == nullptr) {
            return E_OUTOFMEMORY;
        }
        const auto status = source->Initialize(attributes_.Get());
        if (FAILED(status)) {
            return status;
        }
        source_ = std::move(source);
    }
    return source_->QueryInterface(interface_id, object);
}

STDMETHODIMP SolinVirtualCameraActivate::ShutdownObject() {
    Microsoft::WRL::ComPtr<SolinMediaSource> source;
    {
        std::scoped_lock lock{source_mutex_};
        source = source_;
        source_.Reset();
    }
    return source == nullptr ? S_OK : source->Shutdown();
}

STDMETHODIMP SolinVirtualCameraActivate::DetachObject() {
    std::scoped_lock lock{source_mutex_};
    source_.Reset();
    return S_OK;
}

STDMETHODIMP SolinVirtualCameraActivate::GetItem(REFGUID key,
                                                 PROPVARIANT* item_value) {
    return attributes_->GetItem(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::GetItemType(REFGUID key,
                                                     MF_ATTRIBUTE_TYPE* type) {
    return attributes_->GetItemType(key, type);
}

STDMETHODIMP SolinVirtualCameraActivate::CompareItem(REFGUID key,
                                                     REFPROPVARIANT item_value,
                                                     BOOL* result) {
    return attributes_->CompareItem(key, item_value, result);
}

STDMETHODIMP SolinVirtualCameraActivate::Compare(
    IMFAttributes* theirs, MF_ATTRIBUTES_MATCH_TYPE match_type, BOOL* result) {
    return attributes_->Compare(theirs, match_type, result);
}

STDMETHODIMP SolinVirtualCameraActivate::GetUINT32(REFGUID key,
                                                   UINT32* item_value) {
    return attributes_->GetUINT32(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::GetUINT64(REFGUID key,
                                                   UINT64* item_value) {
    return attributes_->GetUINT64(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::GetDouble(REFGUID key,
                                                   double* item_value) {
    return attributes_->GetDouble(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::GetGUID(REFGUID key,
                                                 GUID* item_value) {
    return attributes_->GetGUID(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::GetStringLength(REFGUID key,
                                                         UINT32* length) {
    return attributes_->GetStringLength(key, length);
}

STDMETHODIMP SolinVirtualCameraActivate::GetString(REFGUID key,
                                                   LPWSTR item_value,
                                                   UINT32 value_size,
                                                   UINT32* length) {
    return attributes_->GetString(key, item_value, value_size, length);
}

STDMETHODIMP SolinVirtualCameraActivate::GetAllocatedString(REFGUID key,
                                                            LPWSTR* item_value,
                                                            UINT32* length) {
    return attributes_->GetAllocatedString(key, item_value, length);
}

STDMETHODIMP SolinVirtualCameraActivate::GetBlobSize(REFGUID key, UINT32* size) {
    return attributes_->GetBlobSize(key, size);
}

STDMETHODIMP SolinVirtualCameraActivate::GetBlob(REFGUID key, UINT8* buffer,
                                                 UINT32 buffer_size,
                                                 UINT32* blob_size) {
    return attributes_->GetBlob(key, buffer, buffer_size, blob_size);
}

STDMETHODIMP SolinVirtualCameraActivate::GetAllocatedBlob(REFGUID key,
                                                          UINT8** buffer,
                                                          UINT32* size) {
    return attributes_->GetAllocatedBlob(key, buffer, size);
}

STDMETHODIMP SolinVirtualCameraActivate::GetUnknown(REFGUID key,
                                                    REFIID interface_id,
                                                    LPVOID* object) {
    return attributes_->GetUnknown(key, interface_id, object);
}

STDMETHODIMP SolinVirtualCameraActivate::SetItem(REFGUID key,
                                                 REFPROPVARIANT item_value) {
    return attributes_->SetItem(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::DeleteItem(REFGUID key) {
    return attributes_->DeleteItem(key);
}

STDMETHODIMP SolinVirtualCameraActivate::DeleteAllItems() {
    return attributes_->DeleteAllItems();
}

STDMETHODIMP SolinVirtualCameraActivate::SetUINT32(REFGUID key,
                                                   UINT32 item_value) {
    return attributes_->SetUINT32(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::SetUINT64(REFGUID key,
                                                   UINT64 item_value) {
    return attributes_->SetUINT64(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::SetDouble(REFGUID key,
                                                   double item_value) {
    return attributes_->SetDouble(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::SetGUID(REFGUID key,
                                                 REFGUID item_value) {
    return attributes_->SetGUID(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::SetString(REFGUID key,
                                                   LPCWSTR item_value) {
    return attributes_->SetString(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::SetBlob(REFGUID key,
                                                 const UINT8* buffer,
                                                 UINT32 buffer_size) {
    return attributes_->SetBlob(key, buffer, buffer_size);
}

STDMETHODIMP SolinVirtualCameraActivate::SetUnknown(REFGUID key,
                                                    IUnknown* item_value) {
    return attributes_->SetUnknown(key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::LockStore() {
    return attributes_->LockStore();
}

STDMETHODIMP SolinVirtualCameraActivate::UnlockStore() {
    return attributes_->UnlockStore();
}

STDMETHODIMP SolinVirtualCameraActivate::GetCount(UINT32* item_count) {
    return attributes_->GetCount(item_count);
}

STDMETHODIMP SolinVirtualCameraActivate::GetItemByIndex(UINT32 index, GUID* key,
                                                        PROPVARIANT* item_value) {
    return attributes_->GetItemByIndex(index, key, item_value);
}

STDMETHODIMP SolinVirtualCameraActivate::CopyAllItems(
    IMFAttributes* destination) {
    return attributes_->CopyAllItems(destination);
}

} // namespace solin::media_engine::windows_virtual_camera
