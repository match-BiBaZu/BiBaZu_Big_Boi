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

## Member-conditioned group transfers

Grouped roadmaps may also provide `member_states`, `member_transfers`,
`member_routes`, and `capture_analysis`. Group thumbnails and numeric IDs remain
the same; transfer previews use the actual source member and a possible arrival
member. Commands and sampled downhill settling segments are shown separately.
The map summarizes group connections, while individual choices list required
source members, possible arrivals, and capture intervals. Clicking the same group
twice permits calibration of intra-group repositioning.

Enter explicitly tested source-member IDs in the coverage field, separated by
commas. `*` declares all source members have been tested. Blank coverage keeps the
choice available for calibration without making it eligible for group-level
runtime execution. Profiles retain `member_context`, endpoint quaternions, and
`covered_source_member_ids`, including when the original roadmap is unavailable.

Small grouping barriers do not establish passive motion. Capture intervals are
sampled geometric candidates and may have ambiguous or unresolved outcomes.
Reorientation Control requires explicit profile coverage for every currently
possible source member; it never assumes a free switch between group members.
