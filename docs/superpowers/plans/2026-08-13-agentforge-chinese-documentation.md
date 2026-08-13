# AgentForge 中文项目文档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前完整 AgentForge 能力整理为可用于秋招阅读、演示与技术追溯的中文项目文档入口。

**Architecture:** `README.md` 是中文能力地图和唯一项目主页；`docs/portfolio-guide.zh-CN.md` 提供投递/面试导读；`docs/core-demo.md` 是可执行操作手册；英文 `docs/architecture.md` 保留深入设计并增加中文阅读入口。历史 milestone 和评测报告保持原样，只作为证据链接。

**Tech Stack:** Markdown、Mermaid、PowerShell、Git、现有 pytest/CI demo 脚本。

---

## File Map

- Modify: `README.md` — 中文能力地图、demo、事实边界和文档导航。
- Modify: `README.zh-CN.md` — 兼容入口，链接根 README 和项目导读。
- Create: `docs/portfolio-guide.zh-CN.md` — 投递、演示和面试阅读路线。
- Modify: `docs/core-demo.md` — 中文可复现 Core demo 手册。
- Modify: `docs/architecture.md` — 英文深入架构的中文阅读入口。
- Create: `tests/docs/test_project_documentation.py` — 文档链接、能力边界与 Core demo 关键事实检查。

### Task 1: 为全项目文档建立可执行事实检查

**Files:**
- Create: `tests/docs/test_project_documentation.py`
- Read: `README.md`, `docs/core-demo.md`, `docs/evaluation_guide.md`, `docs/architecture.md`, `docs/superpowers/specs/2026-08-13-agentforge-chinese-documentation-design.md`

- [ ] **Step 1: 写出会失败的中文文档契约测试**

```python
from pathlib import Path


def test_readme_is_chinese_project_entrypoint() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "耐久、可审计、策略受控" in text
    assert "三分钟 Core CLI 演示" in text
    assert "受控工具与审批" in text
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
    assert "官方 SWE-bench 成绩" not in text
    assert "OS 级隔离" in text
```

- [ ] **Step 2: 运行 RED**

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py -q`

Expected: FAIL，因为根 README 尚非中文能力入口，中文项目导读与测试不存在。

- [ ] **Step 3: 保持测试只核对公开事实**

测试不得读取隐藏测试、绝对 verifier 路径、环境变量或原始模型输出；测试只读取版本控制下的
Markdown 并断言公开措辞、链接与事实边界。

- [ ] **Step 4: 运行 RED 后提交测试基线**

```powershell
git add tests/docs/test_project_documentation.py
git commit -m "test: define Chinese project documentation contract"
```

### Task 2: 重写中文 README 能力地图

**Files:**
- Modify: `README.md`
- Read: `docs/architecture.md`, `docs/evaluation_guide.md`, `docs/core-demo.md`, `docs/superpowers/plans/2026-08-10-agentforge-productization-program.md`
- Test: `tests/docs/test_project_documentation.py`

- [ ] **Step 1: 写入中文首页结构**

将 `README.md` 重写为以下章节，保留命令、类型名、文件路径与固定状态枚举的英文拼写：

```markdown
# AgentForge

**一个耐久、可审计、策略受控的代码修复 Agent Runtime。**

## 项目状态

当前稳定基线为 `a2-core-release`：可安装的 `agentforge` CLI 已支持创建 Run、审批、
新进程 resume、inspect 与最终 `VERIFIED` 验证。

## 它解决什么问题

代码修复 Agent 的难点不只是模型生成，而是让副作用、进程中断和验证结果在重启后仍可被
安全地解释与恢复。

## 三分钟 Core CLI 演示

... 链接 `docs/core-demo.md`，列出 trust → exec → approve → resume → VERIFIED ...

## 当前能力地图

### 耐久 Runtime 与模型执行
### 受控工具、审批与 workspace mutation
### 测试、最终验证与证据
### 修复评测与 Pilot
### 可安装的 A2 Core CLI

## 架构概览

... Mermaid 图与 `docs/architecture.md` 链接 ...

## 可核对的验证事实

... unit、fresh wheel、Core demo、Ruff、mypy、CI 配置 ...

## 当前边界

... NON_HERMETIC、非官方 benchmark、真实模型授权、B/C 未开始 ...

## 文档导航

... portfolio guide、demo、architecture、evaluation guide、历史报告 ...
```

- [ ] **Step 2: 用现有事实填充能力地图**

每个能力章节必须只引用已经存在的源码/文档事实：

| README 章节 | 必须链接的证据 |
| --- | --- |
| 耐久 Runtime | `docs/architecture.md` 与 `docs/milestone_03_report.md` |
| mutation 与审批 | `docs/milestone_05_report.md` |
| 测试与验证 | `docs/milestone_06_report.md`、`docs/security_model.md` |
| 修复评测与 Pilot | `docs/milestone_07a_report.md`、`docs/milestone_07b2_4_report.md` |
| A2 CLI | `docs/core-demo.md`、`docs/superpowers/plans/2026-08-10-agentforge-a2-minimal-product-cli.md` |

不得写入未完成 B/C 的实现性陈述、官方 benchmark 分数、OS sandbox 宣称或通用真实模型
性能结论。

- [ ] **Step 3: 运行 README 文档契约**

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py -q`

Expected: PASS。

- [ ] **Step 4: 提交 README**

```powershell
git add README.md tests/docs/test_project_documentation.py
git commit -m "docs: present AgentForge capabilities in Chinese"
```

### Task 3: 新增中文项目导读和演示操作手册

**Files:**
- Create: `docs/portfolio-guide.zh-CN.md`
- Modify: `docs/core-demo.md`
- Modify: `README.zh-CN.md`
- Test: `tests/docs/test_project_documentation.py`

- [ ] **Step 1: 编写中文项目导读**

`docs/portfolio-guide.zh-CN.md` 必须包含以下可直接朗读的内容：

```markdown
# AgentForge 项目导读

## 30 秒介绍

AgentForge 关注的不是“让模型调用工具”，而是让代码修复 Agent 在审批、重启、并发争用和
最终验证后仍留下可审计、可恢复的事实。

## 3 分钟演示顺序

1. 运行 Core demo；
2. 展示 edit_file 与 run_tests 各自暂停等待审批；
3. 用新的 CLI 进程 approve/resume 同一 Run；
4. 展示 `run_finished ... outcome=VERIFIED`；
5. 指向 SQLite 事件、receipt 和 verification evidence 的架构说明。

## 五个工程故事

... 耐久状态、审批副作用、fencing/恢复、验证证据、评测真实性 ...

## 面试阅读路线

... 2 分钟、10 分钟、30 分钟三档链接 ...

## 诚实边界

... 与 README 的 NON_HERMETIC、真实模型和 benchmark 边界一致 ...
```

- [ ] **Step 2: 翻译 Core demo 操作手册**

将 `docs/core-demo.md` 改为中文，并保留现有 PowerShell 与 shell 命令。手册必须说明：

- demo 使用 Mock provider，不需 API key；
- `demo_core` 与 `demo_recovery` 都使用公开 `agentforge` CLI；
- 临时 workspace 与 verifier 目录会在结束时清理；
- 每个 approve/resume 是新进程；
- 成功条件为源文件修改存在且最终输出 `outcome=VERIFIED`；
- `NON_HERMETIC` 不等于 OS 沙箱。

- [ ] **Step 3: 将旧中文 README 改为兼容入口**

将 `README.zh-CN.md` 替换为：

```markdown
# AgentForge 中文入口

中文项目主页已迁移到仓库根目录：[README.md](README.md)。

- [项目导读与面试阅读路线](docs/portfolio-guide.zh-CN.md)
- [Core CLI 演示](docs/core-demo.md)
- [深入架构说明（英文）](docs/architecture.md)
```

- [ ] **Step 4: 扩展文档契约并运行 GREEN**

在 `tests/docs/test_project_documentation.py` 增加：

```python
def test_chinese_guide_and_demo_keep_safe_claims() -> None:
    guide = Path("docs/portfolio-guide.zh-CN.md").read_text(encoding="utf-8")
    demo = Path("docs/core-demo.md").read_text(encoding="utf-8")
    assert "30 秒介绍" in guide
    assert "五个工程故事" in guide
    assert "Mock provider" in demo
    assert "outcome=VERIFIED" in demo
    assert "OS 沙箱" in demo
```

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py -q`

Expected: PASS。

- [ ] **Step 5: 提交导读与 demo 文档**

```powershell
git add docs/portfolio-guide.zh-CN.md docs/core-demo.md README.zh-CN.md tests/docs/test_project_documentation.py
git commit -m "docs: add Chinese portfolio guide and demo"
```

### Task 4: 为英文深入架构增加中文阅读入口并完成校验

**Files:**
- Modify: `docs/architecture.md`
- Modify: `tests/docs/test_project_documentation.py`
- Read: `docs/superpowers/specs/2026-08-13-agentforge-chinese-documentation-design.md`

- [ ] **Step 1: 在架构文档标题后加入中文导航块**

在 `# Architecture` 后插入：

```markdown
> 中文阅读入口：先阅读根目录 [README](../README.md) 了解当前能力地图；投递与面试准备请看
> [中文项目导读](portfolio-guide.zh-CN.md)。本文保留完整的英文技术设计与历史边界，适合需要
> 核对 Runtime、审批、mutation、测试验证和评测流程的读者。
```

- [ ] **Step 2: 在 README 文档导航中加入历史证据索引**

README 必须链接 `docs/implementation_plan.md`、`docs/evaluation_guide.md`、
`docs/milestone_07b2_4_report.md` 与 `docs/security_model.md`，并说明它们是深入事实来源，
不是首页的 headline 结论。

- [ ] **Step 3: 增加架构导航契约**

```python
def test_architecture_has_chinese_reading_entrypoint() -> None:
    text = Path("docs/architecture.md").read_text(encoding="utf-8")
    assert "中文阅读入口" in text
    assert "portfolio-guide.zh-CN.md" in text
```

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py -q`

Expected: PASS。

- [ ] **Step 4: 执行链接与差异检查**

Run:

```powershell
uv run --frozen pytest tests/docs/test_project_documentation.py -q
git diff --check
git status --short
```

Expected: 文档测试通过，diff 无空白错误，只有本任务文档与测试变更。

- [ ] **Step 5: 提交最终导航**

```powershell
git add README.md docs/architecture.md tests/docs/test_project_documentation.py
git commit -m "docs: link Chinese entrypoints to technical evidence"
```

## Plan Self-Review

- **Spec coverage:** Task 2 覆盖完整能力地图与事实边界；Task 3 覆盖中文项目导读、中文 demo
  和旧 README 兼容入口；Task 4 覆盖英文深入架构导航和历史证据索引；Task 1/4 覆盖链接与
  关键事实检查。
- **Scope:** 所有任务只改变文档和文档测试，不触及 runtime、CLI、schema、评测数据或历史报告。
- **Consistency:** 统一使用 `docs/portfolio-guide.zh-CN.md`、`docs/core-demo.md` 和
  `a2-core-release`；所有能力宣称均要求链接到既有报告或架构文档。
- **Placeholder scan:** 本计划不含 TBD/TODO/“适当处理”等未定义实施步骤；每个写入步骤都给出
  文件、章节、代码或命令。
