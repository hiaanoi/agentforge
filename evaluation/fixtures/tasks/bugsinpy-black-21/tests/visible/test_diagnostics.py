from diagnostics import dump_diagnostic


def test_non_ascii_diagnostic_round_trips_as_utf8(tmp_path) -> None:
    target = tmp_path / "diagnostic.log"
    text = "格式错误: 需要缩进"

    dump_diagnostic(target, text)

    assert target.read_bytes() == text.encode("utf-8")
    assert target.read_text(encoding="utf-8") == text
