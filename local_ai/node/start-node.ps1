<#
.SYNOPSIS
  Serve one model from a second PC (e.g. a GTX 970 with 4 GB) to the gateway on another PC.

.DESCRIPTION
  Runs llama.cpp's llama-server with an API key, bound to one address only: the PC's
  Tailscale address by default, so only your own devices can reach it. The gateway on
  the main PC lists this server as a backend (see docs/local_ai.html, "Second PC").

  Use the Vulkan build of llama.cpp on Maxwell cards such as the GTX 970: CUDA 13
  builds no longer support them. Download "llama-<build>-bin-win-vulkan-x64.zip" from
  https://github.com/ggml-org/llama.cpp/releases and unzip it to -LlamaDir.

.EXAMPLE
  .\start-node.ps1 -Model D:\Models\qwen2.5-coder-3b-instruct-q4_k_m.gguf -Alias coder3b
#>
param(
    [Parameter(Mandatory)] [string] $Model,
    [string] $Alias = "lan-small",
    [string] $LlamaDir = "D:\AI\llama.cpp-vulkan",
    [string] $BindAddress = "",          # empty: this PC's Tailscale address
    [int]    $Port = 11437,
    [int]    $Context = 16384,
    [int]    $Slots = 1,
    [string] $KeyFile = "$env:USERPROFILE\.automatica\node.key",
    [string[]] $Extra = @()
)
$ErrorActionPreference = "Stop"

if (-not $BindAddress) {
    $tailscale = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -like "100.*" -and $_.InterfaceAlias -like "*Tailscale*" } |
        Select-Object -First 1
    if (-not $tailscale) { throw "No Tailscale address found. Install Tailscale or pass -BindAddress." }
    $BindAddress = $tailscale.IPAddress
}

if (-not (Test-Path $KeyFile)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $KeyFile) | Out-Null
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_") | Set-Content -Encoding ascii $KeyFile
    Write-Host "Created API key in $KeyFile - copy it to the main PC (gateway backend api_key_file)."
}

$server = Join-Path $LlamaDir "llama-server.exe"
if (-not (Test-Path $server)) { throw "llama-server.exe not found in $LlamaDir" }

# All layers on the GPU (-ngl 99) when the model fits in ~3.5 GB; lower -ngl otherwise.
$arguments = @(
    "-m", $Model, "--alias", $Alias, "--host", $BindAddress, "--port", $Port,
    "--api-key-file", $KeyFile, "-ngl", "99", "-c", $Context, "--parallel", $Slots,
    "--flash-attn", "on", "--jinja", "--no-webui", "--metrics"
) + $Extra

Write-Host "Serving $Alias on http://${BindAddress}:$Port"
& $server @arguments
