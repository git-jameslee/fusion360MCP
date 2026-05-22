# Claude Code Handoff — cam_set_operation_tool (Step 2 continuation)

## Context

Research project: AI-driven CAM automation via MCP, ultimately fine-tuning Qwen3-8B on CAM tool-use training data. Repo is a fork of faust-machines/fusion360-mcp-server at github.com/git-jameslee/fusion360MCP.

Steps 1 (bug fixes) and 2 (8 new CAM tools) are complete. A new tool `cam_set_operation_geometry` was also built and is working. See HANDOFF_CAM_TOOLS.md for full architecture.

---

## Current Blocker

Running the full end-to-end test sequence. Stuck at **Test 6 (generate toolpath)** because operations have no tool assigned.

`cam_get_operation_details` confirmed: `tool: {}` — no tool assigned to the operation. Fusion silently refuses to generate a toolpath with no tool. The UI workaround (double-click operation → Tool tab → select tool) works but breaks the fully-API-driven workflow needed for training data generation.

---

## Your Task: Build `cam_set_operation_tool`

This is the last missing piece before the full create → configure → generate → verify → machining_time loop runs without touching the Fusion UI.

### Inputs
- `setup_name` (string, required)
- `operation_name` (string, required)
- `tool_number` (int, optional) — assign tool by number from document library
- `tool_description` (string, optional) — assign tool by description (partial match)

### Expected output
```json
{
  "success": true,
  "operation": "2D Adaptive1",
  "tool_assigned": {
    "number": 1,
    "description": "6mm Flat End Mill",
    "diameter_mm": 6.0
  }
}
```

### Key Fusion API to try (in this order)

**Approach 1 — direct property assignment:**
```python
cam = self._get_cam()
tool_lib = cam.documentToolLibrary
# find tool by number or description
for i in range(tool_lib.count):
    tool = tool_lib.item(i)
    if tool.number == tool_number:
        op.tool = tool   # try direct assignment
        break
```

**Approach 2 — if op.tool is read-only, recreate via OperationInput:**
The operation may need to be deleted and recreated with the tool reference baked in via `setup.operations.createInput()`. Check if `OperationInput` has a `.tool` or `.toolLibraryTool` property.

**Approach 3 — parameter expression:**
```python
# Tool params are stored in the parameter list
# Try setting tool_number param which Fusion may resolve against the library
p = op.parameters.itemByName('tool_number')
if p: p.expression = str(tool_number)
```

Unknown which works — needs live testing in Fusion. Start with Approach 1.

### Files to edit
1. `addon/server/command_handler.py` — add handler method + dispatch entry
2. `src/fusion360_mcp/tools.py` — add tool definition + annotation sets
3. `src/fusion360_mcp/mock.py` — add mock handler + dispatch entry
4. `tests/test_tools.py` — add to expected tool set
5. `tests/test_mock.py` — add mock test

After each file, run:
```bash
uv run pytest tests/test_tools.py tests/test_mock.py -q
uv run ruff check src/
```

Deploy to Fusion after handler is written:
```bash
bash deploy_addon.sh
# then Stop → Run the Fusion360MCP add-in in Fusion
```

---

## What's Already Working

- `cam_set_operation_geometry` — assigns body faces to operation's `pockets` parameter (2D Adaptive), `model` (3D ops)
- `cam_generate_toolpath` — fixed to wrap op in ObjectCollection before calling `cam.generateToolpath(ops)`
- `cam_get_toolpath_status` — polls toolpath validity
- Full deploy script: `bash deploy_addon.sh` copies `addon/` to Fusion AddIns folder

## Current Test State

| Test | Status |
|------|--------|
| 1 cam_get_tools | ✅ |
| 2 cam_create_setup + cam_create_operation | ✅ |
| 3 cam_get_operation_details | ✅ |
| 4 cam_get_toolpath_status (before) | ✅ |
| 5 cam_update_operation_parameters | ✅ |
| 6 cam_generate_toolpath + verify | ❌ blocked (no tool assigned) |
| 7 cam_get_machining_time | not yet |
| 8 full optimization loop | not yet |
| 9 cam_get_nc_programs | not yet |

---

## Repo Layout (relevant files)

```
addon/server/command_handler.py   ← Fusion add-in handlers (the file that runs inside Fusion)
src/fusion360_mcp/tools.py        ← MCP tool definitions
src/fusion360_mcp/mock.py         ← mock handlers for --mode mock testing
tests/test_tools.py               ← tool registration tests
tests/test_mock.py                ← mock handler tests
deploy_addon.sh                   ← copies addon/ to Fusion AddIns folder
```

Installed add-in path (separate from repo — must deploy explicitly):
```
C:\Users\Nanja\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360MCP\
```
