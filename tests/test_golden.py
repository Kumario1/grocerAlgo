"""Golden-gate regression tests: store 659's walkable grid is frozen, pixel
by pixel, in data/659/golden_free.npy (2026-07-21 blessed baseline).

Any change to universal pipeline code that flips even ONE cell of 659's free
grid fails here. That is the point: onboarding new stores must never move the
blessed store.

Re-bless procedure (a deliberate act, never drift):
    1. Make the intended universal improvement; get every other test green.
    2. Regenerate: python3 build_profile.py 659, then
       python3 -c "import numpy as np; np.save('data/659/golden_free.npy',
           np.load('data/659/profile.npz', allow_pickle=True)['free'])"
    3. Rerun the 659 walkability tests (they are the semantic check that the
       new pixels are still right).
    4. Commit golden + code + justification TOGETHER in one commit.
Re-blessing is forbidden within the universal-map-pipeline plan itself.
"""
import numpy as np

GOLDEN = np.load("data/659/golden_free.npy")


def test_shipped_profile_matches_golden():
    """Catches stale/regenerated artifacts: the committed profile.npz must
    carry exactly the blessed grid."""
    free = np.load("data/659/profile.npz", allow_pickle=True)["free"]
    assert free.shape == GOLDEN.shape, (free.shape, GOLDEN.shape)
    assert np.array_equal(free, GOLDEN), (
        "data/659/profile.npz free grid deviates from golden_free.npy — "
        "a pipeline change moved blessed pixels (see re-bless procedure "
        "in this file's docstring)")
