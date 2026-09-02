"""Core event-record utilities for off-shell ``ZZ -> 2e2mu`` production."""

from .kinematics import (
    ANGULAR_BOOLEAN_FIELDS,
    ANGULAR_NUMERIC_FIELDS,
    LEPTON_KEYS,
    MOMENTUM_COMPONENTS,
    BornProjectionDiagnostics,
    FourLeptonCandidate,
    angular_observables,
    born_project_four_leptons,
    build_four_lepton_candidate,
    build_level_record,
    empty_level_record,
    standard_five_angles,
    wrap_to_pi,
)
from .lhe import (
    PDG_TO_LEPTON_KEY,
    ExtractedLHEEvent,
    LHEStatus,
    extract_event_particles,
    iter_lhe_records,
    load_lhe_dataframe,
    particle_four_vector,
)
from .selection import (
    OffshellSelectionConfig,
    RecoSelectionResult,
    empty_reco_selection_result,
    evaluate_reco_selection,
)

__all__ = [
    "ANGULAR_BOOLEAN_FIELDS",
    "ANGULAR_NUMERIC_FIELDS",
    "LEPTON_KEYS",
    "MOMENTUM_COMPONENTS",
    "PDG_TO_LEPTON_KEY",
    "BornProjectionDiagnostics",
    "ExtractedLHEEvent",
    "FourLeptonCandidate",
    "LHEStatus",
    "OffshellSelectionConfig",
    "RecoSelectionResult",
    "angular_observables",
    "born_project_four_leptons",
    "build_four_lepton_candidate",
    "build_level_record",
    "empty_level_record",
    "empty_reco_selection_result",
    "evaluate_reco_selection",
    "extract_event_particles",
    "iter_lhe_records",
    "load_lhe_dataframe",
    "particle_four_vector",
    "standard_five_angles",
    "wrap_to_pi",
]
