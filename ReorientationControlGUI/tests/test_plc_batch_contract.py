from pathlib import Path


def _main_source() -> str:
    root = Path(__file__).resolve().parents[2]
    return (
        root
        / "CSVSaver"
        / "TwinCAT Projekt3 - Kopie"
        / "TwinCAT Projekt3"
        / "Untitled1"
        / "POUs"
        / "MAIN.TcPOU"
    ).read_text(encoding="utf-8")


def test_batch_contract_has_atomic_128_part_ring_and_results() -> None:
    source = _main_source()
    required = (
        "GuiReorientationQueueCommit",
        "GuiReorientationQueueSequence",
        "GuiReorientationFinish",
        "GuiReorientationResultAck",
        "ReorientationQueueDepth",
        "ReorientationQueueCapacity",
        "ReorientationQueueEnqueueAck",
        "ReorientationResultAvailable",
        "ReorientationResultSequence",
        "ReorientationSensorSequence8",
    )
    for symbol in required:
        assert symbol in source
    assert "BatchQueueSequence\t\t\t\t: ARRAY [0..127] OF UDINT" in source
    assert "BatchResultSequence\t\t\t\t: ARRAY [0..127] OF UDINT" in source
    assert source.index("BatchQueueSequence[BatchSlotIndex] :=") < source.index(
        "ReorientationQueueEnqueueAck := GuiReorientationQueueSequence"
    )


def test_batch_jobs_are_latched_and_legacy_triggers_remain_separate() -> None:
    source = _main_source()
    assert "BatchJobNozzleMask" in source
    assert "BatchJobPressureMbar" in source
    assert "BatchJobDelayCycles" in source
    assert "BatchJobPulseCycles" in source
    for barrier, array in ((2, 1), (4, 2), (6, 3), (8, 4)):
        assert (
            f"IF NOT GuiReorientationControlActive AND LightBarrier{barrier}FallingEdge "
            f"AND Array{array}Active"
        ) in source
    assert "GuiReorientationLegacyTakeover AND ReorientationSafeLatch" in source
    assert "IF GuiReorientationControlActive AND ((ReorientationState = 20) OR " in source

