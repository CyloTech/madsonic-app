#!/usr/bin/env python3
"""Release gate: test the exact built image, then require the tag to be new.

Usage: release-gate.py repo.cylo.net/madsonic:<release tag>
Run on the builder between `docker build` and `docker push`; any failure
exits non-zero so the push does not happen.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('registry_check', here / 'registry-check.py')
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)

if len(sys.argv) != 2 or sys.argv[1] != registry.EXPECTED:
    raise SystemExit('Usage: release-gate.py ' + registry.EXPECTED)
image = sys.argv[1]

# Check the tag before and after the tests: the tests take several minutes.
registry.require_absent(image)
tests = subprocess.run([sys.executable, str(here / 'image-tests.py'), image], timeout=3600)
if tests.returncode:
    raise SystemExit('FAIL release gate: image tests failed')
registry.require_absent(image)
print('PASS release gate for ' + image, flush=True)
