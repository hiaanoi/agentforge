import pytest
from diagnostics import dump_diagnostic


@pytest.mark.parametrize(
    "parts",
    [
        ("plain ASCII",),
        ("emoji: 🧭",),
        ("第一行\n", "second line\n", "终わり"),
        ("Greek λ and CJK 路径",),
    ],
)
def test_exact_utf8_bytes_and_content(tmp_path, parts: tuple[str, ...]) -> None:
    target = tmp_path / "diagnostic.log"
    dump_diagnostic(target, *parts)
    expected = "".join(parts)
    assert target.read_bytes() == expected.encode("utf-8")
    assert target.read_text(encoding="utf-8") == expected


def test_reopening_replaces_previous_content_without_encoding_drift(tmp_path) -> None:
    target = tmp_path / "diagnostic.log"
    dump_diagnostic(target, "旧内容")
    dump_diagnostic(target, "new 🧪\n多行")
    assert target.read_text(encoding="utf-8") == "new 🧪\n多行"
