# Research Trajectory — Fusion360 CAM AI
**Last updated:** 2026-05-27
**Author:** James Lee
**Contact:** james.lee0350@utexas.edu

---

## Research Goal

Build a publishable ML result in industrial-grade CAD/CAM automation via MCP, structured in two phases:

**Phase 1 — Evaluation:** Design and run evaluation protocols to establish how well existing models (Claude Sonnet, Qwen3-8B, Gemma 4) perform on real CAD/CAM tasks. Measures are both subjective (1–7 human ratings) and objective (voxel IoU, toolpath validity, dimension accuracy). This phase stands on its own — it produces the first systematic benchmark of LLM CAM tool-use capability.

**Phase 2 — Fine-tuning:** Use the baseline gaps from Phase 1 to guide QLoRA fine-tuning of Qwen3-8B on TACC Lonestar6 (A100 40GB). Demonstrate that fine-tuned Qwen3-8B matches or outperforms Qwen3-30B base at 4x smaller scale. The key claim is that **reasoning-annotated training data** — not bare tool call logs — is what produces the improvement.

---

## Key Trajectory Change: From Tool-Use to CAM Expertise

The original pipeline framed the fine-tuning goal as **tool-call efficiency** — teaching the model which MCP tools to call, in what order, with correct syntax. This is necessary but insufficient.

**The revised goal is CAM expertise:** the model should reason like a human CAM engineer, not just execute a memorized tool-call sequence.

### What "CAM Expertise" Means

A CAM expert, given a part, can answer:
- What material, machine, and fixturing setup is appropriate?
- Which operations in what order, and why?
- What feeds/speeds for this tool + material + depth of cut — and what breaks if pushed?
- Which toolpath strategy minimizes cycle time without sacrificing surface finish?
- Where are thin walls, deep pockets, or hard-to-reach features needing special treatment?

The model must *reason through* these questions, not imitate a memorized sequence.

---

## Pipeline (Current)

| Step | Status |
|------|--------|
| 1 — Fix faust bugs | ✅ Done |
| 2 — Expand CAM coverage (97 tools) | ✅ Done |
| 3 — Build inspection + write CAM tool suite | ✅ Done |
| 4 — Design evaluation protocols | 🔲 NOT started — **current blocker** |
| 5 — Run baseline evaluation (Claude, Qwen, Gemma 4) | 🔲 Not started |
| 6 — Generate training data | 🔲 Infrastructure done; blocked until Step 5 reveals gaps |
| 7 — Fine-tune Qwen3-8B QLoRA on TACC Lonestar6 | 🔲 Not started |
| 8 — Evaluate fine-tuned model vs baselines | 🔲 Not started |

**Why eval comes before training data (Steps 4–5 inserted):** training data generation should target gaps exposed by the baseline, not guess at them. Running baseline first also establishes the comparison point for Step 8.

---

## What's Been Built (Steps 1–3 Complete)

### MCP Server — 97 Tools
- Full CAD toolset: sketch, extrude, fillet, chamfer, shell, pattern, mirror, thread, etc.
- Full CAM toolset: setup, operation, toolpath, post-process, tool library, feeds/speeds, machining time, toolpath status, operation details, geometry assignment
- All tools tested end-to-end with Qwen3-8B via `fusion_middleware.py`

### Key CAM Bugs Fixed (2026-05-27)
These were blocking reliable CAM execution; all resolved:

**`cam_create_setup` — model assignment**
`setup_input.models` is a `BaseVector` accepting `list[BRepBody]`. All ObjectCollection approaches fail with SWIG type errors. Fix: `setup_input.models = [body]`.

**`cam_create_document_tool` — tool diameter**
Geometry params (`DC`, `LCF`, `OAL`) must be nested under `"geometry"` in the JSON passed to `Tool.createFromJson`. Top-level keys are silently ignored; Fusion defaulted to 6mm for all tools. Fix: nest under `"geometry"`.

**`cam_create_operation` face strategy — tracing**
`useStockContours` defaults to `True` in Fusion for the face strategy, generating a perimeter trace instead of a raster fill. Fix: explicitly set `useStockContours = False` after operation creation.

### Baseline Eval Infrastructure
- **`eval_middleware.py`** — runs any task protocol against any model (Anthropic or OpenAI-compatible backend), logs full JSONL with tool calls, conversation, and auto-objective scores
- **`score_run.py`** — computes post-objective scores from a JSONL run file
- **`compute_iou.py`** — voxel IoU between created STL and ground truth
- **`validate_dataset.py`** — training JSONL validation and cleanup
- **`tasks/face_milling_basic.yaml`** — first eval task protocol (prompt, objective + subjective measures)
- **`tasks/ground_truth/face_milling_basic.stl`** — ground truth mesh

All files live at `C:\Users\Nanja\` and are pushed to https://github.com/git-jameslee/middlewares.

### First Baseline Run — `face_milling_basic` (Claude Sonnet 4.6)
Task: create a 50×50×20mm aluminum block and face mill the top surface with a 12mm flat end mill.

| Metric | Score |
|--------|-------|
| Tool calls | 17/17 (100%) |
| IoU voxel 1mm | 1.0 |
| Dimensions correct | 1.0 |
| Toolpath generated | 1.0 |
| Subjective overall | 7/7 |
| Subjective strategy | 7/7 |

This is the reference bar. Qwen and Gemma 4 will be run on the same task once all protocols are written.

---

## Step 4 — Evaluation Protocol Design

Goal: ~5 task protocol documents. `face_milling_basic` is task 1 and done.

Each protocol specifies:
- A natural language prompt (what the user tells the model)
- Objective measures (computed from the JSONL — IoU, toolpath valid, dimensions, etc.)
- Subjective measures (1–7 scales with anchors, filled by human rater after reviewing the run)
- A ground truth STL or output for objective scoring

**Candidate tasks:**
- `basic_pocket` — rectangular pocket in a solid block (2D pocket/adaptive)
- `chamfer_finish` — block with chamfered edges (chamfer + CAM contour)
- `drilled_holes` — plate with hole pattern (drilling strategy)
- `multi_setup` — part requiring two setups/flipping (setup sequencing)

**Critical discipline for Step 5:** design all protocols first, lock the MCP version, then run all three models without touching the MCP mid-run. Models evaluated on different MCP versions cannot be compared.

---

## Step 6 — Training Data Generation

### Format
ShareGPT JSONL — one example per line, LLaMA-Factory compatible.
```
conversations: [system, human, gpt (tool_call), tool, gpt (answer), ...]
tools: JSON string of all 97 tool schemas
```

### Content — Reasoning-Annotated Traces
**Before:** log `(intent → tool calls → results)`

**After:** log `(intent → reasoning → tool calls → outcome evaluation → correction if needed)`

Claude Sonnet narrates *why* it is choosing each parameter as part of the training target. Example:
```
Intent: Mill a 50mm deep pocket in 6061 aluminum, 12mm end mill

Reasoning:
- Aluminum 6061: high SFM possible, ~300 SFM for carbide
- 12mm end mill → RPM ≈ (300 × 3.82) / 12 ≈ 9500 RPM
- Adaptive clearing preferred for deep pocket — reduces radial load on thin walls
- 0.5D axial, 0.3D radial for roughing; 0.2mm radial stock for finish pass

Tool calls: [set_feeds_speeds, create_adaptive_operation, ...]

Outcome: 4m 32s cycle time, no collision warnings
Evaluation: acceptable — could push feed 10% but conservative for first run
```

The reasoning chain is part of what Qwen trains on — not just the tool calls.

### Infrastructure
- **`training_middleware.py`** (`C:\Users\Nanja\`) — Claude Sonnet drives workflows, saves ShareGPT JSONL to `C:\Users\Nanja\training_data\`. Save/Discard buttons with confirmation dialog.
- Run: `$env:ANTHROPIC_API_KEY = "sk-ant-..."; python C:\Users\Nanja\training_middleware.py`
- After each session: `python C:\Users\Nanja\validate_dataset.py --fix`
- Save: workflow completed end-to-end (self-corrections OK). Discard: failed midway without recovery.
- Cost with prompt caching: ~$20–30 per 200 examples (vs $100–200 without caching)

### Gate
Generation is blocked on Step 5 (baseline). Training data should target gaps the baseline reveals.

---

## Step 7 — Fine-Tuning

Standard SFT on tool call sequences teaches imitation. For expertise:
- **Reasoning traces** are part of the training target (chain-of-thought included)
- **Preference data (DPO)** — pairs of (good setup, bad setup) for the same part, teaching the model to prefer the better one

Stack: LLaMA-Factory + QLoRA on Qwen3-8B, TACC Lonestar6 (A100 40GB, `gpu-a100-small`).
```
rsync training_data/ ls6:~/training_data/
llamafactory-cli train config.yaml
```

---

## Evaluation Methodology

### Baseline (Step 5) — All Three Models on the Same Task Set
- Claude Sonnet 4.6, Qwen3-8B (base), Gemma 4
- Same ~5 task protocols, same locked MCP version
- Scores: tool call success rate, IoU, subjective 1–7 ratings
- Establishes where each model fails and by how much

### Post-Fine-Tune (Step 8) — Same Protocols
- Fine-tuned Qwen3-8B vs Qwen3-8B base vs Qwen3-30B base
- Primary question: does fine-tuned 8B match or beat 30B base?

### Eval Set — Hand-Annotated Gold Standard
- Written by James + machinist collaborator for validation
- Qwen never trains on this — held out entirely
- Represents ground-truth expert CAM reasoning across materials and operation types
- Scoring: correct toolpath strategy, feeds/speeds in acceptable range, difficult geometry handled, operation sequencing logical

---

## Thesis Claim

> Fine-tuned Qwen3-8B produces expert-aligned CAM reasoning on held-out parts, matching a 30B base model's output at 4x smaller scale — and the key ingredient is **reasoning-annotated training data** rather than bare tool call logs.

Two things make this defensible:
1. **A systematic baseline** (Phase 1) that shows where and how much existing models fall short — not just anecdotal failures
2. **A hand-annotated held-out eval set** (not Claude self-evaluation) that measures whether the fine-tuned model generalizes to parts it has never seen

---

## Domain Expertise Plan

**James learns CAM fundamentals** (in parallel with Steps 4–5):
- Feeds/speeds: SFM, chip load, axial/radial depth relationships
- Toolpath strategies: adaptive, contour, pencil, bore — when each applies
- Material families: aluminum vs steel vs titanium constraints
- Resources: Machining Advisor Pro (free), NYC CNC on YouTube, FSWizard

**Machinist collaborator for eval set validation:**
- ME department manufacturing lab at UT Austin (likely candidate)
- Reviews gold eval set and signs off: "this setup is correct / would chatter / wrong strategy"
- Not needed full-time — just for eval set review

---

## Key Files

| File | Location | Purpose |
|------|----------|---------|
| `eval_middleware.py` | `C:\Users\Nanja\` | Run eval tasks against any model |
| `score_run.py` | `C:\Users\Nanja\` | Post-objective scoring from JSONL |
| `compute_iou.py` | `C:\Users\Nanja\` | Voxel IoU scoring |
| `validate_dataset.py` | `C:\Users\Nanja\` | Training JSONL validation + cleanup |
| `fusion_middleware.py` | `C:\Users\Nanja\` | Qwen3-8B testing via vLLM tunnel |
| `training_middleware.py` | `C:\Users\Nanja\` | Claude Sonnet training data generation |
| `command_handler.py` | `addon/server/` | Fusion add-in — deploy with `.\deploy.ps1` |
| `tasks/` | `C:\Users\Nanja\tasks\` | Task protocol YAMLs + ground truth STLs |
| `eval_results/` | `C:\Users\Nanja\eval_results\` | JSONL run logs |
| `training_data/` | `C:\Users\Nanja\training_data\` | ShareGPT JSONL training examples |
| Middlewares repo | github.com/git-jameslee/middlewares | All middleware + eval scripts versioned |

## Notes

- All Fusion API internal units are centimeters. Middleware/eval layer works in mm. MCP converts at the boundary.
- Deploy workflow: edit `addon/server/command_handler.py` → `.\deploy.ps1` → reload add-in in Fusion (Tools → Add-Ins → Fusion360MCP → Stop / Run).
- Qwen3-8B sees all 97 tools via `fusion_middleware.py` (no tool limit). Claude Code only sees 80/97 (128-tool API limit minus built-in tools). `training_middleware.py` uses Anthropic API directly — all 97 tools, no slot competition.
- Valid baseline reference run: `eval_results/face_milling_basic__claude-sonnet-4-6__2026-05-27_15-39-10.jsonl`
