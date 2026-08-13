# Core CLI 演示

本演示验证 A2 Core 的公开产品路径：从全新 wheel 安装的 `agentforge` CLI 创建一个耐久 Run，
跨进程审批并恢复受控副作用，最后得到 `VERIFIED`。它使用本地 Mock provider，不调用远程模型，
也不需要 API key。

## 前提

- Python 3.14.x 与 [uv](https://docs.astral.sh/uv/)；
- 当前仓库可构建 wheel；
- PowerShell 或 POSIX shell；
- 仅使用仓库中的公开 `agentforge` console script，不使用 `python -m agentforge` 或私有 API。

先构建：

```powershell
uv build
```

## 运行 Core 路径

Windows：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/demo_core.ps1
```

Linux/macOS：

```sh
sh ./scripts/demo_core.sh
```

脚本会执行以下步骤：

1. 创建临时 workspace 和独立 verifier 目录；
2. 写入只含 Mock provider、repair policy 和两个 profile 的临时 runtime definition；
3. 通过 `agentforge trust --yes` 信任精确的 visible/verification profile identity；
4. 运行 `agentforge exec`，等待 `edit_file` approval；
5. 由新的 CLI process `approve` 后 `resume` 同一个 Run；
6. 对 visible test 与 final verification 重复这个跨进程 approval/resume 路径；
7. 断言源码修复存在，并要求终端事件为
   `run_finished ... lifecycle=TERMINAL outcome=VERIFIED`；
8. 删除临时 workspace 和 verifier 目录。

成功时脚本输出：

```text
Core demo completed for run_id=<uuid>
```

若脚本没有得到 `outcome=VERIFIED`，它会以失败状态退出；不能把中途 `PAUSED`、`FAILED` 或
`UNVERIFIED` 当作 demo 成功。

## 运行恢复路径

恢复脚本重复同一条公开跨进程路径，用来展示“开始、审批、恢复”并不依赖同一个内存进程：

Windows：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/demo_recovery.ps1
```

Linux/macOS：

```sh
sh ./scripts/demo_recovery.sh
```

## 公开退出语义

CLI 的稳定退出语义为：

| Exit code | 含义 |
| --- | --- |
| `0` | 命令完成；Run 可能已到 terminal，也可能按命令语义已完成一次 phase |
| `20` | Run 正等待人工审批 |
| `21` | 用户 Ctrl-C 后安全 detach；Run 没有被隐式取消 |
| `22` | 结果未知或不确定，不能诚实地报告成功 |
| `30` | 配置、profile identity 或产品装配被拒绝 |

可用 `agentforge inspect --workspace <path> --run-id <uuid>` 查看安全投影；用
`agentforge approvals --workspace <path> --run-id <uuid>` 查询 pending approval。

## 这项演示证明什么

- wheel 内的 console entry point 可被 fresh environment 安装和运行；
- Start、审批、Resume、测试和最终验证跨多个 CLI process 仍共享 durable state；
- 每个副作用受 policy、approval、receipt、lease/fencing 与 source/profile binding 约束；
- final `VERIFIED` 不是单纯的测试退出码，而是 repair policy、workspace diff 与 verification
  evidence 共同成立后的结论。

## 限制

verification runtime 标记为 `NON_HERMETIC`。它通过受控 capsule、digest 和前后重验提供完整性
证据，但不是 OS 级隔离沙箱；它不解决同账户高权限外部进程对文件系统的攻击。

本 demo 的 Mock provider 只证明产品状态机与恢复语义。它不构成真实模型表现、通用代码修复
能力或官方 benchmark 成绩。真实模型运行需要独立授权、凭据和成本预算。
