#!/bin/bash
set -e

echo "================================================"
echo "  力工 Code 一键安装脚本"
echo "================================================"

# 1. 检查 Python 版本
python3 --version || { echo "❌ 未找到 Python3，请先安装 Python 3.8+"; exit 1; }

# 2. 创建虚拟环境
VENV_DIR="$HOME/.ligong_venv"
if [ ! -d "$VENV_DIR" ]; then
    echo ">>> 创建虚拟环境于 $VENV_DIR"
    python3 -m venv "$VENV_DIR"
else
    echo ">>> 虚拟环境已存在，跳过创建"
fi

# 3. 激活并安装依赖
source "$VENV_DIR/bin/activate"
echo ">>> 安装 Python 依赖..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. 安装 Playwright 浏览器（Chromium）
echo ">>> 安装 Playwright 浏览器（可能需要几分钟）..."
python -m playwright install chromium

# 5. 以可编辑模式重新安装本项目（先卸载旧版本）
echo ">>> 安装/更新 lg 命令..."
pip uninstall -y ligong 2>/dev/null || true
pip install -e .

# 6. 确保虚拟环境的 bin 目录在 PATH（临时添加，永久需手动配 shell）
if ! grep -q "ligong_venv/bin" ~/.bashrc 2>/dev/null; then
    echo "" >> ~/.bashrc
    echo "# 力工 Code" >> ~/.bashrc
    echo "export PATH=\"$VENV_DIR/bin:\$PATH\"" >> ~/.bashrc
    echo ">>> 已将虚拟环境路径添加到 ~/.bashrc"
fi

if ! grep -q "ligong_venv/bin" ~/.zshrc 2>/dev/null; then
    echo "" >> ~/.zshrc
    echo "# 力工 Code" >> ~/.zshrc
    echo "export PATH=\"$VENV_DIR/bin:\$PATH\"" >> ~/.zshrc
    echo ">>> 已将虚拟环境路径添加到 ~/.zshrc"
fi

echo ""
echo "✅ 安装完成！请关闭当前终端并重新打开，或执行："
echo "   source ~/.bashrc   (若使用 bash)"
echo "   source ~/.zshrc    (若使用 zsh)"
echo "之后在任意目录输入 'lg' 即可启动力工 Code。"