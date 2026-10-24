# Claude Code Hub — kontrola na Windows.
#
# Odpovídá na to, co se z Linuxu ověřit nedá a co bylo do teď domněnkou:
# jestli půjde odkaz na složku (symlink chce práva správce, křižovatka ne),
# jestli naběhne ConPTY terminál, jak se doopravdy jmenuje složka, do které si
# Claude Code ukládá paměť, a jestli schránka veze češtinu.
#
#     powershell -ExecutionPolicy Bypass -File windows-check.ps1
#
# Nic nemění — jen zkouší a vypisuje. Výstup se dá poslat zpátky tak, jak je.

$ErrorActionPreference = 'Continue'
$results = [ordered]@{}

function Say($label, $value, $ok) {
    $color = if ($ok -eq $true) { 'Green' } elseif ($ok -eq $false) { 'Yellow' } else { 'Gray' }
    Write-Host ("  {0,-24} " -f $label) -NoNewline
    Write-Host $value -ForegroundColor $color
}

Write-Host ''
Write-Host '  Claude Code Hub — kontrola na Windows' -ForegroundColor DarkYellow
Write-Host ('  ' + ('-' * 44)) -ForegroundColor DarkGray

# ── 1. Co na stroji je ───────────────────────────────────────────────────────
$osName = (Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption
Say 'systém' $osName $null
Say 'PowerShell' $PSVersionTable.PSVersion.ToString() $null
$results.os = $osName

# Najít nestačí. Čerstvé Windows mají v PATH zástupce z Microsoft Store —
# `python.exe` ve WindowsApps, který nic nespustí, jen otevře obchod. Get-Command
# ho vrátí jako plnohodnotný nález, takže se každý nástroj radši zkusí spustit.
foreach ($tool in 'python', 'git', 'bash', 'claude', 'winget', 'gh') {
    $found = (Get-Command $tool -ErrorAction SilentlyContinue |
              Select-Object -First 1).Source
    if (-not $found) {
        Say $tool 'CHYBÍ' $false
        $results[$tool] = $null
        continue
    }
    $ver = ''
    try {
        $ver = (& $tool --version 2>&1 | Select-Object -First 1 | Out-String).Trim()
    } catch { $ver = '' }
    # Zástupce obchodu buď neodpoví, nebo rovnou napíše, ať si Python nainstaluju
    # z Microsoft Store. Instalačka na něj jinak naletí a Python bude „nalezený".
    $stub = ($found -like '*\WindowsApps\*') -and
            (-not $ver -or $ver -match 'Microsoft Store|was not found')
    if ($stub) {
        Say $tool 'jen zástupce z Microsoft Store (nic nespustí)' $false
        $results[$tool] = 'store-stub'
    } else {
        Say $tool ("{0}  ({1})" -f $ver, $found) $true
        $results[$tool] = $found
    }
}

# Git for Windows nese bash, na kterém stojí každý tab
$gitBash = @(
    "$env:ProgramFiles\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
Say 'git bash' ($(if ($gitBash) { $gitBash } else { 'CHYBÍ — tab se neotevře' })) ([bool]$gitBash)
$results.gitBash = $gitBash

# ── 2. Odkaz na složku: symlink vs. křižovatka ───────────────────────────────
# Tohle je ta hlavní otázka. Symlink na Windows chce práva správce nebo zapnutý
# vývojářský režim; křižovatka nechce nic a pro složku dělá totéž.
$admin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
Say 'běží jako správce' $(if ($admin) { 'ano' } else { 'ne' }) $null
$results.admin = $admin

# Vývojářský režim je to jediné, co pustí k symlinku i běžného uživatele —
# a na rozdíl od pokusu o symlink se dá přečíst bez ohledu na to, pod kým
# tenhle skript zrovna běží.
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock'
$devMode = (Get-ItemProperty -Path $key -Name AllowDevelopmentWithoutDevLicense `
            -ErrorAction SilentlyContinue).AllowDevelopmentWithoutDevLicense -eq 1
Say 'vývojářský režim' $(if ($devMode) { 'zapnutý' } else { 'vypnutý' }) $null
$results.devMode = $devMode

$tmp = Join-Path $env:TEMP ("hubcheck-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path "$tmp\cil" | Out-Null
# Zkoušíme obojí zvlášť. Kdyby se skončilo u prvního, co projde, řekl by běh
# pod správcem „symlink jde" — jenže instalačku pouští běžný uživatel, kterému
# symlink projde jen ve vývojářském režimu. Křižovatka nechce nic.
$symlink = $false
try {
    New-Item -ItemType SymbolicLink -Path "$tmp\odkaz" -Target "$tmp\cil" -ErrorAction Stop | Out-Null
    $symlink = Test-Path "$tmp\odkaz"
} catch { $symlink = $false }
$note = if ($symlink -and $admin -and -not $devMode) { ' (ale jen díky právům správce)' } else { '' }
Say 'symlink' ($(if ($symlink) { 'jde' + $note } else { 'NEJDE' })) $symlink

$out = cmd /c mklink /J "$tmp\krizovatka" "$tmp\cil" 2>&1
$junction = Test-Path "$tmp\krizovatka"
Say 'křižovatka (/J)' ($(if ($junction) { 'jde' } else { "NEJDE — $out" })) $junction

$linkWay = if ($junction) { 'křižovatka' } elseif ($symlink) { 'symlink' } else { 'NEJDE' }
Say 'hub použije' $linkWay ($linkWay -ne 'NEJDE')
$results.symlink = $symlink
$results.junction = $junction
$results.link = $linkWay
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

# ── 3. Jak si Claude Code jmenuje složku pro paměť ───────────────────────────
# Hub tam vede odkaz, ale jméno si Claude Code tvoří sám a my ho můžeme leda
# najít. Tady je vidět, jestli náš odhad sedí.
$projects = Join-Path $env:USERPROFILE '.claude\projects'
if (Test-Path $projects) {
    $dirs = @(Get-ChildItem $projects -Directory -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty Name)
    Say 'složek sezení' ("{0}" -f $dirs.Count) $null
    $homeSlug = $env:USERPROFILE -replace '\\', '-' -replace ':', ''
    $match = $dirs | Where-Object { ($_ -replace '-', '') -eq ($homeSlug -replace '-', '') }
    Say 'pro domovskou složku' ($(if ($match) { $match } else { 'zatím žádná' })) ([bool]$match)
    $results.homeSlug = $match
    $results.someSlugs = ($dirs | Select-Object -First 5) -join ', '
    if ($dirs.Count) { Say 'ukázka jmen' ((($dirs | Select-Object -First 3) -join ', ')) $null }
} else {
    Say 'složky sezení' 'Claude Code tu ještě neběžel' $false
}

# ── 4. Schránka ──────────────────────────────────────────────────────────────
# Zkouší se to tak, jak to dělá hub: cizím procesem, ne z PowerShellu přímo.
# Uvnitř jednoho běhu je Get-Clipboard obyčejný .NET řetězec a diakritika by
# prošla vždycky — chyba je až na hranici mezi procesy, kde se text kóduje.
$probe = "příliš žluťoučký kůň ĚŠČŘŽ"

$oemCP = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage' `
          -Name OEMCP -ErrorAction SilentlyContinue).OEMCP
if (-not $oemCP) { $oemCP = '437' }
Say 'kódová stránka' ("OEM $oemCP") $null
$results.oemCP = $oemCP

function Clip-RoundTrip($bytes) {
    $tmpf = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllBytes($tmpf, $bytes)
        # Schválně pod systémovou stránkou, ne pod `chcp 65001` téhle konzole:
        # hub pouští clip.exe z Pythonu, kde o žádném UTF-8 nikdo neví.
        cmd /c "chcp $oemCP >nul & clip < `"$tmpf`"" | Out-Null
        Start-Sleep -Milliseconds 250
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        $back = & powershell -NoProfile -Command `
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"
        return (($back | Out-String) -replace "`r`n", "`n").TrimEnd("`n")
    } finally { Remove-Item $tmpf -Force -ErrorAction SilentlyContinue }
}

try {
    # a) naivně UTF-8 — takhle to hub dělal a takhle to nefunguje
    $naive = Clip-RoundTrip ([Text.Encoding]::UTF8.GetBytes($probe))
    $naiveOK = $naive -eq $probe
    Say 'schránka UTF-8' ($(if ($naiveOK) { 'projde' } else { "rozbitá: '$naive'" })) $naiveOK
    $results.clipboardUtf8 = $naiveOK

    # b) UTF-16LE s BOM — jediné, čemu clip.exe uvěří
    $bom = [Text.Encoding]::Unicode.GetPreamble() + [Text.Encoding]::Unicode.GetBytes($probe)
    $wide = Clip-RoundTrip $bom
    $wideOK = $wide -eq $probe
    Say 'schránka UTF-16LE' ($(if ($wideOK) { 'projde tam i zpět' } else { "ROZBITÁ: '$wide'" })) $wideOK
    $results.clipboardUtf16 = $wideOK
} catch {
    Say 'schránka' "chyba: $($_.Exception.Message)" $false
}

# ── 5. Terminál pod ConPTY ───────────────────────────────────────────────────
$py = if ($results.python -and $results.python -ne 'store-stub') { $results.python } else { $null }
if (-not $py) {
    Say 'pty (ConPTY)' 'neni skutecny Python — nelze zkusit' $false
}
if ($py) {
    $hubPty = Join-Path $env:USERPROFILE '.claude\hub\pty_backend.py'
    if (Test-Path $hubPty) {
        $code = @"
import sys; sys.path.insert(0, r'$env:USERPROFILE\.claude')
from hub import pty_backend
ok, detail = pty_backend.selftest()
print('OK' if ok else 'SELHALO: ' + detail[:120])
"@
        $out = $code | & $py - 2>&1 | Select-Object -Last 1
        Say 'pty (ConPTY)' $out ($out -like 'OK*')
        $results.pty = $out
    } else {
        Say 'pty (ConPTY)' 'hub není nainstalovaný' $false
    }
    $wp = & $py -c "import winpty, sys; print(getattr(winpty,'__version__','?'))" 2>&1
    Say 'pywinpty' $wp ($LASTEXITCODE -eq 0)
}

# ── 6. Schránka očima hubu ───────────────────────────────────────────────────
# Zkoušet clip.exe zvenčí je jen náznak. Tohle projde přesně tou cestou, kterou
# jde vkládání v tabu: core.clipboard_write() a core.clipboard_read().
#
# Zdroj se posílá zakódovaný a výsledek se tiskne přes ascii(), protože jinak by
# se česká sonda cestou přes rouru rozsypala o kódování konzole — tedy přesně
# o to, co se tu měří, a nikdo by pak nepoznal, čí je to chyba.
$hubMod = Join-Path $env:USERPROFILE '.claude\hub\core.py'
if ($py -and (Test-Path $hubMod)) {
    $src = @"
import sys
sys.path.insert(0, r'$env:USERPROFILE\.claude')
from hub import core
probe = 'p\u0159\xedli\u0161 \u017elu\u0165ou\u010dk\xfd k\u016f\u0148 \u011a\u0160\u010c\u0158\u017d'
wrote = core.clipboard_write(probe)
back = core.clipboard_read()
print('zapis', wrote, 'shoda', back == probe, ascii(back))
"@
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($src))
    $out = & $py -c "import base64;exec(base64.b64decode('$b64').decode('utf-8'))" 2>&1
    foreach ($line in @($out)) { Say 'hub schránka' $line ($line -match 'shoda True') }
    $results.hubClipboard = ($out -join ' ')

    # Obrázek na schránce: dokud tam žádný není, `clipboard_image()` poctivě
    # vrací None a netestuje se nic. Jeden si tedy vyrobíme — přesně tohle na
    # schránce nechá výstřižek z Win+Shift+S.
    try {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $bmp = New-Object Drawing.Bitmap 48, 24
        $g = [Drawing.Graphics]::FromImage($bmp)
        $g.Clear([Drawing.Color]::Tomato)
        $g.Dispose()
        [Windows.Forms.Clipboard]::SetImage($bmp)
        $bmp.Dispose()
        $srcImg = @"
import sys, os
sys.path.insert(0, r'$env:USERPROFILE\.claude')
from hub import core
p = core.clipboard_image()
print('cesta', ascii(p), 'bajtu', os.path.getsize(p) if p else 0)
"@
        $b64i = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($srcImg))
        $outImg = & $py -c "import base64;exec(base64.b64decode('$b64i').decode('utf-8'))" 2>&1
        foreach ($line in @($outImg)) { Say 'hub obrázek' $line ($line -notmatch 'None') }
        $results.hubImage = ($outImg -join ' ')
    } catch {
        Say 'hub obrázek' "nešlo připravit: $($_.Exception.Message)" $false
    }
} elseif ($py) {
    Say 'hub schránka' 'hub není nainstalovaný' $false
}

# ── 7. Vlastní kontrola hubu ─────────────────────────────────────────────────
$hub = Join-Path $env:USERPROFILE '.claude\claude-hub.py'
if ((Test-Path $hub) -and $py) {
    Write-Host ''
    Write-Host '  --- claude-hub.py --doctor ---' -ForegroundColor DarkGray
    & $py $hub --doctor 2>&1 | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host ''
    Write-Host '  Hub ještě není nainstalovaný. Nainstaluj ho:' -ForegroundColor DarkGray
    Write-Host '    irm https://raw.githubusercontent.com/jurapascal/aladin/main/get.ps1 | iex' -ForegroundColor DarkGray
}

Write-Host ''
Write-Host '  Tenhle výpis pošli zpátky celý.' -ForegroundColor DarkYellow
Write-Host ''
