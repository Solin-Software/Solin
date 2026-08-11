#include "activation.hpp"
#include "solin/media_engine/virtual_camera_identity.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#include <windows.h>
#include <mfapi.h>
#include <mfvirtualcamera.h>
#include <wrl.h>
#include <wrl/implements.h>

#include <atomic>
#include <cstdint>
#include <iterator>
#include <limits>
#include <string>

namespace {

std::atomic_uint32_t server_locks{0U};

using CreateVirtualCameraFunction = HRESULT(STDAPICALLTYPE*)(
    MFVirtualCameraType, MFVirtualCameraLifetime, MFVirtualCameraAccess, LPCWSTR,
    LPCWSTR, const GUID*, ULONG, IMFVirtualCamera**);

[[nodiscard]] std::wstring utf8_to_wide(const std::string_view value) {
    const auto required = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0);
    if (required <= 0) {
        return {};
    }
    std::wstring result(static_cast<std::size_t>(required), L'\0');
    return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                               static_cast<int>(value.size()), result.data(),
                               required) == required
               ? result
               : std::wstring{};
}

[[nodiscard]] HRESULT remove_persistent_virtual_camera() noexcept {
    const auto module = LoadLibraryExW(L"mfsensorgroup.dll", nullptr,
                                       LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (module == nullptr) {
        return HRESULT_FROM_WIN32(GetLastError());
    }
    const auto create = reinterpret_cast<CreateVirtualCameraFunction>(
        GetProcAddress(module, "MFCreateVirtualCamera"));
    if (create == nullptr) {
        const auto status = HRESULT_FROM_WIN32(GetLastError());
        static_cast<void>(FreeLibrary(module));
        return status;
    }
    bool com_initialized = false;
    bool media_foundation_initialized = false;
    HRESULT status = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (SUCCEEDED(status)) {
        com_initialized = true;
    } else if (status == RPC_E_CHANGED_MODE) {
        status = S_OK;
    }
    if (SUCCEEDED(status)) {
        status = MFStartup(MF_VERSION, MFSTARTUP_FULL);
        media_foundation_initialized = SUCCEEDED(status);
    }
    Microsoft::WRL::ComPtr<IMFVirtualCamera> camera;
    if (SUCCEEDED(status)) {
        wchar_t source_id[64]{};
        const auto friendly_name = utf8_to_wide(
            solin::media_engine::kSolinVirtualCameraFriendlyName);
        if (friendly_name.empty() ||
            StringFromGUID2(
                solin::media_engine::windows_virtual_camera::kSourceClassId,
                source_id, static_cast<int>(std::size(source_id))) == 0) {
            status = E_FAIL;
        } else {
            status = create(MFVirtualCameraType_SoftwareCameraSource,
                            MFVirtualCameraLifetime_System,
                            MFVirtualCameraAccess_CurrentUser,
                            friendly_name.c_str(), source_id, nullptr, 0U,
                            &camera);
        }
    }
    if (SUCCEEDED(status)) {
        status = camera->Remove();
    }
    if (camera != nullptr) {
        static_cast<void>(camera->Shutdown());
        camera.Reset();
    }
    if (media_foundation_initialized) {
        static_cast<void>(MFShutdown());
    }
    if (com_initialized) {
        CoUninitialize();
    }
    static_cast<void>(FreeLibrary(module));
    return status;
}

class SolinVirtualCameraClassFactory final
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IClassFactory> {
  public:
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID interface_id,
                                void** object) override {
        if (object == nullptr) {
            return E_POINTER;
        }
        *object = nullptr;
        if (outer != nullptr) {
            return CLASS_E_NOAGGREGATION;
        }
        auto activation = Microsoft::WRL::Make<
            solin::media_engine::windows_virtual_camera::
                SolinVirtualCameraActivate>();
        if (activation == nullptr) {
            return E_OUTOFMEMORY;
        }
        const auto status = activation->Initialize();
        return FAILED(status) ? status
                              : activation->QueryInterface(interface_id, object);
    }

    STDMETHODIMP LockServer(const BOOL lock) override {
        if (lock != FALSE) {
            auto current = server_locks.load(std::memory_order_relaxed);
            do {
                if (current == (std::numeric_limits<std::uint32_t>::max)()) {
                    return E_FAIL;
                }
            } while (!server_locks.compare_exchange_weak(
                current, current + 1U, std::memory_order_relaxed));
        } else {
            auto current = server_locks.load(std::memory_order_relaxed);
            while (current != 0U && !server_locks.compare_exchange_weak(
                                       current, current - 1U,
                                       std::memory_order_relaxed)) {
            }
        }
        return S_OK;
    }
};

} // namespace

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    static_cast<void>(reserved);
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID class_id, REFIID interface_id,
                         void** object) {
    if (object == nullptr) {
        return E_POINTER;
    }
    *object = nullptr;
    if (class_id !=
        solin::media_engine::windows_virtual_camera::kSourceClassId) {
        return CLASS_E_CLASSNOTAVAILABLE;
    }
    auto factory = Microsoft::WRL::Make<SolinVirtualCameraClassFactory>();
    return factory == nullptr ? E_OUTOFMEMORY
                              : factory->QueryInterface(interface_id, object);
}

STDAPI DllCanUnloadNow() {
    const auto objects = Microsoft::WRL::Module<
        Microsoft::WRL::InProc>::GetModule().GetObjectCount();
    return objects == 0U && server_locks.load() == 0U ? S_OK : S_FALSE;
}

STDAPI DllUnregisterServer() { return remove_persistent_virtual_camera(); }
