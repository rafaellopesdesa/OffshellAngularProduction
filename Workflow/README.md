# End-to-end job worker

`run_chain.sh` executes one local job through all three stages and is the
worker entry point used by the HTCondor layer. It keeps the heavy EVNT,
LHE, HepMC, and Delphes files in a stage directory while allowing the compact
analysis ROOT file to be placed separately for transfer.

Before running it:

1. install the Python package and dependencies;
2. build Delphes once with `Simulation/install_delphes.sh` in the ROOT
   environment that will run simulation; and
3. run from a UChicago AF shell/container with ATLAS CVMFS available.

The ATLAS environment in step 3 is required for `gg4l`, `gg4l_h`, `gg4l_b`,
and `qqZZ`. VPolar processes instead require the shared prefix built by
`Generation/VPolar/install_vpolar.sh`.

Example smoke jobs from the repository root:

```bash
Workflow/run_chain.sh gg4l \
  --events 2 --seed 101 --job-id 0 --campaign-id 20260902 \
  --output-dir /data/$USER/offshell/smoke/gg4l_job0

Workflow/run_chain.sh qqZZ \
  --events 10 --seed 201 --job-id 0 --campaign-id 20260902 \
  --output-dir /data/$USER/offshell/smoke/qqZZ_job0
```

For Higgs-only or gluon-continuum-only samples, replace `gg4l` with `gg4l_h`
or `gg4l_b`, including in the output path. Both use the same full chain,
selection, matching checks, and compact output schema. Their separate sample
codes preserve the process identity through analysis and merging.

The polarized interface is identical apart from the installation prefix:

```bash
Workflow/run_chain.sh vpolar_TT \
  --events 10 --seed 301 --job-id 0 --campaign-id 20260902 \
  --generator-prefix /data/$USER/offshell/software/vpolar \
  --output-dir /data/$USER/offshell/smoke/vpolar_TT_job0
```

These gridless commands are intended for one-job smoke tests. Before normal
production, use `Generation/prepare_gridpack.sh` to build a separate compatible
pack for each of `gg4l`, `gg4l_h`, `gg4l_b`, `qqZZ`, or each VPolar
polarization, then pass it through the same workflow interface:

```bash
Workflow/run_chain.sh vpolar_TT \
  --events 50 --seed 302 --job-id 1 --campaign-id 20260902 \
  --generator-prefix /data/$USER/offshell/software/vpolar \
  --gridpack /data/$USER/offshell/gridpacks/vpolar_TT/vpolar_TT_gridpack.tar.gz \
  --generation-cores 1 \
  --output-dir /data/$USER/offshell/production/vpolar_TT_job1
```

`--gridpack-metadata` is optional when the manifest is adjacent at
`GRIDPACK.metadata.json`. Both files are resolved and checked before the stage
directory is claimed; the generation backend then performs its full semantic
compatibility validation. VPolar native gridpack runs are serial and therefore
require `--generation-cores 1`. A gridless VPolar smoke job may use several
cores for its repeated integration.

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
  /data/$USER/offshell/production/gg4l/campaign_20260902/job_*/analysis.root
```

For `gg4l_h` or `gg4l_b`, substitute that name in both paths and merge each
contribution separately; a mixed-process input list is rejected. Each sample
gets its own cross-section normalization, truth angular weights, and the
`lumi = 312000` / `weight = weight_nominal_pb * lumi` branches.

The merger pools the pre-shower normalization primitives rather than averaging
the per-job cross sections. It preserves the raw signed LHE weight, adds a
pb-normalized nominal weight, and derives all angular factors from each event's
Born-projected LHE angles. See `Merging/README.md` for the full schema and
validation contract.

## HTCondor campaigns

`UChicagoAF/condor/submit_campaign.py` produces the job table and submit file,
while `UChicagoAF/condor/worker.sh` invokes this script in execute-node scratch
and publishes only the compact output and provenance. Submission is opt-in;
campaign preparation alone does not call `condor_submit`. Any campaign with
more than one job requires a backend-compatible gridpack, preventing every
worker from rebuilding and then discarding the same expensive integration.
