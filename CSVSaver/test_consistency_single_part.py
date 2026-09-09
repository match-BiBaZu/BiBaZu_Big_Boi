"""Regression tests for first activation capture and the LB8 release latch."""

import unittest

from test_consistency_logging import EventBuffer

import ConveyorSetupGUI as setup


UINT32_MASK = 0xFFFFFFFF


class SinglePartCollectorTests(unittest.TestCase):
    def make_collector(self, selected=True, maximum_traversal_ms=10_000,
                       sequence=0):
        buffer = EventBuffer(sequence)
        collector = setup.BarrierRunCollector(
            selected, single_part=True,
            maximum_traversal_ms=maximum_traversal_ms,
        )
        collector.start([0] * 8, buffer.data)
        return collector, buffer

    def append_events(self, collector, buffer, events, poll_each=False):
        completed = []
        for timestamp, sensor, state in events:
            buffer.append(sensor, timestamp & UINT32_MASK, state)
            if poll_each:
                runs, missed = collector.process(buffer.status())
                self.assertFalse(missed)
                completed.extend(runs)
                self.assertLessEqual(len(collector.active_runs), 1)
        if not poll_each:
            runs, missed = collector.process(buffer.status())
            self.assertFalse(missed)
            completed.extend(runs)
        return completed

    @staticmethod
    def traversal_events(timestamps, clear_delay=25):
        return sorted(
            event
            for sensor, timestamp in enumerate(timestamps, 1)
            for event in ((timestamp, sensor, False),
                          (timestamp + clear_delay, sensor, True))
        )

    def test_single_part_always_uses_physical_activation_not_selected_clear_edge(self):
        for selected in (True, False):
            with self.subTest(selected=selected):
                collector, buffer = self.make_collector(selected)
                self.assertFalse(collector.edge_state)
                # An electrical TRUE is a clear beam and cannot start a part.
                self.assertEqual(self.append_events(collector, buffer, [
                    (900, 1, True), (950, 8, True),
                ]), [])
                self.assertFalse(collector.single_part_locked)
                expected = tuple(range(1000, 1800, 100))
                self.assertEqual(self.append_events(
                    collector, buffer, self.traversal_events(expected),
                ), [expected])

    def test_recorded_repeated_edges_produce_one_row_per_part(self):
        sensor_times = (
            (228071, 228099, 228209),
            (228237, 228254, 228339),
            (228630, 228692),
            (228704, 228714),
            (229001,), (229055,), (229249,), (229298,),
        )
        for poll_each in (False, True):
            with self.subTest(poll_each=poll_each):
                collector, buffer = self.make_collector()
                events = []
                expected = []
                for offset in (0, 5508):
                    expected.append(tuple(times[0] + offset for times in sensor_times))
                    for sensor, times in enumerate(sensor_times, 1):
                        for timestamp in times:
                            events.extend([
                                (timestamp + offset, sensor, False),
                                (timestamp + offset + 1, sensor, True),
                            ])
                self.assertEqual(self.append_events(
                    collector, buffer, sorted(events), poll_each=poll_each,
                ), expected)
                self.assertEqual(collector.active_runs, [])
                self.assertFalse(collector.single_part_locked)
                self.assertEqual(collector.ignored_repeated_edges, 12)
                self.assertEqual(collector.discarded_runs, 0)
                self.assertEqual(collector.process(buffer.status()), ([], False))

    def test_lb3_lb4_pulse_widths_do_not_change_activation_speeds(self):
        collector, buffer = self.make_collector(selected=True)
        for part, pulse_widths in enumerate(((20, 95), (95, 20), (20, 20))):
            timestamps = tuple(1000 + part * 3000 + index * 100 for index in range(8))
            widths = [20, 20, *pulse_widths, 20, 20, 20, 20]
            events = sorted(
                event
                for sensor, (timestamp, width) in enumerate(zip(timestamps, widths), 1)
                for event in ((timestamp, sensor, False), (timestamp + width, sensor, True))
            )
            completed = self.append_events(collector, buffer, events, poll_each=True)
            self.assertEqual(completed, [timestamps])
            result = setup.analyze_barrier_run(completed[0], (40.0,) * 7, 50.0)
            self.assertEqual(result["speeds"], [400.0] * 7)
        self.assertEqual(collector.discarded_runs, 0)

    def test_repeats_at_any_delay_keep_first_timestamps_and_do_not_restart(self):
        collector, buffer = self.make_collector()
        expected = (1000, 1100, 1200, 2300, 2400, 2500, 2600, 2700)
        events = self.traversal_events(expected)
        # All occur long after the former 500 ms repeat window. LB1 also
        # repeats after LB8 activates, while that beam is still blocked.
        events += [(2000, 1, False), (2010, 1, True),
                   (2100, 2, False), (2110, 2, True),
                   (2200, 3, False), (2210, 3, True),
                   (2710, 1, False), (2711, 1, True)]
        self.assertEqual(self.append_events(
            collector, buffer, sorted(events), poll_each=True,
        ), [expected])
        self.assertEqual(collector.ignored_repeated_edges, 4)
        self.assertEqual(collector.discarded_runs, 0)
        self.assertFalse(collector.single_part_locked)

    def test_lb8_activation_alone_neither_emits_nor_releases(self):
        collector, buffer = self.make_collector()
        expected = tuple(range(1000, 1800, 100))
        self.assertEqual(self.append_events(collector, buffer, [
            (timestamp, sensor, False) for sensor, timestamp in enumerate(expected, 1)
        ]), [])
        self.assertEqual(collector.active_runs, [list(expected)])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [
            (2300, 1, False), (2400, 3, False), (2500, 8, False),
        ]), [])
        self.assertEqual(collector.active_runs, [list(expected)])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [(2600, 8, True)]), [expected])
        self.assertEqual(collector.active_runs, [])
        self.assertFalse(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [(2700, 8, True)]), [])

    def test_clear_other_sensors_or_unactivated_lb8_cannot_release(self):
        collector, buffer = self.make_collector()
        self.append_events(collector, buffer, [(1000, 1, False), (1100, 2, False)])
        self.assertEqual(self.append_events(collector, buffer, [
            (1200, 1, True), (1201, 2, True), (1202, 8, True),
        ]), [])
        self.assertEqual(collector.active_runs, [[1000, 1100]])
        self.assertTrue(collector.single_part_locked)
        self.append_events(collector, buffer, [(3000, 1, False)])
        self.assertEqual(collector.active_runs, [[1000, 1100]])
        expected = (1000, 1100, 3100, 3200, 3300, 3400, 3500, 3600)
        self.assertEqual(self.append_events(collector, buffer, [
            (timestamp, sensor, False) for sensor, timestamp in enumerate(expected[2:], 3)
        ] + [(3700, 8, True)]), [expected])

    def test_skipped_barrier_discards_but_keeps_latch_until_lb8_clears(self):
        collector, buffer = self.make_collector()
        self.assertEqual(self.append_events(collector, buffer, [
            (1000, 1, False), (1100, 2, False), (1300, 4, False),
        ]), [])
        self.assertEqual(collector.active_runs, [])
        self.assertEqual(collector.discarded_runs, 1)
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [
            (1400, 8, True), (1500, 1, False), (1600, 3, False),
        ]), [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(collector.active_runs, [])
        self.assertEqual(self.append_events(collector, buffer, [(1700, 8, False)]), [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [(1800, 8, True)]), [])
        self.assertFalse(collector.single_part_locked)
        expected = tuple(range(5000, 5800, 100))
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(expected),
        ), [expected])
        self.assertEqual(collector.discarded_runs, 1)

    def test_backward_timestamp_discards_candidate_without_releasing_latch(self):
        collector, buffer = self.make_collector()
        self.assertEqual(self.append_events(collector, buffer, [
            (1000, 1, False), (1100, 2, False), (1050, 3, False),
        ]), [])
        self.assertEqual(collector.active_runs, [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(collector.discarded_runs, 1)
        self.assertEqual(self.append_events(collector, buffer, [
            (2000, 1, False), (2100, 8, False), (2200, 8, True),
        ]), [])
        self.assertFalse(collector.single_part_locked)

    def test_total_timeout_keeps_latch_until_lb8_clear_then_accepts_next_part(self):
        collector, buffer = self.make_collector(maximum_traversal_ms=2000)
        events = [(1000 + (sensor - 1) * 500, sensor, False) for sensor in range(1, 9)]
        self.assertEqual(self.append_events(collector, buffer, events, poll_each=True), [])
        self.assertEqual(collector.discarded_runs, 1)
        self.assertEqual(collector.active_runs, [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [(4600, 1, False)]), [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [(4700, 8, True)]), [])
        self.assertFalse(collector.single_part_locked)
        expected = tuple(range(10000, 10800, 100))
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(expected),
        ), [expected])

    def test_idle_timeout_uses_plc_clock_and_keeps_latch_across_timestamp_wrap(self):
        collector, buffer = self.make_collector(maximum_traversal_ms=2000)
        start = 0xFFFFFF00
        self.append_events(collector, buffer, [(start, 1, False), (start + 100, 2, False)])
        self.assertEqual(collector.active_runs, [[start, start + 100]])
        buffer.data[2] = (start + 2001) & UINT32_MASK
        self.assertEqual(collector.process(buffer.status()), ([], False))
        self.assertEqual(collector.active_runs, [])
        self.assertEqual(collector.discarded_runs, 1)
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(collector.process(buffer.status()), ([], False))
        self.assertEqual(collector.discarded_runs, 1)
        self.assertEqual(self.append_events(collector, buffer, [
            (start + 2100, 1, False), (start + 2200, 8, True),
        ]), [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [
            (start + 2300, 8, False), (start + 2400, 8, True),
        ]), [])
        self.assertFalse(collector.single_part_locked)
        unwrapped = tuple(start + 3000 + index * 100 for index in range(8))
        expected = tuple(timestamp & UINT32_MASK for timestamp in unwrapped)
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(unwrapped),
        ), [expected])

    def test_valid_traversal_can_cross_timestamp_and_history_sequence_wrap(self):
        collector, buffer = self.make_collector(maximum_traversal_ms=1000, sequence=0xFFFFFFFC)
        unwrapped = tuple(0xFFFFFF00 + index * 100 for index in range(8))
        expected = tuple(timestamp & UINT32_MASK for timestamp in unwrapped)
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(unwrapped),
        ), [expected])
        self.assertEqual(collector.discarded_runs, 0)
        self.assertFalse(collector.single_part_locked)

    def test_batch_applies_event_times_before_latest_plc_clock(self):
        collector, buffer = self.make_collector(maximum_traversal_ms=1000)
        expected = [tuple(start + index * 100 for index in range(8)) for start in (1000, 5000)]
        events = [event for run in expected for event in self.traversal_events(run)]
        for timestamp, sensor, state in events:
            buffer.append(sensor, timestamp, state)
        # The GUI may be delayed even after both parts have fully cleared.
        buffer.data[2] = 10000
        self.assertEqual(collector.process(buffer.status()), (expected, False))
        self.assertEqual(collector.discarded_runs, 0)
        self.assertFalse(collector.single_part_locked)

    def test_history_gap_discards_candidate_but_does_not_rearm_on_lb1(self):
        collector, buffer = self.make_collector()
        self.append_events(collector, buffer, [(1000, 1, False)])
        for index in range(257):
            buffer.append(2, 1100 + index, bool(index % 2))
        self.assertEqual(collector.process(buffer.status()), ([], True))
        self.assertEqual(collector.active_runs, [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [
            (2000, 1, False), (2100, 8, True),
        ]), [])
        self.assertTrue(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [
            (2200, 8, False), (2300, 8, True),
        ]), [])
        self.assertFalse(collector.single_part_locked)
        expected = tuple(range(5000, 5800, 100))
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(expected),
        ), [expected])

    def test_start_explicitly_resets_invalid_latch_and_diagnostics(self):
        collector, buffer = self.make_collector()
        self.append_events(collector, buffer, [
            (1000, 1, False), (1010, 1, False), (1300, 4, False),
        ])
        self.assertEqual(collector.ignored_repeated_edges, 1)
        self.assertEqual(collector.discarded_runs, 1)
        self.assertTrue(collector.single_part_locked)
        collector.start([0] * 8, buffer.data)
        self.assertEqual(collector.ignored_repeated_edges, 0)
        self.assertEqual(collector.discarded_runs, 0)
        self.assertEqual(collector.active_runs, [])
        self.assertFalse(collector.single_part_locked)
        expected = tuple(range(3000, 3800, 100))
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(expected),
        ), [expected])

    def test_start_discards_complete_but_not_yet_cleared_candidate(self):
        collector, buffer = self.make_collector()
        self.append_events(collector, buffer, [
            (1000 + index * 100, index + 1, False) for index in range(8)
        ])
        self.assertTrue(collector.single_part_locked)
        collector.start([0] * 8, buffer.data)
        self.assertFalse(collector.single_part_locked)
        self.assertEqual(self.append_events(collector, buffer, [(1800, 8, True)]), [])
        expected = tuple(range(3000, 3800, 100))
        self.assertEqual(self.append_events(
            collector, buffer, self.traversal_events(expected),
        ), [expected])


if __name__ == "__main__":
    unittest.main()
