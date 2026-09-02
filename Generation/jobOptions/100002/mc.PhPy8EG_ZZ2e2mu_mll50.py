# Copyright (C) 2002-2026 CERN for the benefit of the ATLAS collaboration

# This local job option is derived from ATLAS PMG DSID 603269:
# https://gitlab.cern.ch/atlas-physics/pmg/mcjoboptions/-/blob/
# 78cb99075450b6505fa923e44ec8d3c0ff29c5a8/603xxx/603269/
# mc.PhPy8EG_ZZllll_mll4.py

# -----------------------------------------------------------------------------
# EVGEN configuration
# -----------------------------------------------------------------------------
evgenConfig.description = (
    "POWHEG+Pythia8 ZZ->2e2mu production with PDF4LHC21 PDF variations, "
    "A14 tune, and mllmin=50 GeV"
)
evgenConfig.keywords = ["electroweak", "diboson", "ZZ", "4lepton"]
evgenConfig.contact = ["chiara.arcangeletti@cern.ch"]
evgenConfig.generators = ["Powheg", "Pythia8", "EvtGen"]

# -----------------------------------------------------------------------------
# POWHEG ZZ setup starting from ATLAS defaults.
# -----------------------------------------------------------------------------
include("PowhegControl/PowhegControl_ZZ_Common.py")
PowhegConfig.decay_mode = "z z > mu+ mu- e+ e-"
PowhegConfig.withdamp = 1
PowhegConfig.bornzerodamp = 1
PowhegConfig.mllmin = 50.0  # GeV
PowhegConfig.PDF = (
    list(range(93300, 93343))
    + list(range(90400, 90433))
    + list(range(260000, 260101))
    + [27100]
    + [14400]
    + [331700]
)
PowhegConfig.mu_F = [1.0, 0.5, 0.5, 0.5, 1.0, 1.0, 2.0, 2.0, 2.0]
PowhegConfig.mu_R = [1.0, 0.5, 1.0, 2.0, 0.5, 2.0, 0.5, 1.0, 2.0]
PowhegConfig.generate()

# POWHEG ZZ has no native m4l keywords. Apply the requested hard-event range
# to the LHE stream before Pythia reads it, and attach the named source ID used
# for exact LHE/HepMC matching. The helper repacks the TXT sidecar as well.
from offshell_lhe_contract import prepare_lhe_for_shower

prepare_lhe_for_shower(
    runArgs.inputGeneratorFile,
    runArgs.outputTXTFile,
    process="qqZZ",
    requested_events=runArgs.maxEvents,
    min_m4l=70.0,
    max_m4l=3000.0,
)

# -----------------------------------------------------------------------------
# Pythia8 showering with the A14 NNPDF2.3 tune.
# -----------------------------------------------------------------------------
include("Pythia8_i/Pythia8_A14_NNPDF23LO_EvtGen_Common.py")
include("Pythia8_i/Pythia8_Powheg_Main31.py")
if "UserHooks" in genSeq.Pythia8.__slots__.keys():
    genSeq.Pythia8.Commands += ["Powheg:NFinal = 2"]
else:
    genSeq.Pythia8.UserModes += ["Main31:NFinal = 2"]

# Deliberately no post-shower FourLeptonInvMassFilter: the range is enforced on
# the hard LHE stream before showering, preserving source IDs and normalization
# diagnostics rather than deleting only the HepMC/EVNT side of an event.
