from __future__ import annotations

import os
import uuid

import pytest

from bibazu_reorientation.hardware_lease import HardwareLease


@pytest.mark.skipif(os.name != "nt", reason="Windows named-mutex behavior")
def test_hardware_lease_excludes_second_process_handle() -> None:
    name = rf"Local\BiBaZuHardwareLeaseTest-{uuid.uuid4()}"
    first = HardwareLease.acquire(name)
    assert first is not None
    try:
        assert HardwareLease.acquire(name) is None
    finally:
        first.close()

    replacement = HardwareLease.acquire(name)
    assert replacement is not None
    replacement.close()
