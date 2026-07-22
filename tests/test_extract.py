from extract import stitch_open_boundary


def test_stitches_open_perimeter_chains_at_wall_junction():
    chains = [
        [[40, 10], [10, 10], [10, 70], [80, 70], [80, 90]],
        [[120, 10], [120, 90], [20, 90]],
    ]

    assert stitch_open_boundary(chains, 120, 100) == [
        [40, 10], [10, 10], [10, 70], [80, 70], [80, 90],
        [120, 90], [120, 10], [40, 10],
    ]
