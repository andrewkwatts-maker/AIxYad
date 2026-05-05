"""
Seed the four user-requested Nova3D editor/engine tasks at HIGH priority and
reset the five rate-limit-damaged tasks back to backlog. One-shot script.
"""
import sqlite3
from pathlib import Path
import sys

PROJECT_DB = Path("H:/Github/Nova3D/.automatica/project.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automatica_db import create_task, update_task

DAMAGED_IDS = ["L0140-WC3-1", "L0143-WC3-4", "L0130-GEO-1",
               "L0146-WC3-7", "L0144-WC3-5"]

USER_TASKS = [
    {
        "title": "Wire ContentBrowser drag-drop & viewport Add to Scene render path",
        "description": (
            "USER REPORT: 'Adding camera/meshes/models/SDFs to the scene shows "
            "nothing on screen.'\n\n"
            "Root cause survey:\n"
            "  - engine/scene/Scene::Render walks SceneNode tree correctly.\n"
            "  - game/src/editor/content/ContentBrowser.cpp creates assets in "
            "ContentDatabase only — never calls Scene::AddChild.\n"
            "  - ContentBrowser drag source emits an 'ASSET' payload "
            "(line ~1095) but engine/editor/ViewportPanel.cpp has NO "
            "BeginDragDropTarget to receive it.\n"
            "  - ViewportPanel right-click menu (line ~177) calls "
            "EditorApplication::CreatePrimitive/CreateLight/CreateCamera — "
            "verify these actually add SceneNodes that survive a frame.\n\n"
            "Implement the wiring so that BOTH paths land geometry under "
            "Scene::m_root and the next frame draws it.\n"
            "FIRST STEP: add diagnostic logging (root child count per second, "
            "+Added line per AddChild, DrawMesh counter per frame). This "
            "lets us verify the fix end-to-end."
        ),
        "acceptance_criteria": (
            "1. Scene/SceneNode emit a log line on every AddChild: "
            "'[SceneDiag] +Added <node-name> under <parent>; root subtree=N'.\n"
            "2. Once per second the engine logs '[SceneDiag] root_children=N "
            "visible=N mesh_with_material=N camera=<name|none>'.\n"
            "3. Renderer increments a per-frame DrawMesh counter and logs "
            "it once per second.\n"
            "4. ViewportPanel adds an ImGui::BeginDragDropTarget block that "
            "accepts the 'ASSET' payload from ContentBrowser. On drop, the "
            "asset is loaded into a new SceneNode (with mesh+material if it "
            "is a mesh, or appropriate component otherwise) and AddChild'd "
            "under Scene::m_root at the camera focus point.\n"
            "5. EditorApplication::CreatePrimitive/CreateLight/CreateCamera "
            "verified or fixed to AddChild under root and emit a log line.\n"
            "6. cmake --build build --config Release -j passes.\n"
            "7. ctest --test-dir build -C Release passes existing tests; one "
            "new test covers Scene::AddChild side effect (SceneNode appears in "
            "GetRoot()->GetChildren())."
        ),
        "priority": "CRITICAL",
        "complexity": 13,
        "agent_slug": "Developer",
    },
    {
        "title": "Generic AssetTypeRegistry powering ContentBrowser right-click Add",
        "description": (
            "USER REQUEST: 'Right click should allow me to define any number "
            "of available asset types, either create new or import etc.'\n\n"
            "Today the right-click Add submenu in "
            "game/src/editor/content/ContentBrowser.cpp is hard-coded "
            "(Unit/Building/Spell/Tile/Effect at line ~152). Replace it with "
            "a runtime registry so other translation units can register new "
            "asset types without touching the menu code."
        ),
        "acceptance_criteria": (
            "1. New header engine/editor/AssetTypeRegistry.hpp with: "
            "struct AssetTypeDef { std::string name; "
            "std::function<void()> create_handler; "
            "std::vector<std::string> import_extensions; "
            "std::function<void(const std::string& path)> import_handler; }. "
            "And class AssetTypeRegistry with Register/GetAll/Find static API.\n"
            "2. Implementation in engine/editor/AssetTypeRegistry.cpp.\n"
            "3. ContentBrowser::ShowAssetContextMenu (or the menu-bar Create "
            "submenu — pick whichever is the user-facing right-click) "
            "iterates AssetTypeRegistry::GetAll() to populate the Add submenu. "
            "Hard-coded Unit/Building/Spell/Tile/Effect entries removed.\n"
            "4. At engine init, register at least three default types: Mesh, "
            "Texture, Material. Mesh.create_handler creates a placeholder "
            "asset; Texture.import_handler accepts .png/.jpg; "
            "Material.create_handler opens an empty material asset.\n"
            "5. Build passes; new unit test "
            "engine/tests/test_asset_type_registry.cpp asserts (a) registering "
            "a new type makes it appear in GetAll(), (b) Find by name returns "
            "the correct entry."
        ),
        "priority": "HIGH",
        "complexity": 8,
        "agent_slug": "Developer",
    },
    {
        "title": "Strip viewport camera modes to Fly-only (default), fix dt-scaled movement",
        "description": (
            "USER REQUEST: 'Viewport camera movement seems buggy, default to "
            "fly, doesn't need other modes.'\n\n"
            "engine/editor/ViewportControls.hpp currently exposes Orbit, Fly, "
            "Pan, Turntable, Walkthrough modes. Reduce to Fly only and ensure "
            "movement is smooth and frame-rate-independent."
        ),
        "acceptance_criteria": (
            "1. CameraMode enum reduced to a single value 'Fly' (or remove "
            "the enum entirely if no other code depends on it).\n"
            "2. ViewportControls default-constructs in Fly mode; SetMode "
            "either removed or kept as a no-op accepting only Fly.\n"
            "3. WASD moves the camera along its forward/right vectors at a "
            "speed scaled by dt (so motion is identical at 60fps and 144fps). "
            "Shift = boost, Ctrl = slow.\n"
            "4. Right-mouse-button hold + mouse motion rotates yaw/pitch with "
            "a configurable sensitivity. No jitter; pitch clamped to ±89°.\n"
            "5. Any Orbit/Pan/Turntable/Walkthrough mode-specific code paths "
            "in ViewportControls and its callers are removed or unreachable.\n"
            "6. Build passes; existing camera/viewport tests pass; one new "
            "test asserts ViewportControls default-constructs in Fly and "
            "movement scales with dt (e.g. running update with dt=2 moves "
            "twice as far as dt=1)."
        ),
        "priority": "HIGH",
        "complexity": 5,
        "agent_slug": "Developer",
    },
    {
        "title": "Gizmo: MMB-drag pans view; click-once snaps to axis-lock or 45°",
        "description": (
            "USER REQUEST: 'Gizmo needs option to hold/drag to reposition the "
            "view, also needs options to click once and get new axis "
            "locked/45 angle options.'\n\n"
            "Two enhancements to the editor viewport:\n"
            "  (a) Holding MMB anywhere in the viewport pans the camera "
            "(translates it on its right/up plane).\n"
            "  (b) A short click (<200ms, no drag) on a TransformGizmo axis "
            "snaps the manipulated object: translate snaps to the next world-"
            "axis-aligned grid step on that axis; rotate snaps to the nearest "
            "45° angle. A long click (>=200ms) keeps existing free-drag "
            "behaviour."
        ),
        "acceptance_criteria": (
            "1. ViewportControls (or whichever class handles input — "
            "engine/editor/ViewportControls.cpp / ViewportPanel.cpp) responds "
            "to MMB-down/move/up by translating the camera in its right/up "
            "plane proportional to mouse delta. Speed scaled by dt.\n"
            "2. TransformGizmo::OnAxisInput distinguishes click (<200ms, "
            "drag distance < 4px) from drag (>=200ms or > 4px). On click, "
            "apply snap: translate moves to nearest grid step on the picked "
            "axis (default step 1.0); rotate snaps to nearest multiple of 45°.\n"
            "3. Hold-drag continues to work as before for fine-grained "
            "manipulation.\n"
            "4. Build passes; two new tests in "
            "engine/tests/test_gizmo_input.cpp cover (a) MMB-pan moves the "
            "camera by the expected amount, (b) a click on the X axis with "
            "an object at x=0.3 snaps to x=0 (or 1, whichever is nearer)."
        ),
        "priority": "HIGH",
        "complexity": 8,
        "agent_slug": "Developer",
    },
]


def main() -> None:
    conn = sqlite3.connect(PROJECT_DB)
    conn.row_factory = sqlite3.Row

    # Reset rate-limit-damaged tasks
    print("=== Resetting rate-limit-damaged tasks ===")
    for did in DAMAGED_IDS:
        cur = conn.execute(
            "UPDATE tasks SET failure_count = 0, status = 'backlog', "
            "blocked_reason = NULL WHERE display_id = ? "
            "AND status IN ('blocked', 'sprint_planned')",
            (did,),
        )
        print(f"  {did}: rows updated = {cur.rowcount}")
    conn.commit()

    # Also clear sprint_tasks rows for any sprint that's still open (so the
    # planner doesn't re-pick a stale sprint_planned task).
    cur = conn.execute(
        "DELETE FROM sprint_tasks WHERE status IN ('blocked', 'failed', 'in_progress')"
    )
    print(f"  Cleared {cur.rowcount} stale sprint_tasks rows")
    conn.commit()

    # Add the four user tasks
    print("\n=== Adding user tasks ===")
    for spec in USER_TASKS:
        result = create_task(conn, **spec)
        print(f"  + {result['display_id']}: {spec['title'][:60]}")
        # Bump sort_order so HIGH/CRITICAL appear at top
        conn.execute(
            "UPDATE tasks SET sort_order = -1000 + ? "
            "WHERE display_id = ?",
            (USER_TASKS.index(spec), result["display_id"]),
        )
    conn.commit()

    # Sanity print
    print("\n=== Top backlog after seed ===")
    for row in conn.execute(
        "SELECT display_id, priority, complexity, status, failure_count, title "
        "FROM tasks WHERE status = 'backlog' "
        "ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 "
        "WHEN 'MEDIUM' THEN 2 ELSE 3 END, sort_order LIMIT 10"
    ):
        print(
            f"  {row['display_id']:14s} {row['priority']:8s} "
            f"c={row['complexity']:>2} fc={row['failure_count']} "
            f"{(row['title'] or '')[:60]}"
        )

    conn.close()


if __name__ == "__main__":
    main()
