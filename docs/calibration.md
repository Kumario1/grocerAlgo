# Calibration repair runbook

You are repairing Atlas-to-guide placement for H-E-B store `<N>`.

Read `data/<N>-atlas/calibration.json`, both
`data/<N>{,-atlas}/geometry.json`, and `router/calibrate.py`. Determine whether
the guide uses a different aisle-number correspondence from the current Atlas.

You may create or edit only `data/<N>/store.json`, preserving existing keys.
The supported corrections are:

- `atlas_fit`: pin two real Atlas aisle numbers to their matching guide aisle
  numbers on each axis.
- `aisle_label_shift`: describe a guide-vintage numbering shift with
  `{"from": first_live_aisle, "add": guide_minus_live}`.

Never weaken a gate, edit captured Atlas data, change map geometry, or guess a
correspondence merely to obtain PASS. Validate every edit with:

```sh
python3 calibrate.py <N>
python3 calibrate.py <N> --verify
```

Stop only when both commands pass. If the drawings are genuinely incompatible,
leave `store.json` unchanged and report the evidence.
