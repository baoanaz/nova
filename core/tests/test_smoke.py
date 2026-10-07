from nova_core import __version__


def test_package_importable() -> None:
    assert __version__
