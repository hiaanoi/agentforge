# AgentForge GitHub 演示录像材料 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 AgentForge 的 GitHub 主页提供中文、可复现且安全的演示录像录制材料与 README 发布入口。

**Architecture:** 录制指南是唯一的镜头与隐私事实来源；README 和中文项目导读只链接它，不复制逐镜头内容。现有 `scripts/demo_core.ps1`/`.sh` 仍是唯一的运行路径，文档不得引入新的私有 API、真实模型调用或视频二进制资产。

**Tech Stack:** Markdown、pytest 文档断言、现有公开 `agentforge` CLI、PowerShell/POSIX shell、GitHub Release 外链。

---

## 文件结构

- Create: `docs/demo-recording.zh-CN.md` — 两分四十秒无旁白视频的逐镜头脚本、录制清单、发布说明和重录门槛。
- Modify: `README.md` — 增加“演示视频”文字入口；视频发布前只链接录制指南，不引用不存在的资源。
- Modify: `docs/portfolio-guide.zh-CN.md` — 在三分钟演示顺序后链接录像脚本，区分现场演示与录制剪辑。
- Modify: `tests/docs/test_project_documentation.py` — 验证入口存在、关键事实不漂移且没有假视频资源链接。

### Task 1: 定义文档发布契约

**Files:**
- Modify: `tests/docs/test_project_documentation.py`

- [ ] **Step 1: 写入失败的文档契约测试**

在现有测试文件末尾加入：

```python
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
```

- [ ] **Step 2: 验证测试先失败**

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py::test_demo_video_recording_entrypoint_is_safe_before_release -q`

Expected: FAIL，因为录制指南和两个入口尚不存在。

- [ ] **Step 3: 暂不实现，保留 RED 证据**

不要创建占位图片、视频文件、Release URL 或外部链接。下一任务只创建被测试引用的 Markdown 指南。

- [ ] **Step 4: 提交 RED 以外的已有改动前确认工作树范围**

Run: `git status --short`

Expected: 只有 `tests/docs/test_project_documentation.py` 与本计划后续允许的文档文件会被修改；无法读取的既有 ignored 临时目录不处理。

### Task 2: 编写中文录制脚本与清单

**Files:**
- Create: `docs/demo-recording.zh-CN.md`

- [ ] **Step 1: 写入文件头与真实性范围**

创建文件，使用以下开头：

```markdown
# AgentForge GitHub 演示录像：录制脚本与清单

本指南用于制作一支约 **2 分 40 秒**、16:9、1080p、无真人旁白的中文 GitHub 项目演示视频。
它只使用公开 `agentforge` CLI 与本地 Mock provider，展示产品状态机、审批与跨进程恢复；它不代表
真实模型能力、通用代码修复能力或官方 benchmark 成绩。
```

- [ ] **Step 2: 写入逐镜头时间轴**

新增 `## 逐镜头脚本` 表格，精确列出以下镜头：标题（0:00–0:12）、架构页（0:12–0:28）、
`trust --show`/`trust --yes`（0:28–0:48）、`exec` 后暂停与新终端 `approve`/`resume`
（0:48–1:18）、可见测试审批（1:18–1:52）、最终 verification（1:52–2:20）、
`run_finished ... outcome=VERIFIED` 收束（2:20–2:40）。每行都包含：画面、最多两行的中文字幕、
要保留的真实 CLI 证据，以及不得做出的宣称。

- [ ] **Step 3: 写入可复现终端流程**

新增 `## 录制前验证` 和 `## 终端操作`。前者只允许：

```powershell
uv build
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/demo_core.ps1
```

并给出 POSIX 对应命令：

```sh
uv build
sh ./scripts/demo_core.sh
```

后者明确录制时可按 `docs/core-demo.md` 的同一公开命令分段执行，以保留 trust、exec、approval、
resume 和 final verification 的边界；不得使用 `python -m agentforge`、私有 API 或真实 provider。

- [ ] **Step 4: 写入隐私、重录与发布清单**

新增三个 checklist 小节：

1. `## 隐私检查`：禁止 `.env`、API key、token、用户目录、verifier 绝对路径、隐藏测试、原始
   provider 输出、桌面通知与浏览器标签；
2. `## 重录判定`：终态不是 `outcome=VERIFIED`、没有显示跨进程审批/恢复、或任何敏感信息出现即重录；
3. `## GitHub 发布`：视频本体上传到 GitHub Release 或公开视频页；README 只放外链；发布说明必须
   包含 Mock provider、`NON_HERMETIC` 和不代表 benchmark/真实模型表现三项边界。

- [ ] **Step 5: 运行 RED 测试验证指南已满足核心文字契约的一部分**

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py::test_demo_video_recording_entrypoint_is_safe_before_release -q`

Expected: 仍然 FAIL，因为 README 与项目导读入口尚未添加；失败不应是文件不存在或关键边界缺失。

### Task 3: 添加 README 与项目导读入口

**Files:**
- Modify: `README.md`
- Modify: `docs/portfolio-guide.zh-CN.md`

- [ ] **Step 1: 在 README 的 Core demo 段后添加文字入口**

在“完整的安装前提、预期事件和退出码见”段落之后加入：

```markdown
## 演示视频

GitHub 录制版将以短视频形式展示同一条公开 Core CLI 路径：trust、跨进程 approval/resume、测试与
最终 `VERIFIED`。视频发布前，请先按[录制脚本与清单](docs/demo-recording.zh-CN.md)复现并审查素材；
成片将作为 GitHub Release 或公开视频页的外链发布，不提交视频二进制到仓库历史。
```

不要加入 `docs/assets/demo-video-cover.png`、视频 URL、iframe、HTML video 标签或外部统计脚本。

- [ ] **Step 2: 在中文项目导读的三分钟顺序后添加录制入口**

在“这条 demo 使用 Mock provider”段落之后加入：

```markdown
如果要把这条流程制作成 GitHub 视频，请使用[录制脚本与清单](demo-recording.zh-CN.md)。它将现场演示
和录制剪辑分开，并列出字幕、隐私审查与重录条件。
```

- [ ] **Step 3: 运行完整文档测试使其转绿**

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py -q`

Expected: PASS，包含新增录像入口测试。

- [ ] **Step 4: 检查 Markdown 链接和工作树**

Run: `uv run --frozen ruff check tests/docs; git diff --check; git status --short`

Expected: Ruff 与 diff 检查通过；所有 README 的 `docs/...` 链接指向受版本控制的文件；没有视频二进制或
封面占位文件。

- [ ] **Step 5: 提交文档与测试**

```powershell
git add README.md docs/portfolio-guide.zh-CN.md docs/demo-recording.zh-CN.md tests/docs/test_project_documentation.py
git commit -m "docs: add GitHub demo recording guide"
```

### Task 4: 发布前人工验收记录

**Files:**
- Modify: `docs/demo-recording.zh-CN.md`

- [ ] **Step 1: 在录制指南末尾追加人工验收块**

加入以下不可自动替代的勾选项：

```markdown
## 发布前人工验收

- [ ] 用无凭据 shell 重新跑过 Core demo，结果含 `outcome=VERIFIED`；
- [ ] 已逐帧检查终端、窗口标题和字幕，没有 secret、用户目录、verifier 路径或隐藏测试；
- [ ] 已确认视频说明写明 Mock provider 与 `NON_HERMETIC` 边界；
- [ ] 已把视频上传到 GitHub Release 或已审核的视频页；
- [ ] 已将 README 的文字入口替换为审核后的外链，且未提交视频二进制。
```

- [ ] **Step 2: 重新运行文档测试与格式检查**

Run: `uv run --frozen pytest tests/docs/test_project_documentation.py -q; uv run --frozen ruff check tests/docs; git diff --check`

Expected: 全部通过。

- [ ] **Step 3: 提交人工验收清单**

```powershell
git add docs/demo-recording.zh-CN.md
git commit -m "docs: add demo release checklist"
```

## 计划自检

- 规格覆盖：Task 2 覆盖时间轴、字幕、真实 CLI、隐私和发布边界；Task 3 覆盖 README/项目导读入口；
  Task 1 与 4 覆盖自动与人工验收。
- 范围：不生成视频、封面、Release、远端仓库或运行时代码，符合设计说明的非目标。
- 一致性：所有运行命令来自 `docs/core-demo.md` 和现有 `scripts/demo_core.*`；发布前不引用不存在的
  二进制资源。
- 占位扫描：本计划不含待定实现项；“视频发布前”是明确的发布状态，不是未定义的实现工作。
