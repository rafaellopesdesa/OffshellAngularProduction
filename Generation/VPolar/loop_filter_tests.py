"""Focused tests for the VPolar MG5 3.4.2 loop-filter patch.

Run directly with

``python -m pytest Generation/VPolar/loop_filter_tests.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = _load("oap_loop_filter_runtime", "loop_filter_runtime.py")
patcher = _load("oap_loop_filter_patch", "loop_filter_patch.py")
sys.modules["loop_filter_runtime"] = runtime
sys.modules["loop_filter_patch"] = patcher
validator = _load("oap_validate_diagram_counts", "validate_diagram_counts.py")


class Node(dict):
    """Minimal ``PhysicsObject`` test double exposing MadGraph's ``get``."""


class StructureRepository(list):
    def get_struct(self, identifier):
        for structure in self:
            if structure["id"] == identifier:
                return structure
        return None


def _leg(pdg):
    return Node(id=pdg)


def _vertex(*pdgs):
    return Node(legs=[_leg(pdg) for pdg in pdgs])


def _structure(identifier, binding, vertices, external=()):
    return Node(
        id=identifier,
        binding_leg=_leg(binding),
        external_legs=[_leg(pdg) for pdg in external],
        vertices=list(vertices),
    )


def _diagram(*structure_ids):
    # The first item in a real MG5 tag element is a loop Leg.  The runtime only
    # needs the referenced FDStructure IDs in item 1.
    return Node(tag=[(_leg(6), list(structure_ids), 1)])


def _box_route(mumu_polarization, ee_polarization):
    structures = StructureRepository(
        [
            _structure(
                3,
                ee_polarization,
                [_vertex(11, -11, ee_polarization)],
                (11, -11),
            ),
            _structure(
                5,
                mumu_polarization,
                [_vertex(13, -13, mumu_polarization)],
                (13, -13),
            ),
        ]
    )
    return _diagram(3, 5), structures


def test_standard_ll_tt_exclusions_and_explicit_mixed_tokens():
    assert runtime.PROCESS_FILTERS == {
        "ll": "/ a z za zt",
        "tt": "/ a z z0 za",
        "tl": "/ a z za --loop_filter=oap_tl",
        "lt": "/ a z za --loop_filter=oap_lt",
    }
    assert runtime.normalize_filter_token(" OAP_TL ") == "oap_tl"
    with pytest.raises(ValueError, match="unsupported"):
        runtime.normalize_filter_token("True")


@pytest.mark.parametrize(
    ("token", "mumu", "ee"),
    [
        ("oap_tl", runtime.ZT_PDG, runtime.Z0_PDG),
        ("oap_lt", runtime.Z0_PDG, runtime.ZT_PDG),
    ],
)
def test_box_diagrams_use_z1_mumu_z2_ee(token, mumu, ee):
    diagram, structures = _box_route(mumu, ee)
    assert runtime.diagram_matches_token(diagram, structures, token)
    other = "oap_lt" if token == "oap_tl" else "oap_tl"
    assert not runtime.diagram_matches_token(diagram, structures, other)


@pytest.mark.parametrize(
    ("token", "mumu", "ee"),
    [
        ("oap_tl", runtime.ZT_PDG, runtime.Z0_PDG),
        ("oap_lt", runtime.Z0_PDG, runtime.ZT_PDG),
    ],
)
def test_triangle_subtree_routing(token, mumu, ee):
    higgs_tree = _structure(
        8,
        25,
        [
            _vertex(11, -11, ee),
            _vertex(13, -13, mumu),
            _vertex(ee, mumu, 25),
        ],
        (11, -11, 13, -13),
    )
    assert runtime.diagram_matches_token(
        _diagram(8), StructureRepository([higgs_tree]), token
    )


@pytest.mark.parametrize("forbidden", [22, 23, 232])
def test_photon_zx_and_za_are_rejected_even_when_nested(forbidden):
    diagram, structures = _box_route(runtime.ZT_PDG, runtime.Z0_PDG)
    structures[0]["vertices"].append(_vertex(forbidden, 11, -11))
    assert not runtime.diagram_matches_token(diagram, structures, "oap_tl")


def test_unreferenced_bad_structure_does_not_change_a_diagram():
    diagram, structures = _box_route(runtime.ZT_PDG, runtime.Z0_PDG)
    structures.append(
        _structure(99, 22, [_vertex(22, 11, -11)], (11, -11))
    )
    assert runtime.diagram_matches_token(diagram, structures, "oap_tl")


def test_missing_or_ambiguous_decay_route_is_rejected():
    diagram, structures = _box_route(runtime.ZT_PDG, runtime.Z0_PDG)
    structures[1]["vertices"] = []
    assert not runtime.diagram_matches_token(diagram, structures, "oap_tl")

    diagram, structures = _box_route(runtime.ZT_PDG, runtime.Z0_PDG)
    structures[0]["vertices"].append(
        _vertex(11, -11, runtime.ZT_PDG)
    )
    assert not runtime.diagram_matches_token(diagram, structures, "oap_tl")


def test_hook_injection_is_narrow_and_idempotent():
    assert patcher._ANCHOR == (
        "        edit_filter_manually = False \n"
        "        if not edit_filter_manually and filter in [None,'None']:\n"
    )
    pristine = "prefix\n" + patcher._ANCHOR + "suffix\n"
    patched = patcher._inject_hook(pristine)
    assert "filter.lower() in ('oap_tl', 'oap_lt')" in patched
    assert "from madgraph.loop.oap_vpolar_filter import apply_oap_filter" in patched
    assert patcher._inject_hook(patched) == patched
    assert patched.count(patcher._BEGIN_SENTINEL) == 1
    assert patched.count(patcher._END_SENTINEL) == 1


def test_injection_rejects_an_unknown_source_layout():
    with pytest.raises(patcher.PatchError, match="anchor"):
        patcher._inject_hook("def unrelated():\n    pass\n")


def _amplitude_with_route(mumu_polarization, ee_polarization, count=44):
    diagram, structures = _box_route(mumu_polarization, ee_polarization)
    diagrams = [
        Node(diagram, multiplier=2 if index < 42 else 1)
        for index in range(count)
    ]
    return Node(
        loop_diagrams=diagrams,
        born_diagrams=[],
        has_born=False,
        structure_repository=structures,
    )


def test_installation_validator_uses_the_exact_full_processes():
    base = "g g > e+ e- mu+ mu- QED=4 QCD=2 [noborn = QCD]"
    assert validator.PROCESS_LINES == {
        "LL": f"{base} / a z za zt",
        "TT": f"{base} / a z z0 za",
        "TL": f"{base} / a z za --loop_filter=oap_tl",
        "LT": f"{base} / a z za --loop_filter=oap_lt",
    }
    assert all("/ h" not in process for process in validator.PROCESS_LINES.values())


def test_installation_validator_enables_python3_ufo_conversion_before_import(
    monkeypatch, tmp_path
):
    model = tmp_path / "SM_Loop_ZPolar"
    model.mkdir()
    (model / "__init__.py").write_text("", encoding="utf-8")

    class FakeCommand:
        def __init__(self):
            self.options = {"auto_convert_model": False}
            self._curr_amps = []
            self.commands = []

        def exec_cmd(self, command, **_kwargs):
            if command.startswith("import model "):
                assert self.options["auto_convert_model"] is True
            self.commands.append(command)

    command = FakeCommand()
    monkeypatch.setattr(
        validator,
        "_load_master_command",
        lambda _root: lambda: command,
    )
    monkeypatch.setattr(
        validator,
        "validate_channel_amplitudes",
        lambda channel, _amplitudes: {"channel": channel},
    )

    reports = validator.validate_installation(tmp_path, model)

    assert command.options["auto_convert_model"] is True
    assert command.commands[0].startswith("import model ")
    assert [report["channel"] for report in reports] == ["LL", "TT", "TL", "LT"]


@pytest.mark.parametrize(
    ("channel", "mumu", "ee"),
    [
        ("LL", runtime.Z0_PDG, runtime.Z0_PDG),
        ("TT", runtime.ZT_PDG, runtime.ZT_PDG),
        ("TL", runtime.ZT_PDG, runtime.Z0_PDG),
        ("LT", runtime.Z0_PDG, runtime.ZT_PDG),
    ],
)
def test_installation_validator_checks_count_and_flavor_route(
    channel, mumu, ee
):
    report = validator.validate_channel_amplitudes(
        channel, [_amplitude_with_route(mumu, ee)]
    )
    assert report["loop_diagrams"] == 44
    assert report["raw_equivalent_loop_diagrams"] == 86
    assert report["route"] == {"mumu": mumu, "ee": ee}


def test_installation_validator_rejects_count_or_route_drift():
    with pytest.raises(validator.DiagramValidationError, match="expected 44"):
        validator.validate_channel_amplitudes(
            "LL",
            [_amplitude_with_route(runtime.Z0_PDG, runtime.Z0_PDG, count=43)],
        )

    with pytest.raises(validator.DiagramValidationError, match="violate flavor"):
        validator.validate_channel_amplitudes(
            "TL", [_amplitude_with_route(runtime.Z0_PDG, runtime.ZT_PDG)]
        )

    raw_count_drift = _amplitude_with_route(
        runtime.Z0_PDG, runtime.Z0_PDG
    )
    raw_count_drift["loop_diagrams"][0]["multiplier"] = 1
    with pytest.raises(
        validator.DiagramValidationError, match="raw-equivalent"
    ):
        validator.validate_channel_amplitudes("LL", [raw_count_drift])
