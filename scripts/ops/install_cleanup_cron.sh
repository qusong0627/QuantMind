#!/bin/bash
# QuantMind Backtest History Cleanup Cron Installer

# 1. 设置基础路径
PROJECT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
echo "⚠️  该安装脚本已废弃，数据由官方服务器统一管理，无需本地定时清理。"
exit 0

echo "🔍 正在配置计划任务..."

# 2. 检查环境
if [ ! -f "$PYTHON_EXEC" ]; then
    echo "❌ 错误: 未找到虚拟环境 Python: $PYTHON_EXEC"
    exit 1
fi

if [ ! -f "$SCRIPT_PATH" ]; then
    echo "❌ 错误: 未找到清理脚本: $SCRIPT_PATH"
    exit 1
fi

# 3. 构建 Cron 表达式 (每天 0:00 执行，保留每人 10 条)
# 注意：使用绝对路径以确保环境无关性
CRON_ENTRY="0 0 * * * cd $PROJECT_ROOT && $PYTHON_EXEC $SCRIPT_PATH --keep 10 >> $LOG_PATH 2>&1"

# 4. 检查是否已存在
(crontab -l 2>/dev/null | grep -Fq "$SCRIPT_PATH")
if [ $? -eq 0 ]; then
    echo "ℹ️  计划任务已存在，正在更新配置..."
    # 删除旧的并添加新的
    (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH"; echo "$CRON_ENTRY") | crontab -
else
    echo "🆕 正在添加新计划任务..."
    (crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -
fi

echo "✅ 配置成功！"
echo "📅 任务计划：每天 00:00 (自动执行: keep 10)"
echo "📝 日志位置：$LOG_PATH"
echo "💡 您可以通过 'crontab -l' 查看已配置的任务。"
