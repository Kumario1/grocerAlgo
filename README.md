# grocerAlgo — in-store route optimizer

Paste a grocery list, get the provably shortest walking route through
H-E-B #659 (Austin). Powered by the store's official published directory
PDF — no scraping. See `plan.md` for the full product plan.

## Run it

    pip install -r requirements.txt
    python3 -m uvicorn app:app --port 8000
    # open http://localhost:8000

## Rebuild store data (only when the source PDF changes)

    python3 extract_659.py [store]    # guide-austin-<store>.pdf -> data/<store>/geometry.json
    python3 build_profile.py [store]  # geometry -> data/<store>/profile.npz
    python3 map_qa.py [store]         # walkability diagnostics -> data/<store>/qa/

Per-store data lives in data/<store>/ (659 = pilot, 24 = map-generality test).

## Tests

    python3 -m pytest -q
