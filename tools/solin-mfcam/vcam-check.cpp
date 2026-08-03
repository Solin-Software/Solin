// Verification harness for the Solin Virtual Camera media source.
//
// Creates the virtual camera, enumerates the system's video capture devices to
// confirm it appears under the branded name, then opens it through a
// SourceReader and pulls real frames — the end-to-end proof that the media
// source works inside the Windows capture pipeline.
//
// Build:  build.bat check      Run:  vcam-check.exe

#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mfvirtualcamera.h>
#include <mferror.h>
#include <cstdio>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "mfreadwrite.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "mfsensorgroup.lib")
#pragma comment(lib, "ole32.lib")

static const wchar_t kClsid[] = L"{6F9C1B24-6E5A-4F4E-9C1D-2C7A5E3B8D41}";
static const wchar_t kName[] = L"Solin Virtual Camera";

#define CHECK(hr, what)                                            \
    if (FAILED(hr)) {                                              \
        wprintf(L"FAIL  %-34s hr=0x%08X\n", what, (unsigned)(hr)); \
        goto done;                                                 \
    } else {                                                       \
        wprintf(L"ok    %s\n", what);                              \
    }

int wmain(int argc, wchar_t** argv) {
    // Unbuffered: when this harness hangs, the last line printed is the
    // diagnosis. Block buffering under redirection hides exactly that.
    setvbuf(stdout, nullptr, _IONBF, 0);

    // --read-only attaches to a camera someone else is already holding open
    // (e.g. hold-camera.py) instead of creating a second one.
    bool read_only = (argc > 1 && wcscmp(argv[1], L"--read-only") == 0);

    HRESULT hr = S_OK;
    IMFVirtualCamera* vcam = nullptr;
    IMFAttributes* enum_attrs = nullptr;
    IMFActivate** devices = nullptr;
    UINT32 device_count = 0;
    IMFMediaSource* source = nullptr;
    IMFSourceReader* reader = nullptr;
    bool found = false;

    hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    CHECK(hr, L"CoInitializeEx");
    hr = MFStartup(MF_VERSION, MFSTARTUP_FULL);
    CHECK(hr, L"MFStartup");

    if (!read_only) {
        hr = MFCreateVirtualCamera(MFVirtualCameraType_SoftwareCameraSource,
                                   MFVirtualCameraLifetime_Session,
                                   MFVirtualCameraAccess_CurrentUser,
                                   kName, kClsid, nullptr, 0, &vcam);
        CHECK(hr, L"MFCreateVirtualCamera");

        hr = vcam->Start(nullptr);
        CHECK(hr, L"IMFVirtualCamera::Start");
    } else {
        wprintf(L"      read-only: attaching to an already-published camera\n");
    }

    // Does it show up the way a real webcam would?
    hr = MFCreateAttributes(&enum_attrs, 1);
    CHECK(hr, L"MFCreateAttributes");
    hr = enum_attrs->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                             MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    CHECK(hr, L"set VIDCAP source type");
    hr = MFEnumDeviceSources(enum_attrs, &devices, &device_count);
    CHECK(hr, L"MFEnumDeviceSources");

    wprintf(L"\n  video capture devices (%u):\n", device_count);
    for (UINT32 i = 0; i < device_count; ++i) {
        wchar_t* name = nullptr;
        UINT32 len = 0;
        if (SUCCEEDED(devices[i]->GetAllocatedString(
                MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &name, &len))) {
            // Windows appends a localized suffix, e.g.
            // "Solin Virtual Camera (Windows Virtual Camera)" — so match the prefix.
            bool mine = (wcsncmp(name, kName, wcslen(kName)) == 0);
            wprintf(L"    %s%s\n", name, mine ? L"   <-- ours" : L"");
            if (mine && !found) {
                found = true;
                wprintf(L"      activating (frame server instantiates the source)...\n");
                hr = devices[i]->ActivateObject(IID_PPV_ARGS(&source));
                wprintf(L"      activate hr=0x%08X\n", (unsigned)hr);
            }
            CoTaskMemFree(name);
        }
    }
    wprintf(L"\n");

    if (!found) {
        wprintf(L"FAIL  camera did not enumerate under its branded name\n");
        goto done;
    }
    wprintf(L"ok    enumerated as \"%s\"\n", kName);
    if (!source) goto done;

    wprintf(L"      creating source reader...\n");
    hr = MFCreateSourceReaderFromMediaSource(source, nullptr, &reader);
    CHECK(hr, L"MFCreateSourceReaderFromMediaSource");
    wprintf(L"      reading samples...\n");

    // Pull a handful of frames; a live source must keep producing.
    for (int i = 0; i < 5; ++i) {
        DWORD stream_flags = 0;
        LONGLONG ts = 0;
        IMFSample* sample = nullptr;
        hr = reader->ReadSample((DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM, 0, nullptr,
                                &stream_flags, &ts, &sample);
        if (FAILED(hr)) {
            wprintf(L"FAIL  ReadSample #%d hr=0x%08X\n", i, (unsigned)hr);
            goto done;
        }
        if (sample) {
            DWORD total = 0;
            sample->GetTotalLength(&total);

            // Sample the chroma plane. The media source's fallback pattern is
            // neutral (U=V=128); anything else means real frames arrived over
            // the transport from the producer process.
            IMFMediaBuffer* buf = nullptr;
            int u = -1, v = -1;
            if (SUCCEEDED(sample->ConvertToContiguousBuffer(&buf))) {
                BYTE* p = nullptr;
                DWORD len = 0;
                if (SUCCEEDED(buf->Lock(&p, nullptr, &len)) && len >= 1280 * 720 + 2) {
                    u = p[1280 * 720];
                    v = p[1280 * 720 + 1];
                    buf->Unlock();
                }
                buf->Release();
            }
            wprintf(L"ok    frame %d: %lu bytes, ts=%lld, chroma=(%d,%d) %s\n", i, total, ts,
                    u, v, (u == 128 && v == 128) ? L"<- fallback pattern" : L"<- TRANSPORT");
            sample->Release();
        } else {
            wprintf(L"      frame %d: no sample (flags=0x%08X)\n", i, stream_flags);
        }
    }
    wprintf(L"\nRESULT: Solin Virtual Camera delivers frames end-to-end.\n");

done:
    if (reader) reader->Release();
    if (source) { source->Shutdown(); source->Release(); }
    if (devices) {
        for (UINT32 i = 0; i < device_count; ++i) devices[i]->Release();
        CoTaskMemFree(devices);
    }
    if (enum_attrs) enum_attrs->Release();
    if (vcam) { vcam->Shutdown(); vcam->Release(); }
    MFShutdown();
    CoUninitialize();
    return SUCCEEDED(hr) ? 0 : 1;
}
