$src  = "$PSScriptRoot\addon"
$dest = "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360MCP"

Write-Host "Deploying addon -> $dest"

# Copy every file, preserving folder structure
Get-ChildItem -Path $src -Recurse -File | ForEach-Object {
    $rel    = $_.FullName.Substring($src.Length)
    $target = Join-Path $dest $rel
    $dir    = Split-Path $target

    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    Copy-Item $_.FullName $target -Force
}

# Clear pycache so Fusion recompiles from the fresh .py files
$cache = Join-Path $dest "server\__pycache__"
if (Test-Path $cache) {
    Remove-Item "$cache\*.pyc" -Force
    Write-Host "Cleared pycache"
}

Write-Host "Done. Reload the add-in in Fusion 360 (Tools -> Add-Ins -> Fusion360MCP -> Stop / Run)."
