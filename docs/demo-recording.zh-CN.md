# AgentForge GitHub 演示录像：录制脚本与清单

本指南用于制作一支约 **2 分 40 秒**、16:9、1080p、无真人旁白的中文 GitHub 项目演示视频。
它只使用公开 `agentforge` CLI 与本地 Mock provider，展示产品状态机、审批与跨进程恢复；它不代表
真实模型能力、通用代码修复能力或官方 benchmark 成绩。

视频应以终端实录为主，辅以一张短架构页和中文短字幕。每条字幕最多两行；命令、状态值和类型名沿用
代码中的英文拼写。录制路径以 [Core CLI 演示](core-demo.md) 为唯一事实来源，不调用私有 API，
不使用 `python -m agentforge`，也不接入真实 provider。

## 成片规格

- 比例与清晰度：16:9、1920×1080；终端字体在 1080p 下清晰可读；
- 时长：约 2 分 40 秒；等待、安装和冗余日志可剪短，但不能重排状态顺序；
- 声音：无真人旁白；可无声播放，信息由标题页、字幕和真实 CLI 输出共同表达；
- 叙事重点：耐久状态、审批边界、跨进程恢复、lease/fencing 与最终验证证据；
- 终态标准：成片必须展示 `run_finished ... lifecycle=TERMINAL outcome=VERIFIED`。

## 逐镜头脚本

| 时间 | 画面与操作 | 中文字幕（最多两行） | 保留的真实证据 | 不得做出的宣称 |
| --- | --- | --- | --- | --- |
| 0:00–0:12 | 标题页，项目名和一句定位 | “耐久、可审计、策略受控的代码修复 Agent Runtime”<br>“关注副作用、恢复与验证证据” | 仓库名与项目定位 | 不说“通用修复 Agent”或真实模型成绩。 |
| 0:12–0:28 | 简短架构页：CLI → receipt → Runtime → approval / lease / SQLite / verification evidence | “副作用先经审批，再由耐久状态驱动”<br>“重启和并发接管也保留可审计事实” | 与 README 一致的组件关系 | 不把 `NON_HERMETIC` 描述为 OS 沙箱。 |
| 0:28–0:48 | 终端执行 `agentforge trust --show`，再执行 `agentforge trust --yes` | “测试 profile 是精确身份，不是任意命令”<br>“公开输出只展示安全投影” | profile ID、purpose、digest 和 trust 成功结果 | 不显示 verifier 绝对路径、环境变量或 secret。 |
| 0:48–1:18 | `agentforge exec`；保留 `PAUSED`、Run ID 与 approval ID；切到新终端执行 `approve`、`resume` | “文件修改是受审批的副作用”<br>“新进程恢复同一 Run，而非依赖内存 loop” | 同一 Run ID、approval ID、`PAUSED` 和恢复后的事件 | 不称一次 CLI 执行为“自动完成一切”。 |
| 1:18–1:52 | 显示可见测试的 approval/resume；可用 `approvals` 或 `inspect` 强调耐久事实 | “测试同样受 profile、receipt 与 lease/fencing 约束”<br>“重放命令不会重复驱动同一副作用” | 测试审批、新的 CLI process、对应 Run 事件 | 不展示隐藏验证输入或原始 provider 输出。 |
| 1:52–2:20 | 显示 final verification 的 approval/resume 与 `inspect` 安全投影 | “最终验证绑定 source、verifier 与 process evidence”<br>“完整性漂移或不确定时不会输出 VERIFIED” | verification purpose、证据状态与最终事件前的审批边界 | 不把 `NON_HERMETIC` 说成隔离执行环境。 |
| 2:20–2:40 | 定格 `run_finished ... outcome=VERIFIED`；收束页列出“可恢复、可审计、证据驱动” | “VERIFIED 是 policy、diff 与 verification evidence 共同成立的结论”<br>“本演示使用 Mock provider” | 终态 `VERIFIED` 和项目链接 | 不把 Mock 流程当作真实模型或 benchmark 成绩。 |

## 录制前验证

在无凭据的干净 shell 中先验证公开流程。Windows：

```powershell
uv build
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/demo_core.ps1
```

Linux/macOS：

```sh
uv build
sh ./scripts/demo_core.sh
```

脚本本身会创建临时 workspace 与独立 verifier 目录，并在完成后删除它们。只有脚本输出
`Core demo completed for run_id=<uuid>` 且最终事件包含 `outcome=VERIFIED` 时，才可以开始正式录制。

## 终端操作

正式录制可按照 [Core CLI 演示](core-demo.md) 的同一公开命令分段执行，以便保留 trust、exec、
approval、resume、visible test 与 final verification 的边界。每个 `approve` 和后续 `resume` 都应在
新的终端进程中执行；画面必须能让观看者识别同一个 Run ID 被跨进程恢复。

允许通过剪辑压缩等待时间、移除重复日志，并在标题页中预先展示架构；不允许手工伪造 CLI 输出、
重排审批与状态转换、隐藏终态失败，或改用私有 Python API、真实 provider 与未记录的自定义脚本。

## 隐私检查

- [ ] 只使用临时 workspace、Mock provider 和无凭据 shell；
- [ ] 画面不含 `.env`、API key、token、密码、cookie、用户目录或真实项目路径；
- [ ] 不显示 verifier 绝对路径、隐藏测试内容、私有 manifest、原始 provider 响应或环境变量；
- [ ] 关闭桌面通知、浏览器标签、聊天窗口和可能暴露身份的窗口标题；
- [ ] Run ID 与公开 digest 可以保留；不确定的日志和错误输出应裁切或重录；
- [ ] 字幕、终端字体和最终 `VERIFIED` 在 1080p 下可读。

## 重录判定

出现任一情况时，成品段落必须重录：

- 终态不是 `run_finished ... lifecycle=TERMINAL outcome=VERIFIED`；
- 没有清楚展示至少一个跨进程 approval/resume 边界；
- `PAUSED`、`FAILED`、`UNVERIFIED`、`UNKNOWN` 被剪辑后误呈现为成功；
- 出现 secret、用户目录、verifier 路径、隐藏测试、原始 provider 输出或未审核错误文本；
- 字幕声称真实模型能力、通用修复能力、官方 benchmark 成绩或 OS 级沙箱。

失败片段可以作为单独的工程排查材料保留，但不能替代项目主页中的成功演示。

## GitHub 发布

视频本体应上传到 GitHub Release 或经过审核的公开视频页；README 只放外链，不把 MP4、录屏工程或
大体积二进制提交到 Git 历史。视频尚未发布时，README 只链接本指南，不引用不存在的封面图或视频 URL。

发布说明必须包含以下事实：

- 该演示使用 Mock provider，证明的是产品状态机、审批和恢复路径；
- `VERIFIED` 是 policy、workspace diff 与 verification evidence 的结论，不是单一测试退出码；
- verification runtime 为 `NON_HERMETIC`：它提供完整性证据，不是 OS 级隔离沙箱；
- 演示不代表真实模型表现、通用修复能力或官方 benchmark 分数。

推荐在 GitHub 上先创建私有仓库并推送已验证的本地材料，再录制并上传视频，最后把 README 的文字入口
替换为审核后的外链后公开仓库。

## 发布前人工验收

- [ ] 用无凭据 shell 重新跑过 Core demo，结果含 `outcome=VERIFIED`；
- [ ] 已逐帧检查终端、窗口标题和字幕，没有 secret、用户目录、verifier 路径或隐藏测试；
- [ ] 已确认视频说明写明 Mock provider 与 `NON_HERMETIC` 边界；
- [ ] 已把视频上传到 GitHub Release 或已审核的视频页；
- [ ] 已将 README 的文字入口替换为审核后的外链，且未提交视频二进制。
