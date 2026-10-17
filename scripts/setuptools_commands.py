"""Setuptools adapter for generated translation resources."""

from pathlib import Path
import shutil

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    """Compile current sources into the wheel's resource tree."""

    def run(self) -> None:
        if self.editable_mode or self.dry_run:
            return
        super().run()

        from solin.core.i18n.compiler import compile_catalogs

        source_dir = Path(self.get_package_dir("solin")) / "resources" / "translations"
        build_base = Path(self.get_finalized_command("build").build_base)
        staging_dir = build_base / "translations" / "wheel"
        catalogs = compile_catalogs(source_dir, staging_dir)
        output_dir = Path(self.build_lib) / "solin" / "resources" / "translations"
        self.mkpath(str(output_dir))
        self._translation_outputs = []
        previous_force = self.force
        try:
            # A newer delivery timestamp must never override fingerprinted sources.
            self.force = True
            for catalog in catalogs:
                destination = output_dir / catalog.name
                self.copy_file(str(catalog), str(destination))
                self._translation_outputs.append(str(destination))
        finally:
            self.force = previous_force

        # Exclusions do not remove files left in build_lib by earlier builds.
        current = {catalog.name for catalog in catalogs}
        for path in output_dir.iterdir():
            if path.name.startswith(".solin-translations"):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            elif path.suffix == ".ts" or (path.suffix == ".qm" and path.name not in current):
                path.unlink()

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        return list(dict.fromkeys([
            *super().get_outputs(include_bytecode),
            *getattr(self, "_translation_outputs", ()),
        ]))
