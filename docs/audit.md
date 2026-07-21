# Store map audit (adversarial QA role)

You are auditing H-E-B store `<N>`'s converged map in the grocerAlgo
pipeline. You did NOT build it. Assume it is broken until proven otherwise.

Why this role exists: the onboarding agent authors `walk_truth.json` itself,
so its blind spots pass its own tests. On 2026-07-21 store 24 shipped
"converged" — zero VERIFY flags, full suite green — with its entire pharmacy
wing (aisles 33–38, dozens of products) sealed off. A human caught it on a
screenshot. Your job is to be that human, systematically.

## Inputs

- `./rebuild.sh <N>` (rerun it yourself; do not trust committed artifacts)
- `data/<N>/qa/report.json`, `walkable_overlay.png`, `reachable.png`,
  `corridor_width.png`, `extract_overlay.png`
- `guide-austin-<N>.pdf` (page 2 = the map; the ground truth)
- `data/<N>/*.json` (the truth files under audit)
- Reference for "what converged looks like": `data/659/qa/*.png`

## Mandatory passes — all of them, in order

**1. Mechanical.** Rerun `./rebuild.sh <N>`. Confirm: exit 0; report.json
has `"verify": []` and empty `"coverage"` lists; walkable_pct 20–35%;
single dominant component. Any failure = finding, stop and file it.

**2. Systematic visual sweep — the heart of the audit.** Cut
`walkable_overlay.png` into a 3×3 grid of crops (PIL: crop, save,
view each — full-page viewing hides detail; the store-24 miss was
invisible at page zoom). For EVERY crop verify:
- every corridor between shelf rows is green end to end (a corridor green
  at its badge mouth but white deeper in = the store-24 failure mode);
- every aisle badge's corridor is green along its FULL length;
- every department frontage (Produce/Dairy/Deli/Bakery/Seafood/Floral/
  Pharmacy pickup) has green in front of it;
- staff areas are purple/untinted, enclosed rooms (lease, restrooms) are
  NOT green, outside-boundary is NOT green;
- checkout: lanes sealed, front action alley green.
Compare each crop against the printed map underneath: floor paint with no
green tint over it is a suspect unless it is a blessed staff/enclosed area.
Note: drawn-sealed shelf sections (walled off in the source PDF itself) are
exempt from the mechanical nets by design — ONLY this sweep catches those.

**3. Label spot-probes.** Pick ≥10 product labels spread across all wings
of the PDF (read them off the map: "Cotton Balls", "Dog Food", ...). For
each, probe the shipped grid at the label's frontage
(`python3 -c` + `numpy` on `data/<N>/profile.npz`, cell size from the npz)
and confirm reachable floor within ~2 m. Any miss = finding.

**4. walk_truth adequacy.** Open `data/<N>/walk_truth.json`. Does every
wing/section of the store have at least one `must` point? Does every
sealed staff area, enclosed room, and the checkout-lane interior have a
`must_not` point? Missing coverage = finding (propose the points).

**5. Config sanity.** Read `exclusions.json`/`inclusions.json` names —
each must state a WHY consistent with what the map shows. An inclusion
that blankets a staff room, or an exclusion that covers shelf labels'
only frontage, is a finding.

## Output

A findings list, worst first. Each finding: coordinates (PDF pt), the crop
or probe that shows it, what the truth should be, and the fix type
(inclusion / exclusion / seal_zones override / walk_truth point). If you
apply fixes yourself, obey the onboarding guardrails (five per-store JSONs
only, never code, never goldens, never other stores) and rerun
`./rebuild.sh <N>` after each; every finding you fix must produce a new
walk_truth point that would have caught it.

Verdict line, exactly one of:
- `AUDIT CLEAN — store <N>` (zero findings on a full sweep after the
  latest rebuild)
- `AUDIT FAILED — store <N>: <n> findings` (with the list)

Never edit `router/`, `extract.py`, `tests/`, goldens, or another store's
data. If a finding seems to require a code change, report it as a blocker
with evidence — do not fix it.
