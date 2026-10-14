# Local geometry and shared roadmaps

- `vendor/bibazu_chute_pose/` contains a private geometry package snapshot at commit `41af9d6cb941ded3c86c2e488b28a063a94da607`.
- `uv sync --extra dev` installs the local package; the original geometry checkout is no longer required.
- `src/bibazu_reorientation/assets/geometry/cad/` contains 39 STL/STEP pairs with license and source hashes.
- Both Reorientation Control and Pressure Control read roadmaps directly from the sibling `BiBaZu_StableComponentenRoadmaps` repository.
- `BIBAZU_ROADMAPS_DIR` overrides the roadmap repository location for both applications.
- Roadmap dialogs and legacy profile lookup use that shared local repository; no new runtime roadmap copies are kept here.
- Saved configurations reference roadmap files directly; regenerated roadmaps still require explicit re-import when their hashes change.
- CAD files referenced by a roadmap stay beside that roadmap in its `source/` directory.
- Drop Sim has a separate geometry copy and does not need these roadmaps.

## Existing configurations requiring attention

- `Df1a.yaml`, `Dl1a.yaml`, `Kk1a.yaml`, and `Ql1i.yaml` now point to their component in the shared roadmap repository.
- Their original legacy roadmaps were already missing before this change.
- Current roadmaps have different pose IDs; use **Re-import roadmap** and review the class, target, and pressure-profile mappings before running them.
- Existing hashes and mappings are retained so these changes cannot silently apply old profiles to new pose IDs.
- The model referenced by `Ql1i.yaml` was also already missing; choose the intended trained model.
- Trained models and custom experiment files outside this repository remain operator-selected inputs.
- Robot support and saved pressure profiles remain part of the parent `BiBaZu_Big_Boi` repository.
