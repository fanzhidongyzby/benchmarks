#!/root/miniconda3/envs/openhands/bin/python
# -*- coding: utf-8 -*-
"""
EVE SWE-bench 评测提交脚本

同步提交，异步执行。提交成功后立即返回，后台进程执行推理、评测、上传。

使用示例:
    # 完整流程（远程执行）
    TASK_ID=12345 LLM_CONFIG=$(jq -c . llm_config.json) /data/openhands/benchmarks/eve/submit.py

    # 跳过推理，只评测
    SKIP_INFER=1 TASK_ID=12345 /data/openhands/benchmarks/eve/submit.py

    # 指定实例
    INSTANCES=django__django-11333 TASK_ID=12345 LLM_CONFIG=$(jq -c . llm_config.json) /data/openhands/benchmarks/eve/submit.py

要求:
    - 必须以 root 权限运行
    - conda 环境会自动激活（无需手动 conda activate）
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Optional


# 检查并安装 requests
try:
    import requests
except ImportError:
    print("安装 requests 库...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# Conda 配置
CONDA_SH = "/root/miniconda3/etc/profile.d/conda.sh"
CONDA_ENV = "openhands"

# 升级后的 submit.py 路径
SUBMIT_SCRIPT = "/data/openhands/benchmarks/eve/submit.py"
INSTALL_SCRIPT = "/data/openhands/benchmarks/eve/install.sh"


# ============================================================================
# 配置
# ============================================================================


@dataclass
class Config:
    """评测配置"""

    task_id: str = "unknown"
    llm_config: str = ""
    llm_gemini: bool = False
    llm_timeout: int = 30
    llm_stream: bool = False
    llm_health_check: bool = True
    llm_retries: int = 3
    instances: str = ""
    skip_infer: bool = False
    max_iteration: int = 500
    infer_workers: int = 50
    eval_workers: int = 50
    max_eval_retries: int = 3
    instance_timeout_sec: float = 3600.0
    system_prompt_file: str = None
    eve_file: str = "eve_eval_result.json"
    oss_root: str = "oss://antllm-agentic-jp/ant-eve/swe-openhands"
    oss_prompts_root: str = oss_root + "/prompts"
    benchmarks_repo: str = "https://git@github.com/fanzhidongyzby/benchmarks.git"
    benchmarks_branch: str = "eve-680ce0f-v1.11.0"
    sdk_repo: str = "https://git@github.com/fanzhidongyzby/software-agent-sdk.git"
    sdk_branch: str = "eve-v1.11.0"

    # 路径
    script_dir: Path = field(default_factory=Path)
    root_dir: Path = field(default_factory=Path)
    log_file: Path = field(default_factory=Path)

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载配置"""
        script_dir = Path(__file__).parent.resolve()
        root_dir = script_dir.parent

        oss_root = os.environ.get(
            "OSS_ROOT", "oss://antllm-agentic-jp/ant-eve/swe-openhands"
        ).rstrip("/")
        oss_prompts_root: str = oss_root + "/prompts"

        return cls(
            task_id=os.environ.get("TASK_ID", "unknown"),
            llm_config=os.environ.get("LLM_CONFIG", ""),
            llm_gemini=os.environ.get("LLM_GEMINI", "").lower() in ["1", "y", "yes"],
            llm_timeout=int(os.environ.get("LLM_TIMEOUT", "30")),
            llm_stream=os.environ.get("LLM_STREAM", "").lower() in ["1", "y", "yes"],
            llm_health_check=os.environ.get("LLM_HEALTH_CHECK", "1").lower() in ["1", "y", "yes"],
            llm_retries=int(os.environ.get("LLM_RETRIES", "3")),
            instances=os.environ.get("INSTANCES", ""),
            skip_infer=os.environ.get("SKIP_INFER", "").lower() in ["1", "y", "yes"],
            max_iteration=int(os.environ.get("MAX_ITERATION", "500")),
            infer_workers=int(os.environ.get("INFER_WORKERS", "50")),
            eval_workers=int(os.environ.get("EVAL_WORKERS", "50")),
            instance_timeout_sec=float(os.environ.get("INSTANCE_TIMEOUT_SEC", "3600.0")),
            max_eval_retries=int(os.environ.get("MAX_EVAL_RETRIES", "3")),
            system_prompt_file=os.environ.get("SYSTEM_PROMPT_FILE", None),
            eve_file=os.environ.get("EVE_FILE", "eve_eval_result.json"),
            oss_root=oss_root,
            oss_prompts_root=oss_prompts_root,
            benchmarks_repo=os.environ.get("BENCHMARKS_REPO", "https://git@github.com/fanzhidongyzby/benchmarks.git"),
            benchmarks_branch=os.environ.get("BENCHMARKS_BRANCH", "eve-680ce0f-v1.11.0"),
            sdk_repo=os.environ.get("SDK_REPO", "https://git@github.com/fanzhidongyzby/software-agent-sdk.git"),
            sdk_branch=os.environ.get("SDK_BRANCH", "eve-v1.11.0"),
            script_dir=script_dir,
            root_dir=root_dir,
            log_file=root_dir / "submit.log",
        )

    @property
    def oss_bucket(self) -> str:
        return f"{self.oss_root}/{self.task_id}"

    def validate(self) -> None:
        """验证配置"""
        if self.task_id == "unknown":
            raise ValueError("环境变量 TASK_ID 未设置")
        if not self.skip_infer and not self.llm_config:
            raise ValueError("环境变量 LLM_CONFIG 未设置（SKIP_INFER 未设置时必需）")


# ============================================================================
# 前置检查
# ============================================================================


def check_root():
    """检查 root 权限"""
    if os.geteuid() != 0:
        print("错误: 此脚本必须以 root 权限运行")
        sys.exit(1)


def is_in_conda_env() -> bool:
    """检查是否在 conda openhands 环境中"""
    return os.environ.get("CONDA_DEFAULT_ENV") == CONDA_ENV


def cleanup_environment(config: Config):
    """清理环境：终止残留进程、容器和上次运行产物"""
    print("清理环境...")

    # 1. 终止 infer/eval 相关进程
    try:
        result = subprocess.run(
            ["pgrep", "-f", "(infer|eval)"], capture_output=True, text=True
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            current_pid = str(os.getpid())
            pids_to_kill = [pid for pid in pids if pid != current_pid]
            if pids_to_kill:
                subprocess.run(["kill", "-9"] + pids_to_kill, capture_output=True)
                print(f"  已终止 {len(pids_to_kill)} 个残留进程")
            else:
                print("  无残留进程")
        else:
            print("  无残留进程")
    except Exception as e:
        print(f"  清理进程时出错: {e}")

    # 2. 清理 Docker 容器
    try:
        result = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True)
        if result.stdout.strip():
            container_ids = result.stdout.strip().split("\n")
            subprocess.run(["docker", "rm", "-f"] + container_ids, capture_output=True)
            print(f"  已清理 {len(container_ids)} 个 Docker 容器")
        else:
            print("  无 Docker 容器")
    except Exception as e:
        print(f"  清理 Docker 容器时出错: {e}")

    # 3. 清理上次运行的产物文件
    cleanup_targets = [
        config.root_dir / "eval_outputs",
        config.root_dir / "infer.log",
        config.root_dir / "eval.log",
        config.root_dir / "submit.log",
        config.root_dir / config.eve_file,
    ]
    for target in cleanup_targets:
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    # runid 目录只清理内容，保留目录
    runid_dir = config.root_dir / "runid"
    if runid_dir.exists():
        for item in runid_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    print("  已清理上次运行产物")


# ============================================================================
# EVE 格式转换
# ============================================================================


def find_eval_outputs_dir(base_dir: Path) -> Path:
    """在 eval_outputs 目录下搜索 output.jsonl 文件"""
    pattern = str(base_dir / "eval_outputs" / "**" / "output.jsonl")
    matches = glob.glob(pattern, recursive=True)

    if not matches:
        raise FileNotFoundError(f"未找到 output.jsonl 文件，搜索路径: {pattern}")
    if len(matches) > 1:
        raise FileNotFoundError(f"找到多个 output.jsonl 文件: {matches}")

    return Path(matches[0]).parent


def load_jsonl(jsonl_path: Path) -> list:
    """读取 jsonl 文件"""
    instances = []
    if not jsonl_path.exists():
        return instances

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    instances.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"警告: 解析 JSON 行失败: {e}")
    return instances


def load_json(json_path: Path) -> Optional[dict]:
    """读取 JSON 文件"""
    if not json_path.exists():
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"警告: 读取 {json_path} 失败: {e}")
        return None


def calculate_time_cost(history: list) -> float:
    """计算时间消耗（秒）"""
    if not history or len(history) < 2:
        return 0.0
    try:
        start = datetime.fromisoformat(history[0]["timestamp"])
        end = datetime.fromisoformat(history[-1]["timestamp"])
        return round((end - start).total_seconds(), 2)
    except (KeyError, ValueError):
        return 0.0


def convert_to_eve_format(eval_dir: Path) -> dict:
    """将 output.jsonl 和 output.report.json 转换为 EVE 格式"""
    jsonl_path = eval_dir / "output.jsonl"
    report_path = eval_dir / "output.report.json"
    metadata_path = eval_dir / "metadata.json"

    report = load_json(report_path)
    instances = load_jsonl(jsonl_path)
    metadata = load_json(metadata_path)

    if report:
        resolved_ids = set(report.get("resolved_ids", []))
        total_instances = report.get("total_instances", 0)
        resolved_instances = report.get("resolved_instances", 0)
    else:
        resolved_ids = set()
        total_instances = len(instances) if instances else 0
        resolved_instances = 0
        report = {
            "total_instances": total_instances,
            "submitted_instances": len(instances),
            "completed_instances": len(instances),
            "resolved_instances": 0,
            "unresolved_instances": len(instances),
            "resolved_ids": [],
            "unresolved_ids": [inst.get("instance_id", "") for inst in instances],
            "error": "output.report.json not found",
        }

    score = (resolved_instances / total_instances * 100) if total_instances > 0 else 0.0

    details = []
    judge_details = []

    for idx, instance in enumerate(instances):
        instance_id = instance.get("instance_id", "")
        is_correct = instance_id in resolved_ids

        if metadata is None and idx == 0:
            metadata = instance.get("metadata")

        details.append(
            {
                "idx": idx,
                "instance_id": instance_id,
                "instance_patch": (instance.get("test_result") or {}).get(
                    "git_patch", ""
                ),
                "correct": is_correct,
            }
        )

        history = instance.get("history", [])
        prompt = instance.get("instruction", "")

        judge_details.append(
            {
                "idx": idx,
                "instance_id": instance_id,
                "prompt": prompt,
                "origin_prompt": prompt,
                "origin_prompt_hash": md5(prompt.encode("utf-8")).hexdigest(),
                "correct": is_correct,
                "is_multiturn": True,
                "instance_start_time": history[0].get("timestamp", "")
                if history
                else "",
                "instance_end_time": history[-1].get("timestamp", "")
                if history
                else "",
                "time_costs": calculate_time_cost(history),
                "ext_info": {"messages": history},
            }
        )

    return {
        "score": round(score, 2),
        "details": details,
        "judge_details": judge_details,
        "report": report,
        "metadata": metadata,
    }


def generate_eve_result(config: Config, error_message: Optional[str] = None) -> dict:
    """生成 EVE 结果文件（统一兜底逻辑）"""
    result = None

    if error_message is None:
        try:
            eval_dir = find_eval_outputs_dir(config.root_dir)
            print(f"找到评测输出目录: {eval_dir}")
            result = convert_to_eve_format(eval_dir)
        except FileNotFoundError as e:
            error_message = str(e)
            print(f"错误: {error_message}")
        except Exception as e:
            error_message = f"转换过程中发生异常: {e}"
            print(f"错误: {error_message}")

    # 统一兜底：生成空结果
    if result is None:
        print("生成空结果文件 (score=0)")
        result = {
            "score": 0.0,
            "details": [],
            "judge_details": [],
            "report": {
                "total_instances": 0,
                "resolved_instances": 0,
                "error": error_message or "unknown error",
            },
            "metadata": None,
        }

    eve_path = config.root_dir / config.eve_file
    with open(eve_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"EVE 结果文件: {eve_path}")
    print(f"得分: {result['score']}%")

    return result


# ============================================================================
# 命令执行
# ============================================================================


def run_command(
    cmd: list[str],
    log_file: Optional[Path] = None,
    filter_patterns: Optional[list[str]] = None,
) -> int:
    """执行命令，可选过滤输出"""
    print(f"执行: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    log_handle = open(log_file, "w", encoding="utf-8") if log_file else None

    try:
        assert process.stdout is not None
        for line in process.stdout:
            if log_handle:
                log_handle.write(line)
                log_handle.flush()

            if filter_patterns and any(p in line for p in filter_patterns):
                continue
            print(line, end="")

        process.wait()
        return process.returncode or 0
    finally:
        if log_handle:
            log_handle.close()


# ============================================================================
# 评测流程
# ============================================================================


def prepare_llm_config(config: Config):
    """[1/5] 准备 llm_config.json"""
    print("[1/5] 准备 llm_config.json...")

    if not config.llm_config:
        print("LLM_CONFIG 为空（SKIP_INFER 模式）")
        return

    llm_config = json.loads(config.llm_config)
    safe_config = {**llm_config, "api_key": "********"}
    print("LLM_CONFIG:")
    print(json.dumps(safe_config, indent=2))

    config_path = config.root_dir / "llm_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(llm_config, f, indent=2)


class LLMHealthCheckTimeout(Exception):
    """LLM 健康检查超时异常"""

    pass


def _check_llm_health(llm_config: dict, timeout: int = 30, stream: bool = False, retries: int = 3):
    """检查 LLM 服务是否可用

    Args:
        llm_config: LLM 配置字典
        timeout: 超时时间（秒），默认 30 秒
        stream: 是否使用流式请求，默认 False
        retries: 重试次数，默认 3 次，以防网络抖动
    """

    def timeout_handler(_signum, _frame):
        raise LLMHealthCheckTimeout(f"LLM 服务健康检查超时 ({timeout}秒)")

    print("检查 LLM 服务健康状态...")

    base_url = llm_config.get("base_url", "").rstrip("/")
    api_key = llm_config.get("api_key", "")
    model = llm_config.get("model", "")

    if not base_url:
        raise ValueError("LLM 配置缺少 base_url")
    if not api_key:
        raise ValueError("LLM 配置缺少 api_key")
    if not model:
        raise ValueError("LLM 配置缺少 model")

    # 去掉 litellm 的 provider 前缀（如 openai/、anthropic/ 等）
    if "/" in model:
        model = model.split("/", 1)[1]

    # 构建健康检查请求
    url = f"{base_url}/chat/completions"

    # 构建请求头
    extra_headers = llm_config.get("extra_headers", {})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    } | extra_headers

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ],
        "stream": stream,
    }

    print(f"请求 URL: {url}")
    print(f"模型: {model}")
    print(f"超时时间: {timeout} 秒")
    print(f"流式请求: {stream}")
    print(f"重试次数: {retries}")

    last_error = None
    for attempt in range(1, retries + 1):
        # 设置硬超时
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout)

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=(timeout, timeout),  # (连接超时, 读取超时)
            )
            response.raise_for_status()
            print(f"LLM 服务健康检查通过 (HTTP {response.status_code})")
            return  # 成功则直接返回

        except LLMHealthCheckTimeout as e:
            last_error = e
            print(f"第 {attempt}/{retries} 次尝试超时")

        except requests.exceptions.Timeout as e:
            last_error = RuntimeError(f"LLM 服务健康检查超时 ({timeout}秒)")
            print(f"第 {attempt}/{retries} 次尝试超时: {e}")

        except requests.exceptions.HTTPError as e:
            last_error = RuntimeError(f"LLM 服务健康检查失败: {e}")
            print(f"第 {attempt}/{retries} 次尝试失败: {e}")

        except requests.exceptions.ConnectionError as e:
            last_error = RuntimeError(f"LLM 服务连接失败: {e}")
            print(f"第 {attempt}/{retries} 次连接失败: {e}")

        except Exception as e:
            last_error = RuntimeError(f"LLM 服务健康检查异常: {e}")
            print(f"第 {attempt}/{retries} 次异常: {e}")

        finally:
            # 取消超时并恢复原有 handler
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        # 如果还有重试机会，等待后重试
        if attempt < retries:
            import time
            wait_time = attempt * 2  # 递增等待：2s, 4s, ...
            print(f"等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)

    # 所有重试都失败
    raise last_error


def download_system_prompt(config: Config):
    """从 OSS 下载自定义系统提示词文件到 prompts 目录"""
    if not config.system_prompt_file:
        return

    prompts_dir = (
        config.root_dir
        / "vendor"
        / "software-agent-sdk"
        / "openhands-sdk"
        / "openhands"
        / "sdk"
        / "agent"
        / "prompts"
    )
    oss_path = f"{config.oss_prompts_root}/{config.system_prompt_file}"
    target_path = prompts_dir / config.system_prompt_file

    print(f"从 OSS 下载系统提示词: {oss_path} -> {target_path}")

    if not shutil.which("ossutil"):
        raise RuntimeError("ossutil 不可用，无法下载系统提示词文件")

    result = subprocess.run(
        ["ossutil", "cp", "-f", oss_path, str(target_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"下载系统提示词失败: {result.stderr}")

    print(f"系统提示词下载完成: {target_path}")


def prepare_instances(config: Config):
    """[2/5] 准备实例列表"""
    if config.instances:
        print("[2/5] 准备实例列表 instances.txt...")
        instances_list = config.instances.split(",")
        instances_path = config.root_dir / "instances.txt"
        with open(instances_path, "w", encoding="utf-8") as f:
            f.write("\n".join(instances_list))
        print(f"共 {len(instances_list)} 个实例")
    else:
        print("[2/5] 未指定 INSTANCES，将使用全量数据集")


def run_inference(config: Config):
    """[3/5] 推理阶段"""
    print("[3/5] 开始推理...")

    if config.skip_infer:
        print("SKIP_INFER 已设置，跳过推理阶段")
        return

    cmd = [
        "uv",
        "run",
        "swebench-infer",
        "llm_config.json",
        "--max-iterations",
        str(config.max_iteration),
        "--num-workers",
        str(config.infer_workers),
        "--workspace",
        "docker",
    ]

    if config.instances:
        cmd.extend(["--select", "instances.txt"])
    else:
        cmd.extend(["--dataset", "princeton-nlp/SWE-bench_Verified", "--split", "test"])

    filter_patterns = [
        "benchmarks.utils.conversation",
        "• tail -f",
        "View live output:",
        "========",
    ]

    run_command(cmd, config.root_dir / "infer.log", filter_patterns)
    print("推理完成")


def run_evaluation(config: Config):
    """[4/5] 评测阶段"""
    print("[4/5] 开始评测...")

    pattern = str(config.root_dir / "eval_outputs" / "**" / "output.jsonl")
    matches = glob.glob(pattern, recursive=True)

    if not matches:
        print("警告: 未找到 output.jsonl 文件，跳过评测阶段")
        return

    output_jsonl = Path(matches[0])
    if output_jsonl.stat().st_size == 0:
        print("警告: output.jsonl 文件为空，跳过评测阶段")
        return

    print(f"找到输出文件: {output_jsonl}")

    runid_dir = config.root_dir / "runid"
    runid_dir.mkdir(exist_ok=True)

    cmd = [
        "uv",
        "run",
        "swebench-eval",
        str(output_jsonl),
        "--dataset",
        "princeton-nlp/SWE-bench_Verified",
        "--output-file",
        str(runid_dir / "results.swebench.jsonl"),
        "--workers",
        str(config.eval_workers),
        "--run-id",
        "runid",
        "--no-modal",
    ]

    ret = run_command(cmd, config.root_dir / "eval.log")
    if ret != 0:
        print("警告: 评测阶段出现错误")

    print("评测阶段完成")


# ============================================================================
# 上传
# ============================================================================


def upload_results(config: Config):
    """上传结果到 OSS"""
    print()
    print("=" * 50)
    print(f"上传结果到 OSS: {config.oss_bucket}")
    print("=" * 50)

    try:
        os.chdir(config.root_dir)
    except Exception:
        pass

    # 确保 EVE 结果文件存在（统一兜底）
    eve_path = config.root_dir / config.eve_file
    if not eve_path.exists():
        generate_eve_result(config, "任务异常终止，未生成结果文件")

    # 打包结果
    results_dir = config.root_dir / "results"
    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir()

    for src in ["eval_outputs", "runid"]:
        src_path = config.root_dir / src
        if src_path.exists():
            shutil.copytree(src_path, results_dir / src)

    for src in ["infer.log", "eval.log"]:
        src_path = config.root_dir / src
        if src_path.exists():
            shutil.copy(src_path, results_dir / src)

    if config.log_file.exists():
        shutil.copy(config.log_file, results_dir / "submit.log")

    tar_path = config.root_dir / "results.tgz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(results_dir, arcname="results")

    # 上传到 OSS
    if shutil.which("ossutil"):
        # 先上传 results.tgz 和 eve_eval_result.json（静默处理）
        subprocess.run(
            ["ossutil", "cp", "-f", str(tar_path), f"{config.oss_bucket}/"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["ossutil", "cp", "-f", str(eve_path), f"{config.oss_bucket}/"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("上传完成")
        print("=" * 50)

        # 最后上传 submit.log（静默处理，确保日志内容完整）
        sys.stdout.flush()
        subprocess.run(
            ["ossutil", "cp", "-f", str(config.log_file), f"{config.oss_bucket}/"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        print("警告: ossutil 不可用，跳过上传")
        print("=" * 50)

    # 清理本地临时文件
    try:
        if results_dir.exists():
            shutil.rmtree(results_dir)
        if tar_path.exists():
            tar_path.unlink()
        print("已清理本地临时文件")
    except Exception as e:
        print(f"清理临时文件时出错: {e}")


# ============================================================================
# Worker 进程（异步执行）
# ============================================================================


def worker_main(config: Config):
    """Worker 进程主函数"""
    print("=" * 50)
    print("EVE SWE-bench Worker 启动")
    print("=" * 50)
    print(f"TASK_ID:             {config.task_id}")
    print(f"开始时间:            {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 50)
    print("运行时配置:")
    print(f"  SKIP_INFER:          {config.skip_infer}")
    print(f"  INSTANCES:           {config.instances or '<全量数据集>'}")
    print(f"  MAX_ITERATION:       {config.max_iteration}")
    print(f"  INFER_WORKERS:       {config.infer_workers}")
    print(f"  EVAL_WORKERS:        {config.eval_workers}")
    print(f"  MAX_EVAL_RETRIES:    {config.max_eval_retries}")
    print(f"  INSTANCE_TIMEOUT_SEC:    {config.instance_timeout_sec}s")
    print("-" * 50)
    print("LLM 配置:")
    print(f"  LLM_GEMINI:          {config.llm_gemini}")
    print(f"  LLM_TIMEOUT:         {config.llm_timeout}s")
    print(f"  LLM_STREAM:          {config.llm_stream}")
    print(f"  LLM_HEALTH_CHECK:    {config.llm_health_check}")
    print(f"  LLM_RETRIES:         {config.llm_retries}")
    print("=" * 50)

    try:
        os.chdir(config.root_dir)
        prepare_llm_config(config)
        download_system_prompt(config)
        prepare_instances(config)
        run_inference(config)
        run_evaluation(config)

        print("[5/5] 生成 EVE 格式结果...")
        generate_eve_result(config)

        print("=" * 50)
        print("评测任务完成")
        print("=" * 50)

    except Exception as e:
        error_message = str(e)
        print(f"错误: {error_message}")
        generate_eve_result(config, error_message)

    finally:
        # 打印结束时间（在上传之前，确保日志完整）
        print()
        print("=" * 50)
        print(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 50)
        # 刷新 stdout，确保所有内容写入日志文件
        sys.stdout.flush()

        # 无论成功失败，都上传结果
        upload_results(config)


# ============================================================================
# 主入口
# ============================================================================


def main():
    """主函数"""
    # 检查 root 权限
    check_root()

    # 加载配置
    config = Config.from_env()

    # 验证配置
    try:
        config.validate()
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    # 如果是 worker 模式，直接执行
    if os.environ.get("_EVE_WORKER_MODE"):
        worker_main(config)
        return

    # 升级检查：运行 install.sh 重装环境，然后 exec 新的 submit.py
    if not os.environ.get("_EVE_UPGRADED"):
        print("=" * 50)
        print("检查并升级环境...")
        print("=" * 50)
        ret = subprocess.run(
            ["bash", "-c", f"source {CONDA_SH} && conda activate {CONDA_ENV} && bash {INSTALL_SCRIPT}"],
            stdin=subprocess.DEVNULL,
        )
        if ret.returncode != 0:
            print(f"警告: install.sh 执行失败 (exit {ret.returncode})，使用当前版本继续")
        else:
            # 标记已升级，exec 新的 submit.py
            os.environ["_EVE_UPGRADED"] = "1"
            new_submit = SUBMIT_SCRIPT
            print(f"升级完成，exec {new_submit}")
            os.execv(sys.executable, [sys.executable, new_submit])

    # 打印提交信息（环境配置）
    print("=" * 50)
    print("EVE SWE-bench 提交任务")
    print("=" * 50)
    print(f"TASK_ID:            {config.task_id}")
    print(f"提交时间:           {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 50)
    print("环境配置:")
    print(f"  BENCHMARKS_REPO:    {config.benchmarks_repo}")
    print(f"  BENCHMARKS_BRANCH:  {config.benchmarks_branch}")
    print(f"  SDK_REPO:           {config.sdk_repo}")
    print(f"  SDK_BRANCH:         {config.sdk_branch}")
    print(f"  OSS_ROOT:           {config.oss_root}")
    print(f"  OSS_BUCKET:         {config.oss_bucket}")
    print(f"  SYSTEM_PROMPT_FILE: {config.system_prompt_file or '<默认>'}")
    print(f"  EVE_FILE:           {config.eve_file}")
    print("=" * 50)

    # 清理环境
    cleanup_environment(config)

    # LLM 健康检查
    if config.llm_health_check and config.llm_config:
        try:
            llm_config = json.loads(config.llm_config)
            _check_llm_health(llm_config, timeout=config.llm_timeout, stream=config.llm_stream, retries=config.llm_retries)
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)

    # 启动后台 worker（通过 bash 激活 conda 环境）
    print("启动后台任务...")

    # 构建 bash 命令：激活 conda 环境后执行 python
    script_path = Path(__file__).resolve()

    # 将环境变量传递给子进程
    env_exports = []
    for key in [
        "TASK_ID",
        "LLM_CONFIG",
        "LLM_GEMINI",
        "LLM_TIMEOUT",
        "LLM_STREAM",
        "LLM_HEALTH_CHECK",
        "LLM_RETRIES",
        "INSTANCES",
        "SKIP_INFER",
        "MAX_ITERATION",
        "INFER_WORKERS",
        "EVAL_WORKERS",
        "INSTANCE_TIMEOUT_SEC",
        "SYSTEM_PROMPT_FILE",
        "EVE_FILE",
        "OSS_ROOT",
        "BENCHMARKS_BRANCH",
        "BENCHMARKS_REPO",
        "SDK_BRANCH",
        "SDK_REPO",
        "MAX_EVAL_RETRIES",
    ]:
        value = os.environ.get(key, "")
        if value:
            # 转义单引号
            escaped_value = value.replace("'", "'\"'\"'")
            env_exports.append(f"export {key}='{escaped_value}'")

    bash_cmd = f"""
source {CONDA_SH}
conda activate {CONDA_ENV}
cd {config.root_dir}
{chr(10).join(env_exports)}
export _EVE_WORKER_MODE=1
export PYTHONUNBUFFERED=1
python {script_path}
"""

    # 启动后台进程
    log_file = config.log_file
    with open(log_file, "w") as log_f:
        process = subprocess.Popen(
            ["bash", "-c", bash_cmd],
            stdin=subprocess.DEVNULL,  # 避免 "nohup: ignoring input"
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 脱离终端，等效于 nohup
        )

    print(f"后台进程已启动 (PID: {process.pid})")
    print(f"日志文件: {log_file}")
    print(f"查看日志: tail -f {log_file}")
    print()
    print("提交成功！后台任务将自动执行并上传结果到 OSS。")
    print("=" * 50)

    sys.exit(0)


if __name__ == "__main__":
    main()
