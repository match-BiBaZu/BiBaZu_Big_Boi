# Grouped roadmap composite candidates

Pressure Control reads the optional `composite_transfers` section of schema-v1
roadmap JSON/YAML. The existing direct edge list stays unchanged. Candidates describe
ordered Y/Z rotations through transient orientations; they do not add pose IDs.

Select start and target poses in the roadmap picker. Composite candidates show
their axes and signed angles, a geometry-only label, and a slider for transient
orientation previews. Route choices are loaded from `consumer_export`; the
external grouped roadmap application calculates them and preserves legacy IDs.
Pressure Control performs no route search or geometric feasibility analysis.

Calibration profiles save the candidate ID, ordered commands, transient
quaternions and evidence status in `roadmap_transition` metadata. This context
survives moving or losing the referenced roadmap. New candidate profile names
include their command digest so different transfers do not overwrite one another.
Existing profile names and metadata remain supported.

These are orientation geometry candidates. Contact feasibility, settling,
actuator feasibility and reliability remain unverified. Selecting a candidate
does not generate pressure commands. Calibrate a complete transfer profile and
explicitly assign it in Reorientation Control before runtime use.

Both applications default to the external `BiBaZu_StableComponentenRoadmaps_grouped`
catalogue and support `BIBAZU_ROADMAPS_DIR`. They retain structural checks, explicit
source coverage and profile metadata. Preview interpolation is a display operation.
Older exports expose explicit transitions; missing derived routes need an offline upgrade.

## Whole-group reorientations (consumer-export version 2)

The external grouped application exports group transfers and finished routes,
prioritizing exact ±90°/±180° chute X/Y/Z commands over other rotations. The map
shows each directed pair's preferred reorientation; pickers retain alternatives.
Labels show ordered rotation names. Evidence and competing outcomes remain in
the details. Actual member endpoints and settling segments drive previews;
intermediate legs may use different members of a group without an invented motion.

For a new group choice, assign the calibrated profile and explicitly check
**Calibrated for source group**. Metadata binds this declaration to the exported
transfer ID and stable source-group key. A numeric group ID alone is insufficient.
The declaration survives profile restoration, including when the roadmap is
unavailable. The individual member-coverage field is retained for version-1
exports. Older profiles are never automatically qualified for whole-group use.

Group interchangeability is a planning assumption. Exact Cartesian commands may
bypass contact/support filters during generation. Downhill landing evidence is
sampled geometry; contact feasibility, settling reliability, and successful
execution require calibration. Pressure Control only consumes exported records
and renders their previews. The separate main-branch catalogue is unchanged.
