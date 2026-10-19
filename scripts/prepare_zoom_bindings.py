"""Generate UI Automation interfaces from Windows' type library before packaging.

comtypes supplies COM support; its UIAutomationClient modules are generated
separately. Creating them here lets Nuitka bundle the interfaces with the app.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("UI Automation bindings must be generated on Windows.")
    import comtypes.client
    import comtypes.gen

    # Put generated modules in the installed package so Nuitka discovers both
    # the friendly module and its versioned type-library dependencies.
    comtypes.client.gen_dir = str(Path(comtypes.gen.__file__).resolve().parent)
    module = comtypes.client.GetModule("UIAutomationCore.dll")
    if not hasattr(module, "CUIAutomation8") or not hasattr(module, "IUIAutomation2"):
        raise SystemExit("Required UI Automation interfaces are unavailable.")


if __name__ == "__main__":
    main()
