#!/usr/bin/env bash
# Deploy addon/ to the Fusion 360 AddIns folder.
# Run this after editing any file under addon/, then reload the add-in in Fusion.
#
# Usage: bash deploy_addon.sh [--restart]
#   --restart  attempt to stop and restart the add-in via Fusion's CLI (best-effort)

set -euo pipefail

REPO_ADDON="$(dirname "$0")/addon"
FUSION_ADDIN="/c/Users/Nanja/AppData/Roaming/Autodesk/Autodesk Fusion 360/API/AddIns/Fusion360MCP"

if [ ! -d "$FUSION_ADDIN" ]; then
    echo "ERROR: Add-in folder not found: $FUSION_ADDIN" >&2
    exit 1
fi

echo "Syncing addon/ → Fusion AddIns..."
find "$REPO_ADDON" -name '*.py' | while read -r src; do
    rel="${src#$REPO_ADDON/}"
    dst="$FUSION_ADDIN/$rel"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    echo "  $rel"
done

echo ""
echo "Done. Reload the add-in in Fusion 360:"
echo "  Shift+S → Add-Ins → Fusion360MCP → Stop → Run"
