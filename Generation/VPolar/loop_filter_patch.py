#!/usr/bin/env python3
"""Install the OAP flavor-aware loop-filter hook into MadGraph 3.4.2.

The patch is intentionally smaller than the modified full MadGraph module
distributed with the original VPolar study.  It adds one early branch inside
``LoopAmplitude.user_filter`` and copies ``loop_filter_runtime.py`` into the
MadGraph package.  No command parser or global process dispatch is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile


SUPPORTED_MG5_VERSION = "3.4.2"
LOOP_MODULE_RELATIVE_PATH = Path("madgraph/loop/loop_diagram_generation.py")
RUNTIME_RELATIVE_PATH = Path("madgraph/loop/oap_vpolar_filter.py")

# Official MG5_aMC_v3.4.2 tarball, dated 2023-01-20.  Pinning both its VERSION
# and this module avoids silently patching a changed internal API.
PRISTINE_LOOP_MODULE_SHA256 = (
    "91e1a5b6584e010d02cd2183efe8fac6084df81d6c7ed192c0b237ad07b91b12"
)

_ANCHOR = (
    "        edit_filter_manually = False" + "\x20\n"
    "        if not edit_filter_manually and filter in [None,'None']:\n"
)
_INJECTION = """        # OAP_VPOLAR_FILTER_BEGIN
        if isinstance(filter, str) and filter.lower() in ('oap_tl', 'oap_lt'):
            from madgraph.loop.oap_vpolar_filter import apply_oap_filter
            return apply_oap_filter(self, model, structs, filter.lower())
        # OAP_VPOLAR_FILTER_END

"""
_BEGIN_SENTINEL = "# OAP_VPOLAR_FILTER_BEGIN"
_END_SENTINEL = "# OAP_VPOLAR_FILTER_END"


class PatchError(RuntimeError):
    """Raised when an installation is not the validated MG5 3.4.2 layout."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_version(root: Path) -> str:
    version_path = root / "VERSION"
    try:
        text = version_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PatchError(f"cannot read MadGraph VERSION file: {version_path}") from exc
    match = re.search(r"(?m)^version\s*=\s*([^\s]+)\s*$", text)
    if match is None:
        raise PatchError(f"cannot parse MadGraph version from {version_path}")
    return match.group(1)


def _inject_hook(source: str) -> str:
    """Return source with the narrowly scoped hook inserted exactly once."""

    begin_count = source.count(_BEGIN_SENTINEL)
    end_count = source.count(_END_SENTINEL)
    if begin_count == end_count == 1:
        return source
    if begin_count or end_count:
        raise PatchError("MadGraph loop module contains a partial OAP patch")
    if source.count(_ANCHOR) != 1:
        raise PatchError("validated MG5 3.4.2 user_filter anchor was not unique")
    return source.replace(_ANCHOR, _INJECTION + _ANCHOR, 1)


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.oap-", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def inspect_installation(mg5_root: Path) -> dict[str, object]:
    """Validate and describe a pristine or already-patched installation."""

    root = mg5_root.expanduser().resolve()
    version = _read_version(root)
    if version != SUPPORTED_MG5_VERSION:
        raise PatchError(
            f"unsupported MadGraph version {version!r}; expected "
            f"{SUPPORTED_MG5_VERSION}"
        )

    loop_module = root / LOOP_MODULE_RELATIVE_PATH
    if loop_module.is_symlink():
        raise PatchError(f"refusing to patch symlink: {loop_module}")
    try:
        loop_payload = loop_module.read_bytes()
    except OSError as exc:
        raise PatchError(f"cannot read MadGraph loop module: {loop_module}") from exc
    loop_text = loop_payload.decode("utf-8")
    patched = _BEGIN_SENTINEL in loop_text or _END_SENTINEL in loop_text
    if patched:
        _inject_hook(loop_text)
    elif _sha256_bytes(loop_payload) != PRISTINE_LOOP_MODULE_SHA256:
        raise PatchError(
            "MadGraph loop module does not match the validated 3.4.2 source: "
            f"{loop_module}"
        )

    runtime_source = Path(__file__).with_name("loop_filter_runtime.py")
    runtime_payload = runtime_source.read_bytes()
    installed_runtime = root / RUNTIME_RELATIVE_PATH
    runtime_matches = (
        installed_runtime.is_file()
        and not installed_runtime.is_symlink()
        and installed_runtime.read_bytes() == runtime_payload
    )
    if patched and not runtime_matches:
        raise PatchError(
            "MadGraph contains the OAP hook but its runtime module is missing "
            "or differs from this checkout"
        )

    return {
        "root": root,
        "version": version,
        "loop_module": loop_module,
        "loop_payload": loop_payload,
        "loop_text": loop_text,
        "patched": patched,
        "runtime_source": runtime_source,
        "runtime_payload": runtime_payload,
        "installed_runtime": installed_runtime,
        "runtime_matches": runtime_matches,
    }


def apply_patch(mg5_root: Path) -> bool:
    """Apply the validated patch; return ``False`` when already current."""

    state = inspect_installation(mg5_root)
    if state["patched"] and state["runtime_matches"]:
        return False

    loop_module = state["loop_module"]
    loop_payload = state["loop_payload"]
    loop_text = state["loop_text"]
    installed_runtime = state["installed_runtime"]
    runtime_payload = state["runtime_payload"]
    assert isinstance(loop_module, Path)
    assert isinstance(loop_payload, bytes)
    assert isinstance(loop_text, str)
    assert isinstance(installed_runtime, Path)
    assert isinstance(runtime_payload, bytes)

    if installed_runtime.exists() and not state["runtime_matches"]:
        raise PatchError(
            f"refusing to overwrite an unrelated runtime: {installed_runtime}"
        )

    backup = loop_module.with_name(loop_module.name + ".oap-original")
    if backup.exists():
        if backup.is_symlink() or backup.read_bytes() != loop_payload:
            raise PatchError(f"existing backup is not the pristine module: {backup}")
    else:
        shutil.copy2(loop_module, backup)

    loop_mode = loop_module.stat().st_mode & 0o777
    runtime_mode = state["runtime_source"].stat().st_mode & 0o777
    patched_payload = _inject_hook(loop_text).encode("utf-8")
    _atomic_write(installed_runtime, runtime_payload, runtime_mode)
    _atomic_write(loop_module, patched_payload, loop_mode)

    verified = inspect_installation(Path(state["root"]))
    if not (verified["patched"] and verified["runtime_matches"]):
        raise PatchError("post-installation verification failed")
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mg5-root",
        required=True,
        type=Path,
        help="root of an unpacked, pristine MG5_aMC 3.4.2 installation",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate only")
    action.add_argument("--apply", action="store_true", help="install the hook")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.apply:
            changed = apply_patch(args.mg5_root)
            print("installed" if changed else "already installed")
        else:
            state = inspect_installation(args.mg5_root)
            print("patched" if state["patched"] else "compatible pristine")
    except (OSError, PatchError, UnicodeError) as exc:
        raise SystemExit(f"loop-filter patch error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
