# Installs courier (herdr-courier) as a command with uv, installing uv first if it is missing.
#
#   powershell -ExecutionPolicy Bypass -c "irm -useb https://raw.githubusercontent.com/NaNIsaac/herdr-courier/main/install.ps1 | iex"
#
# Run it again to update. uv's own settings apply, e.g. UV_TOOL_BIN_DIR for where the command goes.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# Not on PyPI: the main branch as a source archive, so git is not needed. --reinstall fetches it anew.
$install = '--reinstall', 'herdr-courier @ https://github.com/NaNIsaac/herdr-courier/archive/refs/heads/main.zip'
$cli = 'courier'

if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing uv (https://docs.astral.sh/uv/)'
    # The uv installer adds its folder to the user PATH unless this is set.
    Remove-Item Env:UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue
    # A child scope keeps the installer's variables out of this script.
    & { Invoke-RestMethod -UseBasicParsing https://astral.sh/uv/install.ps1 | Invoke-Expression }
    # It changes the user PATH for new terminals only.
    $uvDir = @($env:UV_INSTALL_DIR, $env:UV_UNMANAGED_INSTALL, $env:XDG_BIN_HOME, (Join-Path $HOME '.local\bin')) |
        Where-Object { $_ -and (Test-Path (Join-Path $_ 'uv.exe')) } | Select-Object -First 1
    if (-not $uvDir) { throw 'uv was installed but uv.exe was not found; open a new terminal and run this again.' }
    $env:Path = "$uvDir;$env:Path"
}

uv tool install @install
if ($LASTEXITCODE) { throw "uv tool install failed (exit code $LASTEXITCODE)." }
# Puts uv's command folder on the user PATH for new terminals; does nothing if it is there.
uv tool update-shell
if ($LASTEXITCODE) { throw "uv tool update-shell failed (exit code $LASTEXITCODE)." }
$bin = uv tool dir --bin
if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$env:Path;$bin" }
Write-Host "Installed $cli to $bin. Next (in a new terminal): $cli --help"
