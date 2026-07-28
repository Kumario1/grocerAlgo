# Fleet doorway extension

`data/<N>/openings.json` is a sixth permitted per-store truth file. You may
create or edit it alongside the five files named in the runbook.

Use it only when the printed guide visibly erases part of a wall for a customer
doorway but vector extraction retained the underlying stroke. Each named
`{"rect": [x0,y0,x1,y1]}` or `{"poly": [[x,y],...]}` clears wall pixels only;
it cannot erase fixtures or exclusions.

After adding an opening, rerun `./rebuild.sh <N>` and add `walk_truth` points
on both sides of the doorway. Never use an opening to cut through a shelf,
counter, staff room, or a wall without a visible door in the guide.
