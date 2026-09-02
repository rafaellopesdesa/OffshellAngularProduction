# End-to-end job worker

`run_chain.sh` executes one local job through all three stages and is the
worker entry point intended for a later HTCondor layer. It keeps the heavy EVNT,
LHE, HepMC, and Delphes files in a stage directory while allowing the compact
analysis ROOT file to be placed separately for transfer.

Before running it:

1. install the Python package and dependencies;
2. build Delphes once with `Simulation/install_delphes.sh` in the ROOT
   environment that will run simulation; and
3. run from a UChicago AF shell/container with ATLAS CVMFS available.

Example smoke jobs from the repository root:

```bash
Workflow/run_chain.sh gg4l \
  --events 2 --seed 101 --job-id 0 --campaign-id 20260902 \
  --output-dir /data/$USER/offshell/smoke/gg4l_job0

Workflow/run_chain.sh qqZZ \
  --events 10 --seed 201 --job-id 0 --campaign-id 20260902 \
  --output-dir /data/$USER/offshell/smoke/qqZZ_job0
```

The generation setup runs in a child process, so it cannot contaminate the
simulation or Python environment of the caller. `Simulation/env.sh` must point
to a Delphes build compatible with the active ROOT environment. Use
`--analysis-python` if the project dependencies are not installed in the
default `python3`.

The stage directory must not already exist. An external `--analysis-output`
parent is created and write-probed before that directory is claimed. Generation
environment/interface failures remove only a still-empty claim, so the same
path is immediately retryable; after a stage creates any diagnostic artifact,
the failed run is retained for inspection. Analysis outputs equal to or below
the reserved `SUCCESS`, `FAILED`, or generation paths are rejected before the
claim. A top-level `SUCCESS` marker is written only after the compact analysis
file has been atomically published. `--events` is capped at 100000 per job by
the calibrated HepMC2 source-ID precision contract; larger campaigns should
use multiple job IDs.

Before publication, the workflow passes the generation, pre-shower LHE
contract, LHE-to-HepMC alignment, and simulation metadata records to the
reducer. The reducer validates their file hashes and job identities, verifies
the per-event `AUX_OAP_EVENT_ID/AUX_OAP_EVENT_UNIT` match, and embeds their
normalized provenance in `analysis.root`. The compact output therefore remains
self-describing when the larger intermediate files are not transferred.

## Merge completed jobs

Once several jobs for one sample and campaign have succeeded, merge their
compact outputs and add the LHE truth angular weights with:

```bash
uv run python Merging/merge_analysis_outputs.py \
  --output /data/$USER/offshell/merged/gg4l.root \
  /data/$USER/offshell/production/gg4l_job*/analysis.root
```

The merger pools the pre-shower normalization primitives rather than averaging
the per-job cross sections. It preserves the raw signed LHE weight, adds a
pb-normalized nominal weight, and derives all angular factors from each event's
Born-projected LHE angles. See `Merging/README.md` for the full schema and
validation contract.

## HTCondor status

The submit description, site resource requests, retries, and file-transfer
policy are intentionally deferred until small local AF jobs have validated the
AthGeneration, HepMC, ROOT, and Delphes runtime combination. This script has a
non-interactive command-line interface so it can become the executable of that
submission layer without changing the physics stages.
