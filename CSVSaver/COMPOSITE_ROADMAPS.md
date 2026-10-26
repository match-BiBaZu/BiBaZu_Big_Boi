# Grouped roadmap composite candidates

## Individual poses and shared group actions (consumer export v3)

Exports declaring `planning_semantics: pose_to_group` list commands witnessed from the exact selected source pose, accepting any destination-group member. Actions are not shared across source poses. Previews and saved metadata retain this applicability. Explicitly reimport changed exports; older group-shared assignments do not automatically qualify the new actions.

The external grouped catalogue now shows individual poses numbered by their own observed frequencies, with membership and percentages in both GUIs. Members below the configured cutoff are omitted and cannot witness transfers. Outgoing actions are shared by all displayed source members; any qualifying target-group member satisfies the target. There are no internal group moves or composite fallback bridges. Missing connectivity is reported without constructing a fallback.

Recognition classes continue to identify groups. The GUI reports the possible pose set rather than claiming a particular member was recognised. Selecting a member accepts its group as the target, including zero actuator enables when already in that group. Source-group calibration and assigned pressure profiles remain required.

After the catalogue changes, explicitly reimport the roadmap. Migration uses stable group keys and the exact previous export hash; old numeric group IDs are never interpreted as new individual pose numbers. Compatible assignments can be retained, while removed/composite assignments and omitted recognition groups remain unavailable. Existing exports and profile metadata remain readable. Generation and route construction remain entirely in the external grouped roadmap application.


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
