"""Paired host benchmark, optionally against a specified local Git revision."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import vibrance_lut as V


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return (time.perf_counter() - start) * 1000, value


def benchmark(reference=None):
    results = {"python": sys.version, "platform": sys.platform, "size": 33, "encodings": {}}
    for encoding in ("pq", "g22"):
        V._grids.clear()
        cold, output = timed(lambda: V.generate(1.3, 33, encoding))
        warm, baseline = [], []
        for n in range(6):
            calls = [(warm, lambda: V.generate(1.3, 33, encoding))]
            if reference:
                calls.append((baseline, lambda: reference.generate(1.3, 33, encoding)))
            if n % 2:
                calls.reverse()
            for times, call in calls:
                elapsed, text = timed(call)
                assert text == output
                times.append(elapsed)
        result = {"cold_ms": round(cold, 2), "warm_median_ms": round(statistics.median(warm), 2),
                  "sha256": hashlib.sha256(output.encode()).hexdigest()}
        if reference:
            result.update(baseline_median_ms=round(statistics.median(baseline), 2),
                          warm_speedup=round(statistics.median(baseline) / statistics.median(warm), 2))
            for white in (V.D65, (0.296, 0.307)):
                for saturation in (1.0, 1.01, 1.1, 1.3, 1.5):
                    assert V.generate(saturation, 33, encoding, white) == reference.generate(saturation, 33, encoding, white)
            result["reference_equivalence"] = "byte identical for 5 saturations and 2 white points, full 33 cube"
        results["encodings"][encoding] = result
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-ref", help="local Git revision containing the baseline LUT implementation")
    args = parser.parse_args()
    reference = None
    if args.reference_ref:
        source = subprocess.check_output(["git", "show", f"{args.reference_ref}:vibrance_lut.py"], cwd=ROOT)
        reference = types.ModuleType("baseline_lut")
        exec(compile(source, "baseline_lut.py", "exec"), reference.__dict__)
    print(json.dumps(benchmark(reference), indent=2))
