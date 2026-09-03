# VPolarized standalone generation

This backend produces polarized loop-induced
$gg\to e^+e^-\mu^+\mu^-$ events using the `SM_Loop_ZPolar` UFO from
[VPolarization](https://feynrules.irmp.ucl.ac.be/wiki/VPolarization), following
the construction of [arXiv:2401.17365](https://arxiv.org/abs/2401.17365).
It is separate from the ATLAS `Gen_tf.py` backend because this UFO and its
MadGraph loop selection are not distributed in AthGeneration.

Every process in this directory has the same deliberately narrow definition:

- exactly the `e+ e- mu+ mu-` final state;
- the full Higgs-mediated plus continuum-box amplitude, including their
  interference;
- no photon-mediated diagrams;
- $50\leq m_{ee},m_{\mu\mu}\leq200$ GeV and
  $150\leq m_{4\ell}\leq3000$ GeV; and
- 13.6 TeV proton-proton collisions.

There are no signal-only, background-only, inclusive-flavour, or on-shell
VPolar process cards in this project.

## Polarization convention

`Z1` is always the dimuon system and `Z2` the dielectron system. The released
UFO defines `z0` (PDG 230) as longitudinal and `zt` (PDG 231) as the coherent
sum of the two transverse helicities.

| Process | `Z1 -> mu+mu-` | `Z2 -> e+e-` | Loop diagrams (representatives / raw-equivalent) |
|---|---|---|---:|
| `vpolar_LL` | longitudinal | longitudinal | 44 / 86 |
| `vpolar_TT` | transverse | transverse | 44 / 86 |
| `vpolar_TL` | transverse | longitudinal | 44 / 86 |
| `vpolar_LT` | longitudinal | transverse | 44 / 86 |

LL and TT use ordinary MadGraph particle exclusions. TL and LT use the two
explicit, flavour-aware filters in `loop_filter_runtime.py`. Generating them as
separate matrix elements removes the TL/LT amplitude interference. Their
campaign outputs may therefore be concatenated to form the requested
incoherent `TL+LT` sample.

The public reference repository's generic `--loop_filter=True` implementation
is not copied. Its current single-polarization switch selects TT rather than
the documented mixed mode. This project instead inserts only the two named
filters `oap_tl` and `oap_lt` into the stock MadGraph hook. Installation runs
`validate_diagram_counts.py`, which generates all four amplitudes in memory and
requires all `4 x (44 representatives / 86 raw-equivalent)` counts and the
exact flavour routes above before it
builds any process bundle.

## One-time shared installation

Install on a UChicago AF login/build node with a Fortran compiler, C++
compiler, CMake, make, and an existing LHAPDF6 installation containing
`NNPDF31_nlo_as_0118_luxqed` (ID 324900):

```bash
Generation/VPolar/install_vpolar.sh \
  --prefix /data/$USER/offshell/software/vpolar \
  --lhapdf-config /path/to/lhapdf-config \
  --cores 8
```

The installer downloads and SHA-256 verifies MadGraph5_aMC 3.4.2, Pythia
8.306, HepMC2 2.06.09, the MG5/Pythia interface 1.3, zlib 1.2.10, and the
public UFO. MadGraph 3.4.2 is from the same 3.4 release series used in the
paper; the loop hook is pinned to the exact stock 3.4.2 source hash and was
validated directly against that API. The source URLs, versions, and digests
are centralized in `sources.json`.

The UFO is downloaded rather than vendored. The installer extracts archives
without restoring archive ownership, builds the shower stack, generates four
process bundles, and writes `installation-manifest.json` last. The manifest
binds the installed software, process definitions, diagram report, cards,
runtime scripts, the Pythia runtime and HepMC static-link libraries, Pythia
XML/tune data, and the exact LHAPDF library, set metadata, and central member
used by ID 324900. Each job revalidates those fingerprints and prepends the
recorded PDF-data directory to `LHAPDF_DATA_PATH`. A compatible completed
prefix is accepted idempotently. An incomplete or changed prefix is never overwritten
automatically.

Loop export remains optimized, but Ninja and COLLIER are explicitly disabled
and MadLoop is pinned to reduction-library ID 1 (CutTools). Generated processes
link the CutTools library compiled from MadGraph's pinned bundled source. This
avoids MadGraph's first-loop online installer and any ambient reduction library.
The installer accepts a process only after that library and module exist and
the exported subprocess manifest contains optimized loop and MadEvent matrix
sources; the early `generate_events` wrapper alone is not considered success.

MadGraph is distributed under its NCSA-style license and Pythia under GPLv2;
their notices remain in the downloaded installations.

## Common generation interface

The normal dispatcher selects this backend from the process name:

```bash
Generation/run_generation.sh vpolar_LL \
  --events 50 --seed 21001 --first-event 1 \
  --generator-prefix /data/$USER/offshell/software/vpolar \
  --output-dir /data/$USER/offshell/smoke/vpolar_LL_seed21001
```

The same command works for `vpolar_TT`, `vpolar_TL`, and `vpolar_LT`.
`OAP_VPOLAR_PREFIX` can supply the prefix. The runner validates the immutable
manifest before claiming its output path, copies the selected process bundle
to private job storage, and generates a 10% LHE safety margin for shower
retries.

The run card uses `event_norm=average`, so MadGraph emits `IDWTUP=-4` and
pb-valued sample-mean event weights. The common LHE helper adds
`AUX_OAP_EVENT_ID` and `AUX_OAP_EVENT_UNIT` before showering. The standalone
MG5/Pythia interface normally decorates those names; `canonicalize_hepmc.py`
requires one unambiguous occurrence of each, restores their exact names, and
renumbers HepMC events from `--first-event`. The Pythia output scale is
`1e9 * requested_events`, exactly undoing the interface's weighted-event
serialization factor without changing the LHE normalization.

The final alignment step uses the same ratio-based event join as the ATLAS
backend. A completed directory contains:

| File | Meaning |
|---|---|
| `events.hepmc` | Canonical HepMC2 shower output |
| `LHE.TXT.tar.gz` | Tagged phase-space-selected LHE stream supplied to Pythia |
| `events.matched.lhe.gz` | Exact LHE hard events in HepMC order |
| `lhe-contract-metadata.json` | IDWTUP, counts, moments, and normalization contract |
| `alignment-metadata.json` | Backend-neutral hashes and exact matching proof |
| `installation-manifest.json` | Validated generator, Pythia-data, and LHAPDF fingerprints |
| `run-metadata.txt` | Full/polarization/software/card provenance |
| `madgraph-*.dat`, `madgraph-generation.mg5`, `pythia8-card.cmnd` | Frozen job cards, including the CutTools-only MadLoop card |
| `transform.stdout.log` | MadGraph, shower, canonicalization, and alignment log |
| `SUCCESS` | Written only after all validations pass |

There is no synthetic EVNT file and no AthGeneration release field for this
backend. Downstream `Runs` fields reserved for the legacy AthGeneration version
are zero only as a documented not-applicable encoding; authoritative generator
versions live in the embedded provenance.

## HTCondor

Workers must consume an installation already visible on shared storage:

```bash
python UChicagoAF/condor/submit_campaign.py vpolar_TL \
  --jobs 20 --events-per-job 50 --seed-base 30001 \
  --campaign-id 20260902 \
  --generator-prefix /data/$USER/offshell/software/vpolar \
  --output-root /data/$USER/offshell/production
```

See `UChicagoAF/condor/README.md`. Installation is intentionally never run in
an execute-node job.

## Lightweight validation

The repository tests do not build the external generator stack:

```bash
uv run --frozen --extra test python -m pytest -q Generation/VPolar
bash -n Generation/VPolar/*.sh
```

They cover exact process syntax, pinned inputs, the loop-filter route, diagram
count contract, HepMC marker canonicalization, and installer dry-run behavior.
