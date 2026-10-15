# External roadmaps and local display assets

Both control applications read finished JSON/YAML exports from the sibling
`BiBaZu_StableComponentenRoadmaps_grouped` directory. `BIBAZU_ROADMAPS_DIR` selects
another external catalogue; explicitly selected main-branch exports also remain readable.

The grouped roadmap application owns geometry verification, member/capture analysis,
connectivity, route search, stable route identities, and preview-segment construction.
Big Boi loads those records, displays them, assigns calibrated profiles and executes
eligible exported routes. It has no generation package or bundled production roadmaps.
Generic mesh rendering and the licensed CAD/STEP display assets remain local.

Historical bundled roadmaps now live in the external catalogue under
`legacy_catalogue/originals/`. Their original bytes and hashes are preserved.
`legacy_catalogue/path_migrations.json` resolves exact old paths with a SHA-256 check;
no lookup substitutes grouped poses for ungrouped IDs. Offline-produced `.consumer.json`
sidecars supply legacy route choices without changing the historical roadmap hash.
A mismatched sidecar requires an offline upgrade.

Saved configurations and profiles remain operator data. A genuinely changed roadmap
still requires explicit reimport and review of class, target and profile mappings.
Saved profile assignments for absent derived routes survive loading and resaving; an
offline upgrade restores them by their exact exported ID. They remain unavailable for
execution until then. Missing routes are never reconstructed by the control applications.

## Existing configurations requiring attention

- `Df1a.yaml`, `Dl1a.yaml`, `Kk1a.yaml`, and `Ql1i.yaml` now point to their component in the shared roadmap repository.
- Their original legacy roadmaps were already missing before this change.
- Current roadmaps have different pose IDs; use **Re-import roadmap** and review the class, target, and pressure-profile mappings before running them.
- Existing hashes and mappings are retained so these changes cannot silently apply old profiles to new pose IDs.
- The model referenced by `Ql1i.yaml` was also already missing; choose the intended trained model.
- Trained models and custom experiment files outside this repository remain operator-selected inputs.
- Robot support and saved pressure profiles remain part of the parent `BiBaZu_Big_Boi` repository.
