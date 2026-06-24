#!/usr/bin/env bash
# glm-quota 安装脚本 —— 把文件部署到本机路径，配置开机自启。
set -euo pipefail

CONFIG_DIR="$HOME/.config/glm-quota"
SHARE_DIR="$HOME/.local/share/glm-quota"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$HOME/.config/autostart"

mkdir -p "$CONFIG_DIR" "$SHARE_DIR" "$BIN_DIR" "$AUTOSTART_DIR"

# 核心模块
install -m644 glm_quota_core.py "$SHARE_DIR/glm_quota_core.py"

# 可执行脚本
install -m755 glm-quota      "$BIN_DIR/glm-quota"
install -m755 glm-quota-tray "$BIN_DIR/glm-quota-tray"

# 开机自启
install -m644 glm-quota-tray.desktop "$AUTOSTART_DIR/glm-quota-tray.desktop"

# 配置：若已存在则保留，否则从模板创建(权限600)
if [ ! -f "$CONFIG_DIR/config.json" ]; then
    install -m600 config.example.json "$CONFIG_DIR/config.json"
    echo "✓ 已创建配置模板: $CONFIG_DIR/config.json"
    echo "  请编辑该文件填入你的 api_key"
else
    echo "→ 配置已存在，保留不变: $CONFIG_DIR/config.json"
fi

echo
echo "安装完成。"
echo "  命令行查用量:   glm-quota"
echo "  启动托盘:       glm-quota-tray &"
echo "  设置 api_key:   glm-quota set-key  (或直接编辑上面那个 config.json)"
