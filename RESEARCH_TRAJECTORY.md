# Research Trajectory Update — Fusion360 CAM AI
**Date:** 2026-05-24  
**Author:** James Lee  
**Contact:** james.lee0350@utexas.edu

---

## Original Goal (Unchanged)

Fine-tune Qwen3-8B to match or outperform Qwen3-30B on Fusion 360 CAM tool-use tasks, using a structured MCP-based automation pipeline.

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

## What Changes in the Pipeline

### Step 4 (Training Data Generation) — Critical Change

**Before:** log `(intent → tool calls → results)`

**After:** log `(intent → reasoning → tool calls → outcome evaluation → correction if needed)`

Claude Sonnet must narrate *why* it is choosing each parameter as part of the training target. Example:

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

### Step 3 (Tool Suite) — Needs Outcome Signals

Tools must return **quality signals**, not just confirmation:
- Estimated cycle time
- Tool load / overload warnings
- Surface finish prediction
- Collision and gouge detection result

Without these, the model cannot distinguish a good CAM setup from a bad one.

### Step 5 (Fine-tuning) — Different Training Objective

Standard SFT on tool call sequences teaches imitation. For expertise:
- **Reasoning traces** are part of the training target (chain-of-thought included)
- **Preference data (DPO)** — pairs of (good setup, bad setup) for the same part, teaching the model to prefer the better one

---

## Evaluation Methodology (Hybrid Approach)

### Training Set — Auto-Generated at Scale
- Claude Sonnet drives full CAM workflows with structured reasoning narration
- Target: 1000+ reasoning-annotated traces
- Used for fine-tuning Qwen3-8B

### Eval Set — Hand-Annotated Gold Standard (~100 examples)
- Written by James (+ machinist collaborator for validation)
- Qwen **never trains on this** — held out entirely
- Represents ground-truth expert CAM reasoning across materials and operation types
- Used to evaluate whether fine-tuned Qwen generalizes correctly

### Evaluation Question
Given a part Qwen has never seen, does its reasoning match what a domain expert would say?

**Scoring dimensions:**
- Did it select the correct toolpath strategy?
- Were feeds/speeds within acceptable range for the material?
- Did it identify and handle difficult geometry (thin walls, deep pockets)?
- Was operation sequencing logical?

---

## Thesis Claim (Revised)

> Fine-tuned Qwen3-8B produces expert-aligned CAM reasoning on held-out parts, matching a 30B base model's output at 4x smaller scale — and the key ingredient is **reasoning-annotated training data** rather than bare tool call logs.

The hand-annotated eval set (not Claude self-evaluation) is what makes this claim defensible.

---

## Domain Expertise Plan

Qwen cannot become a CAM expert if the training data is wrong. Two parallel tracks:

**James learns CAM fundamentals** (2–4 weeks, in parallel with Steps 1–3):
- Feeds/speeds: SFM, chip load, axial/radial depth relationships
- Toolpath strategies: adaptive, contour, pencil, bore — when each applies
- Material families: aluminum vs steel vs titanium constraints
- Resources: Machining Advisor Pro (free), NYC CNC on YouTube, FSWizard

**Machinist collaborator for eval set validation:**
- ME department manufacturing lab at UT Austin (likely candidate)
- Reviews gold eval set and signs off: "this setup is correct / would chatter / wrong strategy"
- Not needed full-time — just for eval set review

---

## Current Status

| Step | Status |
|------|--------|
| Step 1 — Fix faust bugs | Done |
| Step 2 — Expand CAM coverage | Done |
| Step 3 — Build inspection + write CAM tool suite | In progress (blocked on geometry selection for toolpath generation) |
| Step 4 — Generate training data | Not started |
| Step 5 — Fine-tune Qwen3-8B | Not started |
| Step 6 — Evaluate vs Qwen3-30B | Not started |

**Immediate blocker:** geometry selection for toolpath generation (Step 3). Resolve this before training data generation is feasible.

**Parallel action:** James begins CAM domain learning now so reasoning annotations are credible by Step 4.


For step 3(Tool Suite)

Already exists (no work needed):

Cycle time — cam_get_machining_time
Toolpath validity / error status — cam_get_toolpath_status
Feeds/speeds read/write — cam_get_operation_details, cam_update_operation_parameters

Buildable — I can implement these:

cam_get_body_faces — query face indices, types, and areas on a body so the user isn't guessing blindly when calling cam_set_operation_geometry. This directly unblocks your current geometry selection blocker.

Tool load warnings — derivable from chip load calculation (feeds + tool geometry + material), no new Fusion API needed

Surface finish prediction — theoretical Ra estimate from feedrate + tool nose radius (standard formula)

Not buildable without significant Fusion API work:

Dynamic collision detection (tool vs stock during cutting motion) — Fusion doesn't expose this via its API in a straightforward way
Real tool deflection analysis — requires FEA, not available in Fusion API
My recommendation: start with cam_get_body_faces since it unblocks Step 3 entirely, then add the chip load and surface finish tools since those directly feed training data quality. Skip collision detection for now.

Want me to build those three? I'd touch:

addon/server/command_handler.py — add handlers
src/fusion360_mcp/tools.py — add tool definitions
src/fusion360_mcp/mock.py — add mock handlers
tests/ — add tests