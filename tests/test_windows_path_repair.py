"""Static guard and Windows syntax check for surgical command resolution repair."""
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPAIR = ROOT / "powershell" / "Repair-CodexExporterPath.ps1"
INSTALLER = ROOT / "Install-CodexThreadExporter.ps1"


def test_repair_is_installed_and_explicitly_opt_in():
    script = REPAIR.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "SupportsShouldProcess" in script
    assert "SetEnvironmentVariable('Path', $userFix.Value, 'User')" in script
    assert "SetEnvironmentVariable('Path', $processFix.Value, 'Process')" in script
    assert "'codex-export.exe'" in script
    assert "'Machine'" in script and "'Machine')" in script
    assert "GetEnvironmentVariable('Path', 'Machine')" in script
    assert "[switch]$RepairCommandPath" in installer
    assert "if ($RepairCommandPath)" in installer
    assert "-PythonPath $PythonPath" in installer
    assert "Copy-Item -LiteralPath $sourcePathRepair" in installer
    assert "Package installation failed; PATH was not changed." in installer


def test_powershell_parser_reports_zero_syntax_errors():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return
    # Parse without executing the repair or touching PATH, including in Windows CI.
    for path in [REPAIR, INSTALLER]:
        code = (
            "$t=$null;$e=$null;"
            "[System.Management.Automation.Language.Parser]::ParseFile('"
            + str(path).replace("'", "''")
            + "',[ref]$t,[ref]$e)|Out-Null;"
            "if($e.Count){$e | Out-String | Write-Error;exit 1}"
        )
        result = subprocess.run([pwsh, "-NoProfile", "-Command", code],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (path, result.stderr)