"""Engine selection. No Blender is launched: subprocess.run is patched."""

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from kentsugi import engine

BLENDER = Path("/fake/blender")


def completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def ok():
    return completed(0, f"some noise\n{engine.PROBE_SENTINEL}\nmore noise\n")


class ProbeTest(unittest.TestCase):
    def setUp(self):
        engine.clear_cache()
        self.addCleanup(engine.clear_cache)

    def test_sentinel_and_zero_exit_means_eevee_works(self):
        with mock.patch.object(engine.subprocess, "run", return_value=ok()):
            self.assertTrue(engine.probe_eevee(BLENDER))

    def test_crash_means_no_eevee(self):
        # The real failure on an under-specified GPU: killed by an access
        # violation, so a signal-ish exit code and no sentinel.
        with mock.patch.object(engine.subprocess, "run", return_value=completed(11)):
            self.assertFalse(engine.probe_eevee(BLENDER))

    def test_zero_exit_without_sentinel_means_no_eevee(self):
        # Blender exits 0 even when the embedded script raises.
        with mock.patch.object(engine.subprocess, "run", return_value=completed(0, "Blender quit\n")):
            self.assertFalse(engine.probe_eevee(BLENDER))

    def test_timeout_means_no_eevee(self):
        boom = subprocess.TimeoutExpired(cmd=["blender"], timeout=1)
        with mock.patch.object(engine.subprocess, "run", side_effect=boom):
            self.assertFalse(engine.probe_eevee(BLENDER))

    def test_oserror_means_no_eevee(self):
        with mock.patch.object(engine.subprocess, "run", side_effect=OSError("not executable")):
            self.assertFalse(engine.probe_eevee(BLENDER))

    def test_result_is_cached_per_executable(self):
        with mock.patch.object(engine.subprocess, "run", return_value=ok()) as run:
            self.assertTrue(engine.probe_eevee(BLENDER))
            self.assertTrue(engine.probe_eevee(BLENDER))
            self.assertTrue(engine.probe_eevee(Path("/fake/blender")))
        self.assertEqual(run.call_count, 1)

    def test_distinct_executables_probe_separately(self):
        with mock.patch.object(engine.subprocess, "run", return_value=ok()) as run:
            engine.probe_eevee(BLENDER)
            engine.probe_eevee(Path("/other/blender"))
        self.assertEqual(run.call_count, 2)

    def test_probe_runs_blender_headless_with_factory_startup(self):
        with mock.patch.object(engine.subprocess, "run", return_value=ok()) as run:
            engine.probe_eevee(BLENDER)
        command = run.call_args.args[0]
        self.assertIn("--background", command)
        self.assertIn("--factory-startup", command)
        self.assertIn(str(engine.PROBE_SCRIPT), command)


class ResolveEngineTest(unittest.TestCase):
    def setUp(self):
        engine.clear_cache()
        self.addCleanup(engine.clear_cache)

    def test_explicit_cycles_wins_and_does_not_probe(self):
        with mock.patch.object(engine, "probe_eevee") as probe:
            chosen, note = engine.resolve_engine("cycles", "EEVEE", BLENDER)
        self.assertEqual(chosen, "CYCLES")
        self.assertIsNone(note)
        probe.assert_not_called()

    def test_explicit_eevee_is_respected_even_though_it_may_crash(self):
        with mock.patch.object(engine, "probe_eevee") as probe:
            chosen, note = engine.resolve_engine("eevee", "CYCLES", BLENDER)
        self.assertEqual(chosen, "EEVEE")
        self.assertIsNone(note)
        probe.assert_not_called()

    def test_auto_keeps_cycles_without_probing(self):
        with mock.patch.object(engine, "probe_eevee") as probe:
            chosen, note = engine.resolve_engine("auto", "CYCLES", BLENDER)
        self.assertEqual(chosen, "CYCLES")
        self.assertIsNone(note)
        probe.assert_not_called()

    def test_auto_keeps_eevee_when_the_probe_succeeds(self):
        with mock.patch.object(engine, "probe_eevee", return_value=True):
            chosen, note = engine.resolve_engine("auto", "EEVEE", BLENDER)
        self.assertEqual(chosen, "EEVEE")
        self.assertIsNone(note)

    def test_auto_falls_back_to_cycles_when_the_probe_fails(self):
        with mock.patch.object(engine, "probe_eevee", return_value=False):
            chosen, note = engine.resolve_engine("auto", "EEVEE", BLENDER)
        self.assertEqual(chosen, "CYCLES")
        self.assertIn("Cycles", note)

    def test_auto_without_blender_chooses_cycles(self):
        with mock.patch.object(engine, "probe_eevee") as probe:
            chosen, note = engine.resolve_engine("auto", "EEVEE", None)
        self.assertEqual(chosen, "CYCLES")
        self.assertIsNotNone(note)
        probe.assert_not_called()

    def test_no_probe_chooses_cycles_without_launching_blender(self):
        with mock.patch.object(engine, "probe_eevee") as probe:
            chosen, note = engine.resolve_engine("auto", "EEVEE", BLENDER, probe=False)
        self.assertEqual(chosen, "CYCLES")
        self.assertIsNotNone(note)
        probe.assert_not_called()

    def test_none_request_behaves_like_auto(self):
        with mock.patch.object(engine, "probe_eevee", return_value=False):
            chosen, _ = engine.resolve_engine(None, "EEVEE", BLENDER)
        self.assertEqual(chosen, "CYCLES")

    def test_probe_script_exists(self):
        self.assertTrue(engine.PROBE_SCRIPT.is_file(), engine.PROBE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
