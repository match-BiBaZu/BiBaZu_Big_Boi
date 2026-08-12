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
    assert "ELSIF BatchSensorOrderInvalid" in source
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


def test_airborne_parts_resynchronize_only_after_the_last_active_array() -> None:
    source = _main_source()
    for symbol in (
        "ReorientationWarningCode",
        "ReorientationWarningSensor",
        "ReorientationWarningSequence",
        "ReorientationWarningSkippedBarrierMask",
        "ReorientationWarningCounter",
    ):
        assert symbol in source
    assert "BatchCanResyncSkippedSensors := BatchSensorOrderInvalid" in source
    assert "((BatchLoopIndex - 1) > BatchLastRequiredSensor)" in source
    assert "ELSIF BatchSensorOrderInvalid AND NOT BatchCanResyncSkippedSensors" in source
    assert "BatchSensorSequence[BatchResyncIndex] :=" in source
    assert "ReorientationWarningCounter := ReorientationWarningCounter + 1;" in source


def test_airborne_resync_requires_confirmed_actuation_for_the_exact_queue_slot() -> None:
    source = _main_source()
    assert "BatchQueueActuatedMask" in source
    assert "BatchLastRequiredArrayMask" in source
    assert "BatchCurrentFlightConfirmed" in source

    confirmation = source[source.rindex("BatchCurrentFlightConfirmed :=") :]
    confirmation = confirmation[: confirmation.index(";")]
    assert "BatchQueueActuatedMask[BatchSlotIndex]" in confirmation
    assert "BatchLastRequiredArrayMask" in confirmation

    resync = source[source.rindex("BatchCanResyncSkippedSensors :=") :]
    resync = resync[: resync.index(";")]
    assert "BatchCurrentFlightConfirmed" in resync
    assert "((BatchLoopIndex - 1) > BatchLastRequiredSensor)" in resync

    # A scheduled FIFO job is not sufficient proof that the part became
    # airborne: every array token must be recorded from its real valve pulse.
    for array, bit in ((1, 1), (2, 2), (3, 4), (4, 8)):
        marker = (
            "BatchQueueActuatedMask[BatchSlotIndex] := "
            f"BatchQueueActuatedMask[BatchSlotIndex] OR BYTE#{bit};"
        )
        marker_index = source.index(marker)
        pulse_guard = source[max(0, marker_index - 1200) : marker_index]
        assert f"BatchActiveSequence[{array}]" in pulse_guard
        assert "OpenValve" in pulse_guard


def test_airborne_resync_advances_the_virtual_pair_gate_phase() -> None:
    source = _main_source()
    recovery = source[source.index("IF BatchCanResyncSkippedSensors THEN") :]
    recovery = recovery[: recovery.index("ReorientationWarningCode := 1;")]

    # Advancing only the sequence cursor would leave a skipped pair locked in
    # its old phase and could suppress the following real workpiece for 500 ms.
    assert "IF (BatchResyncIndex MOD 2) = 1 THEN" in recovery
    assert "PairedFirstBarrierLocked[BatchJobIndex] := TRUE;" in recovery
    assert "PairedSecondBarrierLocked[BatchJobIndex] := TRUE;" in recovery
    assert "PairedFirstBarrierLockStartMs[BatchJobIndex] := LightBarrierEventClockMs;" in recovery
    assert "PairedSecondBarrierLockStartMs[BatchJobIndex] := LightBarrierEventClockMs;" in recovery

    # The real edge was evaluated before the virtual ones, so it must be
    # applied last when both belong to the same pair (for example skipped LB7,
    # physically observed LB8).
    assert recovery.count("IF (BatchLoopIndex MOD 2) = 1 THEN") >= 1


def test_airborne_duplicate_edge_is_ignored_only_for_confirmed_previous_part() -> None:
    source = _main_source()
    for symbol in (
        "BatchIgnoreAirborneDuplicate",
        "BatchPreviousSlotIndex",
        "BatchPreviousLastRequiredSensor",
        "BatchPreviousLastRequiredArrayMask",
    ):
        assert symbol in source
    assert "AND (BatchNextSensorSequence[BatchLoopIndex] > 1) THEN" in source

    duplicate_check = source[source.rindex("BatchIgnoreAirborneDuplicate :=") :]
    duplicate_check = duplicate_check[: duplicate_check.index(";")]
    assert "BatchQueueSequence[BatchPreviousSlotIndex]" in duplicate_check
    assert "BatchQueueActuatedMask[BatchPreviousSlotIndex]" in duplicate_check
    assert "BatchPreviousLastRequiredArrayMask" in duplicate_check
    assert "(BatchLoopIndex > BatchPreviousLastRequiredSensor)" in duplicate_check

    ignored_branch = source[source.index("ELSIF BatchIgnoreAirborneDuplicate THEN") :]
    ignored_branch = ignored_branch[: ignored_branch.index("\n\t\t\t\tELSE")]
    assert "ReorientationWarningCode := 2;" in ignored_branch
    assert "ReorientationWarningCounter := ReorientationWarningCounter + 1;" in ignored_branch
    assert "PairedFirstBarrierLocked" in ignored_branch
    assert "PairedSecondBarrierLocked" in ignored_branch
    assert "BatchSensorSequence[BatchLoopIndex] :=" not in ignored_branch


def test_all_light_barrier_sequence_faults_are_nonfatal_warnings() -> None:
    source = _main_source()
    sensor_loop = source[source.index("IF BatchSensorFalling[BatchLoopIndex] THEN") :]
    sensor_loop = sensor_loop[: sensor_loop.index("\n\tEND_FOR")]

    no_clear = sensor_loop.index("ReorientationFaultDetail := 1;")
    queue_mismatch = sensor_loop.index("ReorientationFaultDetail := 2;")
    sensor_order = sensor_loop.index("ReorientationFaultDetail := 3;")
    ignored_duplicate = sensor_loop.index("ELSIF BatchIgnoreAirborneDuplicate THEN")
    accepted_sensor = sensor_loop.index(
        "BatchSensorSequence[BatchLoopIndex] := BatchNextSensorSequence[BatchLoopIndex];"
    )

    assert no_clear < queue_mismatch < ignored_duplicate < sensor_order < accepted_sensor
    assert "ReorientationFaultCode := 96;" not in sensor_loop
    assert "ReorientationState := 96;" not in sensor_loop
    for warning_code in (3, 4, 5):
        assert f"ReorientationWarningCode := {warning_code};" in sensor_loop
    # Every rejected edge restarts the relevant half of the 500-ms pair gate,
    # while the accepted cursor update remains confined to the final ELSE.
    assert sensor_loop.count("PairedFirstBarrierLockStartMs[BatchJobIndex] :=") >= 3
    assert sensor_loop.count("PairedSecondBarrierLockStartMs[BatchJobIndex] :=") >= 3
    assert "BatchQueueExitSeen[BatchSlotIndex] := TRUE;" in sensor_loop
    assert (
        "IF BatchQueueExitSeen[BatchSlotIndex] AND "
        "(BatchQueueFinishedMask[BatchSlotIndex] = BYTE#15) THEN"
    ) in source
