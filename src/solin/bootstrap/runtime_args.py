from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeArgs:
    requested_profile_id: str
    create_profile: bool
    media_files: tuple[str, ...]
    show_profile_selector: bool = False
    profile_handoff: str | None = None


def parse_runtime_args(argv: list[str]) -> RuntimeArgs:
    """
    Extract Solin runtime flags and existing startup media files.

    --profile <id> is used by controlled profile relaunch.
    --create-profile opens onboarding for an additional profile.
    --select-profile shows the selector even when only one profile exists.
    --profile-handoff is the private startup gate for a controlled relaunch.
    All other existing file paths continue to be treated as startup media.
    """
    requested_profile = ""
    create_profile = False
    show_profile_selector = False
    profile_handoff = None
    file_args: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--select-profile":
            show_profile_selector = True
            i += 1
            continue
        if arg == "--profile-handoff":
            profile_handoff = ""
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                profile_handoff = argv[i + 1]
                i += 1
            i += 1
            continue
        if arg.startswith("--profile-handoff="):
            profile_handoff = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--create-profile":
            create_profile = True
            i += 1
            continue
        if arg == "--profile":
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                requested_profile = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith("--profile="):
            requested_profile = arg.split("=", 1)[1]
            i += 1
            continue
        if os.path.isfile(arg):
            file_args.append(arg)
        i += 1
    return RuntimeArgs(
        requested_profile_id=requested_profile,
        create_profile=create_profile,
        media_files=tuple(file_args),
        show_profile_selector=show_profile_selector,
        profile_handoff=profile_handoff,
    )
