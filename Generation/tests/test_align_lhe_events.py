from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


GENERATION_DIR = Path(__file__).resolve().parents[1]
ALIGNER = GENERATION_DIR / "align_lhe_events.py"
SPEC = importlib.util.spec_from_file_location("align_lhe_events", ALIGNER)
assert SPEC is not None and SPEC.loader is not None
ALIGNMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ALIGNMENT)


SOURCE_IDS = [1, 3, 4, 6, 7, 8]
SHOWERED_IDS = [1, 3, 6, 7, 8]


def lhe_document(source_ids: list[int]) -> str:
    events = []
    for source_id in source_ids:
        events.append(
            "<event>\n"
            f" 0 1 {(-1) ** source_id}.0 91.188 0.007297 0.118\n"
            " <rwgt>\n"
            f"  <wgt id='AUX_OAP_EVENT_ID'> {source_id} </wgt>\n"
            "  <wgt id='AUX_OAP_EVENT_UNIT'> 1 </wgt>\n"
            " </rwgt>\n"
            f" # AUX_OAP_EVENT_ID {source_id}\n"
            f" # synthetic-event-{source_id}\n"
            "</event>\n"
        )
    return (
        '<LesHouchesEvents version="3.0">\n'
        "<header><initrwgt><weightgroup name='OffshellAngularProduction'>\n"
        "<weight id='AUX_OAP_EVENT_ID'>AUX_OAP_EVENT_ID</weight>\n"
        "<weight id='AUX_OAP_EVENT_UNIT'>AUX_OAP_EVENT_UNIT</weight>\n"
        "</weightgroup></initrwgt></header>\n"
        "<init>\n 1 1 1 1 1 1 1 1 1 1\n</init>\n"
        + "".join(events)
        + "</LesHouchesEvents>\n"
    )


def hepmc_document(source_ids: list[int], *, factor: float = 1.23456789) -> str:
    names = (
        'N 4 "AUX_OAP_EVENT_ID" "AUX_OAP_EVENT_UNIT" "Default" "scale"\n'
    )
    events = []
    for event_number, source_id in enumerate(source_ids, start=101):
        weights = [source_id * factor, factor, (-1) ** source_id * factor, 2 * factor]
        weight_text = " ".join(f"{weight:.8e}" for weight in weights)
        events.append(
            f"E {event_number} -1 1.0 0.1 0.01 1 0 1 1 2 0 4 {weight_text}\n"
            + names
            + "U GEV MM\n"
        )
    return (
        "HepMC::Version 2.06.11\n"
        "HepMC::IO_GenEvent-START_EVENT_LISTING\n"
        + "".join(events)
        + "HepMC::IO_GenEvent-END_EVENT_LISTING\n"
    )


class NamedWeightAlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.archive = self.root / "LHE.TXT.tar.gz"
        lhe_file = self.root / "LHE.TXT.events"
        lhe_file.write_text(lhe_document(SOURCE_IDS), encoding="utf-8")
        with tarfile.open(self.archive, "w:gz") as archive:
            archive.add(lhe_file, arcname=lhe_file.name)
        self.lhe_contract = self.root / "lhe-contract-metadata.json"
        self.lhe_contract.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "contract": "named-weight-id-v1",
                    "normalization_contract": "idwtup-minus4-sample-mean-v1",
                    "nominal_weight_units": "pb",
                    "lhe_weighting_strategy": -4,
                    "cross_section_method": (
                        "mean nominal LHE weight; rejected events assigned zero "
                        "for filtered estimate"
                    ),
                    "process": "qqZZ",
                    "marker_id_weight": "AUX_OAP_EVENT_ID",
                    "marker_unit_weight": "AUX_OAP_EVENT_UNIT",
                    "requested_hepmc_events": 5,
                    "m4l_min_gev": 70.0,
                    "m4l_max_gev": 3000.0,
                    "generated_lhe_events": 8,
                    "accepted_lhe_events": 6,
                    "rejected_below_m4l": 1,
                    "rejected_above_m4l": 1,
                    "sumw_generated": 4.0,
                    "sumw_accepted": 3.0,
                    "sumw2_generated": 12.0,
                    "sumw2_accepted": 9.0,
                    "sumabsw_generated": 8.0,
                    "sumabsw_accepted": 6.0,
                    "count_filter_efficiency": 0.75,
                    "signed_filter_efficiency": 0.75,
                    "absolute_filter_efficiency": 0.75,
                    "lhe_init": {
                        "idwtup": -4,
                        "process_count": 1,
                        "processes": [
                            {
                                "process_id": 1,
                                "cross_section_pb": 0.51,
                                "cross_section_error_pb": 0.02,
                                "maximum_weight_pb": 1.0,
                            }
                        ],
                        "inclusive_cross_section_pb": 0.51,
                        "inclusive_cross_section_error_pb": 0.02,
                    },
                    "inclusive_cross_section_pb": 0.5,
                    "inclusive_cross_section_mc_error_pb": (5.0 / 28.0) ** 0.5,
                    "filtered_cross_section_pb": 0.375,
                    "filtered_cross_section_mc_error_pb": 0.375,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.hepmc = self.root / "events.hepmc"
        self.hepmc.write_text(hepmc_document(SHOWERED_IDS), encoding="utf-8")
        self.job_option = self.root / "mc.test.py"
        self.job_option.write_text(
            'include("PowhegControl/PowhegControl_ZZ_Common.py")\n'
            "PowhegConfig.generate()\n",
            encoding="utf-8",
        )
        self.log = self.root / "transform.log"
        self.log.write_text("clean transform\n", encoding="utf-8")
        self.output = self.root / "events.matched.lhe.gz"
        self.metadata = self.root / "alignment-metadata.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self) -> list[str]:
        return [
            sys.executable,
            str(ALIGNER),
            "--lhe-archive",
            str(self.archive),
            "--lhe-contract-metadata",
            str(self.lhe_contract),
            "--hepmc",
            str(self.hepmc),
            "--job-option",
            str(self.job_option),
            "--transform-log",
            str(self.log),
            "--output",
            str(self.output),
            "--metadata",
            str(self.metadata),
            "--expected-events",
            "5",
            "--expected-m4l-min",
            "70",
            "--expected-m4l-max",
            "3000",
            "--process",
            "qqZZ",
            "--run-number",
            "100002",
            "--seed",
            "17",
            "--first-event",
            "101",
            "--release",
            "23.6.41",
            "--contract",
            "named-weight-id-v1",
        ]

    def test_matches_named_ids_not_an_lhe_prefix(self) -> None:
        result = subprocess.run(self.command(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with gzip.open(self.output, "rt", encoding="utf-8") as stream:
            aligned = stream.read()
        self.assertEqual(aligned.count("<event>"), 5)
        self.assertNotIn("synthetic-event-4", aligned)
        self.assertIn("synthetic-event-8", aligned)
        self.assertTrue(aligned.rstrip().endswith("</LesHouchesEvents>"))

        metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata["contract"], "named-weight-id-v1")
        self.assertEqual(metadata["counts"]["phase_space_lhe_events"], 6)
        self.assertEqual(metadata["counts"]["matched_lhe_events"], 5)
        self.assertEqual(
            metadata["phase_space_filter"]["normalization_contract"],
            "idwtup-minus4-sample-mean-v1",
        )
        self.assertEqual(metadata["phase_space_filter"]["lhe_init"]["idwtup"], -4)
        self.assertEqual(
            metadata["phase_space_filter"]["filtered_cross_section_pb"], 0.375
        )
        self.assertEqual(metadata["marker"]["id_weight_index"], 0)
        self.assertEqual(
            metadata["marker"]["source_id_sequence_sha256"],
            ALIGNMENT.source_id_sequence_sha256(SHOWERED_IDS),
        )
        self.assertEqual(
            metadata["files"]["lhe_contract_metadata"]["path"],
            self.lhe_contract.name,
        )

    def test_rounded_hepmc_weights_decode_at_atlas_job_scale(self) -> None:
        source_id = 100_001
        text = hepmc_document([source_id])
        path = self.root / "rounded.hepmc"
        path.write_text(text, encoding="utf-8")
        _, decoded, _, _, _ = ALIGNMENT.read_hepmc_source_ids(path)
        self.assertEqual(decoded, [source_id])

    def test_records_pythia_retry_without_breaking_exact_matching(self) -> None:
        self.log.write_text(
            "Pythia8_i Event generation failed - re-trying.\n", encoding="utf-8"
        )
        result = subprocess.run(self.command(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata["transform_log_observations"]["pythia_retry"], 1)

    def test_rejects_missing_named_marker(self) -> None:
        self.hepmc.write_text(
            hepmc_document(SHOWERED_IDS).replace(
                '"AUX_OAP_EVENT_UNIT"', '"not-the-unit-marker"'
            ),
            encoding="utf-8",
        )
        result = subprocess.run(self.command(), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing named marker", result.stderr.lower())

    def test_rejects_wrong_hepmc_count(self) -> None:
        self.hepmc.write_text(hepmc_document(SHOWERED_IDS[:-1]), encoding="utf-8")
        result = subprocess.run(self.command(), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HepMC has 4 events; expected 5", result.stderr)

    def test_rejects_wrong_lhe_contract_schema(self) -> None:
        payload = json.loads(self.lhe_contract.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        self.lhe_contract.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        result = subprocess.run(self.command(), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version=1 (expected 2)", result.stderr)

    def test_rejects_cross_section_inconsistent_with_nominal_weights(self) -> None:
        payload = json.loads(self.lhe_contract.read_text(encoding="utf-8"))
        payload["filtered_cross_section_pb"] = 0.4
        self.lhe_contract.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        result = subprocess.run(self.command(), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disagrees with weight sums/counts", result.stderr)


if __name__ == "__main__":
    unittest.main()
