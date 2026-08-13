# AgentForge 全项目中文文档设计

## 目标

把当前 AgentForge 的完整能力组织成一条适合秋招阅读和演示的中文文档路径。招聘者应能在
几分钟内理解项目解决的问题、运行公开 demo、核对 Runtime、安全执行与评测基础设施的
工程证据，并看到清楚的能力边界。A2 Core Release 是可亲手运行的产品入口，不是文档范围的
上限。

## 范围

本次只修改项目说明与导航，不修改运行时代码、数据库结构、CLI 行为、评测事实或历史
milestone 报告。所有新增叙述只能基于当前分支已经验证的能力；其中 A2 Core 的可安装状态
以 `a2-core-release` tag 为可复现基线。

## 文档结构

### README：中文能力地图

`README.md` 面向第一次访问仓库的招聘官和技术面试官。它采用能力导向而非里程碑导向，
按以下顺序组织：

1. 一句话定位与项目状态；
2. Agent 代码修复中副作用、重启、重复执行和验证可信度的问题；
3. 三分钟 Core CLI 演示及预期 `VERIFIED` 结果；
4. 完整能力地图：耐久 Runtime、模型与上下文、受控工具与审批、文件 mutation、测试与
   最终验证、修复评测与 Pilot、A2 CLI；
5. 运行时、持久化、安全与验证组件的简明架构；
6. 可核对的实现与验证事实；
7. 明确的不做宣称和当前限制；
8. 分层文档导航、历史里程碑与证据索引、下一步路线。

README 使用中文；命令、类型名和固定状态枚举保留代码中的英文拼写。它不复制历史里程碑的
实现过程，而把每项当前能力链接到对应的设计、报告、评测或验收文档。

### 中文项目导读

新增 `docs/portfolio-guide.zh-CN.md`，面向投递和面试准备，提供：

- 30 秒项目介绍；
- 3 分钟演示顺序；
- 五个可展开的工程故事：耐久状态、审批副作用、fencing/恢复、验证证据、评测真实性；
- 面试官按时间预算的阅读路径；
- 真实模型、基准成绩与 OS 沙箱有关的诚实边界。

它是解释材料，不替代 README 或安全/架构事实来源。

### 操作、架构与兼容入口

- `docs/core-demo.md` 改写为中文纯操作手册，明确 fresh wheel、mock、跨进程审批与
  `VERIFIED` 成功条件。
- `docs/architecture.md` 保留英文深入设计；开头增加中文导读链接、适用读者及阅读顺序，
  不翻译历史细节。
- `README.zh-CN.md` 改为简短兼容入口，明确中文主 README 已迁移到根目录，并链接到中文
  项目导读，避免维护两份会漂移的项目主页。

## 可声明事实与边界

文档只能声明以下已核验事实：AgentApplication/CLI、SQLite durable state、模型 attempt
journal 与 context/budget、审批与跨进程 resume、lease/fencing、workspace baseline/diff、
可信测试 profile、verification capsule、受约束修复评测、离线 Pilot、真实模型 Study 基础
设施、fresh-wheel mock demo、静态检查和 CI 配置。

文档必须明确：

- `NON_HERMETIC` verification runtime 是完整性证据，不是 OS 级隔离；
- mock demo、离线矩阵和个别 canary 不构成官方 benchmark 或通用真实模型表现；
- 历史真实模型结果只按其冻结 manifest 和报告解释，未完成 schema-v2 study 不产生新的对外
  headline score；
- B（交互对话）和 C（证据发布）尚未开始；
- 真实模型运行需单独授权、凭据与成本预算。

禁止展示 secret、绝对 verifier 路径、隐藏测试内容、原始 provider 输出或未完成的真实模型
分数。

## 验收

1. 根 README 为中文，以当前能力地图介绍完整 AgentForge，并包含可运行的 Core demo 路径和
   边界说明；
2. 项目导读、demo 手册、评测指南和架构文档之间没有互相矛盾的能力或证据宣称；
3. 文档链接均指向现有受版本控制的文件；
4. 不新增可执行代码，不改变 runtime 行为；
5. 运行 Markdown 链接/关键事实检查，并执行 `git diff --check`。

## 非目标

本次不制作视频、图片或完整中文架构翻译；它们属于后续独立工作。也不合并分支、推送远端
或删除历史文档。
