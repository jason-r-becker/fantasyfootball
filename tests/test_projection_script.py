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


@pytest.mark.skipif(RSCRIPT is None, reason="Rscript is not installed")
@pytest.mark.parametrize("week", ["-1", "19", "1.5", "abc", "999999999999"])
def test_projection_script_rejects_invalid_week_before_scraping(week):
    result = subprocess.run(
        [RSCRIPT, str(SCRIPT), f"--week={week}"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--week must be an integer from 0 through 18" in result.stderr
