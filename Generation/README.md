# ATLAS event generation

This directory provides the first stage of the
`Generation -> Simulation -> Analysis` chain on the UChicago Analysis
Facility. `run_generation.sh` dispatches `gg4l` and `qqZZ` to the ATLAS
`Gen_tf.py` stack, and `vpolar_LL`, `vpolar_TT`, `vpolar_TL`, and `vpolar_LT`
to the separately installed MadGraph/Pythia backend in `VPolar/`. Herwig is
not used by either path.

The two local-only run numbers are:

| Process | Local run number | Matrix element and final state |
|---|---:|---|
| `gg4l` | 100001 | POWHEG-BOX-RES `gg -> (H* + continuum + interference) -> 2e2mu` |
| `qqZZ` | 100002 | POWHEG `qq -> ZZ -> 2e2mu` |
| `vpolar_LL` | 100003 | VPolar full loop-induced `gg -> ZL ZL -> 2e2mu` |
| `vpolar_TT` | 100004 | VPolar full loop-induced `gg -> ZT ZT -> 2e2mu` |
| `vpolar_TL` | 100005 | VPolar full loop-induced `gg -> ZT(mu mu) ZL(e e) -> 2e2mu` |
| `vpolar_LT` | 100006 | VPolar full loop-induced `gg -> ZL(mu mu) ZT(e e) -> 2e2mu` |

These numbers are identifiers for local production, not registered ATLAS
DSIDs.

The VPolar rows are exclusive `e+ e- mu+ mu-` and retain Higgs, continuum-box,
and Higgs/box interference diagrams. See `VPolar/README.md` for the one-time
installation, exact process definitions, and validated incoherent `TL+LT`
construction.

## Run at the UChicago AF

The default release is `AthGeneration 23.6.41`, on which the gg4l source card
was validated. This is also the first of the two source-card releases that has
the `Gen_tf.py --outputEvtFile` executor needed to convert EVNT to a
HepMC2-compatible ASCII file. The older qqZZ validation release, 23.6.18, did
not have that output executor.

On a UChicago AF host with ATLAS CVMFS mounted:

```bash
cd OffshellAngularProduction/Generation

# Small smoke tests first.
./run_generation.sh gg4l --events 2 --seed 101 \
  --output-dir /data/$USER/offshell/smoke/generation/gg4l_seed101
./run_generation.sh qqZZ --events 10 --seed 201 \
  --output-dir /data/$USER/offshell/smoke/generation/qqZZ_seed201

# Normal local job sizes.
./run_generation.sh gg4l --events 50 --seed 1101 \
  --output-dir /data/$USER/offshell/production/gg4l_seed1101
./run_generation.sh qqZZ --events 1000 --seed 1201 \
  --output-dir /data/$USER/offshell/production/qqZZ_seed1201
```

The wrapper sources
`/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase/user/atlasLocalSetup.sh` and runs
`asetup 23.6.41,AthGeneration`. In a container or shell where that release is
already active, pass `--no-setup`. Use `--dry-run` to inspect the resolved
transform command. Before claiming `--output-dir`, a real run verifies the
CVMFS setup, successful `asetup`, active project/release, `Gen_tf.py`, and the
release-specific `--outputEvtFile` interface. A preflight failure therefore
leaves no output claim and can be retried with the same path. Once the
transform starts, its private `.work.*` directory is retained on failure for
diagnosis.

Every production job must have a distinct positive seed. For a split campaign,
also make event-number ranges disjoint, for example
`--first-event $((job_index * events_per_job + 1))`. The wrapper accepts seeds
from 1 through 999999999 and passes the same seed to the ATLAS transform and
POWHEG.

The effective transform invocation is:

```bash
ATHENA_CORE_NUMBER=1 Gen_tf.py \
  --ecmEnergy=13600 \
  --runNumber=100001 \
  --firstEvent=1 \
  --maxEvents=50 \
  --randomSeed=101 \
  --jobConfig=/absolute/path/to/Generation/jobOptions/100001 \
  --outputEVNTFile=EVNT.pool.root \
  --outputEvtFile=events.hepmc \
  --outputTXTFile=LHE.TXT.tar.gz
```

`--outputEvtFile` is the correct HepMC option in release 23.6.41. Newer Athena
main releases call the corresponding option `--outputHEPMCFile`; substituting
that newer spelling in the pinned release will fail.

## Outputs

Use `/data/$USER` for normal AF production. If `--output-dir` is omitted, the
default is the repository-local `Generation/runs/PROCESS_seedSEED/`; that
default is suitable only for smoke tests and other small jobs. A completed run
directory contains:

| File | Meaning |
|---|---|
| `EVNT.pool.root` | ATLAS EVNT output |
| `events.hepmc` | HepMC2-compatible ASCII exported from that EVNT file |
| `LHE.TXT.tar.gz` | Phase-space-selected, source-tagged POWHEG LHE sidecar supplied to Pythia |
| `lhe-contract-metadata.json` | Pre-shower filter counts, signed/absolute sums and efficiencies, and technical-weight contract |
| `events.matched.lhe.gz` | Exact tagged hard event for every HepMC event, in HepMC order |
| `alignment-metadata.json` | Source-ID sequence, counts, hashes, and checked named-weight matching contract |
| `run-metadata.txt` | Small key/value provenance record used by later stages |
| `SUCCESS` | Completion marker written only after all matching checks pass |
| `transform.stdout.log`, `log.*`, `jobReport.json` | Transform diagnostics when produced |
| `integration_grids.tar.gz` | Newly generated POWHEG grids when produced |
| `integration_grids.tar.gz.metadata.json` | Physics/release/beam-energy fingerprint required for grid reuse |

The simulation runner can consume `events.hepmc` directly. Its adjacent
`run-metadata.txt` records `process=gg4l|qqZZ` and `seed=...` so the next stage
does not have to infer either from the filename.

Generated run directories are intentionally not committed.

## Exact LHE-to-HepMC association

When `Gen_tf.py` is asked for EVNT output, PowhegControl starts with

```text
int(1.1 * maxEvents + 0.5)
```

LHE events as a showering safety margin. The qqZZ job option doubles that base
stream (approximately $2.2\,N$ events) to leave headroom for its active
pre-shower mass filter. After POWHEG completes and before Pythia begins,
`offshell_lhe_contract.prepare_lhe_for_shower`:

- applies the configured LHE-level $m_{4\ell}$ range;
- adds two named technical weights, `AUX_OAP_EVENT_ID` and
  `AUX_OAP_EVENT_UNIT`, without changing the nominal physics weight;
- assigns the original positive source-event index to the first weight and one
  to the second; and
- records generated/accepted counts, signed and absolute-weight sums, and
  signed and absolute filter efficiencies in `lhe-contract-metadata.json`.

Pythia8 applies the same shower factor to both technical weights, so
`AUX_OAP_EVENT_ID/AUX_OAP_EVENT_UNIT` recovers the integral source-event ID in
HepMC2. `align_lhe_events.py` implements the `named-weight-id-v1` contract: it
decodes that ratio for every HepMC event, requires strictly increasing IDs,
selects the identically tagged LHE records, and writes them in HepMC order.
The matched LHE is therefore exactly truncated to the requested HepMC event
count, while gaps caused by LHE filtering or shower skips/retries are preserved
rather than misinterpreted as an ordinal prefix. The transform log records
retry/rejection observations; an exhausted Pythia failure allowance is fatal.

The aligner also validates the pre-shower metadata, the complete HepMC2 event
listing and named-weight schema, the source tags in every LHE record, absence
of a post-shower generator filter, and hashes of all contract inputs. It writes
`events.matched.lhe.gz` only after these checks. Any failure leaves the working
directory available for diagnosis.

## Physics configuration and source provenance

The gg4l card is derived from ATLAS PMG
[DSID 602686](https://gitlab.cern.ch/atlas-physics/pmg/mcjoboptions/-/blob/1be4bbe9601b521451b2c22523c0377223b61b94/602xxx/602686/mc.PhPy8_NNPDF30_gg4l_m4l_70_3000.py),
validated with AthGeneration 23.6.41. Relative to that card it uses:

```python
PowhegConfig.contr = "full"
PowhegConfig.vdecaymodeV1 = 11
PowhegConfig.vdecaymodeV2 = 13
PowhegConfig.mllmin = 50
PowhegConfig.mllmax = 200
PowhegConfig.m4lmin = 150
PowhegConfig.m4lmax = 3000
```

`full` includes the Higgs-mediated amplitude, continuum amplitude, and their
interference. The explicit 11/13 modes request the only directly supported
exclusive gg4l ZZ decay, 2e2mu. Leaving the source card's `ll`/`ll` values
would invoke the `gg4l_emu2all` LHE postprocessor and create an inclusive 4l
sample. The remaining integration, scale, PDF, and Pythia settings are retained
from the PMG card. See the
[release-23.6.41 gg4l PowhegControl wrapper](https://gitlab.cern.ch/atlas/athena/-/blob/release/23.6.41/Generators/PowhegControl/python/processes/powheg/gg4l.py)
for the supported process, contribution, decay, and mass keywords.

The qqZZ card is derived from ATLAS PMG
[DSID 603269](https://gitlab.cern.ch/atlas-physics/pmg/mcjoboptions/-/blob/78cb99075450b6505fa923e44ec8d3c0ff29c5a8/603xxx/603269/mc.PhPy8EG_ZZllll_mll4.py),
originally validated with AthGeneration 23.6.18 and run here with the pinned
23.6.41 transform. Its POWHEG configuration requests the fixed flavour and
dilepton lower bound:

```python
PowhegConfig.decay_mode = "z z > mu+ mu- e+ e-"
PowhegConfig.mllmin = 50.0
```

The exact decay-mode spelling is one of the modes accepted by the
[POWHEG ZZ wrapper](https://gitlab.cern.ch/atlas/athena/-/blob/release/23.6.18/Generators/PowhegControl/python/processes/powheg/ZZ.py).

### qqZZ LHE-level four-lepton filter

The POWHEG ZZ process exposes `mllmin` but no native `m4lmin` or `m4lmax`.
ATLAS's neighboring official m4l-sliced samples use a post-Pythia
[`FourLeptonInvMassFilter`](https://gitlab.cern.ch/atlas-physics/pmg/mcjoboptions/-/blob/78cb99075450b6505fa923e44ec8d3c0ff29c5a8/603xxx/603270/mc.PhPy8EG_ZZllll_mll4_m4l100_170.py).
This project instead applies $150\leq m_{4\ell}\leq3000$ GeV directly to the
completed LHE stream before Pythia reads it. The filtered/tagged stream is also
repacked into `LHE.TXT.tar.gz`, so the sidecar and shower input are the same.
No post-shower `FourLeptonInvMassFilter` is configured.

The 150 GeV lower edge aligns qqZZ with the gg4l and VPolar generation phase
space, while the 3000 GeV upper edge remains active. Neither edge is a
reconstructed analysis cut: the current selection has only
$m_{4\ell}>180$ GeV and `analysis_m4l_max_gev=none`.
Because POWHEG `ZZ` has no native four-lepton-mass keyword, the rejected hard
events are still generated; the computational saving starts with Pythia and
the downstream simulation and analysis stages. The doubled LHE safety stream
prevents the new filter from relying on PowhegControl's standard 10% margin.

The helper requires POWHEG `IDWTUP=-4` and records the authoritative filtered
cross section as `sumw_accepted / generated_lhe_events`, in pb. Rejected events
therefore contribute zero. It also records squared-weight sums and the unbiased
finite-sample MC error; these primitive moments must be summed across jobs
before a campaign normalization is recomputed. LHE `<init>` values and running
HepMC/Delphes cross-section fields are retained as diagnostics only.

### Integration grids

Do **not** reuse the GRID tarball distributed with DSID 602686. Besides pointing
to a CERN EOS location that may not be readable at UChicago, it was integrated
for `contr=no_h`, inclusive flavours, `mllmin=10`, and `m4lmin=70`. All four
settings change here, so its grids and upper bounds are invalid.

The first gg4l run without `--gridpack` may spend substantial time rebuilding
the integration. The wrapper preserves the resulting grid archive and creates
an adjacent metadata manifest that binds its SHA-256 digest to the complete job
option, process, local run number, AthGeneration release, and 13600 GeV
collision energy. After physics validation, reuse that exact file only for
jobs with all of those settings unchanged:

Although the job option retains `manyseeds=1` and `parallelstage=4`, this does
not require prebuilt grids. PowhegControl's RES multicore scheduler checks for
each stage's grid files and rewrites `parallelstage` to 1, 2, 3, and 4 in turn;
when no gridpack is present, the missing-file checks cause every required stage
to run. `ATHENA_CORE_NUMBER=1` still uses this staged scheduler with one worker.

```bash
./run_generation.sh gg4l --events 50 --seed 1102 \
  --output-dir /data/$USER/offshell/production/gg4l_seed1102 \
  --gridpack /data/$USER/offshell/production/gg4l_seed1101/integration_grids.tar.gz
```

The default manifest path is `GRIDPACK.metadata.json`; use
`--gridpack-metadata FILE` only if the two files were deliberately renamed or
moved apart. Both the archive and manifest are canonicalized with
`realpath -e` and must be readable regular files before any output is claimed;
missing, dangling-symlink, or mismatched metadata is fatal. Archives containing
links, special files, absolute paths, or parent-directory traversal are also
rejected before extraction. Any change to the process contribution, masses,
cuts, PDFs, scales, integration controls, or release requires fresh grids.
Changing the 13600 GeV collision energy also requires fresh grids.

## Tests

The tests do not require Athena:

```bash
uv run --frozen --extra test python -m pytest -q Generation/tests
bash -n Generation/run_generation.sh
```

They exercise exact named-weight matching with source-ID gaps, LHE truncation,
phase-space filtering and efficiency metadata, count/contract validation, both
physics-card settings, gridpack compatibility, and the release-specific HepMC
transform argument.
