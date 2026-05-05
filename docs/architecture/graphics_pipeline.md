# Graphics Pipeline Contract — UE5-Parity

**ARCH-6 — Architecture Document**
**Status:** Design (authoritative contract)
**Owner:** Rendering Subsystem
**Related:** ARCH-1..5 (engine, scene, asset, ECS, GPU resource model)

This document defines the **contract** for the Automatica renderer. It is intended to
match the *capability surface* of Unreal Engine 5's rendering pipeline (Lumen / Nanite /
Virtual Shadow Maps / TSR-class TAA), expressed in terms of stages, data flow, and
parameter budgets that downstream code must honor.

It is normative for ARCH-6 acceptance. Implementation tickets (ARCH-7+) reference the
stage names, buffer names, and parameter values defined here verbatim.

---

## 1. Overview

The renderer is a **deferred, clustered, GPU-driven** pipeline. Per frame, a
single CPU thread builds a render graph, all draws are issued via indirect
buffers populated on the GPU, and lighting is resolved against a world-space
SDF + radiance probe field.

### 1.1 High-level frame

```
[CPU]  Scene extract → View setup → Render-graph compile → Submit
[GPU]  Cull (compute) → Depth Pre-pass → GBuffer → Lighting → SDF GI →
       Shadows → Transparency → Post-process → Present
```

### 1.2 Design goals

- **Determinism** — same inputs → same pixels (modulo TAA history).
- **GPU-driven** — CPU does not iterate per-object during draw submission.
- **Decoupled stages** — each stage declares inputs/outputs as named resources.
- **Scalable LOD** — geometry density bounded by screen-space cluster count.

---

## 2. Render Graph Stages

The frame is a linear DAG of the following stages, in order. Each stage has a
fixed contract for inputs (read), outputs (write), and side-effects.

| # | Stage             | Reads                          | Writes                          |
|---|-------------------|--------------------------------|---------------------------------|
| 1 | Depth Pre-pass    | Visible cluster list           | `HZB`, `SceneDepth`             |
| 2 | GBuffer           | `SceneDepth`, cluster list     | `GBufferA..D`, `Velocity`       |
| 3 | Lighting          | GBuffer, light list, shadows   | `SceneColor` (HDR)              |
| 4 | SDF GI            | GBuffer, SDF, probe field      | `IndirectDiffuse`, `IndirectSpec` |
| 5 | Shadows           | Light list, VSM page table     | `VSM_Physical`, `ShadowMask`    |
| 6 | Transparency      | `SceneColor`, GBuffer.depth    | `SceneColor` (composited)       |
| 7 | Post-process      | `SceneColor`, `Velocity`, LUT  | `BackBuffer`                    |

### 2.1 Depth Pre-pass

- Renders depth-only for opaque clusters surviving cull.
- Produces a 2-mip **HZB** (Hierarchical-Z Buffer) used by the next frame's
  occlusion cull pass.
- Format: `D32_FLOAT`. Reverse-Z is mandatory (near=1, far=0).

### 2.2 GBuffer

Layout (5 RTs + depth):

| RT       | Format            | Channels                            |
|----------|-------------------|-------------------------------------|
| GBufferA | `R10G10B10A2_UNORM` | World-space normal (oct), AO        |
| GBufferB | `R8G8B8A8_UNORM`  | Metallic, Specular, Roughness, Mask |
| GBufferC | `R8G8B8A8_SRGB`   | Base color (RGB), shading model (A) |
| GBufferD | `R8G8B8A8_UNORM`  | Custom data (subsurface, anisotropy)|
| Velocity | `R16G16_FLOAT`    | Screen-space motion vector          |

### 2.3 Lighting

- Clustered forward+ resolve over `GBufferA..D`.
- Cluster grid: `16 × 8 × 24` froxels, depth-sliced exponentially.
- Direct + indirect terms accumulate into HDR `R16G16B16A16_FLOAT` `SceneColor`.

### 2.4 SDF GI — see §4

### 2.5 Shadows — see §3

### 2.6 Transparency

- Sorted back-to-front per draw call.
- Reads opaque `SceneColor` + depth; writes premultiplied alpha into `SceneColor`.
- Optional order-independent path (weighted blended) gated by material flag.

### 2.7 Post-process — see §6

---

## 3. Shadow System — Virtual Shadow Maps

The shadow system implements **Virtual Shadow Maps (VSM)** following the
UE5 model.

| Parameter            | Value                                  |
|----------------------|----------------------------------------|
| Virtual resolution   | **16 384 × 16 384** per light          |
| Page size            | 128 × 128 texels                       |
| Cascade count        | **8** (directional lights)             |
| Physical pool        | 4 096 × 4 096 atlas (`D16_UNORM`)      |
| Page table           | Sparse, 128² entries, 64-bit each      |
| Cascade distribution | Logarithmic, λ = 0.85 (PSSM blend)     |

### 3.1 Pipeline

1. **Mark pages** — for each visible light, compute which 128² virtual pages
   are touched by visible geometry.
2. **Allocate** — assign physical atlas tiles to marked pages; LRU evict.
3. **Render** — each allocated page rendered with a tight projection.
4. **Sample** — fragment shader maps world-space → virtual page → physical
   tile via the page table.

### 3.2 Cascades

Directional lights produce 8 cascades. Cascade `i` covers depth range:

```
near_i = lerp(linear_near_i, log_near_i, 0.85)
```

Cascades are independently virtual-paged; pages shared between cascades
are reference-counted in the physical pool.

---

## 4. Global Illumination — RadianceCascade SDF Probes

The GI system uses world-space **signed distance fields (SDF)** queried by
a hierarchical **radiance cascade** of probes.

| Parameter             | Value                                     |
|-----------------------|-------------------------------------------|
| Cascade levels        | **4**                                     |
| Probes per cell       | **16** (4×4 angular partition per level)  |
| Cell size (level 0)   | 0.5 m                                     |
| Cell size (level n)   | `0.5 m · 2ⁿ`                              |
| Ray budget per probe  | 64 (level 0) → 8 (level 3)                |
| Storage per probe     | RGB irradiance (R11G11B10F) + 8-bit depth |
| Update budget         | 1/8 of probes per frame, round-robin      |

### 4.1 SDF representation

- Mesh SDFs: per-asset, bricked, 4-bit distance + 4-bit gradient, 16³ bricks.
- Global SDF: composited from instance SDFs into a 256³ volume around the camera.

### 4.2 Cascade query

A surface point queries its enclosing cell at every cascade level, then
trilinearly interpolates the 16 probes per cell. Higher cascades contribute
only the residual (level *n+1* − level *n*) to avoid double-counting.

### 4.3 Outputs

- `IndirectDiffuse` — `R11G11B10F`, screen resolution.
- `IndirectSpec` — `R11G11B10F`, screen resolution, denoised via
  spatiotemporal variance-guided filtering.

---

## 5. Temporal Anti-Aliasing

**TAA contract:**

| Parameter                  | Value                          |
|----------------------------|--------------------------------|
| Jitter sequence            | **Halton (2, 3)**              |
| Jitter samples per cycle   | **8**                          |
| History blend factor (α)   | **0.1** (exponential)          |
| Velocity rejection         | YCoCg neighborhood clip (3×3)  |
| History format             | `R16G16B16A16_FLOAT`           |

### 5.1 Resolve

```
color_out = α · color_current + (1 − α) · clip(color_history, neighborhood)
```

`neighborhood` is the 3×3 AABB of the current frame's clipped color in YCoCg.
Velocity vectors come from `Velocity` RT (§2.2) and reproject the previous
history texel.

### 5.2 Jitter

Camera projection matrix is offset by `Halton(frame_index % 8)` in NDC,
scaled to one pixel. Jitter must be applied **before** view matrix
multiplication and recorded for the velocity computation.

---

## 6. Level of Detail — Cluster-based (Nanite-style)

| Parameter             | Value                       |
|-----------------------|-----------------------------|
| Cluster size          | **128 triangles**           |
| Cluster topology      | Strip-friendly, 1 material  |
| LOD reduction         | **DAG** (graph of clusters) |
| Reduction ratio       | 2:1 per level               |
| Max LOD levels        | 24                          |
| Selection metric      | Screen-space cluster error  |

### 6.1 Build-time

For each mesh:

1. Partition into 128-triangle clusters via METIS-style graph partitioning.
2. Reduce: group adjacent clusters in 4s, simplify to 2 clusters, record
   parent/child edges in a **DAG**.
3. Repeat until single cluster remains.

### 6.2 Runtime selection

Per cluster: select if `parent_error > screen_pixel_threshold` and
`self_error ≤ screen_pixel_threshold`. Selection is cut-uniform across
the DAG (no T-junctions).

### 6.3 Rasterization

- Large clusters → hardware raster path (indirect draw).
- Small clusters (< 32 px area) → software raster via compute (atomic
  64-bit visibility buffer).

---

## 7. Post-Processing Stack

Applied in order, each reads previous and writes next:

| # | Effect                | Parameters                                |
|---|-----------------------|-------------------------------------------|
| 1 | Bloom                 | **Dual-Kawase**, 6 down/up passes         |
| 2 | Tonemap               | **ACES** (Hable-fitted curve)             |
| 3 | Color LUT             | **64³** 3D LUT, trilinear sample          |
| 4 | Chromatic aberration  | Radial offset, R/G/B per-channel scale    |
| 5 | Vignette              | Smoothstep falloff, configurable inner/outer |

### 7.1 Bloom (Dual Kawase)

- Downsample chain: 6 levels, half-res each step.
- Upsample with 4-tap Kawase kernel, additive blend with previous level.
- Threshold soft-knee at 1.0 luminance.

### 7.2 Tonemap (ACES)

```
ACES(x) = (x · (a·x + b)) / (x · (c·x + d) + e)
a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14
```

Input is HDR linear; output is display-referred linear (gamma applied at
swapchain).

### 7.3 Color LUT

64×64×64 RGB LUT, sampled trilinearly. Identity LUT is the default.

### 7.4 Chromatic aberration

Per-channel UV offset along the view-space radial direction:
`uv_R = uv + dir·s`, `uv_G = uv`, `uv_B = uv − dir·s`. `s` = scale param.

### 7.5 Vignette

`v = smoothstep(outer, inner, length(uv − 0.5) · 2)`, multiplied into RGB.

---

## 8. GPU Instancing & Culling

All opaque draws are issued as **indirect draws** populated by GPU compute.

### 8.1 Instance buffer

Per-instance entry (32 bytes):

```
struct Instance {
    float4x3 world_transform;  // 48 bytes -- packed to 32 via quat+scale
    uint     mesh_id;
    uint     material_id;
    uint     cluster_root;
    uint     flags;            // visibility bits, cast_shadow, etc.
};
```

### 8.2 Cull pipeline (compute)

Two passes per view:

1. **Frustum cull** — instance AABB vs. view frustum planes. SIMD over
   threadgroup (64 instances per group).
2. **Occlusion cull** — instance AABB vs. previous frame's HZB; project
   AABB to screen, sample HZB at the appropriate mip, compare to nearest
   instance depth.

Surviving instances append to a per-pass indirect draw buffer using
atomic counters. The CPU issues a single `DrawIndirectCount` per pass.

### 8.3 Cluster cull

After instance cull, a second compute pass culls clusters within
surviving instances using the DAG selection rule (§6.2) plus per-cluster
frustum + HZB test.

---

## 9. Resource Naming Convention

Render-graph resources are addressed by name. Stages declare reads/writes
by name; the graph compiler resolves aliasing and lifetimes.

```
SceneDepth              D32_FLOAT, view-resolution
GBufferA..D, Velocity   see §2.2
SceneColor              R16G16B16A16_FLOAT
HZB                     R32_FLOAT, mip-chain to 1×1
VSM_Physical            D16_UNORM, 4096×4096
ShadowMask              R8_UNORM
IndirectDiffuse/Spec    R11G11B10F
History                 R16G16B16A16_FLOAT (TAA)
BackBuffer              swapchain format (B8G8R8A8_UNORM_SRGB)
```

---

## 10. Open Questions / Future Work

- **Hair/fur** — separate strand-based pass (post-GBuffer, pre-Lighting). Not in this contract.
- **Volumetrics** — froxel-volumetric fog implied but not specified here; deferred to ARCH-7+.
- **Ray-traced reflections** — optional path; SDF reflection is the contract baseline.
- **Mesh shaders** — implementation choice (mesh-shader vs. compute software raster) is left
  to ARCH-8.

---

## 11. Acceptance Mapping

| Acceptance criterion                                      | Section |
|-----------------------------------------------------------|---------|
| Render graph stages (Depth → … → Post)                    | §2      |
| VSM 16k virtual / 8 cascades                              | §3      |
| Radiance cascade SDF, 4 levels, 16 probes/cell            | §4      |
| TAA Halton-8 jitter, α = 0.1                              | §5      |
| Cluster LOD, 128 tri, DAG reduction                       | §6      |
| Bloom (dual Kawase), ACES, 64³ LUT, CA, vignette          | §7      |
| GPU instancing, indirect draw, frustum + HZB occlusion    | §8      |

---
*End of contract — ARCH-6.*
