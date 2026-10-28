"""Shared writer for the suite's sh stubs (fake curl, gcloud, date, ... binaries).

On macOS /bin/sh is bash 3.2 run as `sh` (POSIX mode); on Linux it is dash. A `#!/bin/sh` stub
that works under dash can still break on the macOS CI job (fresh-rev-macos: an escaped brace in
a double-quoted ${...} default kept its backslash there). So write_stub() runs every sh stub
under the test bash in sh mode, as on macOS: it points the shebang at an `sh` symlink to
ATA_TEST_BASH. bash enters POSIX mode when its argv[0] is `sh`, and an absolute shebang does not
depend on PATH, so stubs work wherever they live. With ATA_TEST_BASH=<bash 3.2> the Linux lanes run the stubs under bash 3.2 as `sh`, like
the macOS job; by default they run under the system bash as `sh` instead of dash.

Stub authors: write stubs with `#!/bin/sh` (write_stub rewrites it) and prefer
`printf '%s\\n' ...` over `echo` for anything that is not a fixed word: Apple's /bin/sh has
xpg_echo on, so its echo interprets backslashes in the arguments.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
SH_SHEBANG = "#!/bin/sh\n"

_sh_dir = None


def stub_sh() -> str:
    """Absolute path of an `sh` symlink to ATA_TEST_BASH (one per session)."""
    global _sh_dir
    assert TEST_BASH, f"ATA_TEST_BASH={os.environ.get('ATA_TEST_BASH')!r} not found"
    if _sh_dir is None:
        _sh_dir = tempfile.mkdtemp(prefix="ata-tests-sh-")
        atexit.register(shutil.rmtree, _sh_dir, True)
        os.symlink(TEST_BASH, os.path.join(_sh_dir, "sh"))
    return os.path.join(_sh_dir, "sh")


def sh_stub(text: str) -> str:
    """`text` with a leading #!/bin/sh pointed at the test bash in sh mode."""
    assert text.startswith(SH_SHEBANG), f"not a #!/bin/sh stub: {text[:40]!r}"
    return f"#!{stub_sh()}\n" + text[len(SH_SHEBANG):]


def write_stub(path, text: str) -> Path:
    """Write an executable stub. A #!/bin/sh stub runs under the test bash as `sh` (see above);
    other shebangs (python3, bash) are written as they are."""
    path = Path(path)
    path.write_text(sh_stub(text) if text.startswith(SH_SHEBANG) else text)
    path.chmod(0o755)
    return path
