/* Framework Python host for the editable macOS application. */
#include <Python.h>
#include "host_config.h"

static void check_status(PyStatus status)
{
    if (PyStatus_Exception(status))
        Py_ExitStatusException(status);
}

static void set_string(PyConfig *config, wchar_t **field, const char *value)
{
    check_status(PyConfig_SetBytesString(config, field, value));
}

int main(int argc, char **argv)
{
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.user_site_directory = SOLIN_USER_SITE;
    set_string(&config, &config.program_name, SOLIN_PYTHON);
    set_string(&config, &config.executable, SOLIN_HOST);
    set_string(&config, &config.base_executable, SOLIN_BASE_PYTHON);
    set_string(&config, &config.prefix, SOLIN_PREFIX);
    set_string(&config, &config.exec_prefix, SOLIN_EXEC_PREFIX);
    set_string(&config, &config.base_prefix, SOLIN_BASE_PREFIX);
    set_string(&config, &config.base_exec_prefix, SOLIN_BASE_EXEC_PREFIX);

    /* Supplying the stdlib paths keeps path initialization from mistaking the
       app bundle for a new Python installation. site still discovers the
       active environment's site-packages, including editable-install .pths. */
    const char *paths[] = SOLIN_STDLIB_PATHS;
    config.module_search_paths_set = 1;
    for (size_t i = 0; i < sizeof(paths) / sizeof(paths[0]); ++i) {
        wchar_t *path = Py_DecodeLocale(paths[i], NULL);
        if (path == NULL) {
            PyConfig_Clear(&config);
            fputs("Cannot decode the development Python search path.\n", stderr);
            return 1;
        }
        PyStatus status = PyWideStringList_Append(&config.module_search_paths, path);
        PyMem_RawFree(path);
        check_status(status);
    }
    check_status(PyConfig_SetBytesArgv(&config, argc, argv));
    PyStatus status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    check_status(status);
    return Py_RunMain();
}
