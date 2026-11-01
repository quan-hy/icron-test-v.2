"""Run collection with aggregate-only logs; never publish payloads or endpoint details."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in ("forecast", "tides"):
        print("Invalid collection mode.")
        return 1
    here = Path(__file__).resolve().parent
    command = [sys.executable, str(here / f"{mode}.py"), "--post"]
    if mode == "forecast":
        command.append("--public-spots")
        for variable, flag in (("INPUT_MODEL_CYCLE", "--model-cycle"), ("INPUT_SST_DATE", "--sst-date")):
            if os.environ.get(variable):
                command.extend((flag, os.environ[variable]))
    else:
        mappings = os.environ.get("TIDE_MAPPINGS", "").split()
        if not 1 <= len(mappings) <= 30 or any(re.fullmatch(r"[a-z0-9-]{1,64}=\d{7}", value) is None for value in mappings):
            print("Valid tide mappings are required.")
            return 1
        command.extend(("--hours", "144"))
        for value in mappings:
            command.extend(("--mapping", value))

    # Raw library logs can contain configured origins or point identifiers. Files
    # are private to this invocation and are never artifacts or console output.
    with tempfile.TemporaryDirectory(prefix="icron-run-") as directory:
        result_path = Path(directory) / "result.json"
        with result_path.open("w+") as output, (Path(directory) / "errors.log").open("w") as errors:
            try:
                result = subprocess.run(command, cwd=here, stdout=output, stderr=errors, timeout=3300, check=False)
                output.seek(0)
                document = json.load(output)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                print("Collection failed; detailed output withheld.")
                return 1
        if not isinstance(document, dict):
            print("Invalid collection result; detailed output withheld.")
            return 1
        summary = {"mode": mode, "success": result.returncode == 0}
        for key in ("runs", "errors"):
            if isinstance(document.get(key), list):
                summary[f"{key}Count"] = len(document[key])
        if isinstance(document.get("clusters"), int) and 0 <= document["clusters"] <= 30:
            summary["clusters"] = document["clusters"]
        for source in ("noaa", "ecmwf"):
            cycles = document.get("cycles", {})
            cycle = cycles.get(source) if isinstance(cycles, dict) else None
            if isinstance(cycle, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", cycle):
                summary.setdefault("cycles", {})[source] = cycle
            timings = document.get("timings", {})
            timing = timings.get(source, {}) if isinstance(timings, dict) else {}
            if not isinstance(timing, dict):
                continue
            safe = {key: value for key, value in timing.items() if key in ("seconds", "requests") and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1_000_000}
            if safe:
                summary.setdefault("timings", {})[source] = safe
        print(json.dumps(summary, sort_keys=True))
        return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
