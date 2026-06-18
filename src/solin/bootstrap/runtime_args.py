from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeArgs:
    requested_profile_id: str
    create_profile: bool
    media_files: tuple[str, ...]


def parse_runtime_args(argv: list[str]) -> RuntimeArgs:
    """
    Extract Solin runtime flags and existing startup media files.

    --profile <id> is used by controlled profile relaunch.
    --create-profile opens onboarding for an additional profile.
    All other existing file paths continue to be treated as startup media.
    """
    requested_profile = ""
    create_profile = False
    file_args: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--create-profile":
            create_profile = True
            i += 1
            continue
        if arg == "--profile":
            if i + 1 < len(argv):
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
    )
