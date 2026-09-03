"""Flavor-aware MadGraph loop filters for the VPolar ``gg -> 2e2mu`` jobs.

This module is copied into ``madgraph/loop`` by :mod:`loop_filter_patch`.
Only the two explicit tokens ``oap_tl`` and ``oap_lt`` are intercepted.  All
other MadGraph loop-filter expressions retain their stock behavior.

The project convention is intentionally explicit:

* ``Z1`` is the dimuon system;
* ``Z2`` is the dielectron system;
* ``TL`` means ``Z1=T, Z2=L``; and
* ``LT`` means ``Z1=L, Z2=T``.

``LL`` and ``TT`` need no Python filter.  They are selected with MadGraph's
standard diagram-exclusion syntax, recorded below next to the mixed channels
so that the four process definitions have one auditable source of truth.
"""

from __future__ import absolute_import

import logging


LOGGER = logging.getLogger("madgraph.loop_diagram_generation")

Z0_PDG = 230
ZT_PDG = 231
ZA_PDG = 232
PHOTON_PDG = 22
ZX_PDG = 23

SUPPORTED_TOKENS = ("oap_tl", "oap_lt")

# These fragments follow Appendix C of arXiv:2401.17365.  The mixed channels
# keep Z0 and ZT available and let the flavor-aware filter choose their route.
PROCESS_FILTERS = {
    "ll": "/ a z za zt",
    "tt": "/ a z z0 za",
    "tl": "/ a z za --loop_filter=oap_tl",
    "lt": "/ a z za --loop_filter=oap_lt",
}

_EXPECTED_ROUTE = {
    "oap_tl": {"mumu": ZT_PDG, "ee": Z0_PDG},
    "oap_lt": {"mumu": Z0_PDG, "ee": ZT_PDG},
}
_LEPTON_PAIRS = {
    "ee": frozenset((11, -11)),
    "mumu": frozenset((13, -13)),
}
_FORBIDDEN_ABS_PDGS = frozenset((PHOTON_PDG, ZX_PDG, ZA_PDG))
_PHYSICAL_POLARIZATION_PDGS = frozenset((Z0_PDG, ZT_PDG))


def normalize_filter_token(token):
    """Return a supported lower-case filter token or raise ``ValueError``."""

    if not isinstance(token, str):
        raise ValueError("VPolar loop-filter token must be a string")
    normalized = token.strip().lower()
    if normalized not in SUPPORTED_TOKENS:
        raise ValueError(
            "unsupported VPolar loop-filter token {!r}; expected {}".format(
                token, ", ".join(SUPPORTED_TOKENS)
            )
        )
    return normalized


def _leg_pdg(leg):
    """Read a MadGraph ``Leg`` (or a minimal test double)."""

    return int(leg.get("id"))


def _referenced_structures(diagram, structures):
    """Yield each FDStructure referenced by a tagged loop diagram once."""

    seen = set()
    tag = diagram.get("tag")
    if not tag:
        return
    for tag_element in tag:
        if len(tag_element) < 2:
            continue
        for structure_id in tag_element[1]:
            structure_id = int(structure_id)
            if structure_id in seen:
                continue
            seen.add(structure_id)
            structure = structures.get_struct(structure_id)
            if structure is not None:
                yield structure


def _structure_pdgs(structure):
    """Yield all PDGs visible in one MadGraph FDStructure."""

    binding_leg = structure.get("binding_leg")
    if binding_leg is not None:
        yield _leg_pdg(binding_leg)
    for leg in structure.get("external_legs") or ():
        yield _leg_pdg(leg)
    for vertex in structure.get("vertices") or ():
        for leg in vertex.get("legs") or ():
            yield _leg_pdg(leg)


def _decay_routes(structures):
    """Return the unique ``ee`` and ``mumu`` polarization routing.

    A valid diagram has exactly one direct ``Z0/ZT -> e+e-`` vertex and one
    direct ``Z0/ZT -> mu+mu-`` vertex.  Ambiguous or incomplete graphs return
    ``None`` and are rejected conservatively.
    """

    routes = {name: [] for name in _LEPTON_PAIRS}
    for structure in structures:
        if any(
            abs(pdg) in _FORBIDDEN_ABS_PDGS
            for pdg in _structure_pdgs(structure)
        ):
            return None

        for vertex in structure.get("vertices") or ():
            pdgs = tuple(_leg_pdg(leg) for leg in vertex.get("legs") or ())
            pdg_set = frozenset(pdgs)
            polarizations = {
                abs(pdg)
                for pdg in pdgs
                if abs(pdg) in _PHYSICAL_POLARIZATION_PDGS
            }
            for flavor, lepton_pair in _LEPTON_PAIRS.items():
                if lepton_pair.issubset(pdg_set):
                    if len(polarizations) != 1:
                        return None
                    routes[flavor].append(next(iter(polarizations)))

    if any(len(values) != 1 for values in routes.values()):
        return None
    return {flavor: values[0] for flavor, values in routes.items()}


def diagram_matches_token(diagram, structures, token):
    """Return whether a tagged MG5 loop diagram has the requested routing."""

    normalized = normalize_filter_token(token)
    referenced = tuple(_referenced_structures(diagram, structures))
    if not referenced:
        return False
    return _decay_routes(referenced) == _EXPECTED_ROUTE[normalized]


def apply_oap_filter(amplitude, model, structures, token):
    """Apply one explicit OAP route inside MG5 3.4.2 ``user_filter``.

    The ``model`` argument is part of the MadGraph hook signature and is kept
    even though the flavor-aware routing can be read entirely from tagged
    FDStructures.
    """

    del model
    normalized = normalize_filter_token(token)

    # Imports are deliberately local.  The pure routing helper above remains
    # unit-testable without importing a complete MadGraph installation.
    import madgraph.core.base_objects as base_objects
    from madgraph import MadGraph5Error

    selected = base_objects.DiagramList()
    discarded = 0
    for diagram in amplitude["loop_diagrams"]:
        if diagram.get("tag") == []:
            raise MadGraph5Error(
                "OAP VPolar filtering requires tagged loop diagrams"
            )
        if diagram_matches_token(diagram, structures, normalized):
            selected.append(diagram)
        else:
            discarded += 1

    if not selected:
        raise MadGraph5Error(
            "OAP VPolar filter {} rejected every loop diagram; refusing to "
            "build a silently empty process".format(normalized)
        )

    amplitude["loop_diagrams"] = selected
    LOGGER.info(
        "OAP VPolar filter %s retained %d and discarded %d loop diagrams",
        normalized,
        len(selected),
        discarded,
    )


__all__ = [
    "PROCESS_FILTERS",
    "SUPPORTED_TOKENS",
    "apply_oap_filter",
    "diagram_matches_token",
    "normalize_filter_token",
]
