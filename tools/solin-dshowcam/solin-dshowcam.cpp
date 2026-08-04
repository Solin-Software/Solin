// Solin Virtual Camera — DirectShow capture source filter.
//
// Registered under CLSID_VideoInputDeviceCategory so it enumerates as an
// ordinary webcam, with the exact friendly name "Solin Virtual Camera" and no
// decoration. (The Media Foundation route in tools/solin-mfcam works too, but
// Windows appends a localized "(Windows Virtual Camera)" to every MF virtual
// camera's name; DirectShow names are reported verbatim, which is why OBS and
// other virtual cameras appear cleanly.)
//
// CLEAN ROOM: built on Microsoft's MIT-licensed DirectShow BaseClasses
// (vendored in baseclasses/, see LICENSE.MIT) and the Windows SDK. No code is
// derived from OBS's GPLv2 win-dshow plugin.
//
// Frames arrive over Solin's file-backed transport (frame_transport.h). This
// DLL is loaded in-process by the consuming application, so it must never fault
// and never stall the graph: when Solin is not producing, it delivers black at
// full cadence rather than withholding samples.

#include <streams.h>  // BaseClasses; pulls in the DirectShow SDK headers

#include <dllsetup.h>
#include <ks.h>
#include <ksmedia.h>
#include <olectl.h>
#include <stdio.h>  // swprintf_s

#include "solin_guids.h"
#include "format_convert.h"
#include "frame_transport.h"

// ── diagnostics ───────────────────────────────────────────────────────────────
//
// This DLL runs inside the consuming application, where there is no console and
// no debugger attached. A log is the only way to learn which format a host
// negotiated and whether it is actually pulling frames.

static void solin_log(const char* fmt, ...) {
    HANDLE fh = CreateFileW(L"C:\\ProgramData\\Solin\\dshowcam.log", FILE_APPEND_DATA,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                            FILE_ATTRIBUTE_NORMAL, nullptr);
    if (fh == INVALID_HANDLE_VALUE) return;

    char msg[512];
    va_list args;
    va_start(args, fmt);
    vsnprintf(msg, sizeof(msg), fmt, args);
    va_end(args);

    char exe[MAX_PATH] = "?";
    GetModuleFileNameA(nullptr, exe, MAX_PATH);
    const char* leaf = strrchr(exe, '\\');
    SYSTEMTIME st;
    GetLocalTime(&st);

    char line[1024];
    int n = _snprintf_s(line, sizeof(line), _TRUNCATE, "%02d:%02d:%02d.%03d [%s] %s\r\n",
                        st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
                        leaf ? leaf + 1 : exe, msg);
    DWORD written = 0;
    if (n > 0) WriteFile(fh, line, (DWORD)n, &written, nullptr);
    CloseHandle(fh);
}

// ── media types ───────────────────────────────────────────────────────────────
//
// YUY2 first, deliberately. Consumers commonly take the first acceptable type
// from EnumMediaTypes, and YUY2/I420/RGB24 are universally present in
// DirectShow consumers' subtype tables. NV12 is primarily a Media Foundation
// format and is not reliably handled on the DirectShow path, so it is offered
// but never first.

struct FormatEntry {
    const GUID* subtype;
    DWORD compression;
    WORD bit_count;
    DWORD image_bytes;
};

static const FormatEntry kFormats[] = {
    {&MEDIASUBTYPE_YUY2, MAKEFOURCC('Y', 'U', 'Y', '2'), 16, kYuy2Bytes},
    {&MEDIASUBTYPE_I420_SOLIN, MAKEFOURCC('I', '4', '2', '0'), 12, kNv12Bytes},
    {&MEDIASUBTYPE_NV12, MAKEFOURCC('N', 'V', '1', '2'), 12, kNv12Bytes},
    {&MEDIASUBTYPE_RGB24, BI_RGB, 24, kRgb24Bytes},
};
static const int kFormatCount = sizeof(kFormats) / sizeof(kFormats[0]);

static void fill_video_info(VIDEOINFOHEADER* vih, const FormatEntry& fmt) {
    ZeroMemory(vih, sizeof(VIDEOINFOHEADER));
    vih->AvgTimePerFrame = kFrameDuration;
    vih->dwBitRate = fmt.image_bytes * 8 * kFps;
    BITMAPINFOHEADER& bmi = vih->bmiHeader;
    bmi.biSize = sizeof(BITMAPINFOHEADER);
    bmi.biWidth = kWidth;
    bmi.biHeight = kHeight;  // positive for all four; RGB24 is simply bottom-up
    bmi.biPlanes = 1;
    bmi.biBitCount = fmt.bit_count;
    bmi.biCompression = fmt.compression;
    bmi.biSizeImage = fmt.image_bytes;
}

// ── the output pin ────────────────────────────────────────────────────────────

class CSolinStream : public CSourceStream, public IAMStreamConfig, public IKsPropertySet {
public:
    CSolinStream(HRESULT* hr, CSource* parent)
        : CSourceStream(NAME("Solin Capture"), hr, parent, L"Capture"),
          format_index_(0), frame_index_(0), start_time_(0), next_due_(0) {}

    DECLARE_IUNKNOWN

    STDMETHODIMP NonDelegatingQueryInterface(REFIID riid, void** out) override {
        if (riid == IID_IAMStreamConfig) return GetInterface((IAMStreamConfig*)this, out);
        if (riid == IID_IKsPropertySet) return GetInterface((IKsPropertySet*)this, out);
        return CSourceStream::NonDelegatingQueryInterface(riid, out);
    }

    // -- CSourceStream ------------------------------------------------------

    HRESULT GetMediaType(int position, CMediaType* mt) override {
        if (position < 0) return E_INVALIDARG;
        if (position >= kFormatCount) return VFW_S_NO_MORE_ITEMS;

        VIDEOINFOHEADER* vih = (VIDEOINFOHEADER*)mt->AllocFormatBuffer(sizeof(VIDEOINFOHEADER));
        if (!vih) return E_OUTOFMEMORY;
        const FormatEntry& fmt = kFormats[position];
        fill_video_info(vih, fmt);

        mt->SetType(&MEDIATYPE_Video);
        mt->SetSubtype(fmt.subtype);
        mt->SetFormatType(&FORMAT_VideoInfo);
        mt->SetTemporalCompression(FALSE);
        mt->SetSampleSize(fmt.image_bytes);  // must equal biSizeImage
        return S_OK;
    }

    HRESULT CheckMediaType(const CMediaType* mt) override {
        if (!mt || *mt->Type() != MEDIATYPE_Video) return E_INVALIDARG;
        if (*mt->FormatType() != FORMAT_VideoInfo) return E_INVALIDARG;
        if (mt->FormatLength() < sizeof(VIDEOINFOHEADER)) return E_INVALIDARG;
        const VIDEOINFOHEADER* vih = (const VIDEOINFOHEADER*)mt->Format();
        if (!vih || vih->bmiHeader.biWidth != kWidth ||
            abs(vih->bmiHeader.biHeight) != kHeight) {
            return E_INVALIDARG;
        }
        for (int i = 0; i < kFormatCount; ++i) {
            if (*mt->Subtype() == *kFormats[i].subtype) return S_OK;
        }
        return E_INVALIDARG;
    }

    HRESULT SetMediaType(const CMediaType* mt) override {
        HRESULT hr = CSourceStream::SetMediaType(mt);
        if (FAILED(hr)) return hr;
        for (int i = 0; i < kFormatCount; ++i) {
            if (*mt->Subtype() == *kFormats[i].subtype) {
                format_index_ = i;
                break;
            }
        }
        const VIDEOINFOHEADER* vih = (const VIDEOINFOHEADER*)mt->Format();
        DWORD cc = kFormats[format_index_].compression;
        solin_log("negotiated %c%c%c%c biHeight=%ld (index %d)",
                  (char)(cc & 0xff), (char)((cc >> 8) & 0xff), (char)((cc >> 16) & 0xff),
                  (char)((cc >> 24) & 0xff),
                  vih ? vih->bmiHeader.biHeight : 0, format_index_);
        return S_OK;
    }

    HRESULT DecideBufferSize(IMemAllocator* alloc, ALLOCATOR_PROPERTIES* request) override {
        if (!alloc || !request) return E_POINTER;
        CAutoLock lock(m_pFilter->pStateLock());

        request->cBuffers = max(request->cBuffers, 1);
        request->cbBuffer = max((long)kFormats[format_index_].image_bytes, request->cbBuffer);

        ALLOCATOR_PROPERTIES actual;
        HRESULT hr = alloc->SetProperties(request, &actual);
        if (FAILED(hr)) return hr;
        // Honouring a smaller buffer than we need would corrupt every frame.
        return actual.cbBuffer < request->cbBuffer ? E_FAIL : S_OK;
    }

    HRESULT OnThreadCreate() override {
        start_time_ = 0;
        next_due_ = 0;
        frame_index_ = 0;
        return S_OK;
    }

    HRESULT OnThreadDestroy() override {
        reader_.Close();
        return S_OK;
    }

    HRESULT FillBuffer(IMediaSample* sample) override {
        BYTE* out = nullptr;
        HRESULT hr = sample->GetPointer(&out);
        if (FAILED(hr)) return hr;

        const FormatEntry& fmt = kFormats[format_index_];
        const long wanted = (long)fmt.image_bytes;
        if (sample->GetSize() < wanted) return E_FAIL;

        // Pace to the advertised rate. A source that returns instantly is pulled
        // as fast as it answers, which floods the consumer with near-identical
        // timestamps and renders nothing.
        REFERENCE_TIME now = 0;
        CRefTime clock_now;
        if (start_time_ == 0) {
            start_time_ = GetTickCount64() * 10000;
            next_due_ = start_time_;
        }
        now = (REFERENCE_TIME)GetTickCount64() * 10000;
        if (now < next_due_) {
            DWORD wait = (DWORD)((next_due_ - now) / 10000);
            if (wait > 0) Sleep(wait > 100 ? 100 : wait);
            now = (REFERENCE_TIME)GetTickCount64() * 10000;
        }
        next_due_ = (now - next_due_ > 10 * kFrameDuration) ? now + kFrameDuration
                                                            : next_due_ + kFrameDuration;

        // Solin's composite when it is producing; otherwise the branded standby
        // picture, and black only if even that is unavailable. Never withhold a
        // sample: a starved consumer spins and then errors out.
        const char* source = "solin";
        bool live = reader_.Read(nv12_) && !reader_.Stale();
        if (!live) {
            if (reader_.ReadStandby(nv12_)) {
                source = "standby";
            } else {
                fill_black_nv12(nv12_);
                source = "black";
            }
        }
        if (frame_index_ == 0 || frame_index_ % 90 == 0) {
            solin_log("frame %llu fmt=%d source=%s", frame_index_, format_index_, source);
        }

        switch (fmt.compression) {
            case MAKEFOURCC('Y', 'U', 'Y', '2'): nv12_to_yuy2(nv12_, out); break;
            case MAKEFOURCC('I', '4', '2', '0'): nv12_to_i420(nv12_, out); break;
            case MAKEFOURCC('N', 'V', '1', '2'): memcpy(out, nv12_, kNv12Bytes); break;
            default: nv12_to_rgb24(nv12_, out); break;
        }

        sample->SetActualDataLength(wanted);
        REFERENCE_TIME begin = frame_index_ * kFrameDuration;
        REFERENCE_TIME end = begin + kFrameDuration;
        sample->SetTime(&begin, &end);
        sample->SetSyncPoint(TRUE);  // every frame is a keyframe
        frame_index_++;
        return S_OK;
    }

    // -- IAMStreamConfig ----------------------------------------------------
    //
    // Consumers call GetNumberOfCapabilities/GetStreamCaps to discover
    // resolutions; a capture pin without it looks broken to some hosts.

    STDMETHODIMP SetFormat(AM_MEDIA_TYPE* mt) override {
        if (!mt) return E_POINTER;
        CMediaType candidate(*mt);
        if (CheckMediaType(&candidate) != S_OK) return VFW_E_INVALIDMEDIATYPE;
        if (IsConnected()) return VFW_E_ALREADY_CONNECTED;
        return SetMediaType(&candidate);
    }

    STDMETHODIMP GetFormat(AM_MEDIA_TYPE** mt) override {
        if (!mt) return E_POINTER;
        CMediaType current;
        HRESULT hr = GetMediaType(format_index_, &current);
        if (FAILED(hr)) return hr;
        *mt = CreateMediaType(&current);
        return *mt ? S_OK : E_OUTOFMEMORY;
    }

    STDMETHODIMP GetNumberOfCapabilities(int* count, int* size) override {
        if (!count || !size) return E_POINTER;
        *count = kFormatCount;
        *size = sizeof(VIDEO_STREAM_CONFIG_CAPS);
        return S_OK;
    }

    STDMETHODIMP GetStreamCaps(int index, AM_MEDIA_TYPE** mt, BYTE* caps) override {
        if (!mt || !caps) return E_POINTER;
        if (index < 0 || index >= kFormatCount) return S_FALSE;

        CMediaType entry;
        HRESULT hr = GetMediaType(index, &entry);
        if (FAILED(hr)) return hr;
        *mt = CreateMediaType(&entry);
        if (!*mt) return E_OUTOFMEMORY;

        VIDEO_STREAM_CONFIG_CAPS* vscc = (VIDEO_STREAM_CONFIG_CAPS*)caps;
        ZeroMemory(vscc, sizeof(*vscc));
        vscc->guid = FORMAT_VideoInfo;
        vscc->VideoStandard = AnalogVideo_None;
        vscc->InputSize.cx = vscc->MinCroppingSize.cx = vscc->MaxCroppingSize.cx = kWidth;
        vscc->InputSize.cy = vscc->MinCroppingSize.cy = vscc->MaxCroppingSize.cy = kHeight;
        vscc->MinOutputSize = vscc->MaxOutputSize = vscc->InputSize;
        vscc->CropGranularityX = vscc->CropGranularityY = 1;
        vscc->OutputGranularityX = vscc->OutputGranularityY = 1;
        vscc->MinFrameInterval = vscc->MaxFrameInterval = kFrameDuration;
        vscc->MinBitsPerSecond = vscc->MaxBitsPerSecond =
            (LONG)(kFormats[index].image_bytes * 8 * kFps);
        return S_OK;
    }

    // -- IKsPropertySet -----------------------------------------------------
    //
    // Load-bearing. ICaptureGraphBuilder2::RenderStream(&PIN_CATEGORY_CAPTURE,…)
    // — used by Zoom, Teams and Chromium's pin discovery — QIs each pin for this
    // and asks for its category. A pin that cannot answer is never selected, so
    // the device appears in the list but never streams.

    STDMETHODIMP Set(REFGUID, DWORD, void*, DWORD, void*, DWORD) override {
        return E_NOTIMPL;  // read-only property set
    }

    STDMETHODIMP Get(REFGUID set, DWORD id, void* instance, DWORD instance_bytes,
                     void* data, DWORD data_bytes, DWORD* returned) override {
        if (set != AMPROPSETID_Pin) return E_PROP_SET_UNSUPPORTED;
        if (id != AMPROPERTY_PIN_CATEGORY) return E_PROP_ID_UNSUPPORTED;
        if (returned) *returned = sizeof(GUID);
        if (data_bytes == 0) return S_OK;             // size query
        if (!data) return E_POINTER;
        if (data_bytes < sizeof(GUID)) return E_UNEXPECTED;
        *(GUID*)data = PIN_CATEGORY_CAPTURE;
        UNREFERENCED_PARAMETER(instance);
        UNREFERENCED_PARAMETER(instance_bytes);
        return S_OK;
    }

    STDMETHODIMP QuerySupported(REFGUID set, DWORD id, DWORD* type) override {
        if (set != AMPROPSETID_Pin) return E_PROP_SET_UNSUPPORTED;
        if (id != AMPROPERTY_PIN_CATEGORY) return E_PROP_ID_UNSUPPORTED;
        if (type) *type = KSPROPERTY_SUPPORT_GET;
        return S_OK;
    }

private:
    int format_index_;
    UINT64 frame_index_;
    REFERENCE_TIME start_time_;
    REFERENCE_TIME next_due_;
    FrameReader reader_;
    BYTE nv12_[kNv12Bytes];  // staged source frame before conversion
};

// ── the filter ────────────────────────────────────────────────────────────────

class CSolinFilter : public CSource, public IAMFilterMiscFlags {
public:
    static CUnknown* WINAPI CreateInstance(LPUNKNOWN unk, HRESULT* hr) {
        CSolinFilter* filter = new CSolinFilter(unk, hr);
        if (!filter && hr) *hr = E_OUTOFMEMORY;
        return filter;
    }

    DECLARE_IUNKNOWN

    STDMETHODIMP NonDelegatingQueryInterface(REFIID riid, void** out) override {
        if (riid == IID_IAMFilterMiscFlags) return GetInterface((IAMFilterMiscFlags*)this, out);
        // Answer these honestly rather than returning S_OK with a null pointer,
        // which is a classic way to crash a host mid-negotiation.
        if (riid == IID_IReferenceClock || riid == IID_IMediaSeeking ||
            riid == IID_IMediaPosition || riid == IID_IAMVideoProcAmp ||
            riid == IID_IAMCameraControl || riid == IID_ISpecifyPropertyPages) {
            return E_NOINTERFACE;
        }
        return CSource::NonDelegatingQueryInterface(riid, out);
    }

    STDMETHODIMP_(ULONG) GetMiscFlags() override { return AM_FILTER_MISC_FLAGS_IS_SOURCE; }

private:
    CSolinFilter(LPUNKNOWN unk, HRESULT* hr)
        : CSource(NAME("Solin Virtual Camera"), unk, CLSID_SolinVirtualCamera) {
        stream_ = new CSolinStream(hr, this);
        if (!stream_ && hr) *hr = E_OUTOFMEMORY;
    }
    ~CSolinFilter() { delete stream_; }

    CSolinStream* stream_;
};

// ── class factory table (consumed by baseclasses/dllentry.cpp) ────────────────

CFactoryTemplate g_Templates[] = {
    {kFilterName, &CLSID_SolinVirtualCamera, CSolinFilter::CreateInstance, nullptr, nullptr},
};
int g_cTemplates = sizeof(g_Templates) / sizeof(g_Templates[0]);

// ── registration ──────────────────────────────────────────────────────────────
//
// Two halves: the COM server keys (written by hand — IFilterMapper2 never
// touches them) and the category entry that makes the filter enumerate as a
// video input device.

static const wchar_t kClsidText[] = L"{2A5D626F-89B9-49FC-BEB8-27A6DDF286BB}";

// Our own module handle. BaseClasses keeps g_hInst, but it is only assigned from
// DllEntryPoint — and with the standard CRT entry point that runs only because
// the DllMain below forwards to it. Recording the handle here removes the
// dependency entirely: getting this wrong makes GetModuleFileNameW(NULL) return
// the HOST's path, so registration silently points at regsvr32.exe and every
// later CoCreateInstance fails with CO_E_ERRORINDLL.
static HMODULE g_solin_module = nullptr;

// BaseClasses supplies DllEntryPoint (dllentry.cpp) but no DllMain, so the
// linker's default CRT startup would never call it. Forwarding from DllMain
// keeps CRT initialisation intact for the static-CRT build AND gives
// BaseClasses its process-attach notification.
extern "C" BOOL WINAPI DllEntryPoint(HINSTANCE, ULONG, LPVOID);

extern "C" BOOL WINAPI DllMain(HINSTANCE inst, ULONG reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) g_solin_module = (HMODULE)inst;
    return DllEntryPoint(inst, reason, reserved);
}

static HRESULT write_com_keys() {
    wchar_t path[MAX_PATH];
    if (!GetModuleFileNameW(g_solin_module, path, MAX_PATH)) {
        return HRESULT_FROM_WIN32(GetLastError());
    }

    wchar_t key[256];
    swprintf_s(key, L"CLSID\\%s", kClsidText);
    HKEY clsid = nullptr;
    // HKEY_CLASSES_ROOT with no WOW64 flag: the redirector puts a 32-bit
    // process's writes under WOW6432Node by itself. Building those paths by
    // hand is how you end up registering the x64 path in the 32-bit view.
    LSTATUS rc = RegCreateKeyExW(HKEY_CLASSES_ROOT, key, 0, nullptr, 0, KEY_WRITE, nullptr,
                                 &clsid, nullptr);
    if (rc != ERROR_SUCCESS) return HRESULT_FROM_WIN32(rc);

    rc = RegSetValueExW(clsid, nullptr, 0, REG_SZ, (const BYTE*)kFilterName,
                        (DWORD)((wcslen(kFilterName) + 1) * sizeof(wchar_t)));
    if (rc == ERROR_SUCCESS) {
        HKEY inproc = nullptr;
        rc = RegCreateKeyExW(clsid, L"InprocServer32", 0, nullptr, 0, KEY_WRITE, nullptr,
                             &inproc, nullptr);
        if (rc == ERROR_SUCCESS) {
            rc = RegSetValueExW(inproc, nullptr, 0, REG_SZ, (const BYTE*)path,
                                (DWORD)((wcslen(path) + 1) * sizeof(wchar_t)));
            // "Both", never "Apartment": an STA host would otherwise marshal every
            // Receive() through its message pump and collapse the frame rate.
            const wchar_t both[] = L"Both";
            if (rc == ERROR_SUCCESS) {
                rc = RegSetValueExW(inproc, L"ThreadingModel", 0, REG_SZ,
                                    (const BYTE*)both, sizeof(both));
            }
            RegCloseKey(inproc);
        }
    }
    RegCloseKey(clsid);
    return rc == ERROR_SUCCESS ? S_OK : HRESULT_FROM_WIN32(rc);
}

STDAPI DllRegisterServer() {
    HRESULT hr = write_com_keys();
    if (FAILED(hr)) return hr;

    hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool com_owned = SUCCEEDED(hr);

    IFilterMapper2* mapper = nullptr;
    hr = CoCreateInstance(CLSID_FilterMapper2, nullptr, CLSCTX_INPROC_SERVER,
                          IID_IFilterMapper2, (void**)&mapper);
    if (SUCCEEDED(hr)) {
        REGPINTYPES pin_types[kFormatCount];
        for (int i = 0; i < kFormatCount; ++i) {
            pin_types[i].clsMajorType = &MEDIATYPE_Video;
            pin_types[i].clsMinorType = kFormats[i].subtype;
        }

        REGFILTERPINS2 pin = {};
        pin.dwFlags = REG_PINFLAG_B_OUTPUT;
        pin.cInstances = 1;
        pin.nMediaTypes = kFormatCount;
        pin.lpMediaType = pin_types;

        REGFILTER2 rf = {};
        rf.dwVersion = 2;
        rf.dwMerit = MERIT_DO_NOT_USE;  // a virtual camera must never be auto-selected
        rf.cPins2 = 1;
        rf.rgPins2 = &pin;

        hr = mapper->RegisterFilter(CLSID_SolinVirtualCamera, kFilterName, nullptr,
                                    &CLSID_VideoInputDeviceCategory, nullptr, &rf);
        mapper->Release();
    }

    if (com_owned) CoUninitialize();
    return hr;
}

STDAPI DllUnregisterServer() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool com_owned = SUCCEEDED(hr);

    IFilterMapper2* mapper = nullptr;
    if (SUCCEEDED(CoCreateInstance(CLSID_FilterMapper2, nullptr, CLSCTX_INPROC_SERVER,
                                   IID_IFilterMapper2, (void**)&mapper))) {
        mapper->UnregisterFilter(&CLSID_VideoInputDeviceCategory, nullptr,
                                 CLSID_SolinVirtualCamera);
        mapper->Release();
    }
    if (com_owned) CoUninitialize();

    wchar_t key[256];
    swprintf_s(key, L"CLSID\\%s", kClsidText);
    RegDeleteTreeW(HKEY_CLASSES_ROOT, key);
    return S_OK;
}
