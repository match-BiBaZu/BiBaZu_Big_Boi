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
        "ReorientationFaultDetail",
        "ReorientationFaultSensor",
        "ReorientationFaultExpectedSequence",
        "ReorientationFaultBarrierStableMask",
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
        pair = barrier // 2
        assert (
            f"IF NOT GuiReorientationControlActive AND PairedSecondBarrierFalling[{pair}] "
            f"AND Array{array}Active"
        ) in source
    assert "GuiReorientationLegacyTakeover AND ReorientationSafeLatch" in source
    assert "IF GuiReorientationControlActive AND ((ReorientationState = 20) OR " in source


def test_first_sensor_never_indexes_sensor_sequence_zero() -> None:
    source = _main_source()
    unsafe_guard = (
        "ELSIF (BatchLoopIndex > 1)\n"
        "\t\t\t\t\tAND (BatchSensorSequence[BatchLoopIndex - 1]"
    )
    assert unsafe_guard not in source
    assert "IF BatchLoopIndex > 1 THEN" in source
    assert "ELSIF BatchSensorOrderInvalid THEN" in source
    assert "BatchSensorSequence[BatchLoopIndex] := 0;" in source


def test_plc_consumes_reset_as_a_command_pulse() -> None:
    source = _main_source()
    reset_block = source[source.index("IF GuiReorientationReset") :]
    reset_block = reset_block[: reset_block.index("END_IF")]
    assert "GuiReorientationReset := FALSE;" in reset_block


def test_sensor_loop_latches_only_the_first_fault_context() -> None:
    source = _main_source()
    assert "IF ReorientationSafeLatch THEN\n\t\t\tEXIT;" in source


def test_first_barrier_of_each_pair_is_gated_until_second_or_timeout() -> None:
    source = _main_source()
    assert "PairedBarrierLockTimeoutMs\t\t: UDINT := 500;" in source
    for pair, first, second in ((1, 1, 2), (2, 3, 4), (3, 5, 6), (4, 7, 8)):
        assert (
            f"PairedSecondBarrierFalling[{pair}] := LightBarrier{second}FallingEdge "
            f"AND NOT PairedSecondBarrierLocked[{pair}];"
        ) in source
        assert (
            f"PairedFirstBarrierFalling[{pair}] := LightBarrier{first}FallingEdge "
            f"AND NOT PairedFirstBarrierLocked[{pair}];"
        ) in source
        assert f"BatchSensorFalling[{first}] := PairedFirstBarrierFalling[{pair}];" in source
        assert f"BatchSensorFalling[{second}] := PairedSecondBarrierFalling[{pair}];" in source
        assert source.count(f"IF PairedFirstBarrierFalling[{pair}] THEN") >= 1
        assert source.count(f"IF PairedSecondBarrierFalling[{pair}] THEN") >= 1
    assert "PairedFirstBarrierLocked[BatchLoopIndex] := FALSE;" in source
    assert "PairedSecondBarrierLocked[BatchLoopIndex] := FALSE;" in source
