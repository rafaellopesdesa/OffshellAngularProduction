#!/usr/bin/env python3
"""Build-time metadata and safe extraction for native VPolar MG5 gridpacks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
from typing import Any, BinaryIO


CONTRACT = "oap-vpolar-mg5-gridpack-v1"
SCHEMA_VERSION = 1
BACKEND = "madgraph5-pythia8-vpolar-standalone"
PROCESSES = ("vpolar_LL", "vpolar_TT", "vpolar_TL", "vpolar_LT")
HERE = Path(__file__).resolve().parent
RUN_SETTINGS = HERE / "cards" / "run_settings.mg5"
MAX_MEMBERS = 500_000
MAX_UNCOMPRESSED_BYTES = 100 * 1024**3
MATERIALIZATION_CONTRACT = "oap-vpolar-external-symlink-materialization-v2"
MATERIALIZATION_SCHEMA_VERSION = 2
CUT_PARAMETERS = frozenset(
    """
    cut_decays cutuse deltaeta dparameter draa draamax drab drabmax draj
    drajmax dral dralmax drbb drbbmax drbj drbjmax drbl drblmax drjj
    drjjmax drjl drjlmax drll drllmax dsqrt_shat e_max_pdg e_min_pdg ea
    eamax eb ebmax ej ejmax el elmax eta_max_pdg eta_min_pdg etaa etaamin
    etab etabmin etaj etajmin etal etalmin ht2max ht2min ht3max ht3min
    ht4max ht4min htjmax htjmin ihtmax ihtmin ktdurham misset missetmax
    mmaa mmaamax mmbb mmbbmax mmjj mmjjmax mmll mmllmax mmnl mmnlmax
    mxx_min_pdg pt_max_pdg pt_min_pdg pta ptamax ptb ptbmax ptgmin
    ptheavy ptj ptj1max ptj1min ptj2max ptj2min ptj3max ptj3min ptj4max
    ptj4min ptjmax ptl ptl1max ptl1min ptl2max ptl2min ptl3max ptl3min
    ptl4max ptl4min ptllmax ptllmin ptlmax ptlund xetamin xpta xptb xptj
    xptl xqcut
    """.split()
)
NATIVE_INTEGRATION = {
    "engine": "MadGraph5_aMC-native-LO-gridpack",
    "madgraph_version": "3.4.2",
    "warmup_accuracy": 0.01,
    "warmup_points": 2000,
    "warmup_iterations": 8,
    "worker_mode": "GridPackCmd-refine4grid",
    "worker_parallelism": "serial",
    "mutable_worker_inputs": ["generated_lhe_events", "matrix_element_seed"],
}
PHYSICS = {
    "ecm_energy_gev": 13600.0,
    "pdf_id": 324900,
    "pdf_set": "NNPDF31_nlo_as_0118_luxqed",
    "dynamical_scale_choice": 3,
    "me_frame": [3, 4, 5, 6],
    "mll_min_gev": 50.0,
    "mll_max_gev": 200.0,
    "m4l_min_gev": 150.0,
    "m4l_max_gev": 3000.0,
    "event_norm": "average",
    "use_syst": False,
    "matching": "none",
    "automatic_pt_eta_dr_cuts": False,
    "loop_reduction": "CutTools",
    "madloop_reduction_lib": "1",
    "python_channel_selection_seed": "matrix_element_seed",
}


class GridpackError(RuntimeError):
    """Raised for an unsafe, incomplete, or incompatible gridpack."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_sha256(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GridpackError(f"cannot read {label}: {path}") from error
    if not isinstance(payload, dict):
        raise GridpackError(f"{label} must be a JSON object")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise GridpackError(f"refusing to overwrite metadata: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _member_name(raw: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw:
        raise GridpackError(f"unsafe archive path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GridpackError(f"unsafe archive path: {raw!r}")
    return path.as_posix()


def _resolve_relative(base: PurePosixPath, raw: str, label: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw:
        raise GridpackError(f"unsafe {label}: {raw!r}")
    target = PurePosixPath(raw)
    if target.is_absolute():
        raise GridpackError(f"absolute {label} is forbidden: {raw!r}")
    parts: list[str] = list(base.parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise GridpackError(f"{label} escapes the archive: {raw!r}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise GridpackError(f"{label} resolves to the archive root: {raw!r}")
    return PurePosixPath(*parts).as_posix()


def _link_target(member: tarfile.TarInfo) -> str:
    base = PurePosixPath() if member.islnk() else PurePosixPath(member.name).parent
    return _resolve_relative(base, member.linkname, f"link target for {member.name}")


def _read_regular(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    *,
    limit: int = 10 * 1024 * 1024,
) -> bytes:
    member = members.get(name)
    if member is None or not member.isreg() or member.size <= 0:
        raise GridpackError(f"gridpack is missing nonempty regular file {name}")
    if member.size > limit:
        raise GridpackError(f"gridpack control file is unexpectedly large: {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise GridpackError(f"could not read gridpack member {name}")
    with stream:
        data = stream.read(limit + 1)
    if len(data) != member.size:
        raise GridpackError(f"short read from gridpack member {name}")
    return data


def _hash_regular_member(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> str:
    member = members.get(name)
    if member is None or not member.isreg() or member.size <= 0:
        raise GridpackError(f"gridpack is missing nonempty regular file {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise GridpackError(f"could not read gridpack member {name}")
    digest = hashlib.sha256()
    copied = 0
    with stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            digest.update(chunk)
    if copied != member.size:
        raise GridpackError(f"short read from gridpack member {name}")
    return digest.hexdigest()


def _assignments(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        active = raw_line.split("!", 1)[0].split("#", 1)[0].strip()
        if not active or active.startswith("#") or "=" not in active:
            continue
        value, key = active.split("=", 1)
        key = key.strip().lower()
        if key in values:
            raise GridpackError(f"duplicate run-card key {key} at line {line_number}")
        values[key] = value.strip()
    return values


def _number(values: dict[str, str], key: str) -> float:
    try:
        value = float(values[key].lower().replace("d", "e"))
    except (KeyError, ValueError) as error:
        raise GridpackError(f"gridpack run card has invalid {key}") from error
    if not math.isfinite(value):
        raise GridpackError(f"gridpack run-card value {key} is not finite")
    return value


def _token(values: dict[str, str], key: str) -> str:
    try:
        return values[key].strip().strip("'\"").lower()
    except KeyError as error:
        raise GridpackError(f"gridpack run card is missing {key}") from error


def _canonical_run_value(raw: str) -> str:
    value = raw.strip().strip("'\"").lower()
    if value in {"true", ".true.", "t"}:
        return "true"
    if value in {"false", ".false.", "f"}:
        return "false"
    if value.startswith(("[", "{")):
        return re.sub(r"\s+", "", value)
    return _canonical_numeric_token(value)


def _validate_run_card(text: str) -> dict[str, int]:
    values = _assignments(text)
    expected = {
        "lpp1": 1,
        "lpp2": 1,
        "ebeam1": 6800,
        "ebeam2": 6800,
        "lhaid": 324900,
        "dynamical_scale_choice": 3,
        "ickkw": 0,
        "lhe_version": 3,
        "mmll": 50,
        "mmllmax": 200,
        "mmnl": 150,
        "mmnlmax": 3000,
        "python_seed": -2,
        "scalefact": 1,
        "nhel": 0,
        "sde_strategy": 1,
        "bwcutoff": 15,
        "maxjetflavor": 4,
    }
    for key, wanted in expected.items():
        observed = _number(values, key)
        if not math.isclose(observed, wanted, rel_tol=0.0, abs_tol=1.0e-12):
            raise GridpackError(
                f"gridpack run-card value {key}={observed!r}; expected {wanted!r}"
            )
    if _token(values, "gridpack") not in {"true", ".true.", "t"}:
        raise GridpackError("gridpack run card does not enable native gridpack mode")
    if _token(values, "pdlabel") != "lhapdf":
        raise GridpackError("gridpack run card does not use LHAPDF")
    if _token(values, "event_norm") != "average":
        raise GridpackError("gridpack run card does not use event_norm=average")
    if _token(values, "use_syst") not in {"false", ".false.", "f"}:
        raise GridpackError("gridpack run card did not disable systematics")
    for key in ("fixed_ren_scale", "fixed_fac_scale1", "fixed_fac_scale2"):
        if key in values and _canonical_run_value(values[key]) != "false":
            raise GridpackError(f"gridpack run card unexpectedly enabled {key}")
    frame = _token(values, "me_frame").replace("[", "").replace("]", "")
    if frame.replace(" ", "") != "3,4,5,6":
        raise GridpackError(f"gridpack run card has unexpected me_frame={frame!r}")
    explicit_cut_values = {
        "mmll": 50.0,
        "mmllmax": 200.0,
        "mmnl": 150.0,
        "mmnlmax": 3000.0,
    }
    dict_cuts = {
        "e_max_pdg",
        "e_min_pdg",
        "eta_max_pdg",
        "eta_min_pdg",
        "mxx_min_pdg",
        "pt_max_pdg",
        "pt_min_pdg",
    }
    for key in CUT_PARAMETERS:
        if key not in values:
            continue
        if key in explicit_cut_values:
            continue
        raw = values[key]
        if key in dict_cuts:
            if _canonical_run_value(raw) != "{}":
                raise GridpackError(f"gridpack retained unintended cut {key}={raw!r}")
            continue
        if key == "cut_decays":
            if _canonical_run_value(raw) != "false":
                raise GridpackError(f"gridpack retained unintended cut {key}={raw!r}")
            continue
        wanted = 0.0
        if "min" in key:
            wanted = 0.0
        elif "max" in key or "eta" in key:
            wanted = -1.0
        try:
            numeric = float(raw.lower().replace("d", "e"))
        except ValueError as error:
            raise GridpackError(
                f"cannot prove gridpack cut {key}={raw!r} is disabled"
            ) from error
        if not math.isclose(numeric, wanted, rel_tol=0.0, abs_tol=1.0e-12):
            raise GridpackError(
                f"gridpack retained unintended cut {key}={raw!r}; expected {wanted}"
            )
    build_events_value = _number(values, "nevents")
    archived_seed_value = _number(values, "iseed")
    if not build_events_value.is_integer() or not archived_seed_value.is_integer():
        raise GridpackError("gridpack build nevents and archived iseed must be integers")
    build_events = int(build_events_value)
    archived_seed = int(archived_seed_value)
    if build_events != 10000:
        raise GridpackError("gridpack build nevents must be the frozen value 10000")
    # MG5 consumes the requested survey seed and then deliberately rewrites
    # iseed to zero before creating a native gridpack.  Per-job seeds are
    # supplied through grid_card.dat by run.sh, never through this frozen card.
    if archived_seed != 0:
        raise GridpackError(
            f"native gridpack run card retained iseed={archived_seed}; expected 0"
        )
    return {"build_nevents": build_events, "archived_run_card_seed": archived_seed}


def _validate_frozen_grid_card(text: str) -> None:
    values = _assignments(text)
    if _token(values, "gridrun") not in {"true", ".true.", "t"}:
        raise GridpackError("native gridpack does not freeze GridRun=true")


def _madloop_reduction(text: str) -> str:
    lines = text.splitlines()
    markers = [i for i, line in enumerate(lines) if line.strip().lower() == "#mlreductionlib"]
    if len(markers) != 1 or markers[0] + 1 >= len(lines):
        raise GridpackError("gridpack MadLoop card has no unique MLReductionLib entry")
    value = lines[markers[0] + 1].strip()
    if value != "1":
        raise GridpackError(f"gridpack MadLoop reduction is {value!r}, expected CutTools ID 1")
    return value


def _canonical_numeric_token(token: str) -> str:
    """Normalize ordinary SLHA numeric spelling without losing precision."""

    candidate = token.strip().lower().replace("d", "e")
    try:
        value = float(candidate)
    except ValueError:
        return candidate
    if not math.isfinite(value):
        raise GridpackError(f"non-finite value in parameter card: {token!r}")
    if value == 0.0:
        return "0"
    return format(value, ".17g")


def _param_card_contract(text: str) -> dict[str, Any]:
    """Fingerprint SLHA semantics while isolating MG5's allowed PDF alpha_s update."""

    section: tuple[str, str] | None = None
    records: list[list[str]] = []
    alpha_s_values: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        active = raw_line.split("#", 1)[0].strip()
        if not active:
            continue
        fields = active.split()
        keyword = fields[0].lower()
        if keyword == "block":
            if len(fields) < 2:
                raise GridpackError(f"invalid BLOCK header at parameter-card line {line_number}")
            section = ("block", fields[1].lower())
            records.append(
                ["header", *section]
                + [_canonical_numeric_token(value) for value in fields[2:]]
            )
            continue
        if keyword == "decay":
            if len(fields) != 3:
                raise GridpackError(f"invalid DECAY header at parameter-card line {line_number}")
            section = ("decay", _canonical_numeric_token(fields[1]))
            records.append(
                ["header", *section, _canonical_numeric_token(fields[2])]
            )
            continue
        if section is None or len(fields) < 2:
            raise GridpackError(
                f"parameter-card entry outside a BLOCK/DECAY at line {line_number}"
            )
        normalized = [_canonical_numeric_token(value) for value in fields]
        if section == ("block", "sminputs") and normalized[0] == "3":
            if len(normalized) != 2:
                raise GridpackError("invalid SMINPUTS(3) parameter-card entry")
            alpha_s_values.append(normalized[1])
            continue
        records.append(["entry", *section, *normalized])
    if len(alpha_s_values) > 1:
        raise GridpackError("parameter card contains duplicate SMINPUTS(3) entries")
    records.sort()
    return {
        "semantics_without_pdf_alpha_sha256": _json_sha256(records),
        "alpha_s_mz": alpha_s_values[0] if alpha_s_values else None,
    }


def _symfact_channels(text: str, subprocess: str) -> list[str]:
    """Return exactly the G directories MadEvent will consume for one P dir."""

    factors: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 2:
            raise GridpackError(
                f"invalid {subprocess}/symfact.dat line {line_number}"
            )
        tag, factor_text = fields
        if (
            re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", tag, flags=re.ASCII) is None
            or float(tag) <= 0
            or tag in factors
        ):
            raise GridpackError(
                f"invalid or duplicate channel tag in {subprocess}/symfact.dat: {tag!r}"
            )
        try:
            factor = int(factor_text)
        except ValueError as error:
            raise GridpackError(
                f"invalid multiplicity in {subprocess}/symfact.dat: {factor_text!r}"
            ) from error
        factors[tag] = factor
    expected = [f"G{tag}" for tag, factor in factors.items() if factor > 0]
    if not expected:
        raise GridpackError(f"gridpack has no positive channel in {subprocess}/symfact.dat")
    return expected


def _inspect_tar(archive: tarfile.TarFile) -> dict[str, Any]:
    members: dict[str, tarfile.TarInfo] = {}
    total_size = 0
    links: dict[str, str] = {}
    # Iterate lazily so member and size limits are enforced while tar headers
    # are read rather than after getmembers() has accepted the entire archive.
    for member_count, member in enumerate(archive, start=1):
        if member_count > MAX_MEMBERS:
            raise GridpackError("gridpack has too many archive members")
        name = _member_name(member.name)
        member.name = name
        if name in members:
            raise GridpackError(f"duplicate archive member: {name}")
        if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
            raise GridpackError(f"unsupported archive member type: {name}")
        if member.isreg():
            total_size += member.size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise GridpackError("gridpack uncompressed payload exceeds safety limit")
        members[name] = member
    if not members:
        raise GridpackError("gridpack has no archive members")

    # A later symlink declaration must not turn an earlier child extraction into
    # a path escape. Every explicitly represented ancestor must be a directory.
    for name in members:
        path = PurePosixPath(name)
        for index in range(1, len(path.parts)):
            ancestor = PurePosixPath(*path.parts[:index]).as_posix()
            if ancestor in members and not members[ancestor].isdir():
                raise GridpackError(
                    f"archive member {name} is nested below non-directory {ancestor}"
                )

    for name, member in members.items():
        if member.issym() or member.islnk():
            target = _link_target(member)
            if target not in members:
                raise GridpackError(f"archive link {name} has missing target {target}")
            links[name] = target

    def endpoint(name: str) -> tarfile.TarInfo:
        seen: set[str] = set()
        while members[name].issym() or members[name].islnk():
            if name in seen:
                raise GridpackError(f"archive contains a link cycle at {name}")
            seen.add(name)
            name = _link_target(members[name])
        return members[name]

    for name, member in members.items():
        if member.islnk() and not endpoint(name).isreg():
            raise GridpackError(f"hard link does not resolve to a regular file: {name}")
        if member.issym() and not (endpoint(name).isreg() or endpoint(name).isdir()):
            raise GridpackError(f"symbolic link has unsupported endpoint: {name}")

    required_executables = ("run.sh", "madevent/bin/gridrun")
    for name in required_executables:
        member = members.get(name)
        if member is None or not member.isreg() or not (member.mode & 0o111):
            raise GridpackError(f"gridpack is missing executable regular file {name}")
    materialized_dependency_names = (
        "madevent/lib/libcts.a",
        "madevent/lib/mpmodule.mod",
        "madevent/lib/libiregi.a",
        "madevent/lib/libLHAPDF.a",
    )
    for name in materialized_dependency_names:
        member = members.get(name)
        if member is None or not member.isreg() or member.size <= 0:
            raise GridpackError(
                f"gridpack did not materialize external build dependency {name}"
            )

    control_names = {
        "run": "madevent/Cards/run_card.dat",
        "param": "madevent/Cards/param_card.dat",
        "process": "madevent/Cards/proc_card_mg5.dat",
        "madloop": "madevent/Cards/MadLoopParams.dat",
        "grid": "madevent/Cards/grid_card.dat",
        "subprocesses": "madevent/SubProcesses/subproc.mg",
    }
    controls = {
        role: _read_regular(archive, members, name)
        for role, name in control_names.items()
    }
    try:
        run_text = controls["run"].decode("utf-8")
        param_text = controls["param"].decode("utf-8")
        grid_text = controls["grid"].decode("utf-8")
        subprocess_text = controls["subprocesses"].decode("utf-8")
        madloop_text = controls["madloop"].decode("utf-8")
    except UnicodeError as error:
        raise GridpackError("gridpack control cards must be UTF-8") from error
    run_build = _validate_run_card(run_text)
    _validate_frozen_grid_card(grid_text)
    param_contract = _param_card_contract(param_text)
    _madloop_reduction(madloop_text)

    subprocesses = [line.strip() for line in subprocess_text.splitlines() if line.strip()]
    if not subprocesses or len(subprocesses) != len(set(subprocesses)):
        raise GridpackError("gridpack subprocess manifest is empty or duplicated")
    grid_directories: dict[str, list[str]] = {}
    for subprocess_name in subprocesses:
        if PurePosixPath(subprocess_name).name != subprocess_name or not subprocess_name.startswith("P"):
            raise GridpackError(f"unsafe subprocess name in gridpack: {subprocess_name!r}")
        prefix = f"madevent/SubProcesses/{subprocess_name}/"
        symfact_name = prefix + "symfact.dat"
        try:
            symfact_text = _read_regular(archive, members, symfact_name).decode("utf-8")
        except UnicodeError as error:
            raise GridpackError(
                f"gridpack {subprocess_name}/symfact.dat is not UTF-8"
            ) from error
        expected_names = _symfact_channels(symfact_text, subprocess_name)
        expected_dirs = {prefix + name for name in expected_names}
        observed_dirs = {
            prefix + relative.parts[0]
            for name in members
            if name.startswith(prefix)
            for relative in [PurePosixPath(name.removeprefix(prefix))]
            if relative.parts and relative.parts[0].startswith("G")
        }
        if observed_dirs != expected_dirs:
            missing = sorted(expected_dirs - observed_dirs)
            extra = sorted(observed_dirs - expected_dirs)
            raise GridpackError(
                f"gridpack channel directories disagree with {subprocess_name}/symfact.dat; "
                f"missing={missing}, extra={extra}"
            )
        for directory in sorted(expected_dirs):
            for filename in ("default_results.dat", "default_ftn26.gz"):
                path = f"{directory}/{filename}"
                member = members.get(path)
                if member is None or not member.isreg() or member.size <= 0:
                    raise GridpackError(
                        f"gridpack is missing frozen integration artifact {path}"
                    )
        grid_directories[subprocess_name] = sorted(expected_dirs)

    loop_resources = [
        name
        for name, member in members.items()
        if member.isreg()
        and member.size > 0
        and (
            name.startswith("madevent/SubProcesses/MadLoop5_resources/")
            or name == "madevent/SubProcesses/MadLoop5_resources.tar.gz"
        )
    ]
    if not loop_resources:
        raise GridpackError("gridpack contains no MadLoop5 initialization resources")

    # Native do_create_gridpack invokes bin/internal/clean (not clean4grid), so
    # the complete substantive exported model/matrix source bundle survives.
    # Hash every artifact installation_manifest records and also require each
    # compiled per-subprocess worker executable.
    immutable_artifact_names = {
        "bin/generate_events",
        "bin/internal/madevent_interface.py",
        "bin/internal/common_run_interface.py",
        "bin/internal/gen_ximprove.py",
        "Source/MODEL/model_functions.f",
        "Cards/MadLoopParams.dat",
        "lib/libcts.a",
        "lib/mpmodule.mod",
        "lib/libiregi.a",
        "SubProcesses/subproc.mg",
    }
    for subprocess_name in subprocesses:
        base = f"SubProcesses/{subprocess_name}/"
        immutable_artifact_names.update(
            base + filename
            for filename in (
                "CT_interface.f",
                "loop_matrix.f",
                "polynomial.f",
                "proc_prefix.txt",
            )
        )
        matrix_names = {
            name.removeprefix("madevent/")
            for name, member in members.items()
            if name.startswith("madevent/" + base + "matrix")
            and name.endswith(".f")
            and member.isreg()
        }
        if not matrix_names:
            raise GridpackError(
                f"gridpack has no matrix source for subprocess {subprocess_name}"
            )
        immutable_artifact_names.update(matrix_names)
        worker_name = f"madevent/SubProcesses/{subprocess_name}/madevent"
        worker = members.get(worker_name)
        if (
            worker is None
            or not worker.isreg()
            or worker.size <= 0
            or not (worker.mode & 0o111)
        ):
            raise GridpackError(
                f"gridpack has no executable worker for subprocess {subprocess_name}"
            )
    immutable_artifacts = {
        name: _hash_regular_member(archive, members, "madevent/" + name)
        for name in sorted(immutable_artifact_names)
    }

    runtime_entrypoint_names = {
        "run.sh": "run.sh",
        "gridrun": "madevent/bin/gridrun",
    }

    return {
        "member_count": len(members),
        "regular_file_count": sum(member.isreg() for member in members.values()),
        "uncompressed_bytes": total_size,
        "links": dict(sorted(links.items())),
        "subprocesses": subprocesses,
        "integration_grid_directories": grid_directories,
        "madloop_resource_file_count": len(loop_resources),
        "immutable_process_artifacts_sha256": immutable_artifacts,
        "runtime_entrypoints_sha256": {
            role: _hash_regular_member(archive, members, name)
            for role, name in runtime_entrypoint_names.items()
        },
        "cards_sha256": {
            role: hashlib.sha256(payload).hexdigest()
            for role, payload in sorted(controls.items())
            if role != "subprocesses"
        },
        "param_card_contract": param_contract,
        "materialized_dependencies_sha256": {
            name: _hash_regular_member(archive, members, name)
            for name in materialized_dependency_names
        },
        **run_build,
    }


def inspect_gridpack(path: Path) -> dict[str, Any]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            return _inspect_tar(archive)
    except (OSError, tarfile.TarError) as error:
        raise GridpackError(f"could not read gzip tar gridpack {path}: {error}") from error


def _validated_installation(prefix: Path, process: str) -> tuple[dict[str, Any], Path]:
    if process not in PROCESSES:
        raise GridpackError(f"unknown VPolar process {process!r}")
    prefix = prefix.expanduser().resolve()
    sys.path.insert(0, str(HERE))
    try:
        import installation_manifest

        manifest = installation_manifest.validate_manifest(prefix, process=process)
    except Exception as error:
        raise GridpackError(f"incompatible VPolar installation: {error}") from error
    finally:
        try:
            sys.path.remove(str(HERE))
        except ValueError:
            pass
    return manifest, prefix / "installation-manifest.json"


def _configuration_contract(prefix: Path, process: str) -> dict[str, Any]:
    cards = prefix / "processes" / process / "Cards"
    required = {
        "process": cards / "proc_card_mg5.dat",
        "param": cards / "param_card.dat",
        "madloop": cards / "MadLoopParams.dat",
    }
    for role, path in required.items():
        if not path.is_file() or path.stat().st_size <= 0:
            raise GridpackError(f"installation has no {role} card: {path}")
    try:
        installed_param_contract = _param_card_contract(
            required["param"].read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError) as error:
        raise GridpackError(
            f"cannot read installed parameter card: {required['param']}"
        ) from error
    return {
        "physics": PHYSICS,
        "integration": NATIVE_INTEGRATION,
        "repository_run_settings_sha256": sha256(RUN_SETTINGS),
        "installed_cards_sha256": {
            role: sha256(path) for role, path in sorted(required.items())
        },
        "installed_param_card_contract": installed_param_contract,
    }


def _validate_materialization_records(
    records: Any,
    inspection: dict[str, Any],
    prefix: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise GridpackError("invalid external-link materialization records")
    by_path: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "source_scope",
            "source",
            "kind",
            "sha256",
        }:
            raise GridpackError("invalid external-link materialization record")
        path = record.get("path")
        source_scope = record.get("source_scope")
        source = record.get("source")
        digest = record.get("sha256")
        if (
            not isinstance(path, str)
            or _member_name(path) != path
            or path in by_path
            or not isinstance(source, str)
            or source_scope
            not in {"validated-generator-prefix", "validated-lhapdf-static-library"}
            or record.get("kind") != "file"
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise GridpackError("invalid external-link materialization record values")
        by_path[path] = record
    prefix = prefix.expanduser().resolve()
    lhapdf_static = manifest["lhapdf"]["static_library"]
    expected = {
        "lib/libcts.a": {
            "path": "lib/libcts.a",
            "source_scope": "validated-generator-prefix",
            "source": "madgraph5/vendor/CutTools/includects/libcts.a",
            "kind": "file",
            "sha256": inspection["materialized_dependencies_sha256"]
            ["madevent/lib/libcts.a"],
        },
        "lib/mpmodule.mod": {
            "path": "lib/mpmodule.mod",
            "source_scope": "validated-generator-prefix",
            "source": "madgraph5/vendor/CutTools/includects/mpmodule.mod",
            "kind": "file",
            "sha256": inspection["materialized_dependencies_sha256"]
            ["madevent/lib/mpmodule.mod"],
        },
        "lib/libiregi.a": {
            "path": "lib/libiregi.a",
            "source_scope": "validated-generator-prefix",
            "source": "madgraph5/vendor/IREGI/src/libiregi.a",
            "kind": "file",
            "sha256": inspection["materialized_dependencies_sha256"]
            ["madevent/lib/libiregi.a"],
        },
        "lib/libLHAPDF.a": {
            "path": "lib/libLHAPDF.a",
            "source_scope": "validated-lhapdf-static-library",
            "source": str(Path(lhapdf_static["path"]).expanduser().resolve()),
            "kind": "file",
            "sha256": inspection["materialized_dependencies_sha256"]
            ["madevent/lib/libLHAPDF.a"],
        },
    }
    if by_path != expected:
        missing = sorted(set(expected) - set(by_path))
        extra = sorted(set(by_path) - set(expected))
        raise GridpackError(
            "external-link materialization proof differs from the required "
            f"CutTools/IREGI/LHAPDF payload; missing={missing}, extra={extra}"
        )
    for path, record in expected.items():
        if record["source_scope"] == "validated-generator-prefix":
            source_path = prefix / record["source"]
        else:
            source_path = Path(record["source"])
        if not source_path.is_file() or sha256(source_path) != record["sha256"]:
            raise GridpackError(
                f"materialized source no longer matches validated payload for {path}"
            )
    return records


def _validate_normalization_records(
    records: Any, inspection: dict[str, Any]
) -> list[dict[str, str]]:
    if not isinstance(records, list):
        raise GridpackError("invalid internal-link normalization records")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "target"}:
            raise GridpackError("invalid internal-link normalization record")
        path = record.get("path")
        target = record.get("target")
        if (
            not isinstance(path, str)
            or _member_name(path) != path
            or path in seen
            or not isinstance(target, str)
            or _member_name(target) != target
            or inspection["links"].get("madevent/" + path) != "madevent/" + target
        ):
            raise GridpackError("internal-link normalization proof differs from archive")
        seen.add(path)
    return records


def _expected_dependency_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    installed = manifest["installed_payload_sha256"]["files"]
    return {
        "madevent/lib/libcts.a": installed[
            "madgraph5/vendor/CutTools/includects/libcts.a"
        ],
        "madevent/lib/mpmodule.mod": installed[
            "madgraph5/vendor/CutTools/includects/mpmodule.mod"
        ],
        "madevent/lib/libiregi.a": installed[
            "madgraph5/vendor/IREGI/src/libiregi.a"
        ],
        "madevent/lib/libLHAPDF.a": manifest["lhapdf"]["static_library"][
            "sha256"
        ],
    }


def _archive_installation_mismatches(
    inspection: dict[str, Any],
    contract: dict[str, Any],
    manifest: dict[str, Any],
    process: str,
) -> list[str]:
    mismatches: list[str] = []
    if {
        role: inspection["cards_sha256"].get(role)
        for role in ("process", "madloop")
    } != {
        role: contract["installed_cards_sha256"][role]
        for role in ("process", "madloop")
    }:
        mismatches.append(
            "gridpack process or MadLoop card differs from validated installation"
        )
    observed_param = inspection["param_card_contract"]
    installed_param = contract["installed_param_card_contract"]
    if observed_param["semantics_without_pdf_alpha_sha256"] != (
        installed_param["semantics_without_pdf_alpha_sha256"]
    ):
        mismatches.append(
            "gridpack parameter-card semantics differ from validated installation"
        )
    observed_alpha = observed_param["alpha_s_mz"]
    installed_alpha = installed_param["alpha_s_mz"]
    pdf_alpha = manifest["lhapdf"].get("alpha_s_mz")
    try:
        observed_alpha_value = float(observed_alpha)
        allowed_alpha_values = [
            float(value) for value in (installed_alpha, pdf_alpha) if value is not None
        ]
    except (TypeError, ValueError):
        observed_alpha_value = math.nan
        allowed_alpha_values = []
    if not allowed_alpha_values:
        if observed_alpha is not None or installed_alpha is not None:
            mismatches.append("gridpack parameter card has invalid alpha_s(MZ)")
    elif not any(
        math.isclose(observed_alpha_value, value, rel_tol=1.0e-10, abs_tol=1.0e-12)
        for value in allowed_alpha_values
    ):
        mismatches.append(
            "gridpack parameter-card alpha_s(MZ) is neither the installed nor PDF value"
        )

    process_bundle = manifest["installed_payload_sha256"]["process_bundles"][process]
    expected_artifacts = process_bundle["artifacts"]
    observed_artifacts = inspection["immutable_process_artifacts_sha256"]
    if any(
        observed_artifacts.get(path) != digest
        for path, digest in expected_artifacts.items()
    ):
        mismatches.append(
            "gridpack process model/matrix bundle differs from validated installation"
        )
    if inspection["subprocesses"] != process_bundle["subprocesses"]:
        mismatches.append("gridpack subprocess bundle differs from validated installation")

    installed_files = manifest["installed_payload_sha256"]["files"]
    expected_entrypoints = {
        "run.sh": installed_files[
            "madgraph5/Template/LO/bin/internal/Gridpack/run.sh"
        ],
        "gridrun": installed_files[
            "madgraph5/Template/LO/bin/internal/Gridpack/gridrun"
        ],
    }
    if inspection["runtime_entrypoints_sha256"] != expected_entrypoints:
        mismatches.append(
            "gridpack native runtime entrypoints differ from validated MG5 templates"
        )
    if inspection["materialized_dependencies_sha256"] != (
        _expected_dependency_hashes(manifest)
    ):
        mismatches.append(
            "gridpack CutTools or static LHAPDF payload differs from validated installation"
        )
    return mismatches


def create_metadata(
    gridpack: Path,
    metadata: Path,
    prefix: Path,
    process: str,
    *,
    build_seed: int,
    build_cores: int,
    materialization_report: Path | None = None,
) -> dict[str, Any]:
    if build_seed <= 0 or build_cores <= 0:
        raise GridpackError("gridpack build seed and core count must be positive")
    gridpack = gridpack.expanduser().resolve(strict=True)
    metadata = metadata.expanduser().resolve()
    manifest, manifest_path = _validated_installation(prefix, process)
    prefix = prefix.expanduser().resolve()
    inspection = inspect_gridpack(gridpack)
    contract = _configuration_contract(prefix, process)
    archive_mismatches = _archive_installation_mismatches(
        inspection, contract, manifest, process
    )
    if archive_mismatches:
        raise GridpackError("; ".join(archive_mismatches))
    if materialization_report is None:
        raise GridpackError("external-link materialization report is required")
    report = _load_json(materialization_report, "symlink materialization report")
    if (
        report.get("schema_version") != MATERIALIZATION_SCHEMA_VERSION
        or report.get("contract") != MATERIALIZATION_CONTRACT
    ):
        raise GridpackError("invalid symlink materialization report contract")
    materialization_records = _validate_materialization_records(
        report.get("materialized"), inspection, prefix, manifest
    )
    normalization_records = _validate_normalization_records(
        report.get("normalized_internal_absolute_links", []), inspection
    )
    installed_payload = manifest["installed_payload_sha256"]
    process_tree = installed_payload["trees"][process]
    process_bundle = installed_payload["process_bundles"][process]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
        "generator_backend": BACKEND,
        "process": process,
        "gridpack": {
            "sha256": sha256(gridpack),
            "size_bytes": gridpack.stat().st_size,
            "format": "native-mg5-lo-gridpack-tar-gzip",
            "inspection": inspection,
        },
        "configuration": contract,
        "installation": {
            "contract": manifest["contract"],
            "schema_version": manifest["schema_version"],
            "manifest_sha256": sha256(manifest_path),
            "process_tree": process_tree,
            "process_bundle_sha256": _json_sha256(process_bundle),
        },
        "build": {
            "seed": build_seed,
            "requested_cores": build_cores,
            "external_process_symlinks_materialized": materialization_records,
            "internal_absolute_symlinks_normalized": normalization_records,
        },
        "runtime_dependencies": {
            "validated_generator_prefix_required": True,
            "matrix_element_archive_self_contains_cuttools": True,
            "matrix_element_archive_self_contains_iregi": True,
            "matrix_element_archive_self_contains_static_lhapdf": True,
            "shower_and_lhapdf_runtime_from_validated_installation": True,
            "installed_output_dependencies_setting": "external",
        },
    }
    _write_json_atomic(metadata, payload)
    return payload


def validate_metadata(
    gridpack: Path, metadata: Path, prefix: Path, process: str
) -> dict[str, Any]:
    gridpack = gridpack.expanduser().resolve(strict=True)
    metadata = metadata.expanduser().resolve(strict=True)
    manifest, manifest_path = _validated_installation(prefix, process)
    prefix = prefix.expanduser().resolve()
    payload = _load_json(metadata, "gridpack metadata")
    inspection = inspect_gridpack(gridpack)
    contract = _configuration_contract(prefix, process)
    installed_payload = manifest["installed_payload_sha256"]
    expected = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
        "generator_backend": BACKEND,
        "process": process,
    }
    mismatches = [
        f"{key}={payload.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if payload.get(key) != value
    ]
    archive_record = payload.get("gridpack")
    if not isinstance(archive_record, dict):
        mismatches.append("missing gridpack archive record")
    else:
        if archive_record.get("sha256") != sha256(gridpack):
            mismatches.append("gridpack SHA-256 differs")
        if archive_record.get("size_bytes") != gridpack.stat().st_size:
            mismatches.append("gridpack size differs")
        if archive_record.get("inspection") != inspection:
            mismatches.append("gridpack structure/cards differ")
    if payload.get("configuration") != contract:
        mismatches.append("physics or integration configuration differs")
    mismatches.extend(
        _archive_installation_mismatches(inspection, contract, manifest, process)
    )
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        mismatches.append("missing installation binding")
    else:
        expected_installation = {
            "contract": manifest["contract"],
            "schema_version": manifest["schema_version"],
            "manifest_sha256": sha256(manifest_path),
            "process_tree": installed_payload["trees"][process],
            "process_bundle_sha256": _json_sha256(
                installed_payload["process_bundles"][process]
            ),
        }
        if installation != expected_installation:
            mismatches.append("validated installation/process binding differs")
    requirements = payload.get("runtime_dependencies")
    if requirements != {
        "validated_generator_prefix_required": True,
        "matrix_element_archive_self_contains_cuttools": True,
        "matrix_element_archive_self_contains_iregi": True,
        "matrix_element_archive_self_contains_static_lhapdf": True,
        "shower_and_lhapdf_runtime_from_validated_installation": True,
        "installed_output_dependencies_setting": "external",
    }:
        mismatches.append("runtime dependency contract is incomplete")
    build = payload.get("build")
    if (
        not isinstance(build, dict)
        or not isinstance(build.get("seed"), int)
        or isinstance(build.get("seed"), bool)
        or build["seed"] <= 0
        or not isinstance(build.get("requested_cores"), int)
        or isinstance(build.get("requested_cores"), bool)
        or build["requested_cores"] <= 0
    ):
        mismatches.append("gridpack build provenance is invalid")
    elif isinstance(build, dict):
        try:
            _validate_materialization_records(
                build.get("external_process_symlinks_materialized"),
                inspection,
                prefix,
                manifest,
            )
            _validate_normalization_records(
                build.get("internal_absolute_symlinks_normalized", []), inspection
            )
        except GridpackError:
            mismatches.append("gridpack external-link materialization proof differs")
    if isinstance(archive_record, dict) and archive_record.get("format") != (
        "native-mg5-lo-gridpack-tar-gzip"
    ):
        mismatches.append("gridpack archive format declaration differs")
    if mismatches:
        raise GridpackError("incompatible VPolar gridpack: " + "; ".join(mismatches))
    return payload


def safe_extract_gridpack(
    gridpack: Path,
    output: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    gridpack = gridpack.expanduser().resolve(strict=True)
    output = output.expanduser().resolve()
    if output.exists():
        raise GridpackError(f"refusing to extract into existing path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with gridpack.open("rb") as stream:
        before = os.fstat(stream.fileno())
        observed_sha256 = _stream_sha256(stream)
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise GridpackError("gridpack changed after metadata validation")
        stream.seek(0)
        try:
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                inspection = _inspect_tar(archive)
                members = {member.name: member for member in archive.getmembers()}
                output.mkdir(mode=0o700)
                try:
                    # Extract without tarfile.extractall/filter so the same code
                    # is safe on Python 3.10. Links are created last, and the
                    # complete archive was already proven to have no member
                    # nested below a link or another non-directory.
                    directories = sorted(
                        (member for member in members.values() if member.isdir()),
                        key=lambda member: len(PurePosixPath(member.name).parts),
                    )
                    for member in directories:
                        (output / member.name).mkdir(parents=True, exist_ok=True)
                    for member in members.values():
                        if not member.isreg():
                            continue
                        destination = output / member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source = archive.extractfile(member)
                        if source is None:
                            raise GridpackError(
                                f"could not read archive member during extraction: {member.name}"
                            )
                        copied = 0
                        with source, destination.open("xb") as target:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                target.write(chunk)
                                copied += len(chunk)
                        if copied != member.size:
                            raise GridpackError(
                                f"short extraction of archive member {member.name}"
                            )
                        destination.chmod(member.mode & 0o777)

                    def regular_endpoint(name: str) -> str:
                        seen: set[str] = set()
                        while members[name].issym() or members[name].islnk():
                            if name in seen:
                                raise GridpackError(f"archive link cycle at {name}")
                            seen.add(name)
                            name = _link_target(members[name])
                        return name

                    for member in members.values():
                        if not member.islnk():
                            continue
                        destination = output / member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.link(output / regular_endpoint(member.name), destination)
                    for member in members.values():
                        if not member.issym():
                            continue
                        destination = output / member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(member.linkname, destination)
                    for member in reversed(directories):
                        (output / member.name).chmod(member.mode & 0o777)
                except Exception:
                    shutil.rmtree(output, ignore_errors=True)
                    raise
        except (OSError, tarfile.TarError) as error:
            raise GridpackError(f"could not safely extract {gridpack}: {error}") from error
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        shutil.rmtree(output, ignore_errors=True)
        raise GridpackError("gridpack changed while it was being extracted")
    # Defense in depth after extraction: every link must resolve inside output.
    root = output.resolve(strict=True)
    for path in root.rglob("*"):
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as error:
                shutil.rmtree(output, ignore_errors=True)
                raise GridpackError(f"unsafe extracted link: {path}") from error
    return inspection


def materialize_external_symlinks(
    process_directory: Path,
    prefix: Path,
    output: Path,
    *,
    lhapdf_static_library: Path | None = None,
    prior_report: Path | None = None,
) -> dict[str, Any]:
    process_directory = process_directory.expanduser().resolve(strict=True)
    prefix = prefix.expanduser().resolve(strict=True)
    cuttools_root = prefix / "madgraph5/vendor/CutTools/includects"
    allowed: dict[str, tuple[Path, str, str]] = {
        "lib/libcts.a": (
            (cuttools_root / "libcts.a").resolve(strict=True),
            "validated-generator-prefix",
            "madgraph5/vendor/CutTools/includects/libcts.a",
        ),
        "lib/mpmodule.mod": (
            (cuttools_root / "mpmodule.mod").resolve(strict=True),
            "validated-generator-prefix",
            "madgraph5/vendor/CutTools/includects/mpmodule.mod",
        ),
        "lib/libiregi.a": (
            (
                prefix / "madgraph5/vendor/IREGI/src/libiregi.a"
            ).resolve(strict=True),
            "validated-generator-prefix",
            "madgraph5/vendor/IREGI/src/libiregi.a",
        ),
    }
    if lhapdf_static_library is not None:
        resolved_lhapdf = lhapdf_static_library.expanduser().resolve(strict=True)
        allowed["lib/libLHAPDF.a"] = (
            resolved_lhapdf,
            "validated-lhapdf-static-library",
            str(resolved_lhapdf),
        )

    materialized: list[dict[str, Any]] = []
    normalized_internal_absolute_links: list[dict[str, str]] = []
    if prior_report is not None:
        prior = _load_json(
            prior_report.expanduser().resolve(strict=True),
            "prior materialization report",
        )
        if (
            prior.get("schema_version") != MATERIALIZATION_SCHEMA_VERSION
            or prior.get("contract") != MATERIALIZATION_CONTRACT
            or not isinstance(prior.get("materialized"), list)
            or not isinstance(
                prior.get("normalized_internal_absolute_links", []), list
            )
        ):
            raise GridpackError("invalid prior symlink materialization report")
        materialized.extend(prior["materialized"])
        normalized_internal_absolute_links.extend(
            prior.get("normalized_internal_absolute_links", [])
        )

    def materialize(link: Path, relative: str, expected: tuple[Path, str, str]) -> None:
        try:
            target = link.resolve(strict=True)
        except OSError as error:
            raise GridpackError(f"broken process symlink: {link}") from error
        if target != expected[0] or not target.is_file():
            raise GridpackError(
                f"process dependency has unexpected target: {link} -> {target}"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{link.name}.", dir=str(link.parent)
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        shutil.copy2(target, temporary)
        link.unlink()
        os.replace(temporary, link)
        materialized.append(
            {
                "path": relative,
                "source_scope": expected[1],
                "source": expected[2],
                "kind": "file",
                "sha256": sha256(link),
            }
        )

    # Resolve the root dependencies first.  Internal PV links can point through
    # lib/mpmodule.mod, so a filesystem traversal that sees SubProcesses before
    # lib would otherwise misclassify those contained links as external.
    for relative, expected in allowed.items():
        link = process_directory / relative
        if link.is_symlink():
            materialize(link, relative, expected)

    for walk_root, directory_names, file_names in os.walk(
        process_directory, topdown=True, followlinks=False
    ):
        candidates = [Path(walk_root) / name for name in directory_names + file_names]
        for link in sorted((item for item in candidates if item.is_symlink()), key=str):
            try:
                target = link.resolve(strict=True)
            except OSError as error:
                raise GridpackError(f"broken process symlink: {link}") from error
            try:
                target.relative_to(process_directory)
                if os.path.isabs(os.readlink(link)):
                    relative = link.relative_to(process_directory).as_posix()
                    internal_target = target.relative_to(process_directory).as_posix()
                    replacement = os.path.relpath(target, start=link.parent)
                    link.unlink()
                    link.symlink_to(replacement)
                    normalized_internal_absolute_links.append(
                        {"path": relative, "target": internal_target}
                    )
                continue
            except ValueError:
                pass
            relative = link.relative_to(process_directory).as_posix()
            expected = allowed.get(relative)
            if expected is None or target != expected[0]:
                raise GridpackError(
                    f"process symlink is not an allowed external dependency: {link} -> {target}"
                )
            materialize(link, relative, expected)
    paths = [record.get("path") for record in materialized if isinstance(record, dict)]
    if len(paths) != len(materialized) or len(paths) != len(set(paths)):
        raise GridpackError("duplicate or malformed external-link materialization records")
    materialized.sort(key=lambda record: record["path"])
    normalized_paths = [
        record.get("path")
        for record in normalized_internal_absolute_links
        if isinstance(record, dict)
    ]
    if (
        len(normalized_paths) != len(normalized_internal_absolute_links)
        or len(normalized_paths) != len(set(normalized_paths))
    ):
        raise GridpackError("duplicate or malformed internal-link normalization records")
    normalized_internal_absolute_links.sort(key=lambda record: record["path"])
    report = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "contract": MATERIALIZATION_CONTRACT,
        "materialized": materialized,
        "normalized_internal_absolute_links": normalized_internal_absolute_links,
    }
    _write_json_atomic(output.expanduser().resolve(), report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--gridpack", required=True, type=Path)
    create = subparsers.add_parser("create")
    create.add_argument("--gridpack", required=True, type=Path)
    create.add_argument("--metadata", required=True, type=Path)
    create.add_argument(
        "--generator-prefix", "--prefix", dest="prefix", required=True, type=Path
    )
    create.add_argument("--process", required=True, choices=PROCESSES)
    create.add_argument("--build-seed", required=True, type=int)
    create.add_argument("--build-cores", required=True, type=int)
    create.add_argument("--materialization-report", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--gridpack", required=True, type=Path)
    validate.add_argument("--metadata", required=True, type=Path)
    validate.add_argument(
        "--generator-prefix", "--prefix", dest="prefix", required=True, type=Path
    )
    validate.add_argument("--process", required=True, choices=PROCESSES)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--gridpack", required=True, type=Path)
    extract.add_argument("--metadata", required=True, type=Path)
    extract.add_argument(
        "--generator-prefix", "--prefix", dest="prefix", required=True, type=Path
    )
    extract.add_argument("--process", required=True, choices=PROCESSES)
    extract.add_argument("--output", required=True, type=Path)
    materialize = subparsers.add_parser("materialize-external-links")
    materialize.add_argument("--process-directory", required=True, type=Path)
    materialize.add_argument(
        "--generator-prefix", "--prefix", dest="prefix", required=True, type=Path
    )
    materialize.add_argument("--lhapdf-static-library", type=Path)
    materialize.add_argument("--prior-report", type=Path)
    materialize.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "inspect":
        print(json.dumps(inspect_gridpack(args.gridpack), indent=2, sort_keys=True))
    elif args.command == "create":
        create_metadata(
            args.gridpack,
            args.metadata,
            args.prefix,
            args.process,
            build_seed=args.build_seed,
            build_cores=args.build_cores,
            materialization_report=args.materialization_report,
        )
        print(f"Created VPolar gridpack metadata: {args.metadata}")
    elif args.command == "validate":
        validate_metadata(args.gridpack, args.metadata, args.prefix, args.process)
        print(f"Validated compatible VPolar gridpack: {args.gridpack}")
    elif args.command == "extract":
        metadata = validate_metadata(
            args.gridpack, args.metadata, args.prefix, args.process
        )
        safe_extract_gridpack(
            args.gridpack,
            args.output,
            expected_sha256=metadata["gridpack"]["sha256"],
        )
        print(f"Extracted compatible VPolar gridpack: {args.output}")
    else:
        materialize_external_symlinks(
            args.process_directory,
            args.prefix,
            args.output,
            lhapdf_static_library=args.lhapdf_static_library,
            prior_report=args.prior_report,
        )
        print(f"Materialized external process links: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GridpackError as error:
        raise SystemExit(f"VPolar gridpack error: {error}") from error
