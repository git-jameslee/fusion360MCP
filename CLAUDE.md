# Fusion360 MCP Server — Research Handoff

## Research Goal
Fix bugs in faust-machines/fusion360-mcp-server as Step 1 of a larger research project to build industrial-grade AI-driven CAM automation via MCP, culminating in a fine-tuned Qwen3-8B model that matches or outperforms Qwen3-30B on Fusion 360 CAM tool-use tasks.

## Full Research Pipeline (in order)
1. **[CURRENT] Fix faust bugs** — direct modeling mode, body naming after extrude, body indexing in fillet
2. Expand CAM coverage — port BJam's fusion-cam-cli query logic into faust's architecture
3. Build inspection + write CAM tool suite — feeds/speeds, toolpath status, machining time, tool library, operation params
4. Generate training data — Claude Sonnet driving the full tool suite on real workflows, logging structured (intent → tool calls → results → output) examples
5. Fine-tune Qwen3-8B with QLoRA on TACC Lonestar6
6. Evaluate fine-tuned Qwen3-8B vs base Qwen3-30B on held-out CAM tasks

## Stack
- **LLM**: Qwen3-8B served via vLLM 0.9.0 on TACC Lonestar6 (1x A100 40GB, gpu-a100-small)
- **Serving**: Apptainer (vllm-new.sif), hermes tool call parser, qwen3 reasoning parser, tunneled to localhost:8000
- **Middleware**: `fusion_middleware.py` in user home directory (Python, bridges LLM ↔ MCP server)
- **MCP Server**: faust-machines/fusion360-mcp-server (this repo)
- **Add-in**: Fusion360MCP add-in running inside Fusion 360, TCP :9876
- **Editor**: VS Code on Windows

## Step 1 — Known Bugs to Fix (start here)

### Bug 1: Direct Modeling Mode
- **File**: `addon/server/command_handler.py`
- **Issue**: Some tools crash or behave incorrectly when the design is in direct modeling mode instead of parametric mode
- **Look for**: `set_design_type`, `get_design_type` handlers and any tool that creates features without checking design type first

### Bug 2: Body Naming After Extrude
- **File**: `addon/server/command_handler.py`
- **Issue**: After `extrude`, the resulting body is not reliably named — subsequent tools that reference bodies by name fail to find them
- **Look for**: The `extrude` handler; check whether it assigns a name to `extrudeFeature.bodies[0]` after creation

### Bug 3: Body Indexing in Fillet
- **File**: `addon/server/command_handler.py`
- **Issue**: The `fillet` tool uses incorrect body/edge indexing, causing it to fillet the wrong edges or fail entirely
- **Look for**: The `fillet` handler; check edge collection logic and how it selects which body's edges to fillet

## How to Start
1. Open `addon/server/command_handler.py`
2. Search for the `extrude`, `fillet`, and `set_design_type` handlers
3. Fix Bug 2 first (body naming after extrude) — it's a prerequisite for Bug 3 since fillet needs to find the body by name
4. Then fix Bug 3 (fillet indexing)
5. Then fix Bug 1 (direct modeling mode guard)

## Notes
- All Fusion API units are **centimeters**
- One operation per tool call — batching crashes the add-in
- Add-in logs to `~/fusion360mcp.log`
- The `undo` tool already has a design-type safety guard as reference for how guards are implemented
- After fixing bugs, run: `uv run pytest -v` to check the 171 existing tests still pass
