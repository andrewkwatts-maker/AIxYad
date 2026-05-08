"""Seed all 175 work packages from the SDF-native engine refactor plan
into Nova3D's project DB so the autonomous loop can chew through them.

Plan reference: C:\\Users\\Andrew\\.claude\\plans\\make-a-todo-review-list-unified-lamport.md

Encoding strategy:
  - display_id: SP{sprint}-A{agent:02d}-W{wp}        e.g. SP1-A06-W2
  - sprint N tasks depend on sprint N-1 finishing (last agent of prev sprint)
  - within a sprint, agent ordering is parallel except for explicit infra deps
  - complexity sized: small=3, medium=8, large=13. Large only for net-new
    subsystems (compiler, codegen, render-pipeline, AI helper).
  - priority HIGH for foundational sprints (1, 2, 7), MEDIUM elsewhere.
"""
import sqlite3
import sys
from datetime import datetime, timezone

# ── Sprint plan as data ─────────────────────────────────────────────────────
# Each agent = (role_slug, [(wp_letter, title, desc, complexity), ...])
# wp_letter is 'a'/'b'/'c' to match the markdown plan.

SPRINTS = [
    # ── SPRINT 1: Foundations ────────────────────────────────────────────────
    {
        "name": "Foundations",
        "goal": "kill duplication, ship the shared primitives",
        "priority": "HIGH",
        "agents": [
            ("UI-unify", [
                ("a", "Pick UIWidget as canonical UI base class",
                 "Promote engine/ui/UIWidget.hpp to be THE single Widget base. "
                 "Update CMake + headers so it is the only widget type other code references.", 5),
                ("b", "Migrate EditorPanel to inherit UIWidget",
                 "engine/editor/EditorPanel.hpp becomes a composed UIWidget. Each panel "
                 "constructs its sub-widgets via the unified API. No behaviour change yet.", 13),
                ("c", "Delete engine/ui/runtime/HTMLRenderer (CSS path is dead-end)",
                 "Remove HTMLRenderer.hpp/cpp + StyleProperties + BoxModel + FlexProperties "
                 "if they live only there. Update CMakeLists. Delete tests that exercised it.", 8),
            ]),
            ("Theme-tokens", [
                ("a", "Promote EditorTheme to engine/ui/Theme.hpp as global token store",
                 "Move EditorTheme out of engine/editor/. Keep the same Colors/Sizes/Fonts "
                 "structs but expose a singleton accessible from runtime + game code.", 8),
                ("b", "Replace 197 hardcoded color/style sites with theme token references",
                 "Grep for ImVec4(literal) and PushStyleColor across engine/editor/. Replace "
                 "with Theme::Get().colors.X / Sizes.X / Fonts.X.", 13),
                ("c", "Add ThemeVariant for game-side overrides (HUD vs editor look)",
                 "Theme::SetVariant('editor'|'game'|'mod_xyz') swaps token table at runtime. "
                 "Persist user preference. Hot-reload on JSON edit.", 8),
            ]),
            ("Layout-engine", [
                ("a", "Extract Flex from UIWidget into engine/ui/Layout.hpp",
                 "Pure layout calc lib (no rendering). Inputs: tree of nodes with Length/Display/"
                 "FlexProps. Output: solved rect per node. Used by docking + game UIs.", 13),
                ("b", "Express docking as Flex containers with persisted splits",
                 "DockingSystem becomes a thin shell over Layout.hpp. Splits = flex children "
                 "with stored basis. Layout JSON round-trips through Theme persistence.", 8),
                ("c", "Fuzz tests for layout round-trip",
                 "tests/ui/test_layout_fuzz.cpp generates random tree configs, verifies "
                 "deterministic solve + round-trip equality after JSON serialize/deserialize.", 5),
            ]),
            ("Binding", [
                ("a", "Unify DataBinding (Reflect) + UIBinding (JSON) under one API",
                 "engine/ui/Binding.hpp exposes Bind(widget, target, mode). Internally routes to "
                 "Reflect path-resolver for native types and to JSON path for scripted contexts.", 13),
                ("b", "Add path-expression cache",
                 "Repeated resolves of 'player.health' cache the property pointer chain. "
                 "Invalidate on observable mutation events.", 5),
                ("c", "Migration shim for old DataBinding/UIBinding call sites",
                 "Source-level shim: old API redirects to new via inline forwarding. Mark old "
                 "headers [[deprecated]]. Track migration progress in a CI grep gate.", 5),
            ]),
            ("Event-router", [
                ("a", "Single UIEvent definition with capture/bubble/cancel",
                 "engine/ui/UIEvent.hpp consolidates the three flavours. Phase enum: Capturing, "
                 "AtTarget, Bubbling. preventDefault() + stopPropagation(). currentTarget tracking.", 8),
                ("b", "Wire ImGui events through the new router for editor panels",
                 "ImGui input → UIEvent dispatch → widget tree. Editor keyboard shortcuts go "
                 "through the same path.", 8),
                ("c", "Wire HID input through the same router for game widgets",
                 "Existing HID system feeds UIEvent. Touch + gamepad + keyboard all route the "
                 "same way. Tests for event ordering.", 8),
            ]),
            ("Ollama-client", [
                ("a", "engine/llm/OllamaClient.hpp — REST + streaming",
                 "POST /api/generate + /api/chat. Streaming response via callback. Configurable "
                 "model. Error retries with exponential backoff. Health check ping.", 13),
                ("b", "Disk-cache by SHA-256(prompt+params)",
                 "Cache responses under <user>/.nova3d/llm-cache/. Versioned by template hash. "
                 "FIFO eviction at 500 MB cap. Invalidate on prompt-template edit.", 8),
                ("c", "Health-check + provider fallback chain",
                 "If primary Ollama instance unreachable, try fallback URLs. If all down, return "
                 "synthesised offline response (or fail per caller's policy).", 5),
            ]),
            ("Prompt-templates", [
                ("a", "engine/llm/PromptTemplates/ directory with versioned .md templates",
                 "Each template is a markdown file with YAML frontmatter (version, output_schema, "
                 "examples). Lives under engine/llm/PromptTemplates/{material,sdf,ui,scene,behavior}/.", 8),
                ("b", "Template loader with {{var}} substitution + JSON-schema",
                 "Loader reads .md, parses frontmatter, exposes Render(vars) → final prompt. "
                 "Validates LLM output against output_schema after generation.", 8),
                ("c", "Template unit tests",
                 "Each shipped template has a fixture (vars, expected-shape). Tests load + render "
                 "+ schema-validate without invoking LLM.", 5),
            ]),
            ("AI-protocol", [
                ("a", "Define AIRequest/AIResponse JSON schema",
                 "Mirrors Automatica agent_protocol shape: {action, params, reasoning} response. "
                 "Schema lives at engine/llm/protocol.schema.json. Used by every AI helper.", 5),
                ("b", "engine/llm/AIHelper.hpp façade",
                 "AIHelper::Generate(template_name, vars) → typed result. Wraps OllamaClient + "
                 "PromptTemplates + JSON-schema validation + cache lookup.", 8),
                ("c", "Integration test against local Ollama instance",
                 "tests/llm/test_ai_helper.cpp — skipped if no Ollama running. When running, "
                 "round-trips a 'hello world' template and asserts schema-valid response.", 5),
            ]),
            ("UI-test-infra", [
                ("a", "Headless widget-tree test harness",
                 "tests/ui/HeadlessHarness.hpp — instantiate widgets without GL/window. Drive "
                 "events, query computed layout, snapshot tree state. Used by all UI tests.", 8),
                ("b", "Visual snapshot tests via SDF render → PNG diff",
                 "Render a widget tree to a fixed-size image via SDFRenderPipeline (when ready) "
                 "or fallback. Compare to golden PNG. Threshold-based diff for AA tolerance.", 8),
                ("c", "tests/ui/test_theme_tokens.cpp baseline",
                 "Verify all hardcoded colours have been migrated. Verify ThemeVariant swap "
                 "persists. Verify token JSON round-trip.", 5),
            ]),
            ("Docs-site", [
                ("a", "docs/ skeleton with sphinx-equivalent generator",
                 "Pick mkdocs or doxygen+breathe. CI publishes static site. Auto-build from "
                 "code comments + markdown.", 8),
                ("b", "Auto-generate widget reference from NovaProperty metadata",
                 "Each widget's exposed properties show up in docs with type, default, "
                 "description (from reflection). Living reference.", 8),
                ("c", "Onboarding 'first widget' tutorial",
                 "docs/getting-started/first-widget.md — 10-minute walkthrough creating a panel "
                 "in editor and wiring a button to a callback.", 5),
            ]),
        ],
    },
    # ── SPRINT 2: SDF-native rendering core ──────────────────────────────────
    {
        "name": "SDF-native rendering core",
        "goal": "SDF is the primary path; no triangle G-buffer fallback",
        "priority": "HIGH",
        "agents": [
            ("Compute-pipeline", [
                ("a", "engine/graphics/SDFRenderPipeline.hpp — single compute entrypoint",
                 "Consolidate SDFRenderer + RayMarchShader + HybridPathTracer dispatch. One Render() "
                 "call drives the whole frame's compute work.", 13),
                ("b", "Async-queue scheduler using AsyncComputePipeline",
                 "Graphics, compute, transfer queues with timeline semaphores. SDF dispatches go on "
                 "compute queue, copies on transfer, final composite on graphics.", 8),
                ("c", "Per-frame budget tracker (kernel time, brick-cache hit rate)",
                 "Telemetry ring buffer. Editor overlay shows per-system ms + brick-cache stats. "
                 "Optional CSV export.", 5),
            ]),
            ("Analytical-normals", [
                ("a", "Numerical SDF gradient → analytical normal in compute",
                 "Central-difference or tetrahedron-tap gradient sampling. Returned as world-space "
                 "normal at the raymarch hit.", 5),
                ("b", "Curvature node (∇²) for stylised shading",
                 "Second-derivative sampling exposes mean curvature. Used downstream by stylised "
                 "ink / cell-shade nodes.", 8),
                ("c", "Tests vs reference meshes",
                 "Bake reference mesh → SDF → re-extract normals → compare to mesh normals. PSNR > 35dB.", 5),
            ]),
            ("Cone-shadows", [
                ("a", "Cone-traced soft shadows from any light direction",
                 "Single compute kernel. Step along ray to light, accumulating min-distance ratio. "
                 "Shadow value emerges analytically — no shadow map.", 13),
                ("b", "Sun + spot light variants",
                 "Sun: parallel rays. Spot: cone with falloff. Both use same kernel with different "
                 "ray-gen header.", 5),
                ("c", "Bench: 1 light at 1080p < 1.5 ms on RTX-class GPU",
                 "Profile + commit baseline numbers + CI gate.", 5),
            ]),
            ("Distance-AO", [
                ("a", "Analytical distance-field AO from SDF gradient",
                 "Sample SDF along normal direction, accumulate occlusion proportional to (step_dist - "
                 "actual_dist). No screen-space sampling.", 8),
                ("b", "Multi-radius variant for fine + coarse occlusion",
                 "Two passes with different sample radii. Combine to capture both crevices and "
                 "macro shadowing.", 5),
                ("c", "A/B comparison vs existing PostProcess-AO node",
                 "Side-by-side reference frames. Document quality improvement; deprecate the "
                 "old screen-space path.", 5),
            ]),
            ("Radiance-as-primary", [
                ("a", "Promote RadianceCascade from GI add-on to THE indirect-light source",
                 "Remove all 'fallback to ambient' code paths. RadianceCascade is queried for every "
                 "indirect bounce, period.", 8),
                ("b", "Probe placement strategy adapted to SDF skeleton",
                 "Probes cluster near SDF surface (≤ 2m). Sparse probes in empty space. Use brick-"
                 "cache occupancy to drive placement.", 13),
                ("c", "Temporal stability tests under camera motion",
                 "Move camera in a known path, measure flicker between frames. Threshold-based "
                 "regression test.", 5),
            ]),
            ("Brick-cache-materials", [
                ("a", "Add material_id field to brick voxels",
                 "Extend the 8x8x8 voxel struct. Migrate existing assets via re-bake script.", 8),
                ("b", "Material lookup → graph eval at hit point",
                 "Raymarch hit returns (position, normal, material_id). Material graph evaluated "
                 "in same compute kernel — no separate shading pass.", 8),
                ("c", "Cache invalidation on SDF mutation",
                 "Edit a primitive → affected bricks marked dirty → recompute next frame. Avoid "
                 "global rebuild.", 5),
            ]),
            ("Mesh-to-SDF-bake", [
                ("a", "engine/import/MeshToSDF.hpp — generalized winding number bake",
                 "Per-voxel: sample mesh winding number, store signed distance. Robust to "
                 "non-watertight meshes.", 13),
                ("b", "Sparse output (skip empty bricks)",
                 "Only store bricks within ε of surface. Hash-dedup identical bricks via existing "
                 "SDFBrickCache.", 8),
                ("c", "CLI tool + editor button to rebake an asset",
                 "tools/mesh_to_sdf_cli.cpp + 'Rebake to SDF' option in asset browser context menu.", 5),
            ]),
            ("LOD-via-step", [
                ("a", "Raymarch step size scales with screen-space pixel coverage",
                 "Distant pixels take larger steps. Near-camera pixels step finer. No discrete LOD.", 8),
                ("b", "Profile across distance buckets to ensure no banding",
                 "Test scenes at 1m, 10m, 100m, 1km. Visual diff vs uniform-step reference. Tune "
                 "step heuristic.", 5),
                ("c", "Verify no Nanite-style cluster hierarchy lurks",
                 "Audit code for any mesh-cluster / LOD-table abstraction; remove if present.", 3),
            ]),
            ("AI-prompt-to-SDF", [
                ("a", "Prompt template 'describe a shape' → SDF function composition",
                 "PromptTemplates/sdf/shape_from_prose.md — output JSON: {primitives: [...], "
                 "ops: [union|sub|smin]}. Validated against SDF DSL schema.", 8),
                ("b", "Sandboxed evaluator that validates AI output before bake",
                 "Run the LLM-emitted SDF function in a sandbox: bounded box, max ops, no infinite "
                 "recursion. Reject + ask AI to retry if invalid.", 8),
                ("c", "Editor 'AI shape' button next to 'Import mesh'",
                 "Button opens prompt dialog → calls AIHelper → bakes result → adds to scene. "
                 "Streaming preview.", 5),
            ]),
            ("Sdf-bench-tests", [
                ("a", "Throughput benchmark suite",
                 "tests/graphics/bench_sdf.cpp — primitives count, brick-cache hit rate, GI "
                 "convergence iterations. Stored as CSV for trending.", 8),
                ("b", "Visual regression tests via reference frames",
                 "Render fixed scenes from fixed camera → PNG → compare to golden. CI gate.", 5),
                ("c", "Memory ceiling for 1km world",
                 "Test asserts brick-cache + radiance-cascade + textures stay < 4GB on a 1km test "
                 "world. CI gate.", 5),
            ]),
        ],
    },
    # ── SPRINT 3: Material Graph — SDF-aware surface nodes ───────────────────
    {
        "name": "Material Graph - SDF-aware surface nodes",
        "goal": "graph nodes know about SDFs (triplanar without UVs, level-set isolines, distance-driven blending)",
        "priority": "MEDIUM",
        "agents": [
            ("Sdf-aware-base-node", [
                ("a", "engine/materials/SDFAwareNode.hpp base class",
                 "Standard inputs: distance, gradient, position, view_dir, light_dir. Subclasses "
                 "override Compile() to emit shader source.", 8),
                ("b", "Node validation in compiler (rejects invalid wiring)",
                 "Type-check input/output port compatibility. Detect cycles. Reject before codegen.", 5),
                ("c", "Migration of existing nodes",
                 "Rebase existing 170+ ShaderGraph nodes onto SDFAwareNode where it makes sense. "
                 "Document each migrated node.", 13),
            ]),
            ("Pbr-surface-nodes", [
                ("a", "Albedo / Metallic / Roughness / Normal / Emissive nodes",
                 "Each as a SDFAwareNode subclass. Wire to UI inputs. Default values + ranges.", 8),
                ("b", "Energy-conservation checks",
                 "At compile time, validate diffuse + specular ≤ 1. At runtime, debug overlay shows "
                 "energy violation per pixel.", 5),
                ("c", "Reference scene matches PBR validation suite",
                 "tests/materials/pbr_validation.cpp — render Cornell box + sphere array → compare "
                 "to known-good PBR engine output.", 5),
            ]),
            ("Triplanar-nodes", [
                ("a", "Triplanar projection (no UVs needed)",
                 "Sample texture 3 times (X/Y/Z plane projections), blend by abs(normal). Outputs "
                 "RGBA. Critical for SDF surfaces with no canonical UV.", 8),
                ("b", "Stochastic sampling for tiling break-up",
                 "Hash-based offset jitter per virtual tile. Eliminates visible repeats.", 5),
                ("c", "Comparison vs UV mapping in test scenes",
                 "Side-by-side render of a sphere with both methods. Document where each wins.", 3),
            ]),
            ("Procedural-tex-nodes", [
                ("a", "Worley / Perlin / Voronoi / Gabor / Wavelet noise nodes",
                 "Each as SDFAwareNode. Configurable scale, seed, octaves. GPU-friendly impls.", 13),
                ("b", "Domain-warp node (feed noise back as position)",
                 "Take any noise output, use it to displace input position to another node. Enables "
                 "complex flow patterns.", 5),
                ("c", "Bench: all noises < 0.2 ms / 1080p",
                 "Per-node throughput test. CI gate.", 5),
            ]),
            ("Distance-driven-nodes", [
                ("a", "Level-set band (output 1.0 within ±ε of surface)",
                 "Useful for outlines, halo effects, surface highlights. Configurable ε.", 5),
                ("b", "Isoline / contour node",
                 "Output 1.0 at periodic distance intervals. Architectural / topographic effects.", 5),
                ("c", "Distance-blend node (smooth between materials by signed distance)",
                 "Two material inputs + blend distance → smin'd surface material. e.g. snow on "
                 "mountain peaks based on altitude.", 8),
            ]),
            ("Animation-nodes", [
                ("a", "Time / Sin / Saw / Pulse nodes",
                 "Standard animation primitives. Configurable freq + amplitude + phase.", 5),
                ("b", "SDF push (displace surface along gradient)",
                 "Animated bumps / waves on the SDF surface itself. Evaluated at raymarch time.", 8),
                ("c", "Vertex displacement evaluated at raymarch hit",
                 "For triangle-based UI/decals, equivalent vertex shader path. Same logical node "
                 "compiles for both targets.", 5),
            ]),
            ("Codegen-compiler", [
                ("a", "Single MaterialCompiler target — emit compute shader source",
                 "No separate vert/frag. One compute kernel takes graph → returns colour at hit. "
                 "Cache compiled SPIR-V.", 13),
                ("b", "Common-subexpression elim",
                 "If two nodes feed same input to same op, compile once. Reduces shader instruction "
                 "count significantly for complex graphs.", 8),
                ("c", "JIT recompile on graph edit (sub-second)",
                 "Editor edits → compiler invalidates → recompile + replace shader < 1 second. "
                 "Hot reload without scene restart.", 5),
            ]),
            ("AI-prose-to-graph", [
                ("a", "Prompt template 'describe material' → graph JSON",
                 "PromptTemplates/material/graph_from_prose.md. Output: list of nodes + edges. "
                 "Schema-validated.", 8),
                ("b", "Schema-validate generated graph",
                 "Reject orphan nodes, type-mismatched edges, cycles. Return errors to AI for retry "
                 "with hints.", 5),
                ("c", "Editor button 'ask AI for material'",
                 "Open prompt → generate → preview on sphere → accept/reject. Iteration UI.", 5),
            ]),
            ("Graph-editor-ui", [
                ("a", "Migrate ShaderGraphEditor to unified Widget",
                 "Editor canvas + node + edge widgets all UIWidget-based. Themed via tokens.", 13),
                ("b", "Live preview sphere using SDFRenderPipeline",
                 "Editor preview panel renders an actual SDF sphere with the in-progress graph.", 8),
                ("c", "Undo/redo via existing CommandHistory",
                 "Each node move/edit/wire/unwire is a command. Hooks into the existing editor "
                 "undo system.", 5),
            ]),
            ("Codegen-tests", [
                ("a", "Roundtrip: graph → JSON → graph → identical compiled shader",
                 "Strict equality of resulting SPIR-V (after canonicalisation).", 5),
                ("b", "Numerical equivalence between hand-authored and graph-compiled shaders",
                 "Pick 5 reference shaders. Express as graphs. Render both → PSNR > 50dB.", 5),
                ("c", "Cross-compile target validation (Vulkan SPIR-V + Metal MSL)",
                 "Same graph compiles for both backends. Output renders identical (within FP tolerance).", 8),
            ]),
        ],
    },
    # ── SPRINT 4: Lighting & post-process as graph nodes ─────────────────────
    {
        "name": "Lighting & post-process as graph nodes",
        "goal": "lighting + post-process are nodes that fold into the SDF compute kernel",
        "priority": "MEDIUM",
        "agents": [
            ("Light-source-nodes", [
                ("a", "Point / Spot / Sun / Area light nodes with falloff curves",
                 "Each emits light intensity at a sample point. Falloff configurable.", 8),
                ("b", "IES profile import",
                 "Standard IES file → light intensity LUT. Real-world fixture data.", 5),
                ("c", "Tests for energy conservation under multi-light",
                 "Multiple lights composited should not exceed sum of individual contributions.", 5),
            ]),
            ("Cone-shadow-node", [
                ("a", "Wraps Sprint 2 cone-shadow primitive as a graph-node sampler",
                 "Material graph node that, given light_dir, returns shadow factor [0,1].", 5),
                ("b", "Shadow softness from light radius (analytical)",
                 "Wider light radius → softer cone → softer shadow penumbra. No PCF needed.", 5),
                ("c", "Bench: 4 lights at 1080p stays under 6 ms",
                 "Per-light cost amortised by sharing march steps. CI gate.", 5),
            ]),
            ("Radiance-probe-node", [
                ("a", "Lookup node returning indirect light at a position",
                 "Given world position, return diffuse + specular indirect. Reads RadianceCascade.", 8),
                ("b", "Cascade-level selection by distance to camera",
                 "Near pixels read finer cascade. Far pixels read coarser. No banding at boundaries.", 5),
                ("c", "Roughness-aware filter taps",
                 "Specular lookup samples wider for rougher surfaces (glossy reflection blur).", 5),
            ]),
            ("Df-AO-node", [
                ("a", "Distance-field AO graph node",
                 "Wraps Sprint 2 DF-AO primitive. Output: occlusion factor [0,1].", 5),
                ("b", "Configurable kernel radius + bias",
                 "Artist-facing controls for AO intensity + falloff.", 3),
                ("c", "Decoupled from shading rate",
                 "AO can run at half-res while shading at full-res. Upsample with edge-aware filter.", 5),
            ]),
            ("Tonemap-node", [
                ("a", "ACES + Reinhard + custom curve LUT",
                 "Three modes selectable per-graph. ACES default.", 5),
                ("b", "Exposure compensation hook",
                 "Auto-exposure based on luminance histogram. Optional per-scene overrides.", 5),
                ("c", "Reference image compares",
                 "Render reference HDR scene → tonemap → compare to known good output.", 3),
            ]),
            ("Bloom-node", [
                ("a", "Mip-chain physically-motivated bloom (no hardcoded radii)",
                 "Bloom radius derived from camera FOV + lens parameters, not hand-tuned.", 8),
                ("b", "Distance-aware bloom radius (closer surfaces bloom less)",
                 "Use depth from raymarch hit to scale bloom radius. Naturally cinematic.", 5),
                ("c", "Bench: full-screen bloom < 0.5 ms",
                 "1080p baseline. CI gate.", 3),
            ]),
            ("Color-grade-node", [
                ("a", "LUT-driven 3D-LUT colour grading",
                 ".cube LUT import. Trilinear interpolation in compute.", 5),
                ("b", "Per-channel curves",
                 "RGB or HSV curve editor in graph node UI.", 5),
                ("c", "Editor LUT-import flow + preview",
                 "Drag-drop .cube file → preview on current scene → apply.", 5),
            ]),
            ("Volumetric-fog-node", [
                ("a", "SDF-density-field fog (use the SDF itself as fog occluder)",
                 "Walk the camera ray through SDF density volume. Light scatters through fog "
                 "occluded by SDF surfaces.", 13),
                ("b", "Henyey-Greenstein phase function",
                 "Anisotropic forward / backward scatter. Configurable g parameter.", 5),
                ("c", "Bench: 1km draw distance fog < 1 ms",
                 "Optimised raymarch. CI gate.", 5),
            ]),
            ("AI-lighting-prompt", [
                ("a", "Prompt template 'describe a lighting mood'",
                 "PromptTemplates/material/lighting_mood.md. Output: light placements + tonemap "
                 "params + colour-grade hints.", 8),
                ("b", "Preset library seeded by AI",
                 "'Dramatic sunset', 'noir', 'soft daylight', 'dawn' as starter presets generated "
                 "via AI then refined by hand.", 5),
                ("c", "Editor button 'AI light my scene'",
                 "One-click lighting from the editor. Generates lights + tonemap + grade nodes.", 5),
            ]),
            ("Lighting-tests", [
                ("a", "Visual regression suite (golden frames per lighting setup)",
                 "Each preset has a reference frame. CI compares with PSNR > 40dB.", 5),
                ("b", "Performance budget gate per node",
                 "Per-node max ms enforced in CI bench.", 5),
                ("c", "Energy-conservation test across compositions",
                 "Composite 5 lights + GI + post-process → total radiance ≤ sum of individual.", 5),
            ]),
        ],
    },
    # ── SPRINT 5: Editor migration ───────────────────────────────────────────
    {
        "name": "Editor migration to standardised UI",
        "goal": "every editor panel renders through UIWidget + Theme + Layout",
        "priority": "MEDIUM",
        "agents": [
            ("Property-panel", [
                ("a", "Migrate PropertiesPanel to compose Widgets driven by NovaProperty",
                 "Each reflected property → typed widget (slider, combo, vec3 input, colour picker).", 13),
                ("b", "Custom inspector hooks per type",
                 "Register IInspector<MyType> to override default widget for specific types.", 8),
                ("c", "Tests for round-trip: select object → edit → save → reload",
                 "Headless test: load scene, mutate property via inspector, save, reload, verify.", 5),
            ]),
            ("Scene-outliner", [
                ("a", "Migrate to unified Widget tree-view primitive",
                 "Single TreeView widget reused by outliner + asset browser + console history.", 8),
                ("b", "Drag/drop reparenting via shared event router",
                 "Drop event on outliner node → reparent + emit transform-update.", 5),
                ("c", "Search box uses shared input widget",
                 "Same fuzzy-search widget used in command palette. Themed.", 3),
            ]),
            ("Material-graph-editor", [
                ("a", "Drop legacy editor; rebuild on Widget canvas",
                 "Was rebuilt in Sprint 3 WP-9. This sprint integrates with full editor (dock, "
                 "file menu, etc.).", 8),
                ("b", "Wire AI-prose-to-graph button (Sprint 3 WP-8)",
                 "Connect the AIHelper call into the editor menu + toolbar.", 3),
                ("c", "Graph preview sphere uses SDFRenderPipeline",
                 "Real SDF preview, not a fake gradient.", 5),
            ]),
            ("Sdf-asset-editor", [
                ("a", "Migrate SDFAssetEditor to Widget",
                 "Same widget primitives as material graph editor.", 8),
                ("b", "AI-shape-from-prose button (Sprint 2 WP-9)",
                 "Wire into editor toolbar.", 3),
                ("c", "Brick-cache visualiser overlay",
                 "Toggleable overlay shows brick boundaries + occupancy. Debug aid.", 5),
            ]),
            ("AI-assistant-panel", [
                ("a", "Wire AIAssistantPanel through AIHelper",
                 "Replace Sprint 1's stub. Real Ollama calls. Streaming responses.", 8),
                ("b", "Streaming response rendering",
                 "Token-by-token append in the chat UI. Animated dots while waiting.", 5),
                ("c", "Cancel + retry controls",
                 "Cancel button stops streaming. Retry sends same prompt with adjusted temp.", 3),
            ]),
            ("AI-feedback-panel", [
                ("a", "Quality metrics from AI on user's selected asset",
                 "AI describes detected issues + suggestions per asset type.", 8),
                ("b", "Issues + suggestions list, click-to-fix actions",
                 "Each suggestion has an Apply button. Diff preview before commit.", 8),
                ("c", "Rate-limit + diff display before applying suggestions",
                 "Throttle AI calls. Show full diff on Apply with Confirm/Reject.", 5),
            ]),
            ("Console-panel", [
                ("a", "Migrate to Widget",
                 "ConsolePanel becomes UIWidget composition. Themed.", 5),
                ("b", "AI-suggested commands as autocomplete",
                 "Type a partial command → AI suggests completion based on engine state.", 8),
                ("c", "Rich history with collapse-similar grouping",
                 "Repeated 'set fov 90' calls collapse with a count. Performance for 10K-line histories.", 5),
            ]),
            ("Asset-browser", [
                ("a", "Migrate to Widget",
                 "AssetBrowser uses unified GridView + TreeView widgets.", 8),
                ("b", "AI-generated tags per asset (offline batch)",
                 "On asset import, AI generates 3-5 descriptive tags. Stored in metadata.", 8),
                ("c", "Tag-based filter UI",
                 "Multi-select tag filter + boolean combinators. Themed chips.", 5),
            ]),
            ("Onboarding", [
                ("a", "First-run tour built on the unified Widget system",
                 "Detects first launch → guided tour of viewport, scene tree, properties, console.", 8),
                ("b", "Per-screen 'what is this' overlays",
                 "? button on each panel toggles annotation overlay.", 5),
                ("c", "Telemetry-friendly skip + complete events",
                 "Anonymous metrics: skip rate, completion rate, drop-off step. Privacy-respecting.", 3),
            ]),
            ("Editor-tests", [
                ("a", "Smoke test for every migrated panel",
                 "Headless: open panel, verify it renders without crash.", 5),
                ("b", "Visual regression at 1440p baseline",
                 "Reference golden image per panel. Diff with tolerance.", 5),
                ("c", "Cross-resolution (4K + 720p) layout robustness",
                 "Panels remain readable + functional at extreme resolutions.", 5),
            ]),
        ],
    },
    # ── SPRINT 6: Game UI templates ──────────────────────────────────────────
    {
        "name": "Game UI templates + AI-assisted authoring",
        "goal": "prove the unified UI system can build real game UIs; ship template HUDs that modders can fork",
        "priority": "MEDIUM",
        "agents": [
            ("Rts-hud-template", [
                ("a", "templates/ui/rts_hud/ — minimap, resource bar, command card",
                 "Full HUD layout. Each component a UIWidget. Driven by Binding from game state.", 13),
                ("b", "All driven from in-game state via Binding",
                 "No mutable copies. UI reflects game state through reactive bindings.", 8),
                ("c", "Reference RTS scene shows it live",
                 "examples/rts_demo/ — runnable scene with the HUD active.", 5),
            ]),
            ("Inventory-template", [
                ("a", "Slot grid + drag/drop + tooltip widget",
                 "Reusable inventory primitive. Configurable slot count + size.", 13),
                ("b", "Items defined via NovaProperty (designers add items without code)",
                 "Item definitions in JSON. Reflection drives editor inspector.", 8),
                ("c", "Stack/split/merge interactions",
                 "Standard RPG inventory mechanics. Modder-overridable rules.", 5),
            ]),
            ("Dialog-quest-template", [
                ("a", "Branching dialog runner driven by AIBehavior tree output",
                 "Dialog tree → behaviour node → on choice → next node. Reuses AIBehavior infra.", 13),
                ("b", "Quest-log widget",
                 "Active quests, sub-objectives, hand-in indicators.", 8),
                ("c", "Localisation hook",
                 "All strings go through Localisation::Get(key). String tables per language.", 5),
            ]),
            ("Settings-menu", [
                ("a", "Tabs: Graphics / Audio / Controls / Accessibility",
                 "Standard settings tabs. Theme-tokens applied throughout.", 8),
                ("b", "Live preview for graphics changes (re-render via SDFRenderPipeline)",
                 "Slide quality slider → preview updates in real time.", 8),
                ("c", "Persistence to per-user JSON",
                 "Settings save under <user>/.nova3d/user.json. Survive engine update.", 3),
            ]),
            ("Pause-main-menu", [
                ("a", "Pause overlay + main menu shell",
                 "Both reuse same widget tree, different theme variant.", 8),
                ("b", "Theme switcher demoing ThemeVariant",
                 "Game ↔ editor look toggle. Showcases token system.", 5),
                ("c", "Steam-Deck-friendly gamepad nav out of the box",
                 "Focus traversal + face-button mapping pre-wired.", 5),
            ]),
            ("Ai-prose-to-ui", [
                ("a", "Prompt template 'describe a HUD' → widget tree JSON",
                 "PromptTemplates/ui/hud_from_prose.md. Output schema-validated.", 8),
                ("b", "Schema validation: layout sane, no overflow at 1080p / 4K / 720p",
                 "Auto-test layouts at three resolutions. Reject if any overflow.", 8),
                ("c", "'AI build my HUD' editor button",
                 "Editor toolbar button. Streams generation + preview.", 5),
            ]),
            ("Ui-animation-graph", [
                ("a", "Animation node graph (intro, hover, transition)",
                 "Reuses material-graph compiler infra. Animation = graph evaluated over time.", 13),
                ("b", "Reuses material graph compiler infra for evaluation",
                 "Same codegen, different output target (animation curves vs shader colours).", 8),
                ("c", "Tests for deterministic playback",
                 "Same seed → same animation. Required for replay correctness.", 5),
            ]),
            ("Input-binding-polish", [
                ("a", "Unified input map across keyboard / gamepad / touch",
                 "InputMap abstraction. Each action mapped per device. Persisted.", 8),
                ("b", "Rebindable from Settings menu",
                 "User-facing rebinding UI. Conflict detection.", 5),
                ("c", "AI-suggested bindings for new actions",
                 "When developer adds an action, AI suggests sensible default key + button.", 5),
            ]),
            ("Mobile-variants", [
                ("a", "Touch-optimised variants of each template",
                 "Larger hit areas, vertical-first layouts, virtual sticks.", 8),
                ("b", "Safe-area handling",
                 "Inset for notches, gesture bars. Per-platform configuration.", 5),
                ("c", "Baseline tested on emulator + real device",
                 "Android emulator + a physical device pass smoke tests.", 5),
            ]),
            ("Game-ui-docs", [
                ("a", "'Author a HUD in 30 minutes' tutorial",
                 "docs/tutorials/author-a-hud.md — step-by-step. Screenshots.", 8),
                ("b", "Reference for every shipped template",
                 "Per-template docs page. Live-edit playground if possible.", 5),
                ("c", "Modder cookbook (10 recipes)",
                 "Common modder patterns — custom HUD overlay, replace inventory, add minimap.", 8),
            ]),
        ],
    },
    # ── SPRINT 7: Hardening + perf + docs + handoff ──────────────────────────
    {
        "name": "Hardening, performance, documentation, handoff",
        "goal": "lock the surface area, profile every hot path, document everything, hand off to game teams",
        "priority": "HIGH",
        "agents": [
            ("Sdf-perf", [
                ("a", "Frame-time budget audit per scene tier",
                 "Document target ms per scene complexity tier (simple, complex, stress).", 8),
                ("b", "Optimise top-3 hottest compute kernels",
                 "Profile guides choices. Vectorisation, occupancy tuning, memory layout.", 13),
                ("c", "Document GPU memory headroom per resolution",
                 "Table: 1080p / 1440p / 4K → headroom on RTX 3060 / 3080 / 4090.", 5),
            ]),
            ("Material-perf", [
                ("a", "Compile-time benchmark for graph → shader",
                 "Per-graph compilation time. Target: 200-node graph compiles < 500ms.", 8),
                ("b", "Runtime benchmark per node",
                 "ms cost per node type. CI gate per node.", 5),
                ("c", "Caching strategy: pre-compile common graphs at engine startup",
                 "Common templates compile during loading screen, not first frame.", 5),
            ]),
            ("Ui-perf", [
                ("a", "Layout-pass cost audit",
                 "Profile layout solve at 100, 1K, 10K widgets. Document scaling.", 5),
                ("b", "Widget pooling for high-churn lists (chat, log feeds)",
                 "Recycle widget instances instead of allocating new ones per row.", 8),
                ("c", "Off-thread layout calculation",
                 "Layout solve runs on JobSystem worker. Main thread only does upload.", 8),
            ]),
            ("Memory-audit", [
                ("a", "Per-subsystem memory budget table",
                 "Track allocations per subsystem. Document targets + actuals.", 5),
                ("b", "Leak hunt (Valgrind + Address Sanitizer runs)",
                 "Run full scene under sanitizers. Fix all leaks + UB.", 13),
                ("c", "Streaming budget for 1km worlds",
                 "Asset streaming policy. Eviction strategy. Document peak / steady-state.", 5),
            ]),
            ("Ci-cd", [
                ("a", "GitHub Actions / GitLab pipeline",
                 "Build + unit + integration + GPU-required tests. Matrix across MSVC/Clang/GCC.", 8),
                ("b", "Visual regression artifacts published to docs site",
                 "Each PR's render diffs uploaded as artifact. Reviewer can inspect.", 5),
                ("c", "Nightly benchmark dashboard",
                 "Trends per metric over time. Alert on regression > 5%.", 8),
            ]),
            ("Docs-architecture", [
                ("a", "Living architecture diagram (mermaid + text)",
                 "Top-level subsystem diagram + how data flows through a frame.", 8),
                ("b", "'How a frame is rendered' walk-through",
                 "Step-by-step from input event to pixel.", 5),
                ("c", "Decision log for SDF-native bets",
                 "ADR-style records: why no Lumen, why no Nanite, why radiance cascade.", 5),
            ]),
            ("Docs-material-graph", [
                ("a", "Node reference (auto-generated from registry)",
                 "Every shipped node has docs entry: inputs, outputs, math, examples.", 8),
                ("b", "'Author a material' cookbook — 12 recipes",
                 "Wood, metal, fabric, glass, water, ice, stone, leaves, skin, hair, smoke, fire.", 13),
                ("c", "AI-helper-prompt examples",
                 "Sample prose → graph outputs for the cookbook materials.", 5),
            ]),
            ("Docs-ui-authoring", [
                ("a", "Widget reference (auto-generated)",
                 "Each widget's properties + events. Reflection-driven.", 5),
                ("b", "Theme + token reference",
                 "All tokens with example colours / sizes.", 3),
                ("c", "Modder portal",
                 "Standalone docs site for modders. No engine internals exposed.", 8),
            ]),
            ("Docs-ai-helpers", [
                ("a", "Ollama setup guide",
                 "Install, configure, recommended models, troubleshooting.", 5),
                ("b", "Prompt-template authoring guide",
                 "How to write a new template: frontmatter, schema, examples.", 8),
                ("c", "Fall-back behaviour when LLM unavailable",
                 "Document what each AI feature does without Ollama running.", 3),
            ]),
            ("Migration-examples", [
                ("a", "Sample game ported to the unified UI",
                 "Take BuildingUI demo → migrate to new system → before/after.", 13),
                ("b", "'Old → new' cheat sheet for early adopters",
                 "Side-by-side mapping of old API → new API.", 5),
                ("c", "Game-team office-hours kickoff",
                 "Schedule 1-hour training. Record + post.", 3),
            ]),
        ],
    },
]


def main():
    db_path = r"H:\Github\Nova3D\.automatica\project.db"
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    have_complexity = "complexity" in cols
    have_priority = "priority" in cols
    have_lamport = "lamport" in cols
    have_depends = "depends_lamport" in cols
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Lamport allocation: sprint N starts at lamport N*1000
    # Each agent's WPs share the same lamport (same sprint, parallel).
    # Sprint N+1 tasks have depends_lamport = "N000" (whole prior sprint).
    inserted = 0
    skipped = 0
    for s_idx, sprint in enumerate(SPRINTS, start=1):
        sprint_lamport = s_idx * 1000
        depends = "" if s_idx == 1 else str((s_idx - 1) * 1000)
        for a_idx, (role, wps) in enumerate(sprint["agents"], start=1):
            for wp_letter, title, desc, complexity in wps:
                wp_num = ord(wp_letter) - ord("a") + 1
                did = f"SP{s_idx}-A{a_idx:02d}-W{wp_num}"
                if conn.execute("SELECT 1 FROM tasks WHERE display_id=?", (did,)).fetchone():
                    skipped += 1
                    continue
                full_title = f"[S{s_idx} {role}] {title}"
                full_desc = (
                    f"Sprint {s_idx} ({sprint['name']}) — Agent {a_idx} ({role}) — Work package {wp_letter}.\n\n"
                    f"Sprint goal: {sprint['goal']}\n\n"
                    f"{desc}\n\n"
                    f"Refer to plan: C:\\Users\\Andrew\\.claude\\plans\\make-a-todo-review-list-unified-lamport.md"
                )
                fields = ["display_id", "title", "description", "status", "created_at", "updated_at"]
                values = [did, full_title[:200], full_desc, "backlog", now, now]
                if have_complexity:
                    fields.append("complexity"); values.append(complexity)
                if have_priority:
                    fields.append("priority"); values.append(sprint["priority"])
                if have_lamport:
                    fields.append("lamport"); values.append(sprint_lamport)
                if have_depends:
                    fields.append("depends_lamport"); values.append(depends)
                placeholders = ",".join(["?"] * len(values))
                conn.execute(
                    f"INSERT INTO tasks ({','.join(fields)}) VALUES ({placeholders})",
                    values,
                )
                inserted += 1
    conn.commit()

    # Summary
    print(f"Inserted {inserted} work packages (skipped {skipped} duplicates)")
    print()
    print("=== Per-sprint summary ===")
    for s_idx, sprint in enumerate(SPRINTS, start=1):
        n_agents = len(sprint["agents"])
        n_wps = sum(len(a[1]) for a in sprint["agents"])
        complexity_sum = sum(c for a in sprint["agents"] for _, _, _, c in a[1])
        print(f"  Sprint {s_idx} ({sprint['name'][:40]:40}) "
              f"{n_agents:2} agents  {n_wps:3} WPs  {complexity_sum:4} pts")
    print()
    print("=== Backlog state ===")
    for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status"):
        print(f"  {r[0]:14s} {r[1]:4}")
    conn.close()


if __name__ == "__main__":
    main()
