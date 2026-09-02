"""Stream LHE events into namespace-safe off-shell analysis records."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import numpy as np
import pandas as pd
import pylhe
import vector

from .kinematics import (
    LEPTON_KEYS,
    FourLeptonCandidate,
    build_four_lepton_candidate,
    build_level_record,
    empty_level_record,
)

PDG_TO_LEPTON_KEY = {
    11: "electron_minus",
    -11: "electron_plus",
    13: "muon_minus",
    -13: "muon_plus",
}


class LHEStatus(IntEnum):
    """Machine-readable status for one retained LHE source event."""

    VALID = 0
    INVALID_TOPOLOGY = 1
    PROJECTION_FAILED = 2


@dataclass(frozen=True)
class ExtractedLHEEvent:
    """A cut-independent final-state candidate and all LHE event weights."""

    candidate: FourLeptonCandidate
    nominal_weight: float
    alternative_weights: dict[str, float]

    @property
    def leptons(self) -> dict[str, object]:
        return self.candidate.leptons

    @property
    def z1(self):
        return self.candidate.z1

    @property
    def z2(self):
        return self.candidate.z2

    @property
    def four_lepton(self):
        return self.candidate.four_lepton


def particle_four_vector(particle):
    """Convert a ``pylhe.LHEParticle``-like object to a scalar four-vector."""

    return vector.obj(
        px=float(particle.px),
        py=float(particle.py),
        pz=float(particle.pz),
        E=float(particle.e),
    )


def _event_weights(event) -> tuple[float, dict[str, float]]:
    nominal = float(event.eventinfo.weight)
    alternative = {
        str(key): float(value) for key, value in (event.weights or {}).items()
    }
    if not np.isfinite(nominal):
        raise ValueError("LHE nominal weight must be finite")
    nonfinite = [key for key, value in alternative.items() if not np.isfinite(value)]
    if nonfinite:
        raise ValueError(
            "LHE alternative weights must be finite; invalid IDs: "
            + ", ".join(nonfinite)
        )
    return nominal, alternative


def extract_event_particles(event) -> ExtractedLHEEvent:
    """Construct an exact status-1 ``e- e+ mu- mu+`` candidate, without cuts."""

    lepton_candidates = {key: [] for key in LEPTON_KEYS}
    for particle in event.particles:
        if particle.status == 1 and particle.id in PDG_TO_LEPTON_KEY:
            lepton_candidates[PDG_TO_LEPTON_KEY[particle.id]].append(
                particle_four_vector(particle)
            )

    multiplicities = {
        key: len(candidates) for key, candidates in lepton_candidates.items()
    }
    invalid = {key: count for key, count in multiplicities.items() if count != 1}
    if invalid:
        raise ValueError(
            "Expected exactly one final-state lepton of each flavor and charge; "
            f"found {invalid}"
        )

    candidate = build_four_lepton_candidate(
        {key: candidates[0] for key, candidates in lepton_candidates.items()}
    )
    nominal, alternative = _event_weights(event)
    return ExtractedLHEEvent(candidate, nominal, alternative)


def iter_lhe_records(
    path: str | Path,
    *,
    max_events: int | None = None,
    strict: bool = True,
    include_momenta: bool = True,
) -> Iterator[dict[str, object]]:
    """Yield one record for every scanned source LHE event.

    No physics selection is applied at LHE level.  With ``strict=False``, an
    invalid topology is retained with false masks and NaN-valued kinematics so
    its event index cannot be lost during later LHE--HepMC--Delphes matching.
    """

    if max_events is not None and (
        not isinstance(max_events, (int, np.integer)) or max_events < 0
    ):
        raise ValueError("max_events must be a non-negative integer or None")

    lhe_file = pylhe.LHEFile.fromfile(Path(path), with_attributes=True, generator=True)
    for event_index, event in enumerate(lhe_file.events):
        if max_events is not None and event_index >= max_events:
            break

        nominal_weight, alternative_weights = _event_weights(event)
        record: dict[str, object] = {
            "lhe_event_index": int(event_index),
            "weight_lhe": nominal_weight,
            "lhe_n_alternative_weights": len(alternative_weights),
            "lhe_alternative_weights": alternative_weights,
            "lhe_status": int(LHEStatus.VALID),
        }
        try:
            extracted = extract_event_particles(event)
        except (TypeError, ValueError) as error:
            if strict:
                raise ValueError(f"LHE event {event_index}: {error}") from error
            record.update(empty_level_record("lhe", include_momenta=include_momenta))
            record["lhe_status"] = int(LHEStatus.INVALID_TOPOLOGY)
            yield record
            continue

        try:
            record.update(
                build_level_record(
                    extracted.leptons,
                    "lhe",
                    include_momenta=include_momenta,
                )
            )
        except (TypeError, ValueError, RuntimeError) as error:
            if strict:
                raise ValueError(f"LHE event {event_index}: {error}") from error
            record.update(
                empty_level_record(
                    "lhe",
                    include_momenta=include_momenta,
                    topology_valid=True,
                )
            )
            record["lhe_status"] = int(LHEStatus.PROJECTION_FAILED)
        yield record


def load_lhe_dataframe(
    path: str | Path,
    *,
    max_events: int | None = None,
    strict: bool = True,
    include_momenta: bool = True,
) -> pd.DataFrame:
    """Load the unfiltered, event-aligned LHE records into a DataFrame."""

    records = list(
        iter_lhe_records(
            path,
            max_events=max_events,
            strict=strict,
            include_momenta=include_momenta,
        )
    )
    if not records:
        raise ValueError("The LHE file contains no events")
    frame = pd.DataFrame.from_records(records)
    numeric_columns = frame.select_dtypes(include=[np.number]).columns
    frame[numeric_columns] = frame[numeric_columns].replace([np.inf, -np.inf], np.nan)
    return frame
