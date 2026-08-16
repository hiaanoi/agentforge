# DeepSeek + SWE-bench 单实例 Canary / One-instance Canary

本手册在腾讯云 CVM 上运行一个有界的 `sympy__sympy-20590` canary。它验证 DeepSeek provider、
AgentForge 的审批/恢复链、标准预测导出和官方 SWE-bench harness 能否串通。它不是排行榜成绩，
也不能证明通用代码修复能力。

This runbook connects the DeepSeek provider, AgentForge's durable approval/resume path, the standard
prediction exporter, and the official SWE-bench harness for one instance. It is not a leaderboard
score or a general repair-quality claim.

## 0. 冻结输入 / Frozen inputs

- AgentForge：记录本次实际使用的 Git commit；
- SWE-bench：`4e6126978a16bdfebc6538db8f28cacc2c8b77dc`；
- instance：`sympy__sympy-20590`；
- repo：`sympy/sympy`；
- base commit：`cffd4e0f86fefd4802349a9f9b19ed70934ea354`；
- 硬件建议：x86_64、8 vCPU、32 GiB RAM、200 GiB 系统盘、Docker；
- 并发：始终为 `1`。

先设置不含秘密的路径，并确认当前 AgentForge 源码版本：

```bash
export AGENTFORGE_ROOT=/home/ubuntu/agentforge
export SWEBENCH_ROOT=/home/ubuntu/swebench-lab-4e612697/SWE-bench-4e6126978a16bdfebc6538db8f28cacc2c8b77dc
export CANARY_ROOT=/home/ubuntu/agentforge-deepseek-canary
mkdir -p "$CANARY_ROOT"
git -C "$AGENTFORGE_ROOT" rev-parse HEAD | tee "$CANARY_ROOT/agentforge-commit.txt"
```

将上面的固定 SWE-bench commit 原样写入 `swebench-commit.txt`。codeload 压缩包没有 `.git`，不要
用 `main` 代替固定 commit。

## 1. 安全输入 Key 并做最小 live canary / Safe credential entry

先在 DeepSeek 控制台创建单独的低额度 API Key。终端隐藏输入，不把 Key 写入 history、配置文件或
命令行参数：

```bash
cd "$AGENTFORGE_ROOT"
read -rsp 'DeepSeek API key: ' DEEPSEEK_API_KEY && printf '\n'
export DEEPSEEK_API_KEY
```

用认证后的模型列表取得账号当前可用的精确 model ID；不要从本文复制一个可能已经变化的 ID：

```bash
uv run --frozen python - <<'PY'
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
for item in client.models.list().data:
    print(item.id)
PY
read -rp 'Exact DeepSeek model id: ' DEEPSEEK_MODEL
export DEEPSEEK_MODEL
```

先只发一个最多 400 output tokens 的只读请求：

```bash
DEEPSEEK_LIVE_TEST=1 uv run --frozen pytest tests/live/test_deepseek_live.py -q
```

只有该命令通过才继续。默认执行测试套件时，因为没有 `DEEPSEEK_LIVE_TEST=1`，此测试必须显示为
`skipped`，不会调用远程 API。

## 2. 从已验证的官方镜像复制工作区 / Materialize the official workspace

此前的 gold smoke 已留下一个包含 `/testbed` 的官方实例镜像。先找到它：

```bash
IMAGE=$(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep 'sympy-20590' | head -n 1)
test -n "$IMAGE"
printf 'image=%s\n' "$IMAGE" | tee "$CANARY_ROOT/instance-image.txt"
```

若 `IMAGE` 为空，先按固定 SWE-bench commit 重跑一次该实例的 gold smoke；不要改用未知第三方镜像。
复制时如果目标目录已经存在就停止，以免覆盖旧证据：

```bash
export WORKSPACE="$CANARY_ROOT/sympy__sympy-20590"
test ! -e "$WORKSPACE"
mkdir "$WORKSPACE"
CID=$(docker create "$IMAGE" true)
docker cp "$CID:/testbed/." "$WORKSPACE"
docker rm "$CID"
sudo chown -R "$(id -u):$(id -g)" "$WORKSPACE"
git -C "$WORKSPACE" diff --quiet cffd4e0f86fefd4802349a9f9b19ed70934ea354 HEAD
git -C "$WORKSPACE" checkout --detach cffd4e0f86fefd4802349a9f9b19ed70934ea354
test "$(git -C "$WORKSPACE" rev-parse HEAD)" = cffd4e0f86fefd4802349a9f9b19ed70934ea354
printf '.agentforge/\n' >> "$WORKSPACE/.git/info/exclude"
```

只把公开的 `problem_statement` 保存为任务文本，不保存 `patch` 或 `test_patch`，避免把标准答案泄漏给
Agent：

```bash
export HF_ENDPOINT=https://hf-mirror.com
"$SWEBENCH_ROOT/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
from datasets import load_dataset

rows = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
row = next(item for item in rows if item["instance_id"] == "sympy__sympy-20590")
assert row["base_commit"] == "cffd4e0f86fefd4802349a9f9b19ed70934ea354"
target = Path(os.environ["CANARY_ROOT"]) / "problem-statement.txt"
target.write_text(row["problem_statement"].strip() + "\n", encoding="utf-8")
PY
```

`HF_ENDPOINT` 只用于数据集传输，不能作为模型 API 代理。

## 3. 创建固定的容器测试入口 / Fixed container test entry points

AgentForge 不接受任意 shell test profile。下面两个由操作员审查的 Python 入口只运行固定镜像、固定挂载
和固定测试文件；API Key 不会传入容器。

```bash
export PROFILE_RUNNER="$CANARY_ROOT/development_test.py"
export VERIFIER_ROOT="$CANARY_ROOT/verifier"
PYTHON_REAL=$(realpath "$(command -v python3)")
mkdir -p "$VERIFIER_ROOT"
cat > "$PROFILE_RUNNER" <<PY
#!$PYTHON_REAL
import os
import subprocess
import sys

source = os.path.realpath(sys.argv[1])
image = "$IMAGE"
command = [
    os.path.realpath("/usr/bin/docker"), "run", "--rm", "--network", "none",
    "--mount", f"type=bind,source={source},target=/workspace,readonly",
    "--workdir", "/workspace", image,
    "/bin/bash", "-lc",
    "source /opt/miniconda3/bin/activate && conda activate testbed && "
    "export PYTHONDONTWRITEBYTECODE=1 && "
    "bin/test -C --verbose sympy/core/tests/test_sympify.py",
]
raise SystemExit(subprocess.run(command, check=False, timeout=900).returncode)
PY
cp "$PROFILE_RUNNER" "$VERIFIER_ROOT/__main__.py"
chmod 700 "$PROFILE_RUNNER" "$VERIFIER_ROOT/__main__.py"
```

镜像身份写入受审查的固定入口。development profile 直接绑定该入口的文件 digest；verification
profile 会把 verifier 目录作为独立 capsule 捕获。官方 harness 仍会在最后独立应用并评测导出的补丁。

## 4. 配置有界 AgentForge Run / Configure a bounded run

```bash
mkdir -p "$WORKSPACE/.agentforge"
cat > "$WORKSPACE/.agentforge/config.toml" <<EOF
database_path = ".agentforge/agentforge.db"
model = "$DEEPSEEK_MODEL"
max_steps = 20
profile_ids = ["visible", "verify"]
EOF
cat > "$WORKSPACE/.agentforge/runtime.toml" <<EOF
[provider]
kind = "deepseek"

[policy]
task_id = "swebench-sympy-20590"
policy_version = 1
difficulty = "CHALLENGE"
budget_profile = "CHALLENGE"
allowed_write_paths = ["sympy/**"]
forbidden_write_paths = [".git/**", ".agentforge/**"]
protected_paths = ["sympy/**/tests/**"]
allowed_development_test_profiles = ["visible"]
final_verification_profile_id = "verify"
allow_file_creation = true
allowed_create_paths = ["sympy/**"]
max_created_files = 2
max_changed_files = 6
max_total_changed_bytes = 1048576
max_single_file_changed_bytes = 1048576
path_case_sensitive = true

[model_budget]
max_model_requests = 14
max_retries = 1
max_output_tokens_per_request = 4096
max_total_tokens = 120000

[[profiles]]
profile_id = "visible"
name = "SWE-bench visible SymPy test"
description = "Fixed containerized development test"
executable = "$PROFILE_RUNNER"
argv = ["$WORKSPACE"]
cwd = "."
timeout_seconds = 930
max_output_bytes = 200000
profile_version = 1
purpose = "development"
allowed_env = {}

[[profiles]]
profile_id = "verify"
name = "SWE-bench bounded verifier"
description = "Evaluator-owned fixed containerized verification"
executable = "$PYTHON_REAL"
argv = ["{VERIFIER}", "{SOURCE}"]
cwd = "."
timeout_seconds = 930
max_output_bytes = 200000
profile_version = 1
purpose = "verification"
verifier_root = "$VERIFIER_ROOT"
allowed_env = {}
EOF
```

配置文件只含 provider 类型和模型 ID；Key 只能来自进程环境。先诊断并显式信任两个 profile：

```bash
cd "$AGENTFORGE_ROOT"
uv run --frozen agentforge doctor --workspace "$WORKSPACE"
uv run --frozen agentforge trust --workspace "$WORKSPACE" visible --yes
uv run --frozen agentforge trust --workspace "$WORKSPACE" verify --yes
```

## 5. 运行、逐项审批并恢复 / Run, approve, and resume

```bash
uv run --frozen agentforge exec --workspace "$WORKSPACE" "$(cat "$CANARY_ROOT/problem-statement.txt")"
```

保存输出中的 `run_id`。出现 `approval_required` 后，不要批量自动批准；逐项查看、批准并恢复：

```bash
uv run --frozen agentforge approvals --workspace "$WORKSPACE" --run-id RUN_ID
uv run --frozen agentforge approve --workspace "$WORKSPACE" APPROVAL_ID
uv run --frozen agentforge resume --workspace "$WORKSPACE" RUN_ID
```

每次只替换当前输出中的 `RUN_ID` 和 `APPROVAL_ID`，直到终端出现 `run_finished`。若结果不是
`outcome=VERIFIED`，仍保留事件和 diff，但不要把它描述为成功。

## 6. 导出标准 prediction / Export the standard prediction

先审查状态。若模型有意创建了源文件，逐个使用 `git add -N -- relative/path` 标为
intent-to-add；不要执行笼统的 `git add .`。除此以外，只要存在 `??` 文件，导出器就会拒绝继续：

```bash
git -C "$WORKSPACE" status --short
export PREDICTION="$CANARY_ROOT/predictions.jsonl"
cd "$AGENTFORGE_ROOT"
uv run --frozen python evaluation/export_swebench_prediction.py \
  --workspace "$WORKSPACE" \
  --instance-id sympy__sympy-20590 \
  --repo sympy/sympy \
  --base-commit cffd4e0f86fefd4802349a9f9b19ed70934ea354 \
  --model-identity "deepseek/$DEEPSEEK_MODEL" \
  --output "$PREDICTION"
sha256sum "$PREDICTION" | tee "$CANARY_ROOT/predictions.sha256"
```

导出器固定执行三个 Git 查询，限制合并 stdout/stderr 为 1 MiB，并验证 `HEAD` 仍等于绑定的 base
commit。JSONL 只含官方 harness 的三个标准字段，不包含 Key、原始 provider 响应或隐藏测试。

## 7. 官方 harness 单实例评测 / Official one-instance evaluation

```bash
cd "$SWEBENCH_ROOT"
export HF_ENDPOINT=https://hf-mirror.com
uv run --frozen python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --split test \
  --instance_ids sympy__sympy-20590 \
  --predictions_path "$PREDICTION" \
  --max_workers 1 \
  --timeout 1800 \
  --run_id agentforge-deepseek-canary-20260816
```

只在报告同时满足 `completed=1`、`resolved=1`、`errors=0` 时称该实例 resolved。`resolved=0` 是有效
的模型质量失败；镜像、网络或容器失败则单独归类为基础设施失败，不能混入成功率。

## 8. 收尾、哈希与关机 / Close out

```bash
docker ps --format 'table {{.ID}}\t{{.Image}}\t{{.Status}}'
docker system df
find "$CANARY_ROOT" -maxdepth 3 -type f \
  \( -name '*.json' -o -name '*.jsonl' -o -name '*.txt' -o -name '*.sha256' \) \
  -print0 | sort -z | xargs -0 sha256sum > "$CANARY_ROOT/artifact-sha256.txt"
unset DEEPSEEK_API_KEY
unset DEEPSEEK_MODEL
```

确认没有遗留运行中的 SWE-bench 容器后，在腾讯云控制台选择“关机不收费”。不要执行未经审查的
`docker system prune -a`，因为它会删除后续复核需要的镜像缓存。

## 结果声明模板 / Claim template

- 成功：`AgentForge completed one DeepSeek-backed SWE-bench Lite canary and the pinned official
  harness resolved sympy__sympy-20590.`
- 失败：`AgentForge completed the canary pipeline, but the single prediction was unresolved.`
- 基础设施失败：`The canary did not produce a scoreable model-quality result because the official
  harness infrastructure failed.`

任何一种情况都要同时报告 AgentForge/SWE-bench commit、精确模型 ID、instance ID、预测文件 SHA-256
和 harness run ID。
