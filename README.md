# grocerAlgo — in-store route optimizer

Paste a grocery list, get the provably shortest walking route through
H-E-B #659 (Austin). Powered by the store's official published directory
PDF — no scraping. See `plan.md` for the full product plan.

## Run it

    pip install -r requirements.txt
    python3 -m uvicorn app:app --port 8000
    # open http://localhost:8000

## Rebuild store data (only when the source PDF changes)

    python3 extract_659.py      # PDF -> data/heb659_geometry.json (+ QA overlay)
    python3 build_profile.py    # geometry -> data/heb659_profile.npz

## Tests

    python3 -m pytest -q
