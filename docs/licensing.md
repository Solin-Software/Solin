# Licensing and third-party components

## Solin

Solin is licensed under the **GNU General Public License, version 3 or, at your
option, any later version** (`GPL-3.0-or-later`). The repository includes the
unmodified [official GPL version 3 text](../LICENSE) and the application's
[copyright and license notice](../NOTICE).

## Dependencies and content

Solin's license does not replace dependency licenses. In particular:

- [OBS Studio and libobs](https://github.com/obsproject/obs-studio) are licensed
  under GPL version 2 or later.
- [pylibobs](https://github.com/jonata/pylibobs), the Python binding used by
  Solin, declares `GPL-2.0-or-later`.
- Qt, PySide6, native codecs, browser components, and other dependencies retain
  their upstream terms and notices. Check the exact versions and components
  included in a distribution.
- Notices for vendored native sources are kept alongside them in
  [`native/media_engine/third_party`](../native/media_engine/third_party).

The GPL's “or later” option permits use of GPL-2.0-or-later components under
GPL version 3 when combining them with Solin. GNU's
[GPL version 3 guide](https://www.gnu.org/licenses/quick-guide-gplv3.html)
explains version compatibility.

Third-party and user-provided content retain their respective licenses and
terms. Solin's software license does not grant rights to redistribute that
content.

## Distributing a build

Keep Solin's license and notice with the application, preserve the licenses and
copyright notices of bundled components, and provide the corresponding source
as required by the applicable licenses. A link to a moving development branch
alone does not identify the source for a specific binary.

Use the source for the exact release tag, its build scripts, and the matching
source for bundled dependencies, including any modifications. The
[build guide](building.md) describes the native runtimes, while
[release documentation](releases.md) describes tagged artifacts and manifests.
The public Solin repository covers Solin's source; it does not by itself supply
all corresponding dependency source.

The runtime staging step preserves `pylibobs` notices and copies Solin's
`LICENSE` and `NOTICE` into `licenses/solin` in standalone builds. Linux runtime
packaging also collects notices for copied system libraries. These files are
part of distribution preparation, not a substitute for checking the complete
dependency inventory and corresponding source for the binary being released.

[Back to the README](../README.md)
