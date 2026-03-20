#!/bin/bash

# Docker 自动安装与启动脚本
# 适用于 CentOS/RHEL 7/8 系统

set -e  # 遇到任何错误立即退出脚本

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 加载 .env 文件
load_env() {
    local env_file="${SCRIPT_DIR}/.env"
    if [[ -f "${env_file}" ]]; then
        echo "加载环境变量: ${env_file}"
        set -a
        source "${env_file}"
        set +a
    else
        echo "警告: 未找到 ${env_file}，将使用环境变量"
    fi

    # 验证必需的环境变量
    : "${OSS_ACCESS_KEY_ID:?环境变量 OSS_ACCESS_KEY_ID 未设置}"
    : "${OSS_ACCESS_KEY_SECRET:?环境变量 OSS_ACCESS_KEY_SECRET 未设置}"
    : "${DOCKER_USERNAME:?环境变量 DOCKER_USERNAME 未设置}"
    : "${DOCKER_PASSWORD:?环境变量 DOCKER_PASSWORD 未设置}"
}

# 检查是否以root用户运行
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "此脚本必须以root权限运行"
        exit 1
    fi
}

mount_disk() {
  if [[ -e /data ]]; then
      echo "/data 已挂载磁盘"
      return 0
  fi

  echo "挂载磁盘 /dev/nvme1n1 -> /data"
  mkfs -t ext4 /dev/nvme1n1
  mkdir /data && mount /dev/nvme1n1 /data

  # 开机启动
  echo "$(blkid /dev/nvme1n1 | awk '{print $2}' | sed 's/"//g')" /data ext4 defaults 0 0 >> /etc/fstab
}

install_yum() {
  # 检查是否已安装过（用 git 作为标志）
  if command -v git &> /dev/null; then
    echo "yum 工具包已安装"
    return 0
  fi

  echo "安装 yum 工具包"
  yum install -y gcc gcc-c++ tzdata which unzip tar tree git jq \
    procps-ng psmisc wget iputils net-tools telnet iotop lsof sysstat

  echo "安装 ossutil"
  cd /root
  curl -o ossutil.zip https://gosspublic.alicdn.com/ossutil/v2/2.1.2/ossutil-2.1.2-linux-amd64.zip
  unzip ossutil.zip && rm -f ossutil.zip
  echo "export PATH=/root/ossutil-2.1.2-linux-amd64:\$PATH" >> /root/.bashrc
  cat > /root/.ossutilconfig <<EOF
[default]
region=ap-northeast-1
endpoint=oss-ap-northeast-1-internal.aliyuncs.com
accessKeyID=${OSS_ACCESS_KEY_ID}
accessKeySecret=${OSS_ACCESS_KEY_SECRET}
EOF
}

install_conda() {
  if [[ ! -e /root/miniconda3 ]]; then
    echo "安装 conda"
    wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh
    bash miniconda.sh -b -u -p /root/miniconda3 && rm -f miniconda.sh
    source /root/miniconda3/bin/activate
    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
    conda init bash
  fi

  source /root/miniconda3/etc/profile.d/conda.sh
}

install_openhands() {
  if [[ -e /data/openhands ]]; then
      echo "openhands 已安装"
      return 0
  fi

  if ! conda env list | grep -q "^openhands "; then
    echo "安装 python"
    conda create -n openhands 'python==3.12' -y
    echo "conda activate openhands" >> /root/.bashrc
  else
    echo "conda 环境 openhands 已存在"
  fi

  echo "安装 openhands"
  mkdir -p /data/openhands && cd /data/openhands

  git clone https://git@github.com/fanzhidongyzby/benchmarks.git
  echo "cd /data/openhands/benchmarks" >> /root/.bashrc
  cd benchmarks && git checkout eve-680ce0f-v1.11.0 && mkdir runid

  # 激活 conda 环境并安装依赖
  conda activate openhands
  pip install uv requests
  make build

  # 切换到 v1.11.0 版本
  cd /data/openhands/benchmarks/vendor/software-agent-sdk/
  git remote add fork https://git@github.com/fanzhidongyzby/software-agent-sdk.git
  git fetch fork eve-v1.11.0:eve-v1.11.0 && git checkout eve-v1.11.0 && cd -
}

# 安装Docker
install_docker() {
    if command -v docker &> /dev/null; then
        echo "Docker 已安装: $(docker --version)"
        return 0
    fi

    echo "开始安装 Docker..."
    
    # 安装依赖包
    yum install -y yum-utils
    
    # 添加Docker官方仓库
    yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
    
    # 清理可能存在的旧文件
    [[ -d /usr/bin/docker ]] && rm -rf /usr/bin/docker

    # 安装Docker CE
    yum install -y docker-ce docker-ce-cli
    
    # 验证安装
    if ! command -v docker &> /dev/null; then
        echo "Docker 安装失败"
        exit 1
    fi
    echo "Docker 安装成功: $(docker --version)"
}

# 配置Docker数据目录
config_docker() {
    # 如果已经是软链接，说明已配置过
    if [[ -L /var/lib/docker ]]; then
        echo "Docker 数据目录已配置"
        return 0
    fi

    echo "配置 Docker 数据目录..."

    # 停止 Docker 服务（如果在运行）
    if systemctl is-active --quiet docker; then
        echo "停止 Docker 服务以迁移数据目录..."
        systemctl stop docker
    fi

    # 迁移 docker 数据目录到 /data
    if [[ -e /var/lib/docker ]]; then
        mv /var/lib/docker /var/lib/docker.bak
    fi
    mkdir -p /data/var/lib/docker
    ln -s /data/var/lib/docker /var/lib/docker

    # 迁移 buildkit 数据目录
    mkdir -p /data/var/lib/buildkit
    if [[ -e /var/lib/buildkit ]]; then
        if [[ ! -L /var/lib/buildkit ]]; then
          mv /var/lib/buildkit /var/lib/buildkit.bak
        fi
    else
        ln -s /data/var/lib/buildkit /var/lib/buildkit
    fi

    # 配置 daemon.json
    mkdir -p /etc/docker
    echo '{"data-root": "/var/lib/docker"}' > /etc/docker/daemon.json
}

# 启动Docker服务
start_docker() {
    if systemctl is-active --quiet docker; then
        echo "Docker 服务已在运行"
        return 0
    fi

    echo "启动 Docker 服务..."

    # 清理可能存在的异常 socket
    [[ -d /var/run/docker.sock ]] && rm -rf /var/run/docker.sock

    systemctl start docker
    systemctl enable docker

    if ! systemctl is-active --quiet docker; then
        echo "Docker 服务启动失败"
        exit 1
    fi
    echo "Docker 服务已成功启动"
}

# 验证Docker功能
test_docker() {
    echo "验证 Docker 功能..."
    docker info | grep 'Docker Root Dir'
    if docker ps &> /dev/null; then
        echo "Docker 功能验证成功"
    else
        echo "Docker 功能验证失败"
        exit 1
    fi
}

# 登录避免限流
login_docker() {
    echo "登录 Docker Hub..."
    docker login "docker.io" -u "${DOCKER_USERNAME}" -p "${DOCKER_PASSWORD}"
}

# 主函数
main() {
    check_root
    load_env
    mount_disk
    install_yum

    echo "开始安装 Openhands 环境..."
    install_conda
    install_openhands

    echo "开始检查 Docker 环境..."
    install_docker
    config_docker
    start_docker
    test_docker
    login_docker
    echo "Docker 环境就绪"
}

# 执行主函数
main "$@"
echo "安装完成！正在进入 openhands 环境..."
exec bash --login
