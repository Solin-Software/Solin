#include "bridge.hpp"

#include <shiboken.h>

#include <Python.h>

#include <cstdint>
#include <exception>
#include <memory>
#include <new>
#include <string_view>

namespace solin::qt_media_bridge {
namespace {

constexpr char kCapsuleName[] = "solin.qt_media_bridge.FrameBridge";

struct ModuleState final {
    PyObject* image_type{nullptr};
    PyObject* video_frame_type{nullptr};
    PyObject* video_sink_type{nullptr};
};

struct BridgeHolder final {
    std::unique_ptr<FrameBridge> bridge{};
};

[[nodiscard]] ModuleState* module_state(PyObject* module) noexcept {
    return static_cast<ModuleState*>(PyModule_GetState(module));
}

void capsule_destructor(PyObject* capsule) noexcept {
    if (!PyCapsule_IsValid(capsule, kCapsuleName)) {
        PyErr_Clear();
        return;
    }
    delete static_cast<BridgeHolder*>(PyCapsule_GetPointer(capsule, kCapsuleName));
    PyErr_Clear();
}

[[nodiscard]] BridgeHolder* bridge_holder(PyObject* capsule) {
    auto* holder = static_cast<BridgeHolder*>(
        PyCapsule_GetPointer(capsule, kCapsuleName));
    if (holder == nullptr || holder->bridge == nullptr) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_RuntimeError, "The Qt media bridge is closed");
        }
        return nullptr;
    }
    return holder;
}

[[nodiscard]] PyObject* create(PyObject*, PyObject* arguments) {
    unsigned int width = 0U;
    unsigned int height = 0U;
    unsigned long long generation = 0U;
    if (!PyArg_ParseTuple(arguments, "IIK:create", &width, &height,
                          &generation)) {
        return nullptr;
    }
    try {
        auto holder = std::make_unique<BridgeHolder>();
        holder->bridge = make_frame_bridge(width, height, generation);
        auto* capsule =
            PyCapsule_New(holder.get(), kCapsuleName, capsule_destructor);
        if (capsule == nullptr) {
            return nullptr;
        }
        static_cast<void>(holder.release());
        return capsule;
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
    }
    return nullptr;
}

[[nodiscard]] PyObject* descriptor(PyObject*, PyObject* capsule) {
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    const auto& value = holder->bridge->descriptor();
    return Py_BuildValue(
        "{s:s,s:s,s:K,s:I,s:I}", "channel_id", value.channel_id.c_str(),
        "handle_token", value.handle_token.c_str(), "generation",
        static_cast<unsigned long long>(value.generation), "width", value.width,
        "height", value.height);
}

[[nodiscard]] PyObject* status(PyObject*, PyObject* capsule) {
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    const auto value = holder->bridge->status();
    return Py_BuildValue(
        "{s:O,s:O,s:K,s:K,s:K,s:I,s:I,s:s}", "available",
        value.available ? Py_True : Py_False, "direct_submission_active",
        value.direct_submission_active ? Py_True : Py_False, "published_sequence",
        static_cast<unsigned long long>(value.published_sequence),
        "dropped_frames", static_cast<unsigned long long>(value.dropped_frames),
        "resource_generation",
        static_cast<unsigned long long>(value.resource_generation),
        "resource_width", value.resource_width, "resource_height",
        value.resource_height,
        "error_code", value.error_code.c_str());
}

[[nodiscard]] PyObject* set_decoder_frame_gate(PyObject*,
                                               PyObject* arguments) {
    PyObject* capsule = nullptr;
    unsigned long long playback_session_id = 0U;
    int accepting_frames = 0;
    if (!PyArg_ParseTuple(arguments, "OKp:set_decoder_frame_gate", &capsule,
                          &playback_session_id, &accepting_frames)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    try {
        holder->bridge->set_decoder_frame_gate(playback_session_id,
                                               accepting_frames != 0);
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

[[nodiscard]] PyObject* set_direct_submission(PyObject*,
                                              PyObject* arguments) {
    PyObject* capsule = nullptr;
    int enabled = 0;
    unsigned int maximum_fps = 0U;
    if (!PyArg_ParseTuple(arguments, "OpI:set_direct_submission", &capsule,
                          &enabled, &maximum_fps)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    try {
        holder->bridge->set_direct_submission(enabled != 0, maximum_fps);
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

[[nodiscard]] PyObject* set_route_state(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    unsigned long long session_id = 0U;
    int accepting_frames = 0;
    int demanded = 0;
    if (!PyArg_ParseTuple(arguments, "OKpp:set_route_state", &capsule,
                          &session_id, &accepting_frames, &demanded)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    try {
        holder->bridge->set_route_state(session_id, accepting_frames != 0,
                                        demanded != 0);
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

[[nodiscard]] PyObject* stage_media_epoch(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    unsigned long long media_epoch = 0U;
    if (!PyArg_ParseTuple(arguments, "OK:stage_media_epoch", &capsule,
                          &media_epoch)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    try {
        holder->bridge->stage_media_epoch(media_epoch);
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

[[nodiscard]] PyObject* set_image_transform(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    int enabled = 0;
    int animate = 0;
    unsigned long long media_epoch = 0U;
    unsigned int canvas_width = 0U;
    unsigned int canvas_height = 0U;
    unsigned int duration_ms = 0U;
    double zoom = 1.0;
    double norm_x = 0.5;
    double norm_y = 0.5;
    if (!PyArg_ParseTuple(arguments, "OppKIIIddd:set_image_transform", &capsule,
                          &enabled, &animate, &media_epoch, &canvas_width,
                          &canvas_height, &duration_ms, &zoom, &norm_x,
                          &norm_y)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    try {
        holder->bridge->set_image_transform(
            {.enabled = enabled != 0,
             .animate = animate != 0,
             .media_epoch = media_epoch,
             .canvas_width = canvas_width,
             .canvas_height = canvas_height,
             .duration_ms = duration_ms,
             .zoom = zoom,
             .norm_x = norm_x,
             .norm_y = norm_y});
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

[[nodiscard]] PyTypeObject* video_frame_type(PyObject* module) {
    auto* state = module_state(module);
    if (state == nullptr) {
        PyErr_SetString(PyExc_RuntimeError,
                        "The Qt media bridge module state is unavailable");
        return nullptr;
    }
    if (state->video_frame_type != nullptr) {
        return reinterpret_cast<PyTypeObject*>(state->video_frame_type);
    }
    PyObject* multimedia = PyImport_ImportModule("PySide6.QtMultimedia");
    if (multimedia == nullptr) {
        return nullptr;
    }
    PyObject* type = PyObject_GetAttrString(multimedia, "QVideoFrame");
    Py_DECREF(multimedia);
    if (type == nullptr) {
        return nullptr;
    }
    if (!PyType_Check(type) || !SbkObjectType_Check(reinterpret_cast<PyTypeObject*>(type))) {
        Py_DECREF(type);
        PyErr_SetString(PyExc_RuntimeError,
                        "PySide6.QtMultimedia.QVideoFrame is incompatible");
        return nullptr;
    }
    state->video_frame_type = type;
    return reinterpret_cast<PyTypeObject*>(type);
}

[[nodiscard]] PyTypeObject* image_type(PyObject* module) {
    auto* state = module_state(module);
    if (state == nullptr) {
        PyErr_SetString(PyExc_RuntimeError,
                        "The Qt media bridge module state is unavailable");
        return nullptr;
    }
    if (state->image_type != nullptr) {
        return reinterpret_cast<PyTypeObject*>(state->image_type);
    }
    PyObject* gui = PyImport_ImportModule("PySide6.QtGui");
    if (gui == nullptr) {
        return nullptr;
    }
    PyObject* type = PyObject_GetAttrString(gui, "QImage");
    Py_DECREF(gui);
    if (type == nullptr) {
        return nullptr;
    }
    if (!PyType_Check(type) ||
        !SbkObjectType_Check(reinterpret_cast<PyTypeObject*>(type))) {
        Py_DECREF(type);
        PyErr_SetString(PyExc_RuntimeError,
                        "PySide6.QtGui.QImage is incompatible");
        return nullptr;
    }
    state->image_type = type;
    return reinterpret_cast<PyTypeObject*>(type);
}

[[nodiscard]] PyTypeObject* video_sink_type(PyObject* module) {
    auto* state = module_state(module);
    if (state == nullptr) {
        PyErr_SetString(PyExc_RuntimeError,
                        "The Qt media bridge module state is unavailable");
        return nullptr;
    }
    if (state->video_sink_type != nullptr) {
        return reinterpret_cast<PyTypeObject*>(state->video_sink_type);
    }
    PyObject* multimedia = PyImport_ImportModule("PySide6.QtMultimedia");
    if (multimedia == nullptr) {
        return nullptr;
    }
    PyObject* type = PyObject_GetAttrString(multimedia, "QVideoSink");
    Py_DECREF(multimedia);
    if (type == nullptr) {
        return nullptr;
    }
    if (!PyType_Check(type) ||
        !SbkObjectType_Check(reinterpret_cast<PyTypeObject*>(type))) {
        Py_DECREF(type);
        PyErr_SetString(PyExc_RuntimeError,
                        "PySide6.QtMultimedia.QVideoSink is incompatible");
        return nullptr;
    }
    state->video_sink_type = type;
    return reinterpret_cast<PyTypeObject*>(type);
}

[[nodiscard]] PyObject* bind_video_sink(PyObject* module,
                                        PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* sink = nullptr;
    if (!PyArg_ParseTuple(arguments, "OO:bind_video_sink", &capsule, &sink)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    auto* expected_type = video_sink_type(module);
    if (expected_type == nullptr) {
        return nullptr;
    }
    if (!PyObject_TypeCheck(sink, expected_type) ||
        !Shiboken::Object::isValid(sink, false)) {
        PyErr_SetString(PyExc_TypeError,
                        "sink must be a valid PySide6 QVideoSink");
        return nullptr;
    }
    auto* pointer = Shiboken::Object::cppPointer(
        reinterpret_cast<SbkObject*>(sink), expected_type);
    if (pointer == nullptr) {
        PyErr_SetString(PyExc_TypeError,
                        "The QVideoSink native value is unavailable");
        return nullptr;
    }
    try {
        holder->bridge->bind_video_sink(static_cast<QVideoSink*>(pointer));
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

[[nodiscard]] PyObject* unbind_video_sink(PyObject*, PyObject* capsule) {
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    holder->bridge->unbind_video_sink();
    Py_RETURN_NONE;
}

[[nodiscard]] std::string_view submit_result_name(
    const SubmitResult value) noexcept {
    switch (value) {
    case SubmitResult::accepted:
        return "accepted";
    case SubmitResult::dropped:
        return "dropped";
    case SubmitResult::unavailable:
        return "unavailable";
    case SubmitResult::no_demand:
        return "no_demand";
    case SubmitResult::session_rejected:
        return "session_rejected";
    }
    return "unavailable";
}

[[nodiscard]] PyObject* submit(PyObject* module, PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* frame = nullptr;
    unsigned long long session_id = 0U;
    if (!PyArg_ParseTuple(arguments, "OOK:submit", &capsule, &frame,
                          &session_id)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    auto* expected_type = video_frame_type(module);
    if (expected_type == nullptr) {
        return nullptr;
    }
    if (!PyObject_TypeCheck(frame, expected_type) ||
        !Shiboken::Object::isValid(frame, false)) {
        PyErr_SetString(PyExc_TypeError,
                        "frame must be a valid PySide6 QVideoFrame");
        return nullptr;
    }
    auto* pointer = Shiboken::Object::cppPointer(
        reinterpret_cast<SbkObject*>(frame), expected_type);
    if (pointer == nullptr) {
        PyErr_SetString(PyExc_TypeError,
                        "The QVideoFrame native value is unavailable");
        return nullptr;
    }
    try {
        const auto result = holder->bridge->submit(
            *static_cast<QVideoFrame*>(pointer), session_id);
        const auto name = submit_result_name(result);
        return PyUnicode_FromStringAndSize(name.data(),
                                           static_cast<Py_ssize_t>(name.size()));
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
    }
    return nullptr;
}

[[nodiscard]] PyObject* submit_image(PyObject* module, PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* image = nullptr;
    unsigned long long session_id = 0U;
    if (!PyArg_ParseTuple(arguments, "OOK:submit_image", &capsule, &image,
                          &session_id)) {
        return nullptr;
    }
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    auto* expected_type = image_type(module);
    if (expected_type == nullptr) {
        return nullptr;
    }
    if (!PyObject_TypeCheck(image, expected_type) ||
        !Shiboken::Object::isValid(image, false)) {
        PyErr_SetString(PyExc_TypeError,
                        "image must be a valid PySide6 QImage");
        return nullptr;
    }
    auto* pointer = Shiboken::Object::cppPointer(
        reinterpret_cast<SbkObject*>(image), expected_type);
    if (pointer == nullptr) {
        PyErr_SetString(PyExc_TypeError,
                        "The QImage native value is unavailable");
        return nullptr;
    }
    try {
        const auto result = holder->bridge->submit_image(
            *static_cast<QImage*>(pointer), session_id);
        const auto name = submit_result_name(result);
        return PyUnicode_FromStringAndSize(name.data(),
                                           static_cast<Py_ssize_t>(name.size()));
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
    }
    return nullptr;
}

[[nodiscard]] PyObject* close(PyObject*, PyObject* capsule) {
    auto* holder = bridge_holder(capsule);
    if (holder == nullptr) {
        return nullptr;
    }
    holder->bridge->close();
    holder->bridge.reset();
    Py_RETURN_NONE;
}

int module_traverse(PyObject* module, visitproc visit, void* arg) {
    const auto* state = module_state(module);
    Py_VISIT(state->image_type);
    Py_VISIT(state->video_frame_type);
    Py_VISIT(state->video_sink_type);
    return 0;
}

int module_clear(PyObject* module) {
    auto* state = module_state(module);
    Py_CLEAR(state->image_type);
    Py_CLEAR(state->video_frame_type);
    Py_CLEAR(state->video_sink_type);
    return 0;
}

void module_free(void* module) {
    static_cast<void>(module_clear(static_cast<PyObject*>(module)));
}

PyMethodDef methods[] = {
    {"create", create, METH_VARARGS,
     "Create a Windows D3D11 decoded-frame bridge."},
    {"descriptor", descriptor, METH_O,
     "Return the native frame-channel descriptor."},
    {"status", status, METH_O, "Return current bridge counters and health."},
    {"set_route_state", set_route_state, METH_VARARGS,
     "Update session acceptance and native demand."},
    {"set_decoder_frame_gate", set_decoder_frame_gate, METH_VARARGS,
     "Update decoded-frame playback-session acceptance."},
    {"set_direct_submission", set_direct_submission, METH_VARARGS,
     "Enable bounded native QVideoSink frame submission."},
    {"stage_media_epoch", stage_media_epoch, METH_VARARGS,
     "Stage the epoch committed atomically with the next GPU frame."},
    {"set_image_transform", set_image_transform, METH_VARARGS,
     "Update persistent image zoom and pan state."},
    {"bind_video_sink", bind_video_sink, METH_VARARGS,
     "Bind the decoder sink to the bridge-owned QRhi."},
    {"unbind_video_sink", unbind_video_sink, METH_O,
     "Detach the decoder sink from the bridge-owned QRhi."},
    {"submit", submit, METH_VARARGS,
     "Submit one QVideoFrame without CPU mapping."},
    {"submit_image", submit_image, METH_VARARGS,
     "Upload one static QImage into the shared GPU channel."},
    {"close", close, METH_O, "Stop and release the bridge."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT,
    "solin_qt_media_bridge",
    "Pinned Qt 6.11.1 native decoded-frame bridge.",
    sizeof(ModuleState),
    methods,
    nullptr,
    module_traverse,
    module_clear,
    module_free,
};

} // namespace
} // namespace solin::qt_media_bridge

PyMODINIT_FUNC PyInit_solin_qt_media_bridge() {
    return PyModule_Create(&solin::qt_media_bridge::module_definition);
}
