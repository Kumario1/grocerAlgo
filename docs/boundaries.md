# Fleet boundary extension

`data/<N>/boundary.json` is a permitted per-store truth file. Author it when
the first mechanical pass reports `no closed thick-stroke boundary polygon
found`, or when the extracted boundary visibly follows an exterior curb.

Trace the printed store perimeter in PDF points:

```json
{"name": "printed sales-floor perimeter; exterior curb excluded",
 "poly": [[x0, y0], [x1, y1], [x2, y2], [x3, y3]]}
```

The polygon may be concave and closes automatically. It must stay inside the
page and enclose a store-sized area. Follow the building/floor outline across
doorway gaps; never include parking, drive-through lanes, exterior sidewalks,
or detached page artwork. Rerun `./rebuild.sh <N>` and place `walk_truth`
points just inside and outside every ambiguous edge.
