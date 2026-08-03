// Verifier for the Solin Virtual Camera DirectShow filter.
//
// Builds a real capture graph against the registered filter, grabs a frame, and
// writes it to frame.bmp. That answers — without a browser in the loop — whether
// the filter enumerates, which format a consumer negotiates, whether samples
// actually flow, and whether the image is the right way up.
//
// Build:  build.bat check      Run:  dshow-check.exe

#include <windows.h>
#include <dshow.h>
#include <stdio.h>

#include "solin_guids.h"

#pragma comment(lib, "strmiids.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")

// qedit.h was removed from the modern Windows SDK, so declare the bits we need.
interface ISampleGrabberCB : public IUnknown {
    virtual STDMETHODIMP SampleCB(double, IMediaSample*) = 0;
    virtual STDMETHODIMP BufferCB(double, BYTE*, long) = 0;
};
static const IID IID_ISampleGrabberCB_local = {
    0x0579154A, 0x2B53, 0x4994, {0xB0, 0xD0, 0xE7, 0x73, 0x14, 0x8E, 0xFF, 0x85}};

interface ISampleGrabber : public IUnknown {
    virtual STDMETHODIMP SetOneShot(BOOL) = 0;
    virtual STDMETHODIMP SetMediaType(const AM_MEDIA_TYPE*) = 0;
    virtual STDMETHODIMP GetConnectedMediaType(AM_MEDIA_TYPE*) = 0;
    virtual STDMETHODIMP SetBufferSamples(BOOL) = 0;
    virtual STDMETHODIMP GetCurrentBuffer(long*, long*) = 0;
    virtual STDMETHODIMP GetCurrentSample(IMediaSample**) = 0;
    virtual STDMETHODIMP SetCallback(ISampleGrabberCB*, long) = 0;
};
static const IID IID_ISampleGrabber_local = {
    0x6B652FFF, 0x11FE, 0x4FCE, {0x92, 0xAD, 0x02, 0x66, 0xB5, 0xD7, 0xC7, 0x8F}};
static const CLSID CLSID_SampleGrabber_local = {
    0xC1F400A0, 0x3F08, 0x11D3, {0x9F, 0x0B, 0x00, 0x60, 0x08, 0x03, 0x9E, 0x37}};
static const CLSID CLSID_NullRenderer_local = {
    0xC1F400A4, 0x3F08, 0x11D3, {0x9F, 0x0B, 0x00, 0x60, 0x08, 0x03, 0x9E, 0x37}};

static void write_bmp(const BYTE* rgb24, int width, int height, const char* path) {
    // rgb24 here is top-down; a BMP is bottom-up, so emit rows in reverse.
    BITMAPFILEHEADER fh = {};
    BITMAPINFOHEADER ih = {};
    const int stride = width * 3;
    fh.bfType = 0x4D42;
    fh.bfOffBits = sizeof(fh) + sizeof(ih);
    fh.bfSize = fh.bfOffBits + stride * height;
    ih.biSize = sizeof(ih);
    ih.biWidth = width;
    ih.biHeight = height;
    ih.biPlanes = 1;
    ih.biBitCount = 24;
    ih.biCompression = BI_RGB;

    FILE* f = nullptr;
    if (fopen_s(&f, path, "wb") != 0 || !f) return;
    fwrite(&fh, sizeof(fh), 1, f);
    fwrite(&ih, sizeof(ih), 1, f);
    for (int row = height - 1; row >= 0; --row) fwrite(rgb24 + (size_t)row * stride, stride, 1, f);
    fclose(f);
}

static void i420_to_rgb24_topdown(const BYTE* src, BYTE* dst, int width, int height) {
    const BYTE* y = src;
    const BYTE* u = src + (size_t)width * height;
    const BYTE* v = u + (size_t)width * height / 4;
    for (int row = 0; row < height; ++row) {
        const BYTE* y_row = y + (size_t)row * width;
        const BYTE* u_row = u + (size_t)(row / 2) * (width / 2);
        const BYTE* v_row = v + (size_t)(row / 2) * (width / 2);
        BYTE* d = dst + (size_t)row * width * 3;
        for (int col = 0; col < width; ++col) {
            int c = y_row[col] - 16;
            int du = u_row[col / 2] - 128;
            int dv = v_row[col / 2] - 128;
            int b = (298 * c + 516 * du + 128) >> 8;
            int g = (298 * c - 100 * du - 208 * dv + 128) >> 8;
            int r = (298 * c + 409 * dv + 128) >> 8;
            d[col * 3 + 0] = (BYTE)(b < 0 ? 0 : b > 255 ? 255 : b);
            d[col * 3 + 1] = (BYTE)(g < 0 ? 0 : g > 255 ? 255 : g);
            d[col * 3 + 2] = (BYTE)(r < 0 ? 0 : r > 255 ? 255 : r);
        }
    }
}

static void yuy2_to_rgb24_topdown(const BYTE* src, BYTE* dst, int width, int height) {
    for (int row = 0; row < height; ++row) {
        const BYTE* s = src + (size_t)row * width * 2;
        BYTE* d = dst + (size_t)row * width * 3;
        for (int col = 0; col < width; col += 2) {
            int y0 = s[col * 2 + 0], u = s[col * 2 + 1];
            int y1 = s[col * 2 + 2], v = s[col * 2 + 3];
            for (int k = 0; k < 2; ++k) {
                int c = (k ? y1 : y0) - 16, du = u - 128, dv = v - 128;
                int b = (298 * c + 516 * du + 128) >> 8;
                int g = (298 * c - 100 * du - 208 * dv + 128) >> 8;
                int r = (298 * c + 409 * dv + 128) >> 8;
                d[(col + k) * 3 + 0] = (BYTE)(b < 0 ? 0 : b > 255 ? 255 : b);
                d[(col + k) * 3 + 1] = (BYTE)(g < 0 ? 0 : g > 255 ? 255 : g);
                d[(col + k) * 3 + 2] = (BYTE)(r < 0 ? 0 : r > 255 ? 255 : r);
            }
        }
    }
}

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);

    // "i420" requests the format Chrome/Edge negotiate; the default YUY2 is what
    // Zoom takes. They exercise different conversion paths in the filter.
    bool want_i420 = (argc > 1 && strcmp(argv[1], "i420") == 0);
    printf("requesting %s\n", want_i420 ? "I420" : "YUY2");

    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    IGraphBuilder* graph = nullptr;
    IBaseFilter* source = nullptr;
    IBaseFilter* grabber_filter = nullptr;
    IBaseFilter* null_renderer = nullptr;
    ISampleGrabber* grabber = nullptr;
    IMediaControl* control = nullptr;

    HRESULT hr = CoCreateInstance(CLSID_SolinVirtualCamera, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_IBaseFilter, (void**)&source);
    printf("CoCreateInstance(filter)      hr=0x%08X\n", (unsigned)hr);
    if (FAILED(hr)) goto done;

    hr = CoCreateInstance(CLSID_FilterGraph, nullptr, CLSCTX_INPROC_SERVER,
                          IID_IGraphBuilder, (void**)&graph);
    if (FAILED(hr)) { printf("graph build failed 0x%08X\n", (unsigned)hr); goto done; }

    hr = CoCreateInstance(CLSID_SampleGrabber_local, nullptr, CLSCTX_INPROC_SERVER,
                          IID_IBaseFilter, (void**)&grabber_filter);
    printf("SampleGrabber                 hr=0x%08X\n", (unsigned)hr);
    if (FAILED(hr)) goto done;
    grabber_filter->QueryInterface(IID_ISampleGrabber_local, (void**)&grabber);

    if (grabber) {
        AM_MEDIA_TYPE want = {};
        want.majortype = MEDIATYPE_Video;
        want.subtype = want_i420 ? MEDIASUBTYPE_I420_SOLIN : MEDIASUBTYPE_YUY2;
        want.formattype = FORMAT_VideoInfo;
        grabber->SetMediaType(&want);
        grabber->SetBufferSamples(TRUE);
        grabber->SetOneShot(FALSE);
    }

    CoCreateInstance(CLSID_NullRenderer_local, nullptr, CLSCTX_INPROC_SERVER,
                     IID_IBaseFilter, (void**)&null_renderer);

    graph->AddFilter(source, L"Solin");
    graph->AddFilter(grabber_filter, L"Grabber");
    if (null_renderer) graph->AddFilter(null_renderer, L"Null");

    {
        IEnumPins* pins = nullptr;
        IPin* out_pin = nullptr;
        source->EnumPins(&pins);
        if (pins) { pins->Next(1, &out_pin, nullptr); pins->Release(); }
        if (!out_pin) { printf("no output pin\n"); goto done; }

        IEnumPins* gp = nullptr;
        IPin* grab_in = nullptr;
        grabber_filter->EnumPins(&gp);
        if (gp) { gp->Next(1, &grab_in, nullptr); gp->Release(); }

        hr = graph->Connect(out_pin, grab_in);
        printf("connect source->grabber       hr=0x%08X\n", (unsigned)hr);
        if (grab_in) grab_in->Release();
        out_pin->Release();
        if (FAILED(hr)) goto done;
    }

    if (null_renderer) {
        IEnumPins* gp = nullptr;
        IPin* grab_out = nullptr;
        grabber_filter->EnumPins(&gp);
        if (gp) { gp->Next(1, &grab_out, nullptr); gp->Next(1, &grab_out, nullptr); gp->Release(); }
        IEnumPins* np = nullptr;
        IPin* null_in = nullptr;
        null_renderer->EnumPins(&np);
        if (np) { np->Next(1, &null_in, nullptr); np->Release(); }
        if (grab_out && null_in) graph->Connect(grab_out, null_in);
        if (grab_out) grab_out->Release();
        if (null_in) null_in->Release();
    }

    graph->QueryInterface(IID_IMediaControl, (void**)&control);
    hr = control->Run();
    printf("graph Run()                   hr=0x%08X\n", (unsigned)hr);

    Sleep(1500);  // let a few frames flow

    if (grabber) {
        long size = 0;
        hr = grabber->GetCurrentBuffer(&size, nullptr);
        printf("GetCurrentBuffer size=%ld     hr=0x%08X\n", size, (unsigned)hr);
        if (SUCCEEDED(hr) && size > 0) {
            BYTE* buf = (BYTE*)malloc(size);
            if (buf && SUCCEEDED(grabber->GetCurrentBuffer(&size, (long*)buf))) {
                AM_MEDIA_TYPE mt = {};
                grabber->GetConnectedMediaType(&mt);
                VIDEOINFOHEADER* vih = (VIDEOINFOHEADER*)mt.pbFormat;
                printf("connected biHeight=%ld  bytes=%ld\n",
                       vih ? vih->bmiHeader.biHeight : 0, size);

                BYTE* rgb = (BYTE*)malloc((size_t)kWidth * kHeight * 3);
                if (rgb) {
                    if (want_i420) {
                        i420_to_rgb24_topdown(buf, rgb, kWidth, kHeight);
                    } else {
                        yuy2_to_rgb24_topdown(buf, rgb, kWidth, kHeight);
                    }
                    write_bmp(rgb, kWidth, kHeight, "frame.bmp");
                    printf("wrote frame.bmp\n");
                    // Sample the top and bottom rows: an inverted image is obvious
                    // from the luma of row 0 versus the last row.
                    printf("row0 Y=%d   rowLast Y=%d\n", buf[0], buf[(size_t)(kHeight - 1) * kWidth * 2]);
                    free(rgb);
                }
                if (mt.pbFormat) CoTaskMemFree(mt.pbFormat);
            }
            free(buf);
        }
    }
    control->Stop();

done:
    if (control) control->Release();
    if (grabber) grabber->Release();
    if (grabber_filter) grabber_filter->Release();
    if (null_renderer) null_renderer->Release();
    if (source) source->Release();
    if (graph) graph->Release();
    CoUninitialize();
    return 0;
}
