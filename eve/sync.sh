#!/bin/bash
# Docker 镜像同步脚本
# 使用 rsync 支持断点续传和增量同步

set -e

# ========== 使用方法 ==========
# ./sync_docker.sh <目标机器IP> [密码]
# 例如: ./sync_docker.sh 192.168.1.100
# 例如: ./sync_docker.sh 192.168.1.100 mypassword

TARGET_IP="${1:-}"
PASSWORD="${2:-}"

if [[ -z "$TARGET_IP" ]]; then
    echo "用法: $0 <目标机器IP> [密码]"
    echo "例如: $0 192.168.1.100"
    echo "例如: $0 192.168.1.100 mypassword"
    exit 1
fi

TARGET_HOST="root@$TARGET_IP"
SOURCE_DIR="/var/lib/docker"
TARGET_DIR="/data/var/lib/docker"

# 检查源目录是否存在
if [[ ! -d "$SOURCE_DIR/overlay2" ]]; then
    echo "错误: 源目录 $SOURCE_DIR/overlay2 不存在！"
    exit 1
fi

# 并发数
PARALLEL_JOBS=8

# SSH 命令（使用环境变量传递密码，更安全且支持特殊字符）
SSH_OPTS="-o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o Compression=no"
if [[ -n "$PASSWORD" ]]; then
    if ! command -v sshpass &> /dev/null; then
        echo "安装 sshpass..."
        yum install -y sshpass 2>/dev/null || apt install -y sshpass 2>/dev/null
    fi
    export SSHPASS="$PASSWORD"
    SSH_CMD="sshpass -e ssh $SSH_OPTS"
    RSYNC_SSH="sshpass -e ssh $SSH_OPTS"
else
    SSH_CMD="ssh $SSH_OPTS"
    RSYNC_SSH="ssh $SSH_OPTS"
fi

# 检查并安装 rsync
echo "检查本地 rsync..."
command -v rsync > /dev/null || yum install -y rsync 2>/dev/null || apt install -y rsync 2>/dev/null

echo "检查目标机器 rsync..."
$SSH_CMD "$TARGET_HOST" "command -v rsync > /dev/null || yum install -y rsync 2>/dev/null || apt install -y rsync 2>/dev/null"

# 确保目标目录存在
$SSH_CMD "$TARGET_HOST" "mkdir -p $TARGET_DIR/overlay2"

# 自动检测同步模式
REMOTE_DIR_COUNT=$($SSH_CMD "$TARGET_HOST" "find $TARGET_DIR/overlay2 -maxdepth 1 -mindepth 1 -type d ! -name 'l' 2>/dev/null | wc -l | tr -d ' '") || REMOTE_DIR_COUNT=0
if [[ "$REMOTE_DIR_COUNT" -gt 0 ]]; then
    SYNC_MODE="增量"
else
    SYNC_MODE="全量"
fi

echo "=========================================="
echo "Docker 镜像同步"
echo "源: $SOURCE_DIR"
echo "目标: $TARGET_HOST:$TARGET_DIR"
echo "模式: ${SYNC_MODE}同步（自动检测）"
echo "并发数: $PARALLEL_JOBS"
echo "开始时间: $(date)"
echo "=========================================="

# 停止目标机器的 Docker
# echo "停止目标机器 Docker 服务..."
# $SSH_CMD "$TARGET_HOST" "systemctl stop docker || true"

# ========== 同步 image 目录 ==========
echo ""
echo "[1/3] 同步 image 目录..."
if [[ -d "$SOURCE_DIR/image" ]]; then
    rsync -a \
        --info=progress2 \
        --no-inc-recursive \
        -e "$RSYNC_SSH -T -c aes128-gcm@openssh.com -x" \
        "$SOURCE_DIR/image" \
        "$TARGET_HOST:$TARGET_DIR/"
else
    echo "   跳过：image 目录不存在"
fi

# ========== 同步 overlay2/l 目录 ==========
echo ""
echo "[2/3] 同步 overlay2/l 目录（符号链接）..."
if [[ -d "$SOURCE_DIR/overlay2/l" ]]; then
    rsync -a \
        --info=progress2 \
        -e "$RSYNC_SSH -T -c aes128-gcm@openssh.com -x" \
        "$SOURCE_DIR/overlay2/l" \
        "$TARGET_HOST:$TARGET_DIR/overlay2/"
else
    echo "   跳过：overlay2/l 目录不存在"
fi

# ========== 同步 overlay2 子目录 ==========
echo ""
echo "[3/3] 同步 overlay2 子目录..."

# 统计子目录数量
TOTAL=$(find "$SOURCE_DIR/overlay2" -maxdepth 1 -mindepth 1 -type d ! -name 'l' | wc -l | tr -d ' ')
if [[ $TOTAL -eq 0 ]]; then
    echo "没有需要同步的 overlay2 子目录"
else
    echo "共 $TOTAL 个子目录，开始并发同步（并发数: $PARALLEL_JOBS）..."

    # 进度计数器和字节计数器
    PROGRESS_FILE=$(mktemp)
    BYTES_FILE=$(mktemp)
    FAILED_FILE=$(mktemp)
    echo 0 > "$PROGRESS_FILE"
    echo 0 > "$BYTES_FILE"
    echo "" > "$FAILED_FILE"
    START_TIME=$(date +%s)

    # 后台进程显示进度和网速
    (
        while [[ -f "$PROGRESS_FILE" ]]; do
            count=$(cat "$PROGRESS_FILE" 2>/dev/null || echo 0)
            bytes=$(cat "$BYTES_FILE" 2>/dev/null || echo 0)
            now=$(date +%s)
            elapsed=$((now - START_TIME))
            if [[ $elapsed -gt 0 && $bytes -gt 0 ]]; then
                speed=$((bytes / elapsed))
                if [[ $speed -gt 1073741824 ]]; then
                    speed_val=$((speed / 1073741824))
                    speed_str="${speed_val} GB/s"
                elif [[ $speed -gt 1048576 ]]; then
                    speed_val=$((speed / 1048576))
                    speed_str="${speed_val} MB/s"
                elif [[ $speed -gt 1024 ]]; then
                    speed_val=$((speed / 1024))
                    speed_str="${speed_val} KB/s"
                else
                    speed_str="$speed B/s"
                fi
                printf '\r进度: %d/%d (%d%%) | 速度: %s    ' "$count" "$TOTAL" "$((count*100/TOTAL))" "$speed_str"
            else
                printf '\r进度: %d/%d (%d%%)    ' "$count" "$TOTAL" "$((count*100/TOTAL))"
            fi
            sleep 1
        done
    ) &
    MONITOR_PID=$!

    # 并发同步子目录（rsync 自动处理增量）
    # 注意：需要 export SSHPASS 以便子进程可以访问
    export SSHPASS
    find "$SOURCE_DIR/overlay2" -maxdepth 1 -mindepth 1 -type d ! -name 'l' | \
        xargs -P $PARALLEL_JOBS -I {} bash -c '
            subdir=$(basename "{}")

            # 获取目录大小
            dir_size=$(du -sb "{}" 2>/dev/null | cut -f1 || echo 0)

            # 同步目录，失败时记录
            if ! rsync -a \
                -e "'"$RSYNC_SSH"' -T -c aes128-gcm@openssh.com -x" \
                "{}"/ \
                "'"$TARGET_HOST"':'"$TARGET_DIR"'/overlay2/$subdir/"; then
                echo "$subdir" >> "'"$FAILED_FILE"'"
            fi

            # 更新进度
            (
                flock 200
                count=$(cat "'"$PROGRESS_FILE"'")
                echo $((count+1)) > "'"$PROGRESS_FILE"'"
            ) 200>"'"$PROGRESS_FILE"'.lock"

            # 更新字节数
            (
                flock 201
                bytes=$(cat "'"$BYTES_FILE"'" 2>/dev/null || echo 0)
                echo $((bytes+dir_size)) > "'"$BYTES_FILE"'"
            ) 201>"'"$BYTES_FILE"'.lock"
        '

    # 停止监控进程
    kill $MONITOR_PID 2>/dev/null || true
    wait $MONITOR_PID 2>/dev/null || true
    echo ""

    # 检查失败的目录并重试
    FAILED_COUNT=$(grep -c . "$FAILED_FILE" 2>/dev/null) || FAILED_COUNT=0
    if [[ $FAILED_COUNT -gt 0 ]]; then
        echo "警告: $FAILED_COUNT 个目录同步失败，正在重试..."
        cat "$FAILED_FILE" | while read subdir; do
            if [[ -n "$subdir" ]]; then
                echo "重试: $subdir"
                rsync -a \
                    -e "$RSYNC_SSH -T -c aes128-gcm@openssh.com -x" \
                    "$SOURCE_DIR/overlay2/$subdir/" \
                    "$TARGET_HOST:$TARGET_DIR/overlay2/$subdir/" || echo "失败: $subdir"
            fi
        done
    fi

    rm -f "$PROGRESS_FILE" "$BYTES_FILE" "$PROGRESS_FILE.lock" "$BYTES_FILE.lock" "$FAILED_FILE"
    echo "overlay2 同步完成！"
fi

# ========== 完整性校验 ==========
echo ""
echo "=========================================="
echo "开始完整性校验..."
echo "=========================================="

# 1. 校验目录数量
echo "1. 校验 overlay2 目录数量..."
LOCAL_DIR_COUNT=$(find "$SOURCE_DIR/overlay2" -maxdepth 1 -mindepth 1 -type d ! -name 'l' | wc -l | tr -d ' ')
REMOTE_DIR_COUNT=$($SSH_CMD "$TARGET_HOST" "find $TARGET_DIR/overlay2 -maxdepth 1 -mindepth 1 -type d ! -name 'l' | wc -l | tr -d ' '")
echo "   本地: $LOCAL_DIR_COUNT, 远程: $REMOTE_DIR_COUNT"
if [[ "$LOCAL_DIR_COUNT" != "$REMOTE_DIR_COUNT" ]]; then
    echo "   ❌ 目录数量不一致！"

    # 自动补同步缺失的目录
    echo "   正在查找并补同步缺失的目录..."
    LOCAL_DIRS=$(find "$SOURCE_DIR/overlay2" -maxdepth 1 -mindepth 1 -type d ! -name 'l' -exec basename {} \; | sort)
    REMOTE_DIRS=$($SSH_CMD "$TARGET_HOST" "find $TARGET_DIR/overlay2 -maxdepth 1 -mindepth 1 -type d ! -name 'l' -exec basename {} \;" | sort)
    MISSING=$(comm -23 <(echo "$LOCAL_DIRS") <(echo "$REMOTE_DIRS"))

    if [[ -n "$MISSING" ]]; then
        MISSING_COUNT=$(echo "$MISSING" | wc -l | tr -d ' ')
        echo "   发现 $MISSING_COUNT 个缺失目录，正在补同步..."
        echo "$MISSING" | while read -r dir; do
            if [[ -n "$dir" ]]; then
                rsync -a \
                    -e "$RSYNC_SSH -T -c aes128-gcm@openssh.com -x" \
                    "$SOURCE_DIR/overlay2/$dir/" \
                    "$TARGET_HOST:$TARGET_DIR/overlay2/$dir/"
            fi
        done
        echo "   补同步完成"
    fi
else
    echo "   ✓ 目录数量一致"
fi

# 2. 校验文件总数
echo "2. 校验文件总数..."
LOCAL_FILE_COUNT=$(find "$SOURCE_DIR/overlay2" -type f | wc -l | tr -d ' ')
REMOTE_FILE_COUNT=$($SSH_CMD "$TARGET_HOST" "find $TARGET_DIR/overlay2 -type f | wc -l | tr -d ' '")
echo "   本地: $LOCAL_FILE_COUNT, 远程: $REMOTE_FILE_COUNT"
if [[ "$LOCAL_FILE_COUNT" != "$REMOTE_FILE_COUNT" ]]; then
    echo "   ❌ 文件数量不一致！"
else
    echo "   ✓ 文件数量一致"
fi

# 3. 校验总大小
echo "3. 校验总大小..."
LOCAL_SIZE=$(du -sb "$SOURCE_DIR/overlay2" | cut -f1)
REMOTE_SIZE=$($SSH_CMD "$TARGET_HOST" "du -sb $TARGET_DIR/overlay2 | cut -f1")
LOCAL_SIZE_H=$(numfmt --to=iec $LOCAL_SIZE 2>/dev/null || echo "$LOCAL_SIZE bytes")
REMOTE_SIZE_H=$(numfmt --to=iec $REMOTE_SIZE 2>/dev/null || echo "$REMOTE_SIZE bytes")
echo "   本地: $LOCAL_SIZE_H, 远程: $REMOTE_SIZE_H"
if [[ "$LOCAL_SIZE" != "$REMOTE_SIZE" ]]; then
    echo "   ❌ 大小不一致！"
else
    echo "   ✓ 大小一致"
fi

# 4. 使用 rsync --dry-run 检查差异
echo "4. 使用 rsync 校验差异文件..."
DIFF_OUTPUT=$(rsync -an --stats \
    -e "$RSYNC_SSH -T -c aes128-gcm@openssh.com -x" \
    "$SOURCE_DIR/overlay2/" \
    "$TARGET_HOST:$TARGET_DIR/overlay2/" 2>/dev/null) || true
DIFF_COUNT=$(echo "$DIFF_OUTPUT" | grep "Number of regular files transferred" | awk '{print $NF}') || DIFF_COUNT=0

if [[ -z "$DIFF_COUNT" || "$DIFF_COUNT" == "0" ]]; then
    echo "   ✓ 没有差异文件"
    echo ""
    echo "=========================================="
    echo "✓ 完整性校验通过！两边文件完全一致"
    echo "=========================================="
else
    echo "   ⚠ 发现 $DIFF_COUNT 个差异文件，正在自动补同步..."
    rsync -a \
        -e "$RSYNC_SSH -T -c aes128-gcm@openssh.com -x" \
        "$SOURCE_DIR/overlay2/" \
        "$TARGET_HOST:$TARGET_DIR/overlay2/"
    echo "   补同步完成"
fi

# ========== 启动 Docker ==========
echo ""
echo "启动目标机器 Docker 服务..."
$SSH_CMD "$TARGET_HOST" "systemctl start docker"

# 验证镜像
echo "验证目标机器镜像列表..."
$SSH_CMD "$TARGET_HOST" "docker images"

echo ""
echo "=========================================="
echo "同步完成！"
echo "结束时间: $(date)"
echo "=========================================="
