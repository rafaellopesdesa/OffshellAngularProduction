"""Prepare the exact LHE stream consumed by Pythia in local ATLAS jobs.

The helper runs inside the Athena job option after PowhegControl has finished
and before the Pythia event loop starts. It applies hard-event phase-space
bounds and adds a named, integral source-event identifier as an auxiliary LHE
weight. Pythia propagates named LHE weights into HepMC, providing a durable
event-level join key without changing the nominal physics weight.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tarfile
import tempfile


MARKER_ID_WEIGHT = "AUX_OAP_EVENT_ID"
MARKER_UNIT_WEIGHT = "AUX_OAP_EVENT_UNIT"
CONTRACT = "named-weight-id-v1"
NORMALIZATION_CONTRACT = "idwtup-minus4-sample-mean-v1"
CROSS_SECTION_METHOD = (
    "mean nominal LHE weight; rejected events assigned zero for filtered estimate"
)
MAX_SOURCE_EVENT_ID = 1_000_000
EVENT_OPEN_RE = re.compile(r"<event(?:\s|>)")
INIT_BLOCK_RE = re.compile(r"<init(?:\s[^>]*)?>(.*?)</init>", re.DOTALL)


class LHEContractError(RuntimeError):
    """Raised when the pre-shower LHE contract cannot be established."""


def _lhe_float(value: str, label: str) -> float:
    try:
        parsed = float(value.replace("D", "E").replace("d", "e"))
    except ValueError as error:
        raise LHEContractError(f"invalid {label}") from error
    if not math.isfinite(parsed):
        raise LHEContractError(f"non-finite {label}")
    return parsed


def _parse_init_cross_section(preamble: str) -> dict[str, object]:
    matches = INIT_BLOCK_RE.findall(preamble)
    if len(matches) != 1:
        raise LHEContractError(
            f"LHE preamble must contain exactly one init block; found {len(matches)}"
        )
    data_lines = []
    for line in matches[0].splitlines():
        content = line.split("#", 1)[0].strip()
        if content:
            data_lines.append(content.split())
    if not data_lines or len(data_lines[0]) < 10:
        raise LHEContractError("invalid LHE init beam line")
    try:
        idwtup = int(data_lines[0][8])
        process_count = int(data_lines[0][9])
    except ValueError as error:
        raise LHEContractError("invalid IDWTUP/NPRUP in LHE init block") from error
    if process_count < 1 or len(data_lines) != process_count + 1:
        raise LHEContractError(
            "LHE init process count does not match its cross-section rows"
        )
    if idwtup != -4:
        raise LHEContractError(
            f"LHE normalization requires IDWTUP=-4; found {idwtup}"
        )
    processes = []
    seen_process_ids: set[int] = set()
    for fields in data_lines[1:]:
        if len(fields) < 4:
            raise LHEContractError("invalid LHE init cross-section row")
        cross_section = _lhe_float(fields[0], "LHE init XSECUP")
        cross_section_error = _lhe_float(fields[1], "LHE init XERRUP")
        maximum_weight = _lhe_float(fields[2], "LHE init XMAXUP")
        try:
            process_id = int(fields[3])
        except ValueError as error:
            raise LHEContractError("invalid LHE init LPRUP") from error
        if process_id in seen_process_ids:
            raise LHEContractError(f"duplicate LHE init process ID {process_id}")
        if cross_section_error < 0.0:
            raise LHEContractError("negative LHE init cross-section error")
        seen_process_ids.add(process_id)
        processes.append(
            {
                "process_id": process_id,
                "cross_section_pb": cross_section,
                "cross_section_error_pb": cross_section_error,
                "maximum_weight_pb": maximum_weight,
            }
        )
    return {
        "idwtup": idwtup,
        "process_count": process_count,
        "processes": processes,
        "inclusive_cross_section_pb": math.fsum(
            float(process["cross_section_pb"]) for process in processes
        ),
        "inclusive_cross_section_error_pb": math.sqrt(
            math.fsum(
                float(process["cross_section_error_pb"]) ** 2
                for process in processes
            )
        ),
    }


def _event_parts(event: str) -> tuple[list[str], list[list[str]]]:
    body = EVENT_OPEN_RE.sub("", event, count=1).replace("</event>", "", 1)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    data_lines = [line for line in lines if not line.startswith(("#", "<"))]
    if not data_lines:
        raise LHEContractError("LHE event has no event-information line")
    event_info = data_lines[0].split()
    try:
        particle_count = int(event_info[0])
    except (IndexError, ValueError) as error:
        raise LHEContractError("invalid LHE event-information line") from error
    particle_lines = [line.split() for line in data_lines[1 : 1 + particle_count]]
    if len(particle_lines) != particle_count:
        raise LHEContractError("LHE event has fewer particle lines than NUP")
    if any(len(fields) < 10 for fields in particle_lines):
        raise LHEContractError("invalid LHE particle line")
    return event_info, particle_lines


def _event_m4l_and_weight(event: str) -> tuple[float, float]:
    event_info, particles = _event_parts(event)
    try:
        nominal_weight = _lhe_float(event_info[2], "nominal LHE weight")
    except IndexError as error:
        raise LHEContractError("invalid nominal LHE weight") from error

    leptons = {}
    for fields in particles:
        try:
            pdg_id = int(fields[0])
            status = int(fields[1])
        except ValueError as error:
            raise LHEContractError("invalid LHE particle ID/status") from error
        if status != 1 or pdg_id not in {11, -11, 13, -13}:
            continue
        if pdg_id in leptons:
            raise LHEContractError(
                "expected exactly one status-1 lepton of each charge and flavor"
            )
        try:
            momentum = tuple(
                _lhe_float(fields[index], "LHE lepton four-vector component")
                for index in (6, 7, 8, 9)
            )
        except IndexError as error:
            raise LHEContractError("invalid LHE lepton four-vector") from error
        if momentum[3] < 0.0:
            raise LHEContractError("negative-energy status-1 LHE lepton")
        leptons[pdg_id] = momentum
    if set(leptons) != {11, -11, 13, -13}:
        raise LHEContractError(
            "expected exactly one status-1 lepton of each charge and flavor"
        )
    px = sum(momentum[0] for momentum in leptons.values())
    py = sum(momentum[1] for momentum in leptons.values())
    pz = sum(momentum[2] for momentum in leptons.values())
    energy = sum(momentum[3] for momentum in leptons.values())
    if energy <= 0.0:
        raise LHEContractError("non-positive four-lepton energy")
    mass_squared = energy * energy - px * px - py * py - pz * pz
    scale = max(energy * energy, 1.0)
    if mass_squared < -1.0e-10 * scale:
        raise LHEContractError("status-1 four-lepton system is spacelike")
    return math.sqrt(max(mass_squared, 0.0)), nominal_weight


def _add_header_weights(preamble: str, id_name: str, unit_name: str) -> str:
    if id_name in preamble or unit_name in preamble:
        raise LHEContractError("LHE header already contains OAP marker weights")
    definition = (
        "  <weightgroup name='OffshellAngularProduction' combine='none'>\n"
        f"   <weight id='{id_name}'>{id_name}</weight>\n"
        f"   <weight id='{unit_name}'>{unit_name}</weight>\n"
        "  </weightgroup>\n"
    )
    if "</initrwgt>" in preamble:
        return preamble.replace("</initrwgt>", definition + " </initrwgt>", 1)
    block = " <initrwgt>\n" + definition + " </initrwgt>\n"
    if "</header>" not in preamble:
        raise LHEContractError("LHE preamble has neither initrwgt nor header")
    return preamble.replace("</header>", block + "</header>", 1)


def _add_event_markers(
    event: str, id_name: str, unit_name: str, source_event_id: int
) -> str:
    if id_name in event or unit_name in event:
        raise LHEContractError("LHE event already contains OAP marker weights")
    # Pythia8_i applies the same shower/merging factor to all detailed LHE
    # weights. Their ratio therefore recovers the integral per-job source ID,
    # independently of the nominal weight (including its sign).
    markers = (
        f"  <wgt id='{id_name}'> {source_event_id} </wgt>\n"
        f"  <wgt id='{unit_name}'> 1 </wgt>\n"
    )
    source_comment = f" # {id_name} {source_event_id}\n"
    if "</rwgt>" in event:
        return event.replace("</rwgt>", markers + " </rwgt>\n" + source_comment, 1)
    if "</event>" not in event:
        raise LHEContractError("unterminated LHE event")
    return event.replace(
        "</event>",
        " <rwgt>\n" + markers + " </rwgt>\n" + source_comment + "</event>",
        1,
    )


def _read_preamble(stream) -> tuple[str, str]:
    preamble = ""
    for line in stream:
        opening = EVENT_OPEN_RE.search(line)
        if opening is None:
            preamble += line
            continue
        preamble += line[: opening.start()]
        return preamble, line[opening.start() :]
    raise LHEContractError("LHE file contains no events")


def _iter_events(stream, first_fragment: str):
    event = first_fragment
    while True:
        if "</event>" in event:
            closing = event.index("</event>") + len("</event>")
            yield event[:closing] + "\n"
            remainder = event[closing:]
            opening = EVENT_OPEN_RE.search(remainder)
            if opening is not None:
                event = remainder[opening.start() :]
                continue
            if "</LesHouchesEvents>" in remainder:
                return
            if remainder.strip():
                raise LHEContractError("unexpected content after an LHE event")
            event = ""
        try:
            line = next(stream)
        except StopIteration:
            if event.strip():
                if "</LesHouchesEvents>" in event:
                    return
                raise LHEContractError("unterminated LHE event")
            raise LHEContractError("missing </LesHouchesEvents> in LHE input")
        if not event:
            opening = EVENT_OPEN_RE.search(line)
            if opening is None:
                if "</LesHouchesEvents>" in line:
                    return
                continue
            event = line[opening.start() :]
        else:
            event += line


def prepare_lhe_for_shower(
    input_events,
    output_tarball,
    *,
    process,
    requested_events,
    min_m4l,
    max_m4l,
    metadata_path="lhe-contract-metadata.json",
    marker_id_weight=MARKER_ID_WEIGHT,
    marker_unit_weight=MARKER_UNIT_WEIGHT,
):
    """Filter and tag the LHE file that the Athena Pythia stage will consume."""

    input_path = Path(input_events)
    tarball_path = Path(output_tarball)
    metadata_path = Path(metadata_path)
    requested_events = int(requested_events)
    min_m4l = float(min_m4l)
    max_m4l = float(max_m4l)
    if process not in {"gg4l", "qqZZ"}:
        raise LHEContractError("process must be gg4l or qqZZ")
    if requested_events < 1:
        raise LHEContractError("requested_events must be positive")
    if not (0.0 <= min_m4l < max_m4l and math.isfinite(max_m4l)):
        raise LHEContractError("invalid m4l bounds")
    if not input_path.is_file():
        raise LHEContractError(f"PowhegControl LHE file is missing: {input_path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{input_path.name}.", dir=str(input_path.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    generated = accepted = rejected_below = rejected_above = 0
    weights_all = []
    weights_accepted = []
    init_cross_section: dict[str, object] | None = None
    try:
        with input_path.open("r", encoding="utf-8") as source:
            preamble, first_fragment = _read_preamble(source)
            init_cross_section = _parse_init_cross_section(preamble)
            with temporary.open("w", encoding="utf-8") as output:
                output.write(
                    _add_header_weights(
                        preamble, marker_id_weight, marker_unit_weight
                    )
                )
                for source_event_id, event in enumerate(
                    _iter_events(iter(source), first_fragment), start=1
                ):
                    if source_event_id > MAX_SOURCE_EVENT_ID:
                        raise LHEContractError(
                            "LHE source-event count exceeds the precision-safe "
                            f"limit of {MAX_SOURCE_EVENT_ID}"
                        )
                    generated += 1
                    m4l, nominal_weight = _event_m4l_and_weight(event)
                    weights_all.append(nominal_weight)
                    if m4l < min_m4l:
                        rejected_below += 1
                        continue
                    if m4l > max_m4l:
                        rejected_above += 1
                        continue
                    output.write(
                        _add_event_markers(
                            event,
                            marker_id_weight,
                            marker_unit_weight,
                            source_event_id,
                        )
                    )
                    accepted += 1
                    weights_accepted.append(nominal_weight)
                output.write("</LesHouchesEvents>\n")

        if accepted < requested_events:
            raise LHEContractError(
                f"m4l filtering retained {accepted} events, fewer than the "
                f"{requested_events} requested for showering"
            )
        os.replace(str(temporary), str(input_path))

        tar_descriptor, tar_temporary_name = tempfile.mkstemp(
            prefix=f".{tarball_path.name}.", dir=str(tarball_path.parent)
        )
        os.close(tar_descriptor)
        Path(tar_temporary_name).unlink()
        try:
            with tarfile.open(tar_temporary_name, "w:gz") as archive:
                archive.add(str(input_path), arcname=input_path.name, recursive=False)
            os.replace(tar_temporary_name, str(tarball_path))
        except Exception:
            Path(tar_temporary_name).unlink(missing_ok=True)
            raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    sumw_all = math.fsum(weights_all)
    sumw_accepted = math.fsum(weights_accepted)
    sumw2_all = math.fsum(weight * weight for weight in weights_all)
    sumw2_accepted = math.fsum(weight * weight for weight in weights_accepted)
    sumabsw_all = math.fsum(abs(weight) for weight in weights_all)
    sumabsw_accepted = math.fsum(abs(weight) for weight in weights_accepted)
    if init_cross_section is None:
        raise LHEContractError("internal error: LHE init cross section was not parsed")
    signed_efficiency = sumw_accepted / sumw_all if sumw_all != 0.0 else None
    absolute_efficiency = (
        sumabsw_accepted / sumabsw_all if sumabsw_all != 0.0 else None
    )
    inclusive_cross_section = sumw_all / generated
    filtered_cross_section = sumw_accepted / generated

    def mean_mc_error(sumw: float, sumw2: float) -> float | None:
        if generated < 2:
            return None
        variance_numerator = sumw2 - sumw * sumw / generated
        # Clamp only roundoff: a material violation of the second-moment
        # inequality is an internal contract failure.
        scale = max(sumw2, sumw * sumw / generated, 1.0)
        if variance_numerator < -1.0e-12 * scale:
            raise LHEContractError("nominal LHE weight moments are inconsistent")
        return math.sqrt(
            max(variance_numerator, 0.0) / (generated * (generated - 1))
        )

    metadata = {
        "schema_version": 2,
        "contract": CONTRACT,
        "normalization_contract": NORMALIZATION_CONTRACT,
        "nominal_weight_units": "pb",
        "lhe_weighting_strategy": -4,
        "process": process,
        "marker_id_weight": marker_id_weight,
        "marker_unit_weight": marker_unit_weight,
        "hepmc_recovery_formula": "AUX_OAP_EVENT_ID / AUX_OAP_EVENT_UNIT",
        "requested_hepmc_events": requested_events,
        "m4l_min_gev": min_m4l,
        "m4l_max_gev": max_m4l,
        "generated_lhe_events": generated,
        "accepted_lhe_events": accepted,
        "rejected_below_m4l": rejected_below,
        "rejected_above_m4l": rejected_above,
        "sumw_generated": sumw_all,
        "sumw_accepted": sumw_accepted,
        "sumw2_generated": sumw2_all,
        "sumw2_accepted": sumw2_accepted,
        "sumabsw_generated": sumabsw_all,
        "sumabsw_accepted": sumabsw_accepted,
        "count_filter_efficiency": accepted / generated,
        "signed_filter_efficiency": signed_efficiency,
        "absolute_filter_efficiency": absolute_efficiency,
        "lhe_init": init_cross_section,
        "inclusive_cross_section_pb": inclusive_cross_section,
        "inclusive_cross_section_mc_error_pb": mean_mc_error(
            sumw_all, sumw2_all
        ),
        "filtered_cross_section_pb": filtered_cross_section,
        "filtered_cross_section_mc_error_pb": mean_mc_error(
            sumw_accepted, sumw2_accepted
        ),
        "cross_section_method": CROSS_SECTION_METHOD,
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_descriptor, metadata_temporary_name = tempfile.mkstemp(
        prefix=f".{metadata_path.name}.", dir=str(metadata_path.parent)
    )
    try:
        with os.fdopen(metadata_descriptor, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(metadata_temporary_name, str(metadata_path))
    except Exception:
        Path(metadata_temporary_name).unlink(missing_ok=True)
        raise
    print(
        "Prepared {} LHE events for showering: {} accepted, {} below and {} "
        "above m4l bounds".format(
            process, accepted, rejected_below, rejected_above
        )
    )
    return metadata
