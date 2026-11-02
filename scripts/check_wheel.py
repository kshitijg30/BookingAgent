"""Install the built core wheel in a clean environment and exercise it outside the checkout."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    distribution = Path(sys.argv[1]).resolve()
    wheel = sorted(distribution.glob("booking_agent-*.whl"))[-1]
    project = Path(__file__).resolve().parents[1]
    with ZipFile(wheel) as archive:
        assert "booking_agent/py.typed" in archive.namelist()
        assert "booking_agent/prompts.toml" in archive.namelist()
    with tempfile.TemporaryDirectory(prefix="booking-agent-consumer-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], check=True)
        check = """
import importlib.util
for package in ('fastapi', 'uvicorn', 'openai', 'sqlalchemy', 'psycopg'):
    assert importlib.util.find_spec(package) is None, package
from booking_agent import BookingEngine, BookingRequest, ConfirmQuote
from booking_agent.prompts import load_prompts
assert load_prompts().system
"""
        subprocess.run([str(python), "-I", "-c", check], cwd=root, check=True)
        subprocess.run([str(python), "-I", str(project / "examples/headless_booking.py")], cwd=root, check=True)
    print("Installed core wheel: imports, packaged resources and complete booking verified")


if __name__ == "__main__":
    main()
