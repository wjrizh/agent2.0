#!/bin/bash
set -e

echo "================================================"
echo "  力工 Code (agent2.0) 一键安装脚本"
echo "================================================"

# 0. 基础环境检查
echo ""
echo "[1/6] 检查 Python 版本..."
python3 --version || { echo "❌ 未找到 Python3，请先安装 Python 3.10+"; exit 1; }
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "   Python $PY_VERSION ✓"

# 1. 创建/激活虚拟环境
VENV_DIR="$HOME/.ligong_venv"
if [ ! -d "$VENV_DIR" ]; then
    echo ""
    echo "[2/6] 创建虚拟环境于 $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    echo ""
    echo "[2/6] 虚拟环境已存在，跳过创建"
fi

source "$VENV_DIR/bin/activate"

# 2. 安装 Python 基础依赖
echo ""
echo "[3/6] 安装 Python 基础依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt

# 3. 安装 agent2.0 新增工具链依赖
echo ""
echo "[4/6] 安装 agent2.0 工具链依赖..."

# LSP 代码智能
pip install python-lsp-server -q

# RAG 语义搜索
pip install chromadb -q

# 磁力搜索浏览器 sniff 依赖
pip install markdownify -q

# 4. 安装 Playwright 浏览器引擎
echo ""
echo "[5/6] 安装 Playwright 浏览器引擎（可能需要几分钟）..."
python -m playwright install chromium

# 安装系统级浏览器依赖
python -m playwright install-deps chromium 2>/dev/null || echo "   系统依赖已安装或非 root，跳过"

# 5. 安装本项目（lg 命令）
echo ""
echo "[6/6] 安装 lg 命令..."
pip uninstall -y ligong 2>/dev/null || true
pip install -e .

# 6. 配置 PATH
echo ""
echo ">>> 配置 PATH..."

if ! grep -q "ligong_venv/bin" ~/.bashrc 2>/dev/null; then
    echo "" >> ~/.bashrc
    echo "# 力工 Code (agent2.0)" >> ~/.bashrc
    echo "export PATH=\"$VENV_DIR/bin:\$PATH\"" >> ~/.bashrc
    echo "   ~/.bashrc 已添加 ✓"
fi

if [ -f ~/.zshrc ] && ! grep -q "ligong_venv/bin" ~/.zshrc 2>/dev/null; then
    echo "" >> ~/.zshrc
    echo "# 力工 Code (agent2.0)" >> ~/.zshrc
    echo "export PATH=\"$VENV_DIR/bin:\$PATH\"" >> ~/.zshrc
    echo "   ~/.zshrc 已添加 ✓"
fi

# 7. 完成
echo ""
echo "================================================"
echo "  ✅ agent2.0 安装完成！"
echo "================================================"
echo ""
echo "安装内容概览："
echo "  • 核心依赖：openai, rich, pexpect, flask, requests, prompt_toolkit"
echo "  • 浏览器引擎：Playwright Chromium (支持 search/goto/sniff/search_magnet)"
echo "  • 代码智能：python-lsp-server (LSP 语法分析)"
echo "  • 语义搜索：chromadb (RAG 代码库索引)"
echo "  • HTML 清洗：markdownify (网页转 Markdown)"
echo "  • 执行引擎：PTY 流控 + sudo 密码自动注入 + 无畏模式"
echo ""
echo "启动方式："
echo "   关闭当前终端并重新打开，或执行 source ~/.bashrc"
echo "   然后输入 'lg' 即可启动力工 Code"
echo ""
echo "首次启动后建议："
echo "   1. /fearless on    # 开启无畏模式（命令自动执行）"
echo "   2. 让力工帮你 rag_tool build   # 构建代码库索引"
echo ""