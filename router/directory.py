"""Load heb659_directory.csv; parse messy aisle_raw values into anchor keys."""
import csv, re, sys

def parse_aisle_raw(raw):
    keys = []
    for part in re.split(r"[,&]", raw):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)     # range "33 - 35"
        if m:
            keys += [f"AISLE {i}" for i in range(int(m[1]), int(m[2]) + 1)]
        elif part.isdigit():
            keys.append(f"AISLE {int(part)}")
        else:
            keys.append(part.upper())                     # named zone
    return keys

def load_directory(csv_path, known_anchors):
    out = {}
    for row in csv.DictReader(open(csv_path)):
        keys = [k for k in parse_aisle_raw(row["aisle_raw"]) if k in known_anchors]
        dropped = set(parse_aisle_raw(row["aisle_raw"])) - set(keys)
        if dropped:
            print(f"warn: {row['item']}: unknown anchors {dropped}", file=sys.stderr)
        if keys:
            out[row["item"]] = keys
    return out
