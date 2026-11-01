import io
import json
import subprocess
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_scheduled


class ScheduledLogTests(unittest.TestCase):
    def test_only_aggregate_fields_reach_public_logs(self):
        def collect(*args, **kwargs):
            json.dump({"runs": [{"endpoint": "hidden.example.invalid", "token": "private-value"}], "errors": [{"detail": "private-value"}], "cycles": {"noaa": "2026-11-01T00:00:00Z", "secret": "private-value"}, "timings": {"noaa": {"seconds": 3.2, "requests": 7, "endpoint": "private-value"}}}, kwargs["stdout"])
            kwargs["stderr"].write("private-value")
            return SimpleNamespace(returncode=1)
        output = io.StringIO()
        with patch.object(run_scheduled.sys, "argv", ["runner", "forecast"]), patch.object(run_scheduled.subprocess, "run", side_effect=collect), redirect_stdout(output):
            self.assertEqual(run_scheduled.main(), 1)
        text = output.getvalue()
        self.assertNotIn("private-value", text)
        self.assertNotIn("hidden.example.invalid", text)
        self.assertEqual(json.loads(text)["runsCount"], 1)
        self.assertEqual(json.loads(text)["timings"], {"noaa": {"seconds": 3.2, "requests": 7}})

    def test_timeout_details_are_withheld(self):
        output = io.StringIO()
        with patch.object(run_scheduled.sys, "argv", ["runner", "forecast"]), patch.object(run_scheduled.subprocess, "run", side_effect=subprocess.TimeoutExpired("private-value", 1)), redirect_stdout(output):
            self.assertEqual(run_scheduled.main(), 1)
        self.assertNotIn("private-value", output.getvalue())
