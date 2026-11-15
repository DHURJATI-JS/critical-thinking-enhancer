<#
.SYNOPSIS
  One-click installer AND runner for Critical Thinking Enhancer (Windows 10 / 11).

.DESCRIPTION
  Idempotent - re-running skips everything already installed and just starts the app.
    1. Installs anything missing with winget: Docker Desktop, uv (Python manager), Tesseract OCR, Git.
    2. Detects the GPU (NVIDIA / AMD) + VRAM and picks a Qwen2.5-Coder size and context length.
       NVIDIA -> vLLM.  AMD (or -LlamaCpp) -> llama.cpp with Vulkan on Direct3D 12 (Mesa Dozen) - still on the GPU.
       Models always run on the GPU: without a usable GPU the local coder is not started at all.
    3. Writes .env (random secrets) and selects the right docker compose files for your GPU.
    4. Creates a Python 3.12 virtual environment and installs requirements.txt.
    5. Downloads Tailwind CSS, Icons8 icons, fonts and the avatar voice LOCALLY and builds the CSS.
    6. Starts PostgreSQL + pgvector, Redis and the Qwen coder container (vLLM or llama.cpp).
    7. Optionally opens Windows Firewall for your PRIVATE network so other devices can connect.
    8. Runs the web app on 0.0.0.0 and opens your browser.

.EXAMPLE
  Double-click Start-Windows.cmd   (or)   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Yes -NoRun
#>
[CmdletBinding()]
param(
    [switch]$Yes,          # answer "yes" to every prompt
    [switch]$NoRun,        # install only
    [switch]$SkipModels,   # do not start the model containers
    [switch]$Vision,       # also run the MiniCPM-V vision container (NVIDIA only; Windows OCR already covers OCR)
    [switch]$LlamaCpp,     # use llama.cpp (Vulkan on the GPU) for the Qwen coder even on NVIDIA
    [switch]$NoFirewall    # never touch Windows Firewall
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$EnvFile = Join-Path $Root '.env'

function Step([string]$m) { Write-Host ''; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok([string]$m) { Write-Host "    [ok] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "    [!]  $m" -ForegroundColor Yellow }
function Info([string]$m) { Write-Host "         $m" -ForegroundColor Gray }
function Have([string]$c) { return [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Ask([string]$q) {
    if ($Yes) { return $true }
    $a = Read-Host "    $q [Y/n]"
    return ($a -eq '' -or $a -match '^(y|yes)$')
}
function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Invoke-Native([string]$exe, [string[]]$argv, [switch]$Quiet) {
    # Windows PowerShell 5.1 turns native stderr into errors when ErrorActionPreference=Stop.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) { & $exe @argv *> $null } else { & $exe @argv 2>&1 | ForEach-Object { Write-Host "         $_" } }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}
function Install-Pkg([string]$id, [string]$name) {
    if (-not (Have 'winget')) { throw "winget is not available - please install $name manually, then re-run this script." }
    Info "installing $name with winget ($id) - accept the UAC prompt if Windows asks"
    $code = Invoke-Native 'winget' @('install', '--id', $id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    Update-SessionPath
    if ($code -ne 0 -and $code -ne -1978335189) { Warn "winget returned $code for $name" }  # -1978335189 = already installed
}
function Read-EnvFile {
    $map = [ordered]@{}
    if (Test-Path $EnvFile) {
        foreach ($line in Get-Content $EnvFile) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { $map[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $map
}
function Set-EnvValue([string]$key, [string]$value) {
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile) }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") { $lines[$i] = "$key=$value"; $found = $true }
    }
    if (-not $found) { $lines += "$key=$value" }
    Set-Content -Path $EnvFile -Value $lines -Encoding ASCII
}
function New-Secret([int]$bytes = 32) {
    $b = New-Object byte[] $bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return ([Convert]::ToBase64String($b) -replace '[+/=]', '')
}
function Get-Gpu {
    $gpu = [ordered]@{ vendor = 'none'; name = ''; vram = 0.0 }
    if (Have 'nvidia-smi') {
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $line = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
        $ErrorActionPreference = $old
        if ($line) {
            $p = $line.Split(',')
            $gpu.vendor = 'nvidia'; $gpu.name = $p[0].Trim(); $gpu.vram = [math]::Round([double]$p[1].Trim() / 1024, 1)
            return $gpu
        }
    }
    $class = 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    foreach ($key in (Get-ChildItem $class -ErrorAction SilentlyContinue)) {
        $props = Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue
        if (-not $props) { continue }
        $raw = $props.'HardwareInformation.qwMemorySize'
        if ($null -eq $raw) { continue }
        if ($raw -is [byte[]]) { $raw = [BitConverter]::ToUInt64($raw, 0) }
        $name = [string]$props.DriverDesc
        $vendor = ''
        if ($name -match 'NVIDIA|GeForce|RTX|Quadro') { $vendor = 'nvidia' } elseif ($name -match 'AMD|Radeon') { $vendor = 'amd' }
        $vram = [math]::Round([double]$raw / 1GB, 1)
        if ($vendor -and $vram -gt $gpu.vram) { $gpu.vendor = $vendor; $gpu.name = $name; $gpu.vram = $vram }
    }
    return $gpu
}
function Select-Coder([double]$vram) {
    if ($vram -ge 40) { return @('Qwen/Qwen2.5-Coder-32B-Instruct-AWQ', 32768) }
    if ($vram -ge 20) { return @('Qwen/Qwen2.5-Coder-14B-Instruct-AWQ', 32768) }
    if ($vram -ge 12) { return @('Qwen/Qwen2.5-Coder-7B-Instruct-AWQ', 16384) }
    if ($vram -ge 8) { return @('Qwen/Qwen2.5-Coder-7B-Instruct-AWQ', 8192) }
    if ($vram -ge 6) { return @('Qwen/Qwen2.5-Coder-3B-Instruct-AWQ', 8192) }
    return @('', 0)
}
function Select-Gguf([double]$vram) {
    # llama.cpp loads 4-bit GGUF weights (much smaller than vLLM's AWQ + KV cache); context is split over CODER_PARALLEL slots
    if ($vram -ge 10) { return @('Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M', 16384) }
    if ($vram -ge 6) { return @('Qwen/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M', 16384) }
    if ($vram -ge 4) { return @('Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF:Q4_K_M', 8192) }
    return @('', 0)
}
function Wait-Docker([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if ((Invoke-Native 'docker' @('info') -Quiet) -eq 0) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

Write-Host ''
Write-Host '  Critical Thinking Enhancer - one-click installer & runner' -ForegroundColor Magenta
Write-Host '  Student-grade project. Not a medical tool, not an IQ test. MIT licensed, no warranty, no liability.' -ForegroundColor DarkGray
Write-Host '  Everything runs and is stored locally on this machine.' -ForegroundColor DarkGray

# ---------------------------------------------------------------- 1. prerequisites
Step 'Checking prerequisites'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (Have 'uv') { Ok 'uv (Python manager)' } else {
    Install-Pkg 'astral-sh.uv' 'uv'
    if (-not (Have 'uv')) {
        Info 'falling back to the official uv installer (https://astral.sh/uv)'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        Update-SessionPath
        $env:Path += ";$env:USERPROFILE\.local\bin"
    }
    if (-not (Have 'uv')) { throw 'uv could not be installed - see https://docs.astral.sh/uv/' }
    Ok 'uv installed'
}
$dockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
if ((Have 'docker') -or (Test-Path $dockerDesktop)) { Ok 'Docker' } else {
    if (Ask 'Docker Desktop is required (PostgreSQL, Redis, vLLM). Install it now?') {
        Install-Pkg 'Docker.DockerDesktop' 'Docker Desktop'
        Warn 'Docker Desktop may need a restart / WSL2 setup. If the next step fails, finish Docker setup and run this script again.'
    } else { throw 'Docker is required.' }
}
$tesseract = @('C:\Program Files\Tesseract-OCR\tesseract.exe', 'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
if ((Have 'tesseract') -or $tesseract) { Ok 'Tesseract OCR (layer 1)' } else {
    if (Ask 'Install Tesseract OCR (first OCR layer)?') { Install-Pkg 'UB-Mannheim.TesseractOCR' 'Tesseract OCR' } else { Warn 'skipping Tesseract - RapidOCR and Windows OCR still work' }
}
if (Have 'git') { Ok 'Git' } else { if (Ask 'Install Git (used to download the AI server repo)?') { Install-Pkg 'Git.Git' 'Git' } }
Ok 'Windows OCR (system OCR layer) is built into Windows 10/11'

Step 'Starting Docker'
if (-not (Wait-Docker 3)) {
    if (Test-Path $dockerDesktop) { Info 'starting Docker Desktop...'; Start-Process $dockerDesktop | Out-Null }
    if (-not (Wait-Docker 240)) { throw 'Docker is not running. Open Docker Desktop, finish its setup (WSL2), then run this script again.' }
}
Ok 'Docker is running'

# ---------------------------------------------------------------- 2. hardware
Step 'Detecting hardware'
$gpu = Get-Gpu
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
Info "RAM: $ramGb GB"
if ($gpu.vendor -eq 'none') { Info 'GPU: no NVIDIA / AMD GPU found' } else { Info ("GPU: {0} ({1}, {2} GB VRAM)" -f $gpu.name, $gpu.vendor.ToUpper(), $gpu.vram) }
$coder = Select-Coder $gpu.vram
$gguf = Select-Gguf $gpu.vram
$engine = ''
$composeFiles = @('docker-compose.yml')
$profiles = @()
$coderNote = ''
if ($SkipModels) {
    $coderNote = 'model containers skipped (-SkipModels)'
} elseif ($gpu.vendor -eq 'nvidia' -and $coder[0] -and -not $LlamaCpp) {
    $engine = 'vllm'
    $composeFiles += 'docker/compose.nvidia.yml'; $profiles += 'coder'
    if ($Vision) { $profiles += 'vision' }
    $coderNote = "NVIDIA GPU -> vLLM running $($coder[0]) with $($coder[1]) tokens of context"
} elseif ($gpu.vendor -ne 'none' -and $gguf[0]) {
    # vLLM cannot reach AMD GPUs from Docker Desktop on Windows (ROCm is Linux-only); llama.cpp's Vulkan backend
    # can, through Mesa Dozen (Vulkan on Direct3D 12) and WSL2 GPU paravirtualisation (/dev/dxg).
    $engine = 'llamacpp'
    $composeFiles += 'docker/compose.llamacpp-dxg.yml'; $profiles += 'coder-llamacpp'
    $coderNote = "$($gpu.vendor.ToUpper()) GPU -> llama.cpp (Vulkan via Direct3D 12, in Docker) running $($gguf[0])"
    # MiniCPM-V (last OCR layer, ~6 GB of VRAM) fits next to the coder from 12 GB of VRAM - also on the GPU
    if ($Vision -or $gpu.vram -ge 12) { $profiles += 'vision-llamacpp' }
} else {
    $coderNote = 'No usable GPU (4 GB+ VRAM): the local coder is not started - models never run on the CPU.'
}
Info $coderNote
if ($ramGb -lt 8) { Warn 'Less than 8 GB RAM - your device is low-end for local AI. Use your own Claude / ChatGPT key in Settings.' }
if (-not $engine -and -not $SkipModels) {
    Warn 'The Qwen coder will NOT run locally. Choose one:'
    Info '  a) run scripts/install.sh on a Linux machine with an NVIDIA / AMD (ROCm) GPU and set CTE_CODER_URL in .env to it'
    Info '  b) point CTE_CODER_URL / CTE_CODER_MODEL in .env at a Qwen coder served by your AI server'
    Info '  c) add your own Claude or ChatGPT API key in the app (Settings > AI fallback)'
}

# ---------------------------------------------------------------- 3. .env
Step 'Writing .env (private, git-ignored)'
$fresh = -not (Test-Path $EnvFile)
if ($fresh) {
    "# Generated by scripts/install.ps1 on $(Get-Date -Format s). Private - never commit this file." | Set-Content $EnvFile -Encoding ASCII
}
$envMap = Read-EnvFile
$volumeExists = (Invoke-Native 'docker' @('volume', 'inspect', 'cte_cte_pgdata') -Quiet) -eq 0
$defaults = [ordered]@{
    CTE_SECRET_KEY = (New-Secret 48); CTE_DB_USER = 'cte'; CTE_DB_NAME = 'cte'; CTE_DB_PORT = '5440'; CTE_REDIS_PORT = '6380'
    CTE_DB_PASSWORD = $(if ($volumeExists) { 'cte_local_dev' } else { New-Secret 24 })
    CTE_VLLM_API_KEY = (New-Secret 24)
    CTE_GATEWAY_URL = 'http://127.0.0.1:4000/v1'; CTE_GATEWAY_API_KEY = ''; CTE_GATEWAY_MODEL = 'gateway-default'
    CTE_CODER_URL = 'http://127.0.0.1:8011/v1'; CTE_CODER_MODEL = 'qwen-coder'
    CTE_VISION_URL = 'http://127.0.0.1:8012/v1'; CTE_VISION_MODEL = 'minicpm-v'; VISION_MODEL = 'openbmb/MiniCPM-V-2_6'; HF_TOKEN = ''
}
foreach ($k in $defaults.Keys) { if (-not $envMap.Contains($k)) { Set-EnvValue $k $defaults[$k] } }
# a copied .env.example still holds public placeholders ("change-me...") - never keep those as real secrets
function Test-Placeholder([string]$v) { return (-not $v) -or $v -match '^(change-?me|your-|<)' }
foreach ($k in @('CTE_SECRET_KEY', 'CTE_VLLM_API_KEY')) {
    if ($envMap.Contains($k) -and (Test-Placeholder $envMap[$k])) { Set-EnvValue $k (New-Secret 48); Warn "$k was a placeholder - replaced with a random secret" }
}
if ($envMap.Contains('CTE_DB_PASSWORD') -and (Test-Placeholder $envMap['CTE_DB_PASSWORD'])) {
    if ($volumeExists) { Warn 'CTE_DB_PASSWORD is a placeholder, but the database already uses it - change it in PostgreSQL first, then here.' }
    else { Set-EnvValue 'CTE_DB_PASSWORD' (New-Secret 24); Warn 'CTE_DB_PASSWORD was a placeholder - replaced with a random password' }
}
if ($volumeExists -and -not $envMap.Contains('CTE_DB_PASSWORD')) { Warn 'Existing database volume found - keeping its original password.' }
$util = $(if ($profiles -contains 'vision') { '0.55' } else { '0.85' })
Set-EnvValue 'COMPOSE_PATH_SEPARATOR' ';'
Set-EnvValue 'COMPOSE_FILE' ($composeFiles -join ';')
Set-EnvValue 'COMPOSE_PROFILES' ($profiles -join ',')
Set-EnvValue 'CTE_GPU_VENDOR' $gpu.vendor
if ($engine -eq 'vllm') {
    Set-EnvValue 'CTE_CODER_ENGINE' 'vLLM (Docker, GPU)'
    Set-EnvValue 'QWEN_CODER_MODEL' $coder[0]; Set-EnvValue 'CODER_MAX_MODEL_LEN' ([string]$coder[1]); Set-EnvValue 'CODER_GPU_UTIL' $util
} elseif ($engine -eq 'llamacpp') {
    Set-EnvValue 'CTE_CODER_ENGINE' 'llama.cpp (Vulkan via Dozen on the GPU, Docker)'
    Set-EnvValue 'LLAMACPP_IMAGE' 'cte-llamacpp-dozen:latest'
    # keep a model you picked yourself on re-runs
    if (-not $envMap.Contains('QWEN_CODER_GGUF')) { Set-EnvValue 'QWEN_CODER_GGUF' $gguf[0]; Set-EnvValue 'CODER_MAX_MODEL_LEN' ([string]$gguf[1]) }
    if (-not $envMap.Contains('CODER_PARALLEL')) { Set-EnvValue 'CODER_PARALLEL' '2' }
    if (-not $envMap.Contains('CODER_GPU_LAYERS')) { Set-EnvValue 'CODER_GPU_LAYERS' '99' }
}
if ($profiles -contains 'vision-llamacpp') {
    Set-EnvValue 'CTE_VISION_ENABLED' 'auto'
    Set-EnvValue 'CTE_VISION_ENGINE' 'llama.cpp (Vulkan via Dozen on the GPU, Docker)'
    if (-not $envMap.Contains('VISION_GGUF')) { Set-EnvValue 'VISION_GGUF' 'openbmb/MiniCPM-V-2_6-gguf:Q4_K_M' }
} elseif ($profiles -contains 'vision') {
    Set-EnvValue 'CTE_VISION_ENABLED' 'auto'
    Set-EnvValue 'CTE_VISION_ENGINE' 'vLLM (Docker, GPU)'
} elseif (-not $envMap.Contains('CTE_VISION_ENABLED')) {
    Set-EnvValue 'CTE_VISION_ENABLED' 'false'  # no vision container here: Windows OCR covers the last OCR layer
}
# .env holds the database password and the key that protects stored API keys: readable by you only
$acl = Invoke-Native 'icacls' @($EnvFile, '/inheritance:r', '/grant:r', "$($env:USERDOMAIN)\$($env:USERNAME):F") -Quiet
if ($acl -ne 0) { Warn 'could not restrict .env to your user account (icacls) - keep this folder private' }
Ok $(if ($fresh) { '.env created with random secrets (readable by you only)' } else { '.env updated (secrets kept)' })

# ---------------------------------------------------------------- 4. Python
Step 'Python 3.12 environment (uv)'
if (-not (Test-Path '.venv\Scripts\python.exe')) { if ((Invoke-Native 'uv' @('venv', '--python', '3.12', '.venv')) -ne 0) { throw 'uv venv failed' } }
if ((Invoke-Native 'uv' @('pip', 'install', '--python', '.venv\Scripts\python.exe', '-r', 'requirements.txt')) -ne 0) {
    throw 'Installing Python packages failed (see output above).'
}
$py = Join-Path $Root '.venv\Scripts\python.exe'
Ok 'packages installed'

# ---------------------------------------------------------------- 5. assets
Step 'Downloading Tailwind CSS, Icons8 icons and fonts locally'
if ((Invoke-Native $py @('scripts\fetch_assets.py')) -ne 0) { Warn 'some assets could not be downloaded - the app still works with fallbacks' } else { Ok 'assets ready' }
Step 'Pre-downloading the local embedding model (pgvector memory)'
$code = Invoke-Native $py @('-c', "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir=r'.runtime\models\fastembed'); print('ok')") -Quiet
if ($code -eq 0) { Ok 'BAAI/bge-small-en-v1.5 cached' } else { Warn 'embedding model download failed - hashing embeddings will be used until it succeeds' }
Step 'Pre-downloading the avatar voice (Piper neural text-to-speech)'
$code = Invoke-Native $py @('-c', 'from app.services.tts import tts, DEFAULT_VOICE; tts.download(DEFAULT_VOICE); print(1)') -Quiet
if ($code -eq 0) { Ok 'default voice ready' } else { Warn 'voice download failed - the app retries on first use (or uses your browser voice)' }

# ---------------------------------------------------------------- 6. AI server repo
Step 'AI gateway (your AI server)'
$cfg = Get-Content (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
$repo = [string]$cfg.app.ai_server_repo
$port = [int]$cfg.app.port
if (-not $repo -or $repo.StartsWith('<')) {
    Warn 'config.json -> app.ai_server_repo is not set yet. Add your AI server repo link there.'
} else {
    Info "AI server repo: $repo"
    if (-not (Test-Path (Join-Path $Root 'ai-server')) -and (Have 'git') -and (Ask 'Download (git clone) the AI server repo into .\ai-server ?')) {
        Invoke-Native 'git' @('clone', '--depth', '1', $repo, 'ai-server') | Out-Null
    }
    Info 'Start it as its README describes, then put its URL / key / model into .env (CTE_GATEWAY_*).'
}
Info 'Note: locally-run Gemma models are not supported - use the AI server repo instead.'

# ---------------------------------------------------------------- 7. Docker services
Step 'Starting Docker services'
if ((Invoke-Native 'docker' @('compose', 'up', '-d', 'postgres', 'redis')) -ne 0) { throw 'docker compose up failed' }
$deadline = (Get-Date).AddSeconds(120)
do {
    Start-Sleep -Seconds 2
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $health = (& docker inspect -f '{{.State.Health.Status}}' cte-postgres 2>$null)
    $ErrorActionPreference = $old
} until ($health -eq 'healthy' -or (Get-Date) -gt $deadline)
if ($health -eq 'healthy') { Ok 'PostgreSQL + pgvector and Redis are up' } else { Warn "PostgreSQL health: $health" }
if ($profiles.Count -gt 0) {
    $build = ''
    if ($engine -eq 'llamacpp') {
        Info 'The first run builds the llama.cpp GPU image (about 5-10 minutes) and downloads the model weights.'
        $build = 'docker image inspect cte-llamacpp-dozen:latest *> $null; if ($LASTEXITCODE -ne 0) { docker compose build qwen-coder-llama }; '
    } else {
        Info 'The first run downloads the vLLM image and the model weights (can take a while).'
    }
    Info 'Starting the model container(s) in a separate window (it stays open if anything fails)...'
    $rootQuoted = $Root.Replace("'", "''")
    $done = "if (`$LASTEXITCODE -ne 0) { Write-Host 'Starting the model containers FAILED - see the messages above.' -ForegroundColor Red; Read-Host 'Press Enter to close' } " +
            "else { Write-Host 'Model containers started - this window can be closed.'; Start-Sleep 15 }"
    Start-Process powershell -WindowStyle Minimized -ArgumentList @('-NoProfile', '-Command', "Set-Location -LiteralPath '$rootQuoted'; $build docker compose up -d; $done")
}

# ---------------------------------------------------------------- 8. firewall (private LAN only)
if (-not $NoFirewall) {
    Step "Network access (port $port)"
    $rule = "Critical Thinking Enhancer ($port)"
    if (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue) { Ok 'firewall rule already present' }
    elseif (Ask "Let other devices on your PRIVATE network open the app (Windows Firewall, TCP $port)?") {
        $cmd = "New-NetFirewallRule -DisplayName '$rule' -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private | Out-Null"
        try {
            if ($isAdmin) { Invoke-Expression $cmd } else { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList @('-NoProfile', '-Command', $cmd) }
            # check the result instead of trusting the elevated window
            if (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue) { Ok 'firewall rule added (Private profile only)' }
            else { Warn 'the firewall rule was not created (UAC declined?) - other devices cannot connect yet' }
        } catch { Warn "could not add the firewall rule: $($_.Exception.Message)" }
    }
}

# ---------------------------------------------------------------- 9. run
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
    $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notmatch 'vEthernet|WSL|Loopback|Docker'
} | Select-Object -ExpandProperty IPAddress
Step 'Ready'
Info "This computer:  http://localhost:$port"
foreach ($ip in $ips) { Info "Your network:   http://${ip}:$port" }
if ($NoRun) { Ok 'Installed. Run this script again (or Start-Windows.cmd) to start the app.'; exit 0 }
Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 6; Start-Process $u } -ArgumentList "http://localhost:$port" | Out-Null
Info 'Press Ctrl+C to stop the app.'
& $py -m uvicorn app.main:app --host 0.0.0.0 --port $port
