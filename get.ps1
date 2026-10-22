# Aladin — jednořádková instalace pro Windows.
#
#     irm https://raw.githubusercontent.com/jurapascal/claude-code-hub/main/get.ps1 | iex
#
# Stáhne repo do %USERPROFILE%\.claude\hub-src a spustí install.ps1. Nic jiného
# nedělá — všechna rozhodnutí padají tam.
#
# Proměnné pro nestandardní případy:
#     $env:HUB_REPO = 'owner/repo'   $env:HUB_BRANCH = 'main'   $env:HUB_YES = '1'
#
# POZOR: tenhle soubor NESMÍ mít BOM. Ostatní .ps1 v repu ho mají, protože
# PowerShell 5.1 bez něj čte soubor z disku v systémové kódové stránce a
# rozsype si diakritiku. Tenhle se ale nikdy nespouští z disku — teče do `iex`
# jako řetězec, a tam BOM zůstane znakem U+FEFF na začátku první řádky.
# PowerShell si ho pak přilepí k mřížce a místo komentáře vidí příkaz:
#     # : The term '#' is not recognized as the name of a cmdlet

$ErrorActionPreference = 'Stop'

$repo   = if ($env:HUB_REPO)   { $env:HUB_REPO }   else { 'jurapascal/claude-code-hub' }
$branch = if ($env:HUB_BRANCH) { $env:HUB_BRANCH } else { 'main' }
$dest   = if ($env:HUB_DEST)   { $env:HUB_DEST }   else { Join-Path $env:USERPROFILE '.claude\hub-src' }

function Write-Ok   ($m) { Write-Host "  ✓ $m" -ForegroundColor Green }
function Write-Info ($m) { Write-Host "  ▸ $m" -ForegroundColor DarkYellow }
function Die        ($m) { Write-Host "  ⚠ $m" -ForegroundColor Yellow; exit 1 }

Write-Host ''
Write-Host '  ✦ Aladin' -ForegroundColor DarkYellow
Write-Host '  ────────────────────────────────────' -ForegroundColor DarkGray

# Git když je (jde pak updatovat přes git pull), jinak ZIP — na Windows je
# Expand-Archive součástí systému, takže se nic dalšího stahovat nemusí.
$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)

if ($hasGit -and (Test-Path (Join-Path $dest '.git'))) {
    Write-Info "aktualizuju $dest"
    git -C $dest fetch --quiet origin $branch
    git -C $dest reset --quiet --hard "origin/$branch"
    if ($LASTEXITCODE -ne 0) { Die "update nevyšel — smaž $dest a spusť to znovu" }
} elseif ($hasGit) {
    Write-Info "stahuju $repo"
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
    git clone --quiet --depth 1 --branch $branch "https://github.com/$repo.git" $dest
    if ($LASTEXITCODE -ne 0) { Die 'klonování nevyšlo — je repo veřejné a máš internet?' }
} else {
    Write-Info "stahuju $repo (bez gitu, přes ZIP)"
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("hub-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        # TLS 1.2 explicitně: Windows PowerShell 5.1 ho sám od sebe nezapne
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $zip = Join-Path $tmp 'hub.zip'
        Invoke-WebRequest -UseBasicParsing `
            -Uri "https://codeload.github.com/$repo/zip/refs/heads/$branch" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (-not $inner) { Die 'ZIP se rozbalil prázdný' }
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Move-Item $inner.FullName $dest
    } catch {
        Die "stahování nevyšlo: $($_.Exception.Message)"
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
Write-Ok "zdroj v $dest"

$installer = Join-Path $dest 'install.ps1'
if (-not (Test-Path $installer)) { Die "v $dest není install.ps1 — něco se stáhlo špatně" }

Write-Host ''
# Instalačka si podle $MyInvocation hledá vlastní složku, takže se musí spustit
# jako soubor. Jenže na spouštění .ps1 souborů má Windows ve výchozím stavu
# zámek (ExecutionPolicy Restricted) a `irm | iex` ho obchází jen sám pro sebe,
# ne pro to, co zavolá dál — proto vlastní PowerShell s výjimkou na jeden běh.
$psExe = 'powershell.exe'
try {
    $own = (Get-Process -Id $PID).Path
    if ($own) { $psExe = $own }     # ať se pokračuje tím, čím se začalo (i pwsh)
} catch { }

$argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer)
if ($env:HUB_YES) { $argv += '-Yes' }
& $psExe @argv

if ($null -eq $LASTEXITCODE) { exit 0 }
exit $LASTEXITCODE
