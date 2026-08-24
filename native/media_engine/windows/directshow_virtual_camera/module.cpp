#include "filter.hpp"

#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <dshow.h>
#include <windows.h>

#include <array>
#include <string>

using solin::media_engine::windows_virtual_camera::DirectShowVirtualCameraFilter;

extern HINSTANCE g_hInst;
extern "C" BOOL WINAPI DllEntryPoint(HINSTANCE instance, ULONG reason,
                                      LPVOID reserved);
STDAPI AMovieSetupRegisterServer(CLSID class_id, LPCWSTR description,
                                LPCWSTR file_name,
                                LPCWSTR threading_model = L"Both",
                                LPCWSTR server_type = L"InprocServer32");
STDAPI AMovieSetupUnregisterServer(CLSID class_id);

CFactoryTemplate g_Templates[] = {
    {const_cast<LPWSTR>(solin::media_engine::windows_virtual_camera::kFriendlyName),
     &solin::media_engine::windows_virtual_camera::kDirectShowFilterClassId,
     DirectShowVirtualCameraFilter::CreateInstance, nullptr, nullptr},
};
int g_cTemplates = static_cast<int>(std::size(g_Templates));

namespace {

template <typename Interface> class ComPtr final {
  public:
    ~ComPtr() {
        if (value_ != nullptr) {
            value_->Release();
        }
    }
    ComPtr(const ComPtr&) = delete;
    ComPtr& operator=(const ComPtr&) = delete;
    ComPtr() = default;
    [[nodiscard]] Interface* get() const noexcept { return value_; }
    [[nodiscard]] Interface** put() noexcept { return &value_; }

  private:
    Interface* value_{nullptr};
};

class RegistryKey final {
  public:
    ~RegistryKey() {
        if (value_ != nullptr) {
            static_cast<void>(RegCloseKey(value_));
        }
    }
    RegistryKey(const RegistryKey&) = delete;
    RegistryKey& operator=(const RegistryKey&) = delete;
    RegistryKey() = default;
    [[nodiscard]] HKEY get() const noexcept { return value_; }
    [[nodiscard]] HKEY* put() noexcept { return &value_; }

  private:
    HKEY value_{nullptr};
};

class ClassesRootOverride final {
  public:
    explicit ClassesRootOverride(const HKEY replacement) noexcept
        : result_(HRESULT_FROM_WIN32(
              RegOverridePredefKey(HKEY_CLASSES_ROOT, replacement))) {}
    ~ClassesRootOverride() {
        if (SUCCEEDED(result_)) {
            static_cast<void>(RegOverridePredefKey(HKEY_CLASSES_ROOT, nullptr));
        }
    }
    ClassesRootOverride(const ClassesRootOverride&) = delete;
    ClassesRootOverride& operator=(const ClassesRootOverride&) = delete;
    [[nodiscard]] HRESULT result() const noexcept { return result_; }

  private:
    HRESULT result_{E_FAIL};
};

class ComApartment final {
  public:
    ComApartment() noexcept : result_(CoInitializeEx(nullptr, COINIT_MULTITHREADED)) {}
    ~ComApartment() {
        if (SUCCEEDED(result_)) {
            CoUninitialize();
        }
    }
    [[nodiscard]] HRESULT result() const noexcept {
        return result_ == RPC_E_CHANGED_MODE ? S_OK : result_;
    }

  private:
    HRESULT result_{E_FAIL};
};

[[nodiscard]] HRESULT open_classes_root(RegistryKey& classes_root) noexcept {
    const auto result = RegCreateKeyExW(
        HKEY_CURRENT_USER, L"Software\\Classes", 0U, nullptr,
        REG_OPTION_NON_VOLATILE, KEY_READ | KEY_WRITE, nullptr,
        classes_root.put(), nullptr);
    return HRESULT_FROM_WIN32(result);
}

[[nodiscard]] HRESULT module_path(std::wstring& destination) {
    std::array<wchar_t, 32'768U> path{};
    const auto length = GetModuleFileNameW(g_hInst, path.data(),
                                           static_cast<DWORD>(path.size()));
    if (length == 0U) {
        return HRESULT_FROM_WIN32(GetLastError());
    }
    if (length >= path.size()) {
        return HRESULT_FROM_WIN32(ERROR_INSUFFICIENT_BUFFER);
    }
    destination.assign(path.data(), length);
    return S_OK;
}

[[nodiscard]] HRESULT create_filter_mapper(ComPtr<IFilterMapper2>& mapper) {
    return CoCreateInstance(CLSID_FilterMapper2, nullptr, CLSCTX_INPROC_SERVER,
                            IID_IFilterMapper2,
                            reinterpret_cast<void**>(mapper.put()));
}

[[nodiscard]] HRESULT register_filter(IFilterMapper2* mapper) noexcept {
    using namespace solin::media_engine::windows_virtual_camera;
    static const std::array<REGPINTYPES, 2U> media_types{{
        {&MEDIATYPE_Video, &MEDIASUBTYPE_NV12},
        {&MEDIATYPE_Video, &MEDIASUBTYPE_YUY2},
    }};
    static const REGFILTERPINS2 capture_pin{
        .dwFlags = REG_PINFLAG_B_OUTPUT,
        .cInstances = 1U,
        .nMediaTypes = static_cast<UINT>(media_types.size()),
        .lpMediaType = media_types.data(),
        .nMediums = 0U,
        .lpMedium = nullptr,
        .clsPinCategory = &PIN_CATEGORY_CAPTURE,
    };
    REGFILTER2 registration{};
    registration.dwVersion = 2U;
    registration.dwMerit = MERIT_DO_NOT_USE;
    registration.cPins2 = 1U;
    registration.rgPins2 = &capture_pin;
    return mapper->RegisterFilter(kDirectShowFilterClassId, kFriendlyName,
                                  nullptr, &CLSID_VideoInputDeviceCategory,
                                  nullptr, &registration);
}

[[nodiscard]] HRESULT register_current_user(IFilterMapper2* mapper) {
    using namespace solin::media_engine::windows_virtual_camera;
    std::wstring path;
    auto result = module_path(path);
    if (FAILED(result)) {
        return result;
    }
    RegistryKey classes_root;
    result = open_classes_root(classes_root);
    if (FAILED(result)) {
        return result;
    }
    ClassesRootOverride override{classes_root.get()};
    if (FAILED(override.result())) {
        return override.result();
    }
    result = AMovieSetupRegisterServer(kDirectShowFilterClassId, kFriendlyName,
                                       path.c_str(), L"Both");
    if (SUCCEEDED(result)) {
        result = register_filter(mapper);
    }
    if (FAILED(result)) {
        static_cast<void>(mapper->UnregisterFilter(
            &CLSID_VideoInputDeviceCategory, nullptr,
            kDirectShowFilterClassId));
        static_cast<void>(AMovieSetupUnregisterServer(kDirectShowFilterClassId));
    }
    return result;
}

[[nodiscard]] HRESULT unregister_current_user(IFilterMapper2* mapper) {
    using namespace solin::media_engine::windows_virtual_camera;
    RegistryKey classes_root;
    auto result = open_classes_root(classes_root);
    if (FAILED(result)) {
        return result;
    }
    ClassesRootOverride override{classes_root.get()};
    if (FAILED(override.result())) {
        return override.result();
    }
    const auto mapper_result = mapper->UnregisterFilter(
        &CLSID_VideoInputDeviceCategory, nullptr, kDirectShowFilterClassId);
    const auto server_result =
        AMovieSetupUnregisterServer(kDirectShowFilterClassId);
    const auto is_absent = [](const HRESULT result) noexcept {
        return result == HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND) ||
               result == HRESULT_FROM_WIN32(ERROR_PATH_NOT_FOUND) ||
               result == VFW_E_NOT_FOUND;
    };
    if (FAILED(mapper_result) && !is_absent(mapper_result)) {
        return mapper_result;
    }
    return FAILED(server_result) && !is_absent(server_result) ? server_result
                                                               : S_OK;
}

[[nodiscard]] bool filter_is_enumerated() noexcept {
    using namespace solin::media_engine::windows_virtual_camera;
    ComPtr<ICreateDevEnum> device_enumerator;
    if (FAILED(CoCreateInstance(CLSID_SystemDeviceEnum, nullptr,
                                CLSCTX_INPROC_SERVER, IID_ICreateDevEnum,
                                reinterpret_cast<void**>(
                                    device_enumerator.put())))) {
        return false;
    }
    ComPtr<IEnumMoniker> monikers;
    if (device_enumerator.get()->CreateClassEnumerator(
            CLSID_VideoInputDeviceCategory, monikers.put(), 0U) != S_OK) {
        return false;
    }
    for (;;) {
        IMoniker* raw_moniker = nullptr;
        ULONG fetched = 0U;
        if (monikers.get()->Next(1U, &raw_moniker, &fetched) != S_OK) {
            return false;
        }
        ComPtr<IPropertyBag> properties;
        const auto bind_result = raw_moniker->BindToStorage(
            nullptr, nullptr, IID_IPropertyBag,
            reinterpret_cast<void**>(properties.put()));
        raw_moniker->Release();
        if (FAILED(bind_result)) {
            continue;
        }
        VARIANT class_id;
        VariantInit(&class_id);
        const auto read_result =
            properties.get()->Read(L"CLSID", &class_id, nullptr);
        if (SUCCEEDED(read_result) && class_id.vt == VT_BSTR &&
            class_id.bstrVal != nullptr) {
            CLSID parsed{};
            if (SUCCEEDED(CLSIDFromString(class_id.bstrVal, &parsed)) &&
                IsEqualGUID(parsed, kDirectShowFilterClassId) != FALSE) {
                VariantClear(&class_id);
                return true;
            }
        }
        VariantClear(&class_id);
    }
}

[[nodiscard]] HRESULT verify_registration() noexcept {
    using namespace solin::media_engine::windows_virtual_camera;
    ComPtr<IBaseFilter> filter;
    const auto result = CoCreateInstance(
        kDirectShowFilterClassId, nullptr, CLSCTX_INPROC_SERVER, IID_IBaseFilter,
        reinterpret_cast<void**>(filter.put()));
    return SUCCEEDED(result) && filter_is_enumerated() ? S_OK : E_FAIL;
}

} // namespace

extern "C" BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason,
                               LPVOID reserved) {
    return DllEntryPoint(instance, reason, reserved);
}

STDAPI DllRegisterServer() {
    ComApartment apartment;
    if (FAILED(apartment.result())) {
        return apartment.result();
    }
    ComPtr<IFilterMapper2> mapper;
    auto result = create_filter_mapper(mapper);
    if (FAILED(result)) {
        return result;
    }
    result = register_current_user(mapper.get());
    if (SUCCEEDED(result)) {
        result = verify_registration();
    }
    if (FAILED(result)) {
        static_cast<void>(unregister_current_user(mapper.get()));
    }
    return result;
}

STDAPI DllUnregisterServer() {
    ComApartment apartment;
    if (FAILED(apartment.result())) {
        return apartment.result();
    }
    ComPtr<IFilterMapper2> mapper;
    const auto result = create_filter_mapper(mapper);
    return FAILED(result) ? result : unregister_current_user(mapper.get());
}
