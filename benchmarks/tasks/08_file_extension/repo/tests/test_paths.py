from paths import file_extension


def test_extension_is_lowercase() -> None:
    assert file_extension("archive.TAR.GZ") == "gz"


def test_names_without_extension_return_empty() -> None:
    assert file_extension("README") == ""
    assert file_extension(".gitignore") == ""
