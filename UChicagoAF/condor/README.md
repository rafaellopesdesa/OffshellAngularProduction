# UChicago AF HTCondor campaigns

This directory is the batch layer for the common
`Generation -> Simulation -> Analysis` interface. It supports the two ATLAS
generators (`gg4l`, `qqZZ`) and the four exclusive VPolarized modes
(`vpolar_LL`, `vpolar_TT`, `vpolar_TL`, `vpolar_LT`). Every worker processes
only the `e+e-mu+mu-` final state selected by the underlying generation card.

## Prepare a campaign

Run the submitter from a login node whose repository, software installations,
and `/data` destination are visible on execute nodes. Preparation is safe by
default: it writes the campaign records and `condor.sub`, but does **not** call
`condor_submit`.

First prepare and validate one process-specific gridpack outside Condor. For
example:

```bash
Generation/prepare_gridpack.sh gg4l \
  --events 50 --seed 9001 \
  --output-dir /data/$USER/offshell/gridpacks/gg4l
```

Then prepare the campaign with that shared archive:

```bash
python UChicagoAF/condor/submit_campaign.py gg4l \
  --jobs 20 --events-per-job 100 --seed-base 10001 \
  --campaign-id 20260902 \
  --gridpack /data/$USER/offshell/gridpacks/gg4l/integration_grids.tar.gz \
  --output-root /data/$USER/offshell/production
```

The default manifest is the adjacent `GRIDPACK.metadata.json`. Pass
`--gridpack-metadata FILE` only if the pair was deliberately renamed or moved
apart. The submitter runs the backend's complete compatibility validator before
creating the campaign directory. A pack made for another process, card,
release, energy, polarization, or VPolar installation is rejected immediately.
It also records the SHA-256 digest of both the archive and metadata in
`campaign.json` and every job record. Each worker verifies those exact bytes
before starting the workflow, so neither shared file may be replaced after the
campaign is prepared.

Every campaign with `--jobs` greater than one requires a gridpack. This avoids
launching many workers that independently repeat the same integration and then
delete it with their private scratch. A one-job gridless campaign remains
available as an explicit pilot/smoke path, but its generated grid artifacts are
not published by the Condor worker; use `Generation/prepare_gridpack.sh` when
the objective is to retain a pack.

Review `campaign.json`, `jobs.tsv`, and `condor.sub` below the printed campaign
directory, then either run `condor_submit` there or repeat the preparation with
`--submit` and a fresh campaign directory. Use `--dry-run` to print the fully
resolved manifest and submit description without writing anything.
Repository and campaign paths written into `condor.sub` are restricted to
letters, digits, `/`, `.`, `_`, `+`, and `-`, preventing path text from being
interpreted as HTCondor macros or submit syntax.

Preparation records both the Git `HEAD` revision and a SHA-256 fingerprint of
every tracked or non-ignored working-tree file. Each job JSON also has a
separate SHA-256 value embedded into the Condor job arguments. The worker
checks the JSON and repository fingerprints before starting and checks the
repository again before publication. Editing the checkout or a queued job
record therefore holds the affected job instead of silently changing the
campaign. Campaign and result directories must be outside the repository so
they do not change that snapshot. Regenerate the campaign records after an
intentional code change.

VPolar campaigns require the one-time, shared installation prefix and one
native MadGraph gridpack for the selected polarization. Installation and
gridpack preparation are deliberately never attempted on a worker. Build the
pack once, for example:

```bash
Generation/prepare_gridpack.sh vpolar_LL \
  --generator-prefix /data/$USER/offshell/software/vpolar \
  --seed 19001 --cores 8 \
  --output-dir /data/$USER/offshell/gridpacks/vpolar_LL
```

The submitter fully validates the installation manifest, selected process
bundle, gridpack structure, and gridpack metadata before preparing any jobs.
Result and campaign paths are forbidden below the immutable prefix:

```bash
python UChicagoAF/condor/submit_campaign.py vpolar_LL \
  --jobs 20 --events-per-job 100 --seed-base 20001 \
  --campaign-id 20260902 \
  --generator-prefix /data/$USER/offshell/software/vpolar \
  --gridpack /data/$USER/offshell/gridpacks/vpolar_LL/vpolar_LL_gridpack.tar.gz \
  --request-cpus 1 \
  --output-root /data/$USER/offshell/production
```

Submit `vpolar_TL` and `vpolar_LT` separately. Their completed samples can be
combined incoherently by the polarization-composition merger; no mixed-mode
amplitude interference is introduced by this batch layer.

Each job gets consecutive, unique seeds and job IDs. Its event-number interval
is

```text
first_event(job i) = first_event_base + i * events_per_job
```

so intervals cannot overlap. The submitter rejects seed, event-number, job-ID,
and per-job event-count overflow before producing a campaign.

## Worker environment and resources

The submit file uses the UChicago AF shared-filesystem model:

```text
should_transfer_files = NO
```

The repository, gridpack and metadata, VPolar prefix, Delphes installation,
Python environment, and output root must therefore be readable through the
same absolute paths on the login and execute nodes. `getenv = True` preserves
the submit environment. For setups that need explicit ROOT or site
initialization, pass a readable shared script with `--setup-script`;
`worker.sh` sources it before calling `Workflow/run_chain.sh`.
`--analysis-python` selects the project Python executable.

Resource requests are campaign options:

```bash
  --request-cpus 1 --request-memory 6GB --request-disk 30GB
```

The defaults are one CPU, 4 GB of memory, and 20 GB of scratch disk. Failed
jobs are placed on hold by the submit description rather than silently retried.
Inspect the Condor stderr and, when the workflow was reached, the published
failure bundle before releasing or resubmitting one. Record, repository, and
environment validation can fail before a safe publication location is trusted;
those diagnostics remain in Condor stderr.

For a one-job, gridless VPolar pilot, `--request-cpus` is forwarded to MadGraph
as its local integration worker count and may be from 1 through 256. Native
VPolar gridpack execution is serial, so any VPolar campaign supplying a
gridpack must use `--request-cpus 1`. POWHEG runs are also single-core through
the current wrapper.

POWHEG campaigns may additionally pass `--release` and
`--no-generation-setup`. Those Athena-specific options are rejected for
VPolar. `--gridpack` and `--gridpack-metadata` are common to both backends.

## Outputs and failure handling

Workers create a private directory below `_CONDOR_SCRATCH_DIR` and pass that as
the stage area to the normal workflow. A successful job atomically publishes:

```text
OUTPUT_ROOT/PROCESS/campaign_CAMPAIGN_ID/job_JOB_ID/
  analysis.root
  publication.json
  job-record.json
  generation-metadata.txt
  lhe-contract-metadata.json
  alignment-metadata.json
  simulation-metadata.txt
  workflow.log.gz
  SUCCESS
```

No EVNT, LHE, HepMC, or Delphes ROOT file is copied from worker scratch. The
analysis ROOT file already embeds the normalized provenance used by the
merger; the small sidecars make operational audits possible without opening
ROOT. `publication.json` records SHA-256 checksums for the analysis file,
compressed log, and immutable worker-local snapshot of the job record.

An existing result directory is never overwritten. Once execution reaches the
workflow, a failure publishes a uniquely named directory under the campaign's
`failures/` folder containing the compressed workflow log, the immutable job
record, exit code, and a `FAILED` marker. Earlier contract/environment failures
are reported in Condor stderr. Worker scratch is always removed.
