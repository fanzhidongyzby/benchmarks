#!/bin/bash

# OpenHands 升级脚本
# 适用于 CentOS/RHEL 7/8 系统

set -e  # 遇到任何错误立即退出脚本

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检查是否以root用户运行
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "此脚本必须以 root 权限运行"
        exit 1
    fi
}

# 清理函数 - 升级失败时回滚
cleanup_on_failure() {
    local backup_dir="$1"
    if [[ -n "$backup_dir" && -d "$backup_dir" ]]; then
        log_warn "升级失败，正在回滚..."
        rm -rf /data/openhands
        mv "$backup_dir" /data/openhands
        log_info "已恢复到备份版本"
    fi
}

upgrade_openhands() {
    local install_dir="/data/openhands"
    local backup_dir=""

    if [[ ! -e "$install_dir" ]]; then
        log_error "openhands 未安装，请先使用 install.sh 初始化"
        return 1
    fi

    # 创建备份
    backup_dir="${install_dir}.bak-$(date +%Y%m%d%H%M%S)"
    log_info "备份当前版本到 $backup_dir"
    mv "$install_dir" "$backup_dir"

    # 设置失败时自动回滚
    trap "cleanup_on_failure '$backup_dir'" ERR

    mkdir -p "$install_dir"
    cd "$install_dir"

    log_info "克隆 benchmarks 仓库..."
    git clone https://github.com/fanzhidongyzby/benchmarks.git
    cd benchmarks
    git checkout eve-680ce0f-v1.11.0
    mkdir -p runid

    # 激活 conda 环境并安装依赖
    log_info "激活 conda 环境并安装依赖..."
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate openhands
    pip install uv requests
    make build

    # 切换到 v1.11.0 版本
    log_info "配置 software-agent-sdk..."
    cd "$install_dir/benchmarks/vendor/software-agent-sdk/"
    git remote add fork https://github.com/fanzhidongyzby/software-agent-sdk.git 2>/dev/null || true
    git fetch fork eve-v1.11.0:eve-v1.11.0
    git checkout eve-v1.11.0

    # 重新构建镜像
    log_info "开始构建 Docker 镜像..."
    cd "$install_dir/benchmarks"
    export IGNORE_REMOTE_IMAGE=1

    uv run python -m benchmarks.swebench.build_images \
        --dataset princeton-nlp/SWE-bench_Verified \
        --split test \
        --image ghcr.io/openhands/eval-agent-server \
        --target source-minimal \
        2>&1 | tee image.log

    # 清理旧备份（保留最近 3 个备份）
    log_info "清理旧备份..."
    # shellcheck disable=SC2012
    ls -dt /data/openhands.bak-* 2>/dev/null | tail -n +4 | xargs -r rm -rf

    # 取消 trap
    trap - ERR
    log_info "升级完成！"
}

# 主函数
main() {
    check_root
    upgrade_openhands
}

# 执行主函数
main "$@"

log_info "正在进入 openhands 环境..."
exec bash --login