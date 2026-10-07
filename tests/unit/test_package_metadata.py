from importlib.metadata import version

import tether


def test_package_version_matches_distribution_metadata() -> None:
    assert tether.__version__ == version("tether-runtime")
