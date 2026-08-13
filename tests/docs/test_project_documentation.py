import re
from pathlib import Path


def test_readme_is_chinese_project_entrypoint() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    assert "耐久、可审计、策略受控" in text
    assert "三分钟 Core CLI 演示" in text
    assert "受控工具、审批与 workspace mutation" in text
    assert "修复评测与 Pilot" in text
    assert "NON_HERMETIC" in text


def test_documentation_links_and_truth_boundaries() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    for target in (
        "docs/core-demo.md",
        "docs/portfolio-guide.zh-CN.md",
        "docs/architecture.md",
        "docs/evaluation_guide.md",
    ):
        assert target in text
        assert Path(target).is_file()
    assert "不构成真实模型能力、通用代码\n  修复能力或官方 SWE-bench 成绩" in text
    assert "OS 级隔离" in text


def test_chinese_guide_and_demo_keep_safe_claims() -> None:
    guide = Path("docs/portfolio-guide.zh-CN.md").read_text(encoding="utf-8")
    demo = Path("docs/core-demo.md").read_text(encoding="utf-8")

    assert "30 秒介绍" in guide
    assert "五个工程故事" in guide
    assert "Mock provider" in demo
    assert "outcome=VERIFIED" in demo
    assert "OS 级隔离" in demo


def test_architecture_has_chinese_reading_entrypoint() -> None:
    text = Path("docs/architecture.md").read_text(encoding="utf-8")

    assert "中文阅读入口" in text
    assert "portfolio-guide.zh-CN.md" in text


def test_readme_evidence_links_exist() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\((docs/[^)#]+)", text)

    assert targets
    assert all(Path(target).is_file() for target in targets)


def test_demo_video_recording_entrypoint_is_safe_before_release() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    guide = Path("docs/portfolio-guide.zh-CN.md").read_text(encoding="utf-8")
    recording = Path("docs/demo-recording.zh-CN.md")

    assert recording.is_file()
    text = recording.read_text(encoding="utf-8")
    assert "2 分 40 秒" in text
    assert "Mock provider" in text
    assert "outcome=VERIFIED" in text
    assert "NON_HERMETIC" in text
    assert "GitHub Release" in text
    assert "docs/demo-recording.zh-CN.md" in readme
    assert "demo-recording.zh-CN.md" in guide
    assert "demo-video-cover.png" not in readme
