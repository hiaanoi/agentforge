# AgentForge GitHub 演示录像设计

## 目标

为 GitHub 项目主页准备一支可静音观看的中文演示视频。视频应在两到三分钟内让首次访问仓库的
招聘者理解 AgentForge 解决的工程问题，并用真实的公开 Core CLI 路径证明：Run 可以跨进程等待
审批、恢复受控副作用，并在最终验证后给出 `VERIFIED` 结论。

## 范围与交付物

本轮只产出录制与发布材料，不录制、不渲染、不提交视频二进制，也不修改运行时行为：

1. 新增中文录制脚本与清单，包含逐镜头时间轴、屏幕字幕、终端操作、隐私检查和重录标准；
2. 在根 `README.md` 增加“演示视频”入口与发布占位约定；
3. 在中文项目导读中链接录制说明；
4. 为文档入口与关键事实增加轻量回归检查。

视频文件在录制完成后发布到 GitHub Release 或其他公开视频页；README 使用静态封面图或文字链接
跳转，不把大体积视频提交到 Git 历史。

## 目标观众与形式

目标观众是浏览 GitHub 的招聘者和技术面试官。视频为 16:9 横屏、1080p、约 2 分 40 秒；不使用
真人旁白，以简短中文字幕、少量标题页和真实终端输出推进。所有终端画面来自项目已有的公开 CLI、
Mock provider 与 Core demo，不手工伪造事件或状态。

字幕使用中文，命令、类型名和固定状态值保留代码中的英文拼写。每帧字幕最多两行；敏感或不稳定的
长日志不作为信息载体。

## 视频结构

| 时间 | 画面 | 字幕要点 | 证据边界 |
| --- | --- | --- | --- |
| 0:00–0:12 | 标题页 | “耐久、可审计、策略受控的代码修复 Agent Runtime” | 不宣称模型能力或 benchmark 成绩。 |
| 0:12–0:28 | 简短架构页 | CLI、SQLite durable state、approval、lease/fencing、verification evidence 的关系 | 只画当前已实现的 A2 Core。 |
| 0:28–0:48 | `trust --show` 与 `trust --yes` | 测试 profile 是精确身份；公开输出使用安全投影 | 不展示 verifier 绝对路径、secret 或原始环境。 |
| 0:48–1:18 | `exec`、`PAUSED`、新终端的 `approve`/`resume` | 副作用先经审批；新进程继续同一 Run | 清楚保留 Run ID 与 approval ID。 |
| 1:18–1:52 | 修改后可见测试的审批/恢复 | receipt、lease/fencing 防止重复驱动 | 不把单次过程夸大为通用修复能力。 |
| 1:52–2:20 | 最终 verification 的审批/恢复 | verification 绑定证据；漂移或不确定时不输出 `VERIFIED` | 标明 runtime 为 `NON_HERMETIC`。 |
| 2:20–2:40 | `run_finished ... outcome=VERIFIED` 与收束页 | “可恢复、可审计、证据驱动” | 明确该演示使用 Mock provider。 |

## 录制流程

录制者在干净终端中执行已验证的公开脚本。脚本创建临时 workspace 和独立 verifier 目录，并通过多个
独立的 `agentforge` CLI 进程完成 trust、exec、approve 与 resume。录制时可以使用剪辑缩短等待和
隐藏冗余日志，但不得重排状态顺序、伪造终端输出或删除终态失败信息。

录制前先运行 `uv build`，然后运行对应平台的 `scripts/demo_core.ps1` 或 `scripts/demo_core.sh` 以
确认环境。实际拍摄可依据脚本的同一路径分段执行，以便在画面中保留每个审批边界。成功标准是最终
终端事件包含 `run_finished ... lifecycle=TERMINAL outcome=VERIFIED`；仅出现 `PAUSED`、`FAILED`、
`UNVERIFIED` 或 `UNKNOWN` 时必须重录或如实展示为失败排查片段，不能作为成品成功画面。

## 发布与 README 约定

README 的视频区域使用一个稳定的外链占位：在视频完成前显示“录制中”和录制指南链接；发布后替换为
Release 资产或公开视频页 URL。封面图只提交经审查的轻量 PNG/WebP，文件名固定为
`docs/assets/demo-video-cover.png`；若封面尚未制作，README 不引用不存在的文件，而使用文字链接。

Release 说明需要写明：演示使用 Mock provider；它证明的是产品状态机、审批和恢复路径，不代表真实
模型表现或官方 benchmark 分数；`NON_HERMETIC` 不是 OS 沙箱。

## 隐私与重录门槛

录制前、中、后执行以下检查：

- 只使用临时 workspace、Mock provider 和无凭据 shell；
- 终端不得显示 `.env`、API key、token、用户目录、verifier 绝对路径、隐藏测试内容或原始 provider
  输出；
- 操作系统通知、浏览器标签、用户名与本地项目路径应关闭或裁切；
- 使用可读字体和固定窗口尺寸，字幕与终端文本均可在 1080p 下阅读；
- 导出前逐帧检查 Run ID 以外的标识符和所有错误输出；
- 终态不是 `VERIFIED`、任一敏感信息可见、或跨进程审批没有被画面证明时，整段重录。

## 验收

1. 录制说明能让另一位开发者在不读私有代码的前提下复现镜头流程；
2. README 与项目导读均提供录制/视频入口，且不链接不存在的二进制资源；
3. 所有叙述与 `docs/core-demo.md`、安全模型和当前 README 的事实边界一致；
4. 文档测试覆盖入口与 `VERIFIED`、Mock、`NON_HERMETIC` 等关键边界；
5. 不修改运行时代码、数据库结构或 CLI 行为。

## 非目标

本次不生成或上传视频、封面图、配音、GIF、Release、GitHub 仓库或外部链接；也不开始 B（交互对话）
或 C（证据发布）功能开发。
