#!/usr/bin/env python3
"""Create and validate an immutable OAP VPolar installation manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


CONTRACT = "oap-vpolar-installation-v4"
SCHEMA_VERSION = 4
PROCESSES = ("vpolar_LL", "vpolar_TT", "vpolar_TL", "vpolar_LT")
EXPECTED_DIAGRAM_COUNTS = {
    process: {"representatives": 44, "raw_equivalent": 86}
    for process in PROCESSES
}
PDF_SET = "NNPDF31_nlo_as_0118_luxqed"
PDF_ID = 324900
LOOP_REDUCTION = {
    "backend": "CutTools",
    "collier": None,
    "loop_optimized_output": True,
    "madloop_reduction_lib": "1",
    # In MadGraph terminology, ``external`` means that generated processes
    # link the MadGraph-wide bundled vendor/CutTools build rather than copying
    # or discovering a library from the ambient environment.
    "output_dependencies": "external",
    "ninja": None,
}
MG5_REDUCTION_SETTINGS = {
    "collier": "None",
    "crash_on_error": "True",
    "loop_optimized_output": "True",
    "ninja": "None",
    "output_dependencies": "external",
}

HERE = Path(__file__).resolve().parent
SOURCES_PATH = HERE / "sources.json"
MANIFEST_NAME = "installation-manifest.json"

REPOSITORY_INPUTS = (
    "sources.json",
    "install_vpolar.sh",
    "installation_manifest.py",
    "loop_filter_patch.py",
    "loop_filter_runtime.py",
    "validate_diagram_counts.py",
    "prepare_lhe_for_shower.py",
    "canonicalize_hepmc.py",
    "run_vpolar_generation.sh",
    "cards/process_vpolar_LL.mg5",
    "cards/process_vpolar_TT.mg5",
    "cards/process_vpolar_TL.mg5",
    "cards/process_vpolar_LT.mg5",
    "cards/run_settings.mg5",
    "cards/pythia8.cmnd.in",
)


class ManifestError(RuntimeError):
    """Raised for an incomplete or incompatible shared installation."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash every installed tree entry, including paths and executable modes."""

    if not root.is_dir():
        raise ManifestError(f"installed tree is unavailable: {root}")
    digest = hashlib.sha256()
    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    for path in entries:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0")
            digest.update(os.readlink(path).encode("utf-8") + b"\0")
        elif path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(f"{path.stat().st_mode & 0o777:o}".encode("ascii") + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise ManifestError(f"unsupported installed tree entry: {path}")
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read {label}: {path}") from error
    if not isinstance(payload, dict):
        raise ManifestError(f"{label} must be a JSON object")
    return payload


def _source_manifest() -> dict[str, Any]:
    payload = _load_json(SOURCES_PATH, "pinned source manifest")
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("sources"), dict
    ):
        raise ManifestError("invalid pinned source manifest schema")
    for name, source in payload["sources"].items():
        if not isinstance(source, dict):
            raise ManifestError(f"source {name} must be an object")
        for key in ("archive", "sha256", "url", "version"):
            if not isinstance(source.get(key), str) or not source[key]:
                raise ManifestError(f"source {name} is missing {key}")
        digest = source["sha256"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ManifestError(f"source {name} has an invalid SHA-256 digest")
    return payload


def _repository_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in REPOSITORY_INPUTS:
        path = HERE / relative
        if not path.is_file():
            raise ManifestError(f"missing repository installation input: {path}")
        hashes[relative] = sha256(path)
    return hashes


def _run_text(command: list[str], label: str) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ManifestError(f"could not query {label}") from error
    return completed.stdout.strip()


def _resolved_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ManifestError(f"{label} is unavailable: {path}") from error
    if not resolved.is_file():
        raise ManifestError(f"{label} is not a regular file: {resolved}")
    return resolved


def _nonempty_file(path: Path, label: str) -> Path:
    resolved = _resolved_file(path, label)
    if resolved.stat().st_size <= 0:
        raise ManifestError(f"{label} is empty: {resolved}")
    return resolved


def _configuration_assignments(path: Path) -> dict[str, str]:
    """Read active ``name = value`` assignments from an MG5 config file."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ManifestError(f"could not read MadGraph configuration: {path}") from error
    assignments: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        active = raw_line.split("#", 1)[0].strip()
        if not active or "=" not in active:
            continue
        key, value = (item.strip() for item in active.split("=", 1))
        if key in assignments:
            raise ManifestError(
                f"duplicate MadGraph configuration key {key!r} at line {line_number}"
            )
        assignments[key] = value
    return assignments


def _reduction_configuration_fingerprint(prefix: Path) -> dict[str, Any]:
    """Validate and fingerprint the persisted optimized CutTools-only setup."""

    generated_card = _nonempty_file(
        prefix / "configure-madgraph.mg5", "generated MadGraph configuration card"
    )
    saved_config = _nonempty_file(
        prefix / "madgraph5" / "input" / "mg5_configuration.txt",
        "saved MadGraph configuration",
    )
    assignments = _configuration_assignments(saved_config)
    observed = {key: assignments.get(key) for key in MG5_REDUCTION_SETTINGS}
    if observed != MG5_REDUCTION_SETTINGS:
        raise ManifestError(
            "saved MadGraph reduction configuration is not the required optimized "
            f"CutTools-only setup: observed {observed!r}, expected "
            f"{MG5_REDUCTION_SETTINGS!r}"
        )

    commands = {
        line.strip()
        for line in generated_card.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required_commands = {
        "set collier None",
        "set crash_on_error True",
        "set loop_optimized_output True",
        "set ninja None",
        "set output_dependencies external",
        "save options ninja collier loop_optimized_output output_dependencies crash_on_error",
    }
    missing_commands = sorted(required_commands - commands)
    if missing_commands:
        raise ManifestError(
            "generated MadGraph configuration card is missing required commands: "
            + ", ".join(missing_commands)
        )

    return {
        "contract": dict(LOOP_REDUCTION),
        "generated_card": {
            "path": generated_card.relative_to(prefix.resolve()).as_posix(),
            "sha256": sha256(generated_card),
        },
        "saved_configuration": {
            "path": saved_config.relative_to(prefix.resolve()).as_posix(),
            "sha256": sha256(saved_config),
            "settings": observed,
        },
    }


def _lhapdf_library(libdir: Path) -> Path:
    candidates = [
        libdir / "libLHAPDF.so",
        libdir / "libLHAPDF.dylib",
        libdir / "libLHAPDF.a",
        *sorted(libdir.glob("libLHAPDF.so.*")),
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise ManifestError(f"could not find the LHAPDF library below {libdir}")


def _installed_library(
    prefix: Path,
    label: str,
    candidates: tuple[Path, ...],
) -> dict[str, str]:
    """Resolve and fingerprint one library artifact inside the shared prefix."""

    root = prefix.expanduser().resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file():
            continue
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as error:
            raise ManifestError(
                f"installed {label} library resolves outside the prefix: {resolved}"
            ) from error
        return {"path": relative, "sha256": sha256(resolved)}
    raise ManifestError(f"could not find the installed {label} library")


def _installed_runtime_libraries(prefix: Path) -> dict[str, dict[str, str]]:
    pythia_dirs = (
        prefix / "heptools" / "pythia8" / "lib",
        prefix / "heptools" / "pythia8" / "lib64",
    )
    # The pinned interface links with -lpythia8 and embeds this directory as an
    # rpath.  Pythia 8.306 installs both forms, so the linker selects the shared
    # library; retain a static fallback for an otherwise compatible layout.
    pythia_candidates: list[Path] = []
    for directory in pythia_dirs:
        pythia_candidates.extend(
            (directory / "libpythia8.so", directory / "libpythia8.dylib")
        )
        pythia_candidates.extend(sorted(directory.glob("libpythia8.so.*")))
    pythia_candidates.extend(directory / "libpythia8.a" for directory in pythia_dirs)

    hepmc_dirs = (
        prefix / "heptools" / "hepmc" / "lib",
        prefix / "heptools" / "hepmc" / "lib64",
    )
    # The same interface compiler copies libHepMC.a to a private link directory
    # specifically to guarantee static HepMC2 linkage.
    hepmc_candidates = tuple(directory / "libHepMC.a" for directory in hepmc_dirs)

    return {
        "pythia8": _installed_library(
            prefix, "Pythia8 runtime", tuple(pythia_candidates)
        ),
        "hepmc2_static": _installed_library(
            prefix, "HepMC2 static", hepmc_candidates
        ),
    }


def _pdf_set_index(info_path: Path) -> int:
    try:
        lines = info_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ManifestError(f"could not read LHAPDF set metadata: {info_path}") from error
    values = []
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key.strip() == "SetIndex":
            try:
                values.append(int(value.strip()))
            except ValueError as error:
                raise ManifestError(
                    f"invalid SetIndex in LHAPDF metadata: {info_path}"
                ) from error
    if values != [PDF_ID]:
        raise ManifestError(
            f"LHAPDF metadata {info_path} has SetIndex {values!r}; expected {PDF_ID}"
        )
    return values[0]


def _lhapdf_fingerprint(lhapdf_config: Path, pdf_set_dir: Path) -> dict[str, Any]:
    """Fingerprint the exact LHAPDF code and central member used at runtime."""

    config = _resolved_file(lhapdf_config, "lhapdf-config")
    prefix_text = _run_text([str(config), "--prefix"], "LHAPDF prefix")
    libdir_text = _run_text([str(config), "--libdir"], "LHAPDF library directory")
    prefix = Path(prefix_text).expanduser().resolve()
    libdir = Path(libdir_text).expanduser().resolve()
    if not prefix.is_dir():
        raise ManifestError(f"LHAPDF prefix is unavailable: {prefix}")
    if not libdir.is_dir():
        raise ManifestError(f"LHAPDF library directory is unavailable: {libdir}")
    library = _lhapdf_library(libdir)

    try:
        set_dir = pdf_set_dir.expanduser().resolve(strict=True)
    except OSError as error:
        raise ManifestError(f"LHAPDF set directory is unavailable: {pdf_set_dir}") from error
    if not set_dir.is_dir() or set_dir.name != PDF_SET:
        raise ManifestError(
            f"LHAPDF set directory must be named {PDF_SET}: {set_dir}"
        )
    info = _resolved_file(set_dir / f"{PDF_SET}.info", "LHAPDF set metadata")
    _pdf_set_index(info)
    member_candidates = [
        candidate
        for candidate in (
            set_dir / f"{PDF_SET}_0000.dat",
            set_dir / f"{PDF_SET}_0000.dat.gz",
        )
        if candidate.exists()
    ]
    if len(member_candidates) != 1:
        raise ManifestError(
            f"expected exactly one LHAPDF central-member grid below {set_dir}; "
            f"found {len(member_candidates)}"
        )
    member = _resolved_file(member_candidates[0], "LHAPDF central-member grid")

    return {
        "config_path": str(config),
        "config_sha256": sha256(config),
        "version": _run_text([str(config), "--version"], "LHAPDF version"),
        "prefix": str(prefix),
        "libdir": str(libdir),
        "library": {"path": str(library), "sha256": sha256(library)},
        "pdf_set": PDF_SET,
        "pdf_id": PDF_ID,
        "pdf_set_dir": str(set_dir),
        "pdf_files": {
            "info": {"path": str(info), "sha256": sha256(info)},
            "member_zero": {"path": str(member), "sha256": sha256(member)},
        },
    }


def _madloop_reduction_value(path: Path) -> str:
    """Return the explicitly selected MadLoop reduction-library sequence."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ManifestError(f"could not read MadLoop parameter card: {path}") from error
    markers = [
        index
        for index, line in enumerate(lines)
        if line.strip().lower() == "#mlreductionlib"
    ]
    if len(markers) != 1 or markers[0] + 1 >= len(lines):
        raise ManifestError(
            f"MadLoop parameter card must contain one #MLReductionLib entry: {path}"
        )
    value = lines[markers[0] + 1].strip()
    # A leading exclamation mark denotes MadGraph's commented default rather
    # than a user-pinned value.  The production contract requires an explicit 1.
    if value.startswith("!") or not value:
        raise ManifestError(
            f"MadLoop reduction library is not explicitly pinned in {path}"
        )
    return value


def pin_process_reduction(prefix: Path, process: str) -> Path:
    """Atomically pin one generated process card to CutTools (library ID 1)."""

    if process not in PROCESSES:
        raise ManifestError(f"unknown VPolar process {process!r}")
    path = prefix.expanduser().resolve() / "processes" / process / "Cards" / "MadLoopParams.dat"
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as error:
        raise ManifestError(f"could not read MadLoop parameter card: {path}") from error
    markers = [
        index
        for index, line in enumerate(lines)
        if line.strip().lower() == "#mlreductionlib"
    ]
    if len(markers) != 1 or markers[0] + 1 >= len(lines):
        raise ManifestError(
            f"MadLoop parameter card must contain one #MLReductionLib entry: {path}"
        )
    value_index = markers[0] + 1
    newline = "\n" if lines[value_index].endswith("\n") else ""
    lines[value_index] = "1" + newline

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if _madloop_reduction_value(path) != LOOP_REDUCTION["madloop_reduction_lib"]:
        raise ManifestError(f"failed to pin MadLoop reduction library in {path}")
    return path


def _process_bundle_fingerprint(prefix: Path, process: str) -> dict[str, Any]:
    """Reject partial MG5 exports and fingerprint substantive loop payloads."""

    if process not in PROCESSES:
        raise ManifestError(f"unknown VPolar process {process!r}")
    prefix = prefix.expanduser().resolve()
    root = prefix / "processes" / process
    global_cuttools = {
        "libcts.a": _nonempty_file(
            prefix / "madgraph5" / "vendor" / "CutTools" / "includects" / "libcts.a",
            "bundled CutTools library",
        ),
        "mpmodule.mod": _nonempty_file(
            prefix / "madgraph5" / "vendor" / "CutTools" / "includects" / "mpmodule.mod",
            "bundled CutTools module",
        ),
    }

    executable = _nonempty_file(root / "bin" / "generate_events", f"{process} generator")
    if not os.access(executable, os.X_OK):
        raise ManifestError(f"{process} generator is not executable: {executable}")
    _nonempty_file(
        root / "Source" / "MODEL" / "model_functions.f",
        f"{process} exported model source",
    )
    madloop_card = _nonempty_file(
        root / "Cards" / "MadLoopParams.dat", f"{process} MadLoop parameter card"
    )
    reduction = _madloop_reduction_value(madloop_card)
    if reduction != LOOP_REDUCTION["madloop_reduction_lib"]:
        raise ManifestError(
            f"{process} MadLoop reduction is {reduction!r}; expected explicit CutTools ID 1"
        )

    artifacts: dict[str, str] = {}

    def record(path: Path, label: str) -> None:
        installed = _nonempty_file(path, label)
        try:
            relative = installed.relative_to(root).as_posix()
        except ValueError:
            # A process CutTools symlink deliberately resolves to the global,
            # bundled MadGraph artifact.  Record its logical process path below.
            relative = path.relative_to(root).as_posix()
        artifacts[relative] = sha256(installed)

    record(root / "bin" / "generate_events", f"{process} generator")
    record(root / "Source" / "MODEL" / "model_functions.f", f"{process} model source")
    record(root / "Cards" / "MadLoopParams.dat", f"{process} MadLoop card")
    for name, global_path in global_cuttools.items():
        process_path = root / "lib" / name
        installed = _nonempty_file(process_path, f"{process} CutTools {name}")
        if installed != global_path:
            raise ManifestError(
                f"{process} {name} does not resolve to bundled CutTools: {installed}"
            )
        artifacts[process_path.relative_to(root).as_posix()] = sha256(installed)

    subprocess_manifest = _nonempty_file(
        root / "SubProcesses" / "subproc.mg", f"{process} subprocess manifest"
    )
    try:
        subprocesses = [
            line.strip()
            for line in subprocess_manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except UnicodeError as error:
        raise ManifestError(
            f"{process} subprocess manifest is not UTF-8: {subprocess_manifest}"
        ) from error
    if not subprocesses or len(subprocesses) != len(set(subprocesses)):
        raise ManifestError(
            f"{process} subprocess manifest is empty or contains duplicates"
        )
    artifacts["SubProcesses/subproc.mg"] = sha256(subprocess_manifest)

    for entry in subprocesses:
        if Path(entry).name != entry or not entry.startswith("P"):
            raise ManifestError(
                f"{process} subprocess manifest contains unsafe entry {entry!r}"
            )
        directory = root / "SubProcesses" / entry
        if not directory.is_dir():
            raise ManifestError(
                f"{process} exported subprocess directory is unavailable: {directory}"
            )
        for filename in (
            "CT_interface.f",
            "loop_matrix.f",
            "polynomial.f",
            "proc_prefix.txt",
        ):
            record(
                directory / filename,
                f"{process} optimized-loop subprocess artifact {entry}/{filename}",
            )
        matrix_files = sorted(directory.glob("matrix*.f"))
        if not matrix_files:
            raise ManifestError(
                f"{process} subprocess {entry} contains no MadEvent matrix source"
            )
        for matrix in matrix_files:
            record(matrix, f"{process} MadEvent matrix source {matrix.name}")

    return {
        "artifacts": dict(sorted(artifacts.items())),
        "madloop_reduction_lib": reduction,
        "subprocesses": subprocesses,
    }


def _required_installation_paths(prefix: Path) -> tuple[Path, ...]:
    paths = [
        prefix / "madgraph5" / "VERSION",
        prefix / "madgraph5" / "bin" / "mg5_aMC",
        prefix / "madgraph5" / "models" / "SM_Loop_ZPolar" / "particles.py",
        prefix
        / "madgraph5"
        / "madgraph"
        / "loop"
        / "oap_vpolar_filter.py",
        prefix
        / "madgraph5"
        / "madgraph"
        / "loop"
        / "loop_diagram_generation.py",
        prefix / "madgraph5" / "input" / "mg5_configuration.txt",
        prefix / "madgraph5" / "vendor" / "CutTools" / "includects" / "libcts.a",
        prefix / "madgraph5" / "vendor" / "CutTools" / "includects" / "mpmodule.mod",
        prefix / "configure-madgraph.mg5",
        prefix / "heptools" / "pythia8" / "bin" / "pythia8-config",
        prefix
        / "heptools"
        / "MG5aMC_PY8_interface"
        / "MG5aMC_PY8_interface",
    ]
    for process in PROCESSES:
        root = prefix / "processes" / process
        paths.extend(
            (
                root / "bin" / "generate_events",
                root / "Cards" / "MadLoopParams.dat",
                root / "Source" / "MODEL" / "model_functions.f",
                root / "SubProcesses" / "subproc.mg",
                root / "lib" / "libcts.a",
                root / "lib" / "mpmodule.mod",
            )
        )
    return tuple(paths)


def _installed_fingerprints(
    prefix: Path, processes: tuple[str, ...] = PROCESSES
) -> dict[str, Any]:
    """Return the immutable installed payload bound to every generated job."""

    file_paths = (
        "madgraph5/bin/mg5_aMC",
        "madgraph5/input/mg5_configuration.txt",
        "madgraph5/madgraph/loop/loop_diagram_generation.py",
        "madgraph5/madgraph/loop/oap_vpolar_filter.py",
        "madgraph5/vendor/CutTools/includects/libcts.a",
        "madgraph5/vendor/CutTools/includects/mpmodule.mod",
        "configure-madgraph.mg5",
        "heptools/pythia8/bin/pythia8-config",
        "heptools/MG5aMC_PY8_interface/MG5aMC_PY8_interface",
    )
    files: dict[str, str] = {}
    for relative in file_paths:
        path = prefix / relative
        if not path.is_file():
            raise ManifestError(f"installed file is unavailable: {path}")
        files[relative] = sha256(path)

    tree_paths = {
        "ufo": "madgraph5/models/SM_Loop_ZPolar",
        "pythia8_xml": "heptools/pythia8/share/Pythia8/xmldoc",
        **{
            process: f"processes/{process}"
            for process in processes
        },
    }
    trees = {
        label: {
            "path": relative,
            "sha256": tree_sha256(prefix / relative),
        }
        for label, relative in tree_paths.items()
    }
    return {
        "files": files,
        "libraries": _installed_runtime_libraries(prefix),
        "loop_reduction": _reduction_configuration_fingerprint(prefix),
        "process_bundles": {
            process: _process_bundle_fingerprint(prefix, process)
            for process in processes
        },
        "trees": trees,
    }


def create_manifest(
    prefix: Path,
    lhapdf_config: Path,
    lhapdf_set_dir: Path,
    diagram_report_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    prefix = prefix.expanduser().resolve()
    lhapdf_config = lhapdf_config.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise ManifestError(f"refusing to overwrite manifest: {output_path}")
    if not lhapdf_config.is_file():
        raise ManifestError(f"lhapdf-config is unavailable: {lhapdf_config}")
    missing = [str(path) for path in _required_installation_paths(prefix) if not path.is_file()]
    if missing:
        raise ManifestError("installation is missing required files: " + ", ".join(missing))

    diagram_report = _load_json(diagram_report_path, "diagram validation report")
    observed_counts = diagram_report.get("diagram_counts")
    if observed_counts != EXPECTED_DIAGRAM_COUNTS:
        raise ManifestError(
            f"diagram report has {observed_counts!r}; expected {EXPECTED_DIAGRAM_COUNTS!r}"
        )
    sources = _source_manifest()
    processes = {
        process: {
            "diagram_counts": EXPECTED_DIAGRAM_COUNTS[process],
            "process_card": f"cards/process_{process}.mg5",
            "process_directory": f"processes/{process}",
        }
        for process in PROCESSES
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
        "generator_backend": "madgraph5-pythia8-vpolar-standalone",
        "loop_reduction": dict(LOOP_REDUCTION),
        "physics": {
            "amplitude": "full Higgs plus continuum boxes plus interference",
            "final_state": "e+ e- mu+ mu-",
            "photon_diagrams": False,
            "z1_decay": "mu+ mu-",
            "z2_decay": "e+ e-",
            "mixed_sample_combination": "incoherent concatenation of separate TL and LT samples",
            "generator_mll_min_gev": 50.0,
            "generator_mll_max_gev": 200.0,
            "generator_m4l_min_gev": 150.0,
            "generator_m4l_max_gev": 3000.0,
            "ecm_energy_gev": 13600.0,
            "pdf_id": PDF_ID,
            "pdf_set": PDF_SET,
        },
        "sources": sources["sources"],
        "repository_inputs_sha256": _repository_hashes(),
        "installed_payload_sha256": _installed_fingerprints(prefix),
        "diagram_validation": diagram_report,
        "processes": processes,
        "lhapdf": _lhapdf_fingerprint(lhapdf_config, lhapdf_set_dir),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=str(output_path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return manifest


def validate_manifest(
    prefix: Path,
    manifest_path: Path | None = None,
    process: str | None = None,
) -> dict[str, Any]:
    prefix = prefix.expanduser().resolve()
    path = (manifest_path or prefix / MANIFEST_NAME).expanduser().resolve()
    manifest = _load_json(path, "installation manifest")
    expected_scalars = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
        "generator_backend": "madgraph5-pythia8-vpolar-standalone",
    }
    mismatches = [
        f"{key}={manifest.get(key)!r} (expected {value!r})"
        for key, value in expected_scalars.items()
        if manifest.get(key) != value
    ]
    if manifest.get("sources") != _source_manifest()["sources"]:
        mismatches.append("pinned sources differ from this checkout")
    if manifest.get("repository_inputs_sha256") != _repository_hashes():
        mismatches.append("repository installation inputs differ from this checkout")
    if manifest.get("loop_reduction") != LOOP_REDUCTION:
        mismatches.append("loop reduction contract is not optimized CutTools-only")
    processes = manifest.get("processes")
    if not isinstance(processes, dict) or set(processes) != set(PROCESSES):
        mismatches.append("process manifest does not contain exactly LL/TT/TL/LT")
    else:
        for process, expected in EXPECTED_DIAGRAM_COUNTS.items():
            if processes[process].get("diagram_counts") != expected:
                mismatches.append(
                    f"{process} diagram counts are not {expected}"
                )
    missing = [str(item) for item in _required_installation_paths(prefix) if not item.is_file()]
    if missing:
        mismatches.append("missing installed files: " + ", ".join(missing))
    try:
        selected_processes = (process,) if process is not None else PROCESSES
        installed_fingerprints = _installed_fingerprints(prefix, selected_processes)
    except ManifestError as error:
        mismatches.append(str(error))
    else:
        recorded_fingerprints = manifest.get("installed_payload_sha256")
        if not isinstance(recorded_fingerprints, dict):
            mismatches.append("installed generator payload fingerprint is missing")
        else:
            recorded_files = recorded_fingerprints.get("files")
            recorded_libraries = recorded_fingerprints.get("libraries")
            recorded_reduction = recorded_fingerprints.get("loop_reduction")
            recorded_bundles = recorded_fingerprints.get("process_bundles")
            recorded_trees = recorded_fingerprints.get("trees")
            if (
                recorded_files != installed_fingerprints["files"]
                or recorded_libraries != installed_fingerprints["libraries"]
                or recorded_reduction != installed_fingerprints["loop_reduction"]
                or not isinstance(recorded_bundles, dict)
                or not isinstance(recorded_trees, dict)
            ):
                mismatches.append("installed generator payload has changed")
            else:
                for label, value in installed_fingerprints["process_bundles"].items():
                    if recorded_bundles.get(label) != value:
                        mismatches.append(
                            f"installed process bundle has changed: {label}"
                        )
                for label, value in installed_fingerprints["trees"].items():
                    if recorded_trees.get(label) != value:
                        mismatches.append(
                            f"installed generator tree has changed: {label}"
                        )
    lhapdf = manifest.get("lhapdf")
    if (
        not isinstance(lhapdf, dict)
        or not isinstance(lhapdf.get("config_path"), str)
        or not isinstance(lhapdf.get("pdf_set_dir"), str)
    ):
        mismatches.append("invalid LHAPDF manifest")
    else:
        try:
            observed_lhapdf = _lhapdf_fingerprint(
                Path(lhapdf["config_path"]), Path(lhapdf["pdf_set_dir"])
            )
        except ManifestError as error:
            mismatches.append(str(error))
        else:
            if observed_lhapdf != lhapdf:
                mismatches.append("LHAPDF runtime payload has changed")
    if mismatches:
        raise ManifestError("incompatible VPolar installation: " + "; ".join(mismatches))

    # The patch checker verifies the exact MG version, pristine-base hash, and
    # installed runtime payload rather than trusting the JSON declaration.
    from loop_filter_patch import inspect_installation

    try:
        state = inspect_installation(prefix / "madgraph5")
    except Exception as error:
        raise ManifestError(f"MadGraph loop-filter validation failed: {error}") from error
    if not state["patched"] or not state["runtime_matches"]:
        raise ManifestError("MadGraph OAP loop filter is not installed")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--prefix", required=True, type=Path)
    create.add_argument("--lhapdf-config", required=True, type=Path)
    create.add_argument("--lhapdf-set-dir", required=True, type=Path)
    create.add_argument("--diagram-report", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--prefix", required=True, type=Path)
    validate.add_argument("--manifest", type=Path)
    validate.add_argument("--process", choices=PROCESSES)
    reduction_config = subparsers.add_parser("check-reduction-config")
    reduction_config.add_argument("--prefix", required=True, type=Path)
    pin_reduction = subparsers.add_parser("pin-reduction")
    pin_reduction.add_argument("--prefix", required=True, type=Path)
    pin_reduction.add_argument("--process", required=True, choices=PROCESSES)
    check_process = subparsers.add_parser("check-process")
    check_process.add_argument("--prefix", required=True, type=Path)
    check_process.add_argument("--process", required=True, choices=PROCESSES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "create":
        create_manifest(
            args.prefix,
            args.lhapdf_config,
            args.lhapdf_set_dir,
            args.diagram_report,
            args.output,
        )
        print(f"Created {args.output}")
    elif args.command == "validate":
        validate_manifest(args.prefix, args.manifest, args.process)
        print(f"Validated {args.prefix}")
    elif args.command == "check-reduction-config":
        _reduction_configuration_fingerprint(args.prefix.expanduser().resolve())
        print(f"Validated optimized CutTools configuration in {args.prefix}")
    elif args.command == "pin-reduction":
        path = pin_process_reduction(args.prefix, args.process)
        print(f"Pinned CutTools reduction in {path}")
    else:
        _process_bundle_fingerprint(args.prefix, args.process)
        print(f"Validated complete process bundle {args.process}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as error:
        raise SystemExit(f"installation manifest error: {error}") from error
