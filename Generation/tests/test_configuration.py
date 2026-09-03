from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


GENERATION_DIR = Path(__file__).resolve().parents[1]


class GenerationConfigurationTest(unittest.TestCase):
    def test_gg4l_physics_configuration(self) -> None:
        card = (
            GENERATION_DIR
            / "jobOptions/100001/mc.PhPy8_NNPDF30_gg4l_full_2e2mu_m4l150_3000.py"
        ).read_text(encoding="utf-8")
        for setting in (
            'PowhegConfig.proc = "ZZ"',
            'PowhegConfig.contr = "full"',
            "PowhegConfig.vdecaymodeV1 = 11",
            "PowhegConfig.vdecaymodeV2 = 13",
            "PowhegConfig.mllmin = 50",
            "PowhegConfig.m4lmin = 150",
            "PowhegConfig.m4lmax = 3000",
        ):
            self.assertIn(setting, card)
        self.assertNotIn("PowhegConfig.nEvents *=", card)
        self.assertNotIn("GeneratorFilters/", card)

    def test_qqzz_physics_configuration(self) -> None:
        card = (
            GENERATION_DIR / "jobOptions/100002/mc.PhPy8EG_ZZ2e2mu_mll50.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PowhegConfig.decay_mode = "z z > mu+ mu- e+ e-"', card
        )
        self.assertIn("PowhegConfig.mllmin = 50.0", card)
        self.assertIn("min_m4l=150.0", card)
        self.assertIn("max_m4l=3000.0", card)
        self.assertIn("PowhegConfig.nEvents *= 2.0", card)
        self.assertLess(
            card.index("PowhegConfig.nEvents *= 2.0"),
            card.index("PowhegConfig.generate()"),
        )
        self.assertIn(
            'evgenConfig.generators = ["Powheg", "Pythia8", "EvtGen"]', card
        )
        active = "\n".join(line.split("#", 1)[0] for line in card.splitlines())
        self.assertNotIn("GeneratorFilters/", active)
        self.assertNotIn("filtSeq", active)

    def test_wrapper_uses_release_specific_hepmc_argument(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(GENERATION_DIR / "run_generation.sh"),
                "gg4l",
                "--events",
                "5",
                "--seed",
                "7",
                "--dry-run",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--outputEVNTFile=EVNT.pool.root", result.stdout)
        self.assertIn("--outputEvtFile=events.hepmc", result.stdout)
        self.assertIn("--outputTXTFile=LHE.TXT.tar.gz", result.stdout)
        self.assertIn("--jobConfig=", result.stdout)
        self.assertIn("jobOptions/100001", result.stdout)

        runner = (GENERATION_DIR / "run_generation.sh").read_text(encoding="utf-8")
        self.assertIn('"$SCRIPT_DIR/align_lhe_events.py"', runner)
        self.assertEqual(runner.count("GENERATOR_M4L_MIN_GEV=150"), 2)
        self.assertNotIn("GENERATOR_M4L_MIN_GEV=70", runner)
        for option in (
            "--lhe-contract-metadata",
            "--expected-m4l-min",
            "--expected-m4l-max",
            "--contract named-weight-id-v1",
        ):
            self.assertIn(option, runner)

    def test_wrapper_rejects_event_counts_beyond_marker_precision_contract(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(GENERATION_DIR / "run_generation.sh"),
                "qqZZ",
                "--events",
                "100001",
                "--dry-run",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("from 1 through 100000", result.stderr)

    def test_wrapper_rejects_integer_overflow_inputs(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(GENERATION_DIR / "run_generation.sh"),
                "gg4l",
                "--seed",
                "18446744073709551616",
                "--dry-run",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("999999999", result.stderr)


if __name__ == "__main__":
    unittest.main()
