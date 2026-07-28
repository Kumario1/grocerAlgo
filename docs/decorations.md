# Fleet decoration extension

`data/<N>/decorations.json` is a permitted per-store truth file. Use it only
when printed logo or brand artwork is visibly drawn on customer floor but
vector extraction classified the artwork as a fixture.

Each named `{"rect": [x0,y0,x1,y1]}` or `{"poly": [[x,y],...]}` clears fixture
pixels only; it cannot erase walls or exclusions. Fit the shape tightly to the
printed artwork, rerun `./rebuild.sh <N>`, and add a `walk_truth` point inside
the restored corridor. Never use it to erase a shelf, counter, or furniture.
