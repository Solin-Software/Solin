// camera-diag — list video capture devices as BOTH Windows capture stacks see them.
//
// Why both: apps do not agree on which stack to use. Zoom, OBS and Solin's own
// projector enumerate DirectShow; Chromium-based apps (Edge, Chrome) and the
// Windows Camera app enumerate Media Foundation. A device visible to one is not
// automatically visible to the other — a DirectShow filter (which is what
// Solin's virtual camera is) does not appear in Media Foundation at all.
//
// So a camera missing from Solin, or a virtual camera missing from Edge, is
// usually a "wrong stack" problem rather than a broken device. This prints both
// lists side by side so the difference is visible instead of inferred.
//
// Build: see build.bat (target: diag)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dshow.h>
#include <mfapi.h>
#include <mfidl.h>
#include <stdio.h>

#pragma comment(lib, "strmiids.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "mfuuid.lib")

static void print_dshow() {
    printf("== DirectShow (Zoom, OBS, Solin's projector) ==\n");
    ICreateDevEnum* dev_enum = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_SystemDeviceEnum, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_PPV_ARGS(&dev_enum));
    if (FAILED(hr)) { printf("  CoCreateInstance failed 0x%08lX\n", hr); return; }

    IEnumMoniker* enum_mon = nullptr;
    hr = dev_enum->CreateClassEnumerator(CLSID_VideoInputDeviceCategory, &enum_mon, 0);
    if (hr != S_OK || !enum_mon) {   // S_FALSE == category empty
        printf("  (none)\n");
        dev_enum->Release();
        return;
    }

    int n = 0;
    IMoniker* mon = nullptr;
    while (enum_mon->Next(1, &mon, nullptr) == S_OK) {
        IPropertyBag* bag = nullptr;
        if (SUCCEEDED(mon->BindToStorage(nullptr, nullptr, IID_PPV_ARGS(&bag)))) {
            VARIANT v; VariantInit(&v);
            if (SUCCEEDED(bag->Read(L"FriendlyName", &v, nullptr))) {
                printf("  [%d] %ws\n", n, v.bstrVal);
                VariantClear(&v);
            }
            VariantInit(&v);
            if (SUCCEEDED(bag->Read(L"DevicePath", &v, nullptr))) {
                printf("       path: %ws\n", v.bstrVal);
                VariantClear(&v);
            }
            bag->Release();
        }
        mon->Release();
        n++;
    }
    if (n == 0) printf("  (none)\n");
    enum_mon->Release();
    dev_enum->Release();
}

static void print_mf() {
    printf("\n== Media Foundation (Edge, Chrome, Windows Camera app) ==\n");
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET))) {
        printf("  MFStartup failed\n"); return;
    }
    IMFAttributes* attrs = nullptr;
    HRESULT hr = MFCreateAttributes(&attrs, 1);
    if (SUCCEEDED(hr))
        hr = attrs->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    IMFActivate** devices = nullptr;
    UINT32 count = 0;
    if (SUCCEEDED(hr)) hr = MFEnumDeviceSources(attrs, &devices, &count);
    if (FAILED(hr)) {
        printf("  MFEnumDeviceSources failed 0x%08lX\n", hr);
    } else {
        for (UINT32 i = 0; i < count; i++) {
            WCHAR* name = nullptr; UINT32 len = 0;
            if (SUCCEEDED(devices[i]->GetAllocatedString(
                    MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &name, &len))) {
                printf("  [%u] %ws\n", i, name);
                CoTaskMemFree(name);
            }
            devices[i]->Release();
        }
        if (count == 0) printf("  (none)\n");
        CoTaskMemFree(devices);
    }
    if (attrs) attrs->Release();
    MFShutdown();
}

static void print_filter_registration() {
    printf("\n== Solin virtual camera filter registration ==\n");
    // {2A5D626F-89B9-49FC-BEB8-27A6DDF286BB}
    // CLSIDs live under SOFTWARE\Classes, not at the hive root. Probing
    // "CLSID\..." directly reports "not registered" for a filter that plainly is.
    const wchar_t* clsid =
        L"SOFTWARE\\Classes\\CLSID\\{2A5D626F-89B9-49FC-BEB8-27A6DDF286BB}\\InprocServer32";
    struct { HKEY root; const wchar_t* label; REGSAM view; } probes[] = {
        { HKEY_LOCAL_MACHINE, L"HKLM 64-bit", KEY_WOW64_64KEY },
        { HKEY_LOCAL_MACHINE, L"HKLM 32-bit", KEY_WOW64_32KEY },
        { HKEY_CURRENT_USER,  L"HKCU 64-bit", KEY_WOW64_64KEY },
        { HKEY_CURRENT_USER,  L"HKCU 32-bit", KEY_WOW64_32KEY },
    };
    for (auto& p : probes) {
        HKEY key = nullptr;
        LONG rc = RegOpenKeyExW(p.root, clsid, 0, KEY_READ | p.view, &key);
        if (rc == ERROR_SUCCESS) {
            wchar_t path[MAX_PATH] = {0};
            DWORD cb = sizeof(path);
            RegQueryValueExW(key, nullptr, nullptr, nullptr, (LPBYTE)path, &cb);
            printf("  %ws: registered -> %ws\n", p.label, path);
            RegCloseKey(key);
        } else {
            printf("  %ws: not registered\n", p.label);
        }
    }
}

int main() {
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    printf("Solin camera diagnostic\n");
    printf("=======================\n\n");
    print_dshow();
    print_mf();
    print_filter_registration();
    printf("\nHow to read this:\n");
    printf("  * A camera missing from the DirectShow list will NOT appear in Solin,\n");
    printf("    because Solin opens cameras through DirectShow (libobs).\n");
    printf("  * Solin's virtual camera is a DirectShow filter, so it is EXPECTED to be\n");
    printf("    absent from the Media Foundation list. That by itself does NOT stop Edge\n");
    printf("    or Chrome using it - they fall back to DirectShow.\n");
    printf("  * To see what a given app actually negotiated, read the filter's own log:\n");
    printf("      C:\\ProgramData\\Solin\\dshowcam.log\n");
    printf("    Each line names the host process, the negotiated format, and whether the\n");
    printf("    frame came from Solin ('source=solin') or was the placeholder\n");
    printf("    ('source=black').\n");
    CoUninitialize();
    return 0;
}
