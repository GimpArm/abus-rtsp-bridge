#!/usr/bin/env python3
"""Which cameras the app treats as bidirectional-intercom capable (talking and listening at the
same time). The app then keeps playing the camera's own audio while the talk button is held;
for every other camera it drops that audio to avoid feedback (P2PDev.dropAudioData).

Read from the decompiled app, not guessed: P2PDev.isBiDirectionalIntercomSupported() for OV-serial
cameras (DID prefix CO/DO), and ModelHelper.parseModel()/initialModelParameter() plus each
Model*.isSupportBiDirectionalIntercom() override for the others. The model name is the first 16
bytes of the camera's DEVINFO response.
"""
from __future__ import annotations

from typing import Optional

# Exact model strings (ModelHelper compares with equals()) whose model class returns true.
BIDIRECTIONAL_MODELS = frozenset({
    "BC-1R", "DC-X15", "DC-X15P", "DC-X16", "DC-X16P", "DC-X18", "DC-X19", "DP-D2", "DP-D2R",
    "LC-W3", "LC-W4", "RVDP-RAS", "RVDP-RASR", "RVDP-RASRA", "RVDP-RASU",
})
# OV-serial cameras: only DDV ones (DID starts with DO) of these device types (equalsIgnoreCase).
OV_BIDIRECTIONAL_TYPES = frozenset({
    "RVDP-GSO", "RVDP-GSOD", "RVDP-BS", "RVDP-OAS", "RVDP-OASR", "RVDP-OASPR", "RVDP-OCS",
    "RVDP-OCSR", "RVDP-OCSRA", "RVDP-OCSRO", "RVDP-OASP",
})


def is_bidirectional_intercom(did: Optional[str], model: Optional[str]) -> bool:
    """False when the model isn't known yet - the safe default is to mute while talking."""
    if not model:
        return False
    model = model.strip()
    if did and did.upper().startswith(("CO", "DO")):
        return did.upper().startswith("DO") and model.upper() in OV_BIDIRECTIONAL_TYPES
    return model in BIDIRECTIONAL_MODELS
