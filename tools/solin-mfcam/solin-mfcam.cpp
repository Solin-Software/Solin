// Solin Virtual Camera — Windows 11 Media Foundation camera source.
//
// Windows exposes a virtual camera by pairing two things:
//   1. a COM media source, registered under a CLSID, that PRODUCES frames; and
//   2. an MFCreateVirtualCamera() registration that gives that source a
//      user-visible name (settable at runtime — this is why Media Foundation
//      beats a DirectShow filter, whose name is compiled into the DLL).
//
// The consequence that shapes this file: the media source is instantiated by
// the Windows Camera Frame Server, a SEPARATE system process — not by Solin.
// Frames therefore have to cross a process boundary. Before committing to a
// transport we need to know which account and session host us, so this build
// emits a diagnostic line (see solin_log) on load and on every lifecycle step,
// and renders a self-contained test pattern rather than reading Solin's output.
//
// Once the hosting context is known, the pattern generator is replaced by a
// reader over a Solin-owned shared mapping. Everything else here stays.

#include <windows.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <ks.h>
#include <ksmedia.h>
#include <ksproxy.h>
#include <mfvirtualcamera.h>
#include <shlobj.h>
#include <new>
#include <cstdio>
#include <cwchar>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "shell32.lib")

// {6F9C1B24-6E5A-4F4E-9C1D-2C7A5E3B8D41} — minted for Solin; never reuse OBS's.
static const GUID CLSID_SolinCameraSource = {
    0x6f9c1b24, 0x6e5a, 0x4f4e, {0x9c, 0x1d, 0x2c, 0x7a, 0x5e, 0x3b, 0x8d, 0x41}};

static const wchar_t kClsidString[] = L"{6F9C1B24-6E5A-4F4E-9C1D-2C7A5E3B8D41}";
static const wchar_t kFriendlyName[] = L"Solin Virtual Camera";

// Frame geometry. Fixed for now: a media type must be advertised before any
// producer is necessarily running, so the writer is required to match this.
static const UINT32 kWidth = 1280;
static const UINT32 kHeight = 720;
static const UINT32 kFps = 30;
static const LONGLONG kFrameDuration = 10000000LL / kFps;  // 100ns units
static const DWORD kFrameBytes = kWidth * kHeight * 3 / 2; // NV12

static HMODULE g_module = nullptr;
static LONG g_locks = 0;

// ── diagnostics ───────────────────────────────────────────────────────────────
//
// Written to ProgramData (world-writable, and readable whichever account ends up
// hosting us) precisely because we do not yet know who that is — a per-user path
// would be resolved against the HOST's profile, not Solin's, and could silently
// land somewhere we never look.

static void solin_log(const char* fmt, ...) {
    wchar_t dir[MAX_PATH];
    if (FAILED(SHGetFolderPathW(nullptr, CSIDL_COMMON_APPDATA, nullptr, 0, dir))) return;
    wcscat_s(dir, L"\\Solin");
    CreateDirectoryW(dir, nullptr);
    wchar_t path[MAX_PATH];
    swprintf_s(path, L"%s\\mfcam.log", dir);

    HANDLE fh = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (fh == INVALID_HANDLE_VALUE) return;

    char msg[1024];
    va_list args;
    va_start(args, fmt);
    vsnprintf_s(msg, sizeof(msg), _TRUNCATE, fmt, args);
    va_end(args);

    // Who and where are we? This is the question the whole probe exists to answer.
    char exe[MAX_PATH] = "?";
    GetModuleFileNameA(nullptr, exe, MAX_PATH);
    DWORD session = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &session);

    char user[256] = "?";
    DWORD user_len = sizeof(user);
    GetUserNameA(user, &user_len);

    SYSTEMTIME st;
    GetLocalTime(&st);

    char line[2048];
    int n = snprintf(line, sizeof(line),
                     "%02d:%02d:%02d.%03d pid=%lu sess=%lu user=%s exe=%s | %s\r\n",
                     st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
                     GetCurrentProcessId(), session, user, exe, msg);
    DWORD written = 0;
    if (n > 0) WriteFile(fh, line, (DWORD)n, &written, nullptr);
    CloseHandle(fh);
}

// ── frame transport ───────────────────────────────────────────────────────────
//
// Solin runs as the logged-on user in an interactive session; this DLL is hosted
// by the frame server in session 0 under LocalService. That boundary rules out
// both `Local\` named memory (wrong session) and `Global\` (creating one needs
// SeCreateGlobalPrivilege, which unelevated Solin lacks). A file-backed mapping
// at a fixed machine-wide path crosses both boundaries.
//
// Layout: a 64-byte header followed by kSlotCount frame slots. The writer fills
// a slot that is not the published one, then publishes it by storing active_slot
// and bumping frame_index. With three slots a reader mid-copy is never the slot
// being written, so no lock is needed on the hot path.

static const wchar_t kFramePath[] = L"C:\\ProgramData\\Solin\\vcam-frame.bin";
static const UINT32 kFrameMagic = 0x31435653;  // 'SVC1'
static const UINT32 kFrameVersion = 1;
static const UINT32 kSlotCount = 3;
static const DWORD kHeaderBytes = 64;

#pragma pack(push, 1)
struct FrameHeader {
    UINT32 magic;
    UINT32 version;
    UINT32 width;
    UINT32 height;
    UINT32 format;  // 0 = NV12
    UINT32 frame_bytes;
    UINT32 slot_count;
    volatile UINT32 active_slot;
    volatile UINT64 frame_index;
    volatile UINT64 timestamp;
    UINT8 reserved[16];
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == kHeaderBytes, "frame header must be 64 bytes");

// Reading a mapped view can raise EXCEPTION_IN_PAGE_ERROR if the producer
// truncates the file underneath us. This DLL is hosted by a system service, so
// an unhandled fault takes the camera down for every application on the box —
// contain it. Separate function: SEH cannot coexist with C++ unwinding.
static bool safe_copy(BYTE* dst, const BYTE* src, size_t bytes) {
    __try {
        memcpy(dst, src, bytes);
        return true;
    } __except (GetExceptionCode() == EXCEPTION_IN_PAGE_ERROR
                    ? EXCEPTION_EXECUTE_HANDLER
                    : EXCEPTION_CONTINUE_SEARCH) {
        return false;
    }
}

class FrameReader {
public:
    FrameReader() : file_(INVALID_HANDLE_VALUE), mapping_(nullptr), view_(nullptr),
                    size_(0), last_index_(0) {}
    ~FrameReader() { Close(); }

    // Re-opened lazily: Solin may start producing after the camera is already
    // streaming, and may stop and restart while a consumer stays connected.
    bool Ensure() {
        if (view_) return true;

        file_ = CreateFileW(kFramePath, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;

        // Size the mapping from the file and remember it. The producer creates
        // the file and only then extends it, so a reader that attaches during
        // that window would otherwise index past the end of the view.
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(file_, &size) ||
            (ULONGLONG)size.QuadPart < (ULONGLONG)kHeaderBytes + kFrameBytes) {
            Close();
            return false;
        }
        size_ = (size_t)size.QuadPart;

        mapping_ = CreateFileMappingW(file_, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (!mapping_) { Close(); return false; }

        view_ = (const BYTE*)MapViewOfFile(mapping_, FILE_MAP_READ, 0, 0, 0);
        if (!view_) { Close(); return false; }

        const FrameHeader* h = (const FrameHeader*)view_;
        if (h->magic != kFrameMagic || h->version != kFrameVersion ||
            h->width != kWidth || h->height != kHeight || h->frame_bytes != kFrameBytes ||
            h->slot_count == 0 || h->slot_count > 8) {
            solin_log("frame file rejected (magic=%08X ver=%u %ux%u bytes=%u)", h->magic,
                      h->version, h->width, h->height, h->frame_bytes);
            Close();
            return false;
        }
        solin_log("frame transport attached: %ux%u", h->width, h->height);
        return true;
    }

    // Copies the newest published frame into dst. False when no producer is
    // attached or nothing new has been published yet.
    bool Read(BYTE* dst) {
        if (!Ensure()) return false;
        const FrameHeader* h = (const FrameHeader*)view_;
        UINT64 index = h->frame_index;
        if (index == 0) return false;

        UINT32 slot = h->active_slot;
        if (slot >= h->slot_count) return false;

        // Never trust the header's slot index against the real mapping length —
        // it lives in memory another process writes concurrently.
        size_t offset = kHeaderBytes + (size_t)slot * kFrameBytes;
        if (offset + kFrameBytes > size_) return false;

        if (!safe_copy(dst, view_ + offset, kFrameBytes)) {
            solin_log("frame read faulted; detaching transport");
            Close();
            return false;
        }
        last_index_ = index;
        return true;
    }

    void Close() {
        if (view_) { UnmapViewOfFile(view_); view_ = nullptr; }
        if (mapping_) { CloseHandle(mapping_); mapping_ = nullptr; }
        if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
        size_ = 0;
    }

private:
    HANDLE file_;
    HANDLE mapping_;
    const BYTE* view_;
    size_t size_;
    UINT64 last_index_;
};

// ── test pattern ──────────────────────────────────────────────────────────────

static void fill_test_pattern(BYTE* dst, UINT64 frame_index) {
    // NV12: full-res Y plane, then interleaved half-res UV.
    BYTE* y = dst;
    BYTE* uv = dst + (kWidth * kHeight);

    const UINT32 bar = kWidth / 8;
    const BYTE luma[8] = {235, 210, 170, 145, 106, 81, 41, 16};
    const int scroll = (int)((frame_index * 4) % kWidth);

    for (UINT32 row = 0; row < kHeight; ++row) {
        BYTE* line = y + (size_t)row * kWidth;
        for (UINT32 col = 0; col < kWidth; ++col) {
            UINT32 shifted = (col + scroll) % kWidth;
            line[col] = luma[shifted / bar];
        }
        // A moving bright band makes it obvious the stream is live, not frozen.
        if (row == (frame_index * 6) % kHeight) memset(line, 255, kWidth);
    }
    // Neutral chroma → greyscale bars. 128 is the NV12 zero point.
    memset(uv, 128, (size_t)kWidth * kHeight / 2);
}

// ── media type ────────────────────────────────────────────────────────────────

static HRESULT create_media_type(IMFMediaType** out) {
    IMFMediaType* type = nullptr;
    HRESULT hr = MFCreateMediaType(&type);
    if (FAILED(hr)) return hr;

    hr = type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (SUCCEEDED(hr)) hr = type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    if (SUCCEEDED(hr))
        hr = type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
    if (SUCCEEDED(hr)) hr = MFSetAttributeSize(type, MF_MT_FRAME_SIZE, kWidth, kHeight);
    if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(type, MF_MT_FRAME_RATE, kFps, 1);
    if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(type, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_DEFAULT_STRIDE, kWidth);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_SAMPLE_SIZE, kFrameBytes);

    if (FAILED(hr)) {
        type->Release();
        return hr;
    }
    *out = type;
    return S_OK;
}

// ── stream ────────────────────────────────────────────────────────────────────

class SolinSource;

class SolinStream : public IMFMediaStream {
public:
    SolinStream(SolinSource* parent, IMFStreamDescriptor* sd)
        : refs_(1), parent_(parent), descriptor_(sd), queue_(nullptr),
          frame_index_(0), timestamp_(0), start_time_(0), next_due_(0),
          shutdown_(false) {
        descriptor_->AddRef();
        MFCreateEventQueue(&queue_);
        InitializeCriticalSection(&lock_);
    }

    STDMETHODIMP QueryInterface(REFIID riid, void** out) override;
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&refs_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&refs_);
        if (n == 0) delete this;
        return n;
    }

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** ev) override {
        return queue_ ? queue_->GetEvent(flags, ev) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
        return queue_ ? queue_->BeginGetEvent(cb, state) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** ev) override {
        return queue_ ? queue_->EndGetEvent(r, ev) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP QueueEvent(MediaEventType t, REFGUID ext, HRESULT s,
                            const PROPVARIANT* v) override {
        return queue_ ? queue_->QueueEventParamVar(t, ext, s, v) : MF_E_SHUTDOWN;
    }

    // IMFMediaStream
    STDMETHODIMP GetMediaSource(IMFMediaSource** src) override;
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** sd) override {
        if (!sd) return E_POINTER;
        *sd = descriptor_;
        descriptor_->AddRef();
        return S_OK;
    }
    STDMETHODIMP RequestSample(IUnknown* token) override;

    void QueueStarted(const PROPVARIANT* start) {
        EnterCriticalSection(&lock_);
        start_time_ = MFGetSystemTime();
        next_due_ = start_time_;
        frame_index_ = 0;
        timestamp_ = 0;
        LeaveCriticalSection(&lock_);
        queue_->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, start);
    }
    void QueueStopped() { queue_->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr); }

    void Shutdown() {
        EnterCriticalSection(&lock_);
        shutdown_ = true;
        LeaveCriticalSection(&lock_);
        if (queue_) {
            queue_->Shutdown();
            queue_->Release();
            queue_ = nullptr;
        }
    }

private:
    ~SolinStream() {
        if (queue_) queue_->Release();
        if (descriptor_) descriptor_->Release();
        DeleteCriticalSection(&lock_);
    }

    HRESULT CreateSample(IMFSample** out);

    LONG refs_;
    SolinSource* parent_;  // weak: the source owns us
    IMFStreamDescriptor* descriptor_;
    IMFMediaEventQueue* queue_;
    UINT64 frame_index_;
    LONGLONG timestamp_;
    LONGLONG start_time_;  // MFGetSystemTime at stream start (100ns)
    LONGLONG next_due_;    // when the next frame may be emitted
    bool shutdown_;
    bool last_from_transport_ = false;
    FrameReader reader_;
    BYTE scratch_[kFrameBytes];  // contiguous NV12 staged before the 2D copy
    CRITICAL_SECTION lock_;
};

// ── source ────────────────────────────────────────────────────────────────────

class SolinSource : public IMFMediaSourceEx, public IKsControl {
public:
    SolinSource()
        : refs_(1), queue_(nullptr), presentation_(nullptr), stream_(nullptr),
          attributes_(nullptr), stream_descriptor_(nullptr), started_(false),
          shutdown_(false) {
        InitializeCriticalSection(&lock_);
        MFCreateEventQueue(&queue_);
        MFCreateAttributes(&attributes_, 8);
        // The pipeline reads these off GetSourceAttributes as well as off the
        // activation object; leaving the source's own store empty makes it
        // abandon the source between CreatePresentationDescriptor and Start.
        attributes_->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                             MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
        attributes_->SetString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, kFriendlyName);
        attributes_->SetUINT32(MF_VIRTUALCAMERA_PROVIDE_ASSOCIATED_CAMERA_SOURCES, 0);
        solin_log("SolinSource constructed");
    }

    HRESULT Init() {
        IMFMediaType* type = nullptr;
        HRESULT hr = create_media_type(&type);
        if (FAILED(hr)) return hr;

        IMFStreamDescriptor* sd = nullptr;
        hr = MFCreateStreamDescriptor(0, 1, &type, &sd);
        if (SUCCEEDED(hr)) {
            IMFMediaTypeHandler* handler = nullptr;
            hr = sd->GetMediaTypeHandler(&handler);
            if (SUCCEEDED(hr)) {
                hr = handler->SetCurrentMediaType(type);
                handler->Release();
            }
        }
        if (SUCCEEDED(hr)) {
            // Frame-server plumbing: without these the capture pipeline cannot
            // classify the stream and Start() fails with MF_E_ATTRIBUTENOTFOUND.
            sd->SetUINT32(MF_DEVICESTREAM_STREAM_ID, 0);
            sd->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, PINNAME_VIDEO_CAPTURE);
            sd->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1);
            sd->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES,
                          MFFrameSourceTypes_Color);
            stream_descriptor_ = sd;
            stream_descriptor_->AddRef();
        }
        if (SUCCEEDED(hr)) {
            hr = MFCreatePresentationDescriptor(1, &sd, &presentation_);
            if (SUCCEEDED(hr)) presentation_->SelectStream(0);
        }
        if (SUCCEEDED(hr)) {
            stream_ = new (std::nothrow) SolinStream(this, sd);
            if (!stream_) hr = E_OUTOFMEMORY;
        }
        if (sd) sd->Release();
        type->Release();
        solin_log("SolinSource::Init hr=0x%08X", hr);
        return hr;
    }

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** out) override {
        if (!out) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IMFMediaSource ||
            riid == IID_IMFMediaEventGenerator) {
            *out = static_cast<IMFMediaSource*>(this);
        } else if (riid == IID_IMFMediaSourceEx) {
            *out = static_cast<IMFMediaSourceEx*>(this);
        } else if (riid == __uuidof(IKsControl)) {
            *out = static_cast<IKsControl*>(this);
        } else {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&refs_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&refs_);
        if (n == 0) delete this;
        return n;
    }

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** ev) override {
        return queue_ ? queue_->GetEvent(flags, ev) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
        return queue_ ? queue_->BeginGetEvent(cb, state) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** ev) override {
        return queue_ ? queue_->EndGetEvent(r, ev) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP QueueEvent(MediaEventType t, REFGUID ext, HRESULT s,
                            const PROPVARIANT* v) override {
        return queue_ ? queue_->QueueEventParamVar(t, ext, s, v) : MF_E_SHUTDOWN;
    }

    // IMFMediaSource
    STDMETHODIMP GetCharacteristics(DWORD* c) override {
        if (!c) return E_POINTER;
        *c = MFMEDIASOURCE_IS_LIVE;
        return S_OK;
    }
    STDMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** pd) override {
        if (!pd) return E_POINTER;
        EnterCriticalSection(&lock_);
        HRESULT hr = shutdown_ ? MF_E_SHUTDOWN
                               : presentation_->Clone(pd);
        LeaveCriticalSection(&lock_);
        solin_log("CreatePresentationDescriptor hr=0x%08X", hr);
        return hr;
    }
    STDMETHODIMP Start(IMFPresentationDescriptor* pd, const GUID* fmt,
                       const PROPVARIANT* start) override;
    STDMETHODIMP Stop() override;
    STDMETHODIMP Pause() override { return MF_E_INVALID_STATE_TRANSITION; }
    STDMETHODIMP Shutdown() override;

    // IMFMediaSourceEx — the frame server queries these; a live software source
    // needs no D3D manager, so accept and ignore.
    STDMETHODIMP GetSourceAttributes(IMFAttributes** attrs) override {
        if (!attrs) return E_POINTER;
        *attrs = attributes_;
        attributes_->AddRef();
        return S_OK;
    }
    STDMETHODIMP GetStreamAttributes(DWORD, IMFAttributes** attrs) override {
        if (!attrs) return E_POINTER;
        // The stream descriptor carries the MF_DEVICESTREAM_* attributes the
        // capture pipeline looks for; the source's own store does not.
        if (stream_descriptor_) return stream_descriptor_->QueryInterface(IID_PPV_ARGS(attrs));
        *attrs = attributes_;
        attributes_->AddRef();
        return S_OK;
    }
    STDMETHODIMP SetD3DManager(IUnknown*) override { return S_OK; }

    // IKsControl — the capture pipeline probes for camera controls. A software
    // source has none; failing cleanly is correct and expected.
    STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG* r) override {
        if (r) *r = 0;
        return HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);
    }
    STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG* r) override {
        if (r) *r = 0;
        return HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);
    }
    STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG* r) override {
        if (r) *r = 0;
        return HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);
    }

private:
    ~SolinSource() {
        if (stream_) stream_->Release();
        if (presentation_) presentation_->Release();
        if (stream_descriptor_) stream_descriptor_->Release();
        if (attributes_) attributes_->Release();
        if (queue_) queue_->Release();
        DeleteCriticalSection(&lock_);
    }

    LONG refs_;
    IMFMediaEventQueue* queue_;
    IMFPresentationDescriptor* presentation_;
    SolinStream* stream_;
    IMFAttributes* attributes_;
    IMFStreamDescriptor* stream_descriptor_;
    bool started_;
    bool shutdown_;
    CRITICAL_SECTION lock_;

    friend class SolinStream;
};

// ── stream methods needing the full source definition ─────────────────────────

STDMETHODIMP SolinStream::QueryInterface(REFIID riid, void** out) {
    if (!out) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IMFMediaStream ||
        riid == IID_IMFMediaEventGenerator) {
        *out = static_cast<IMFMediaStream*>(this);
        AddRef();
        return S_OK;
    }
    *out = nullptr;
    return E_NOINTERFACE;
}

STDMETHODIMP SolinStream::GetMediaSource(IMFMediaSource** src) {
    if (!src) return E_POINTER;
    return parent_->QueryInterface(IID_IMFMediaSource, (void**)src);
}

HRESULT SolinStream::CreateSample(IMFSample** out) {
    IMFSample* sample = nullptr;
    HRESULT hr = MFCreateSample(&sample);
    if (FAILED(hr)) return hr;

    // Solin's composite when it is producing; the test pattern is the stand-in
    // so the camera stays valid before Solin starts and after it stops, rather
    // than stalling the consumer.
    last_from_transport_ = reader_.Read(scratch_);
    if (!last_from_transport_) fill_test_pattern(scratch_, frame_index_);

    // A 2D buffer, not a linear one: the capture pipeline expects NV12 laid out
    // with a real stride, and ContiguousCopyFrom does that conversion for us.
    IMFMediaBuffer* buffer = nullptr;
    // NV12's FourCC is the first field of its subtype GUID. Taking it from there
    // beats a multi-character literal, whose value is implementation-defined.
    hr = MFCreate2DMediaBuffer(kWidth, kHeight, MFVideoFormat_NV12.Data1, FALSE, &buffer);
    if (SUCCEEDED(hr)) {
        IMF2DBuffer2* two_d = nullptr;
        hr = buffer->QueryInterface(IID_PPV_ARGS(&two_d));
        if (SUCCEEDED(hr)) {
            hr = two_d->ContiguousCopyFrom(scratch_, kFrameBytes);
            two_d->Release();
        }
        if (SUCCEEDED(hr)) buffer->SetCurrentLength(kFrameBytes);
    }
    if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
    // Timestamp against the real clock, not a synthetic counter: this source
    // advertises MFMEDIASOURCE_IS_LIVE, and a live stream whose timestamps drift
    // away from wall time gets the device invalidated by the capture pipeline.
    if (SUCCEEDED(hr)) hr = sample->SetSampleTime(MFGetSystemTime() - start_time_);
    if (SUCCEEDED(hr)) hr = sample->SetSampleDuration(kFrameDuration);

    if (buffer) buffer->Release();
    if (FAILED(hr)) {
        sample->Release();
        return hr;
    }
    frame_index_++;
    *out = sample;
    return S_OK;
}

STDMETHODIMP SolinStream::RequestSample(IUnknown* token) {
    EnterCriticalSection(&lock_);
    if (shutdown_) {
        LeaveCriticalSection(&lock_);
        return MF_E_SHUTDOWN;
    }
    // Hold the advertised frame rate. The pipeline pulls as fast as this call
    // returns, so answering instantly produces a ~600fps firehose of nearly
    // identical timestamps and the consumer never renders anything. Wait outside
    // the lock so Stop/Shutdown can still get in.
    LONGLONG now = MFGetSystemTime();
    if (next_due_ == 0) next_due_ = now;
    while (now < next_due_ && !shutdown_) {
        DWORD wait_ms = (DWORD)((next_due_ - now) / 10000);
        if (wait_ms == 0) break;
        LeaveCriticalSection(&lock_);
        Sleep(wait_ms > 100 ? 100 : wait_ms);
        EnterCriticalSection(&lock_);
        now = MFGetSystemTime();
    }
    if (shutdown_) {
        LeaveCriticalSection(&lock_);
        return MF_E_SHUTDOWN;
    }
    // Re-base if we have fallen far behind (consumer stalled, machine slept)
    // rather than trying to emit a burst to catch up.
    next_due_ = (now - next_due_ > 10 * kFrameDuration) ? now + kFrameDuration
                                                        : next_due_ + kFrameDuration;

    IMFSample* sample = nullptr;
    HRESULT hr = CreateSample(&sample);
    if (SUCCEEDED(hr)) {
        if (token) sample->SetUnknown(MFSampleExtension_Token, token);
        hr = queue_->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample);
        sample->Release();
    }
    // Log the first sample, then periodically: a stream that stops being pulled
    // is the difference between "the source is broken" and "the consumer left".
    if (frame_index_ == 1 || frame_index_ % 60 == 0) {
        solin_log("sample %llu delivered hr=0x%08X (transport=%s)", frame_index_, hr,
                  last_from_transport_ ? "solin" : "fallback");
    }
    LeaveCriticalSection(&lock_);
    return hr;
}

// ── source lifecycle ──────────────────────────────────────────────────────────

STDMETHODIMP SolinSource::Start(IMFPresentationDescriptor* pd, const GUID*,
                                const PROPVARIANT* start) {
    EnterCriticalSection(&lock_);
    HRESULT hr = S_OK;
    if (shutdown_) {
        hr = MF_E_SHUTDOWN;
    } else {
        // Tell the pipeline the stream exists (first start) or resumed.
        IUnknown* unk = nullptr;
        stream_->QueryInterface(IID_IUnknown, (void**)&unk);
        queue_->QueueEventParamUnk(started_ ? MEUpdatedStream : MENewStream,
                                   GUID_NULL, S_OK, unk);
        if (unk) unk->Release();

        stream_->QueueStarted(start);
        queue_->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, start);
        started_ = true;
    }
    LeaveCriticalSection(&lock_);
    solin_log("SolinSource::Start hr=0x%08X", hr);
    return hr;
}

STDMETHODIMP SolinSource::Stop() {
    EnterCriticalSection(&lock_);
    HRESULT hr = S_OK;
    if (shutdown_) {
        hr = MF_E_SHUTDOWN;
    } else {
        stream_->QueueStopped();
        queue_->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, nullptr);
    }
    LeaveCriticalSection(&lock_);
    solin_log("SolinSource::Stop hr=0x%08X", hr);
    return hr;
}

STDMETHODIMP SolinSource::Shutdown() {
    EnterCriticalSection(&lock_);
    shutdown_ = true;
    if (stream_) stream_->Shutdown();
    if (queue_) {
        queue_->Shutdown();
        queue_->Release();
        queue_ = nullptr;
    }
    LeaveCriticalSection(&lock_);
    solin_log("SolinSource::Shutdown");
    return S_OK;
}

// ── activation object ─────────────────────────────────────────────────────────
//
// The capture pipeline does not consume a registered CLSID as an IMFMediaSource
// directly: it CoCreateInstances the CLSID, queries IMFActivate, and calls
// ActivateObject() to obtain the source. Handing back the source itself makes
// MFCreateVirtualCamera's Start fail with E_NOINTERFACE.

// Log which attribute the pipeline asked for when we don't have it. Guessing the
// required set from documentation is slower and less reliable than letting the
// capture stack tell us exactly what it wants.
static HRESULT trace_miss(const char* accessor, REFGUID key, HRESULT hr) {
    if (hr == MF_E_ATTRIBUTENOTFOUND) {
        solin_log("MISS %s {%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}", accessor,
                  key.Data1, key.Data2, key.Data3, key.Data4[0], key.Data4[1], key.Data4[2],
                  key.Data4[3], key.Data4[4], key.Data4[5], key.Data4[6], key.Data4[7]);
    }
    return hr;
}

class SolinActivate : public IMFActivate {
public:
    SolinActivate() : refs_(1), attributes_(nullptr), source_(nullptr) {
        MFCreateAttributes(&attributes_, 8);
        // The capture pipeline identifies a device source by these.
        attributes_->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                             MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
        attributes_->SetString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, kFriendlyName);
        // Deliberately NOT setting MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK:
        // MFCreateVirtualCamera mints the real device symbolic link itself, and
        // supplying a made-up value makes Start fail with ERROR_PATH_NOT_FOUND.
        // Solin composites its own picture; it wraps no physical camera.
        attributes_->SetUINT32(MF_VIRTUALCAMERA_PROVIDE_ASSOCIATED_CAMERA_SOURCES, 0);
    }

    STDMETHODIMP QueryInterface(REFIID riid, void** out) override {
        if (!out) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IMFActivate) {
            *out = static_cast<IMFActivate*>(this);
        } else if (riid == IID_IMFAttributes) {
            *out = static_cast<IMFAttributes*>(this);
        } else {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&refs_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&refs_);
        if (n == 0) delete this;
        return n;
    }

    // IMFActivate
    STDMETHODIMP ActivateObject(REFIID riid, void** ppv) override {
        solin_log("ActivateObject requested");
        if (!ppv) return E_POINTER;
        if (!source_) {
            SolinSource* src = new (std::nothrow) SolinSource();
            if (!src) return E_OUTOFMEMORY;
            HRESULT hr = src->Init();
            if (FAILED(hr)) {
                src->Release();
                return hr;
            }
            source_ = src;
        }
        HRESULT hr = source_->QueryInterface(riid, ppv);
        solin_log("ActivateObject hr=0x%08X", hr);
        return hr;
    }
    STDMETHODIMP ShutdownObject() override {
        if (source_) {
            source_->Shutdown();
            source_->Release();
            source_ = nullptr;
        }
        return S_OK;
    }
    STDMETHODIMP DetachObject() override {
        if (source_) {
            source_->Release();
            source_ = nullptr;
        }
        return S_OK;
    }

    // IMFAttributes — plain delegation to the backing store, with misses traced.
    STDMETHODIMP GetItem(REFGUID k, PROPVARIANT* v) override {
        return trace_miss("GetItem", k, attributes_->GetItem(k, v));
    }
    STDMETHODIMP GetItemType(REFGUID k, MF_ATTRIBUTE_TYPE* t) override {
        return trace_miss("GetItemType", k, attributes_->GetItemType(k, t));
    }
    STDMETHODIMP CompareItem(REFGUID k, REFPROPVARIANT v, BOOL* r) override {
        return attributes_->CompareItem(k, v, r);
    }
    STDMETHODIMP Compare(IMFAttributes* a, MF_ATTRIBUTES_MATCH_TYPE t, BOOL* r) override {
        return attributes_->Compare(a, t, r);
    }
    STDMETHODIMP GetUINT32(REFGUID k, UINT32* v) override {
        return trace_miss("GetUINT32", k, attributes_->GetUINT32(k, v));
    }
    STDMETHODIMP GetUINT64(REFGUID k, UINT64* v) override {
        return trace_miss("GetUINT64", k, attributes_->GetUINT64(k, v));
    }
    STDMETHODIMP GetDouble(REFGUID k, double* v) override { return attributes_->GetDouble(k, v); }
    STDMETHODIMP GetGUID(REFGUID k, GUID* v) override {
        return trace_miss("GetGUID", k, attributes_->GetGUID(k, v));
    }
    STDMETHODIMP GetStringLength(REFGUID k, UINT32* n) override {
        return trace_miss("GetStringLength", k, attributes_->GetStringLength(k, n));
    }
    STDMETHODIMP GetString(REFGUID k, LPWSTR b, UINT32 n, UINT32* len) override {
        return trace_miss("GetString", k, attributes_->GetString(k, b, n, len));
    }
    STDMETHODIMP GetAllocatedString(REFGUID k, LPWSTR* b, UINT32* n) override {
        return trace_miss("GetAllocatedString", k, attributes_->GetAllocatedString(k, b, n));
    }
    STDMETHODIMP GetBlobSize(REFGUID k, UINT32* n) override {
        return attributes_->GetBlobSize(k, n);
    }
    STDMETHODIMP GetBlob(REFGUID k, UINT8* b, UINT32 n, UINT32* w) override {
        return attributes_->GetBlob(k, b, n, w);
    }
    STDMETHODIMP GetAllocatedBlob(REFGUID k, UINT8** b, UINT32* n) override {
        return attributes_->GetAllocatedBlob(k, b, n);
    }
    STDMETHODIMP GetUnknown(REFGUID k, REFIID i, LPVOID* v) override {
        return attributes_->GetUnknown(k, i, v);
    }
    STDMETHODIMP SetItem(REFGUID k, REFPROPVARIANT v) override {
        return attributes_->SetItem(k, v);
    }
    STDMETHODIMP DeleteItem(REFGUID k) override { return attributes_->DeleteItem(k); }
    STDMETHODIMP DeleteAllItems() override { return attributes_->DeleteAllItems(); }
    STDMETHODIMP SetUINT32(REFGUID k, UINT32 v) override { return attributes_->SetUINT32(k, v); }
    STDMETHODIMP SetUINT64(REFGUID k, UINT64 v) override { return attributes_->SetUINT64(k, v); }
    STDMETHODIMP SetDouble(REFGUID k, double v) override { return attributes_->SetDouble(k, v); }
    STDMETHODIMP SetGUID(REFGUID k, REFGUID v) override { return attributes_->SetGUID(k, v); }
    STDMETHODIMP SetString(REFGUID k, LPCWSTR v) override { return attributes_->SetString(k, v); }
    STDMETHODIMP SetBlob(REFGUID k, const UINT8* b, UINT32 n) override {
        return attributes_->SetBlob(k, b, n);
    }
    STDMETHODIMP SetUnknown(REFGUID k, IUnknown* v) override {
        return attributes_->SetUnknown(k, v);
    }
    STDMETHODIMP LockStore() override { return attributes_->LockStore(); }
    STDMETHODIMP UnlockStore() override { return attributes_->UnlockStore(); }
    STDMETHODIMP GetCount(UINT32* n) override { return attributes_->GetCount(n); }
    STDMETHODIMP GetItemByIndex(UINT32 i, GUID* k, PROPVARIANT* v) override {
        return attributes_->GetItemByIndex(i, k, v);
    }
    STDMETHODIMP CopyAllItems(IMFAttributes* d) override { return attributes_->CopyAllItems(d); }

private:
    ~SolinActivate() {
        if (source_) source_->Release();
        if (attributes_) attributes_->Release();
    }

    LONG refs_;
    IMFAttributes* attributes_;
    SolinSource* source_;
};

// ── class factory ─────────────────────────────────────────────────────────────

class SolinClassFactory : public IClassFactory {
public:
    SolinClassFactory() : refs_(1) {}

    STDMETHODIMP QueryInterface(REFIID riid, void** out) override {
        if (!out) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) {
            *out = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *out = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&refs_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&refs_);
        if (n == 0) delete this;
        return n;
    }

    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** out) override {
        if (outer) return CLASS_E_NOAGGREGATION;
        solin_log("CreateInstance requested");
        SolinActivate* activate = new (std::nothrow) SolinActivate();
        if (!activate) return E_OUTOFMEMORY;
        HRESULT hr = activate->QueryInterface(riid, out);
        activate->Release();
        solin_log("CreateInstance hr=0x%08X", hr);
        return hr;
    }
    STDMETHODIMP LockServer(BOOL lock) override {
        if (lock) InterlockedIncrement(&g_locks);
        else InterlockedDecrement(&g_locks);
        return S_OK;
    }

private:
    LONG refs_;
};

// ── COM entry points ──────────────────────────────────────────────────────────

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void** out) {
    if (clsid != CLSID_SolinCameraSource) return CLASS_E_CLASSNOTAVAILABLE;
    SolinClassFactory* factory = new (std::nothrow) SolinClassFactory();
    if (!factory) return E_OUTOFMEMORY;
    HRESULT hr = factory->QueryInterface(riid, out);
    factory->Release();
    return hr;
}

STDAPI DllCanUnloadNow() { return g_locks == 0 ? S_OK : S_FALSE; }

static LONG register_under(HKEY hive, const wchar_t* path) {
    wchar_t key[256];
    swprintf_s(key, L"Software\\Classes\\CLSID\\%s", kClsidString);
    HKEY clsid_key = nullptr;
    LONG rc = RegCreateKeyExW(hive, key, 0, nullptr, 0, KEY_WRITE, nullptr, &clsid_key, nullptr);
    if (rc != ERROR_SUCCESS) return rc;
    RegSetValueExW(clsid_key, nullptr, 0, REG_SZ, (const BYTE*)kFriendlyName,
                   (DWORD)((wcslen(kFriendlyName) + 1) * sizeof(wchar_t)));

    HKEY inproc = nullptr;
    rc = RegCreateKeyExW(clsid_key, L"InprocServer32", 0, nullptr, 0, KEY_WRITE, nullptr,
                         &inproc, nullptr);
    if (rc == ERROR_SUCCESS) {
        RegSetValueExW(inproc, nullptr, 0, REG_SZ, (const BYTE*)path,
                       (DWORD)((wcslen(path) + 1) * sizeof(wchar_t)));
        const wchar_t both[] = L"Both";
        RegSetValueExW(inproc, L"ThreadingModel", 0, REG_SZ, (const BYTE*)both, sizeof(both));
        RegCloseKey(inproc);
    }
    RegCloseKey(clsid_key);
    return rc;
}

// Prefer HKLM. The Camera Frame Server runs as NT AUTHORITY\LocalService and
// cannot see a user's HKCU\Software\Classes, so an HKCU-only registration builds
// and activates in-process but fails IMFVirtualCamera::Start with
// ERROR_PATH_NOT_FOUND. HKCU remains a useful unelevated fallback for in-process
// development, so fall back rather than failing outright.
STDAPI DllRegisterServer() {
    wchar_t path[MAX_PATH];
    if (!GetModuleFileNameW(g_module, path, MAX_PATH)) return HRESULT_FROM_WIN32(GetLastError());

    LONG rc = register_under(HKEY_LOCAL_MACHINE, path);
    if (rc == ERROR_SUCCESS) {
        solin_log("registered machine-wide (HKLM) -> %ls", path);
        return S_OK;
    }
    solin_log("HKLM registration failed rc=%ld; falling back to HKCU", rc);
    rc = register_under(HKEY_CURRENT_USER, path);
    return rc == ERROR_SUCCESS ? S_OK : HRESULT_FROM_WIN32(rc);
}

STDAPI DllUnregisterServer() {
    wchar_t key[256];
    swprintf_s(key, L"Software\\Classes\\CLSID\\%s", kClsidString);
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, key);
    RegDeleteTreeW(HKEY_CURRENT_USER, key);
    return S_OK;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = (HMODULE)inst;
        DisableThreadLibraryCalls(inst);
        solin_log("DLL loaded");
    }
    return TRUE;
}
