import shutil
import subprocess
from pathlib import Path

import pytest

RSCRIPT = shutil.which("Rscript")
SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "generate_ffanalytics_projections.R"
)


@pytest.mark.skipif(RSCRIPT is None, reason="Rscript is not installed")
def test_projection_script_help_exits_without_season_data():
    result = subprocess.run(
        [RSCRIPT, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--refresh" in result.stdout


@pytest.mark.skipif(RSCRIPT is None, reason="Rscript is not installed")
def test_projection_script_rejects_unknown_arguments():
    result = subprocess.run(
        [RSCRIPT, str(SCRIPT), "--bogus"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Unknown argument(s): --bogus" in result.stderr
