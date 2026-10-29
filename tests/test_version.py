from importlib.metadata import version

import majordom_integration_sdk


def test_version_matches_installed_distribution():
    assert majordom_integration_sdk.__version__ == version("majordom-integration-sdk")
