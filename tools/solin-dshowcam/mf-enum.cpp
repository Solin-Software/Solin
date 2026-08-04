// Enumerate video capture devices as MEDIA FOUNDATION sees them.
//
// Solin's virtual camera is a DirectShow filter. Apps differ in which capture
// stack they use: Zoom and OBS enumerate DirectShow, while Chromium-based apps
// (Edge, Chrome, Teams-in-browser) and the Windows Camera app enumerate Media
// Foundation. A DirectShow filter is NOT automatically visible to MF, so this
// prints what an MF-based app can actually see — compare it against the
// DirectShow list that libobs reports.
//
// Build: cl /nologo /EHsc /MT mf-enum.cpp /Fe:mf-enum.exe mfplat.lib mf.lib \
//            mfuuid.lib ole32.lib shlwapi.lib
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mferror.h>
#include <stdio.h>

int main() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(hr)) { printf("CoInitializeEx failed 0x%08lX\n", hr); return 1; }
    hr = MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET);
    if (FAILED(hr)) { printf("MFStartup failed 0x%08lX\n", hr); return 1; }

    IMFAttributes* attrs = nullptr;
    hr = MFCreateAttributes(&attrs, 1);
    if (SUCCEEDED(hr)) {
        hr = attrs->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    }

    IMFActivate** devices = nullptr;
    UINT32 count = 0;
    if (SUCCEEDED(hr)) hr = MFEnumDeviceSources(attrs, &devices, &count);
    if (FAILED(hr)) { printf("MFEnumDeviceSources failed 0x%08lX\n", hr); return 1; }

    printf("Media Foundation video capture devices: %u\n", count);
    for (UINT32 i = 0; i < count; i++) {
        WCHAR* name = nullptr; UINT32 len = 0;
        if (SUCCEEDED(devices[i]->GetAllocatedString(
                MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &name, &len))) {
            printf("  [%u] %ws\n", i, name);
            CoTaskMemFree(name);
        } else {
            printf("  [%u] <no friendly name>\n", i);
        }
        devices[i]->Release();
    }
    if (count == 0) printf("  (none)\n");

    CoTaskMemFree(devices);
    if (attrs) attrs->Release();
    MFShutdown();
    CoUninitialize();
    return 0;
}
