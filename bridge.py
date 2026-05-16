# bridge.py
import json
import time
import os
import sys
import re
import signal  # 添加 signal 模块导入
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# 全局组件
PLAYWRIGHT = None
BROWSER = None
CONTEXT = None
PAGE = None

# 停止请求标志
STOP_REQUESTED = False

def handle_sigint(signum, frame):
    """拦截 Ctrl+C，防止被主进程株连秒杀"""
    global STOP_REQUESTED
    print("\n>>> [BRIDGE] 拦截到终端 Ctrl+C (SIGINT)，保持后台存活，强制打断当前任务...")
    STOP_REQUESTED = True

# 注册信号拦截
signal.signal(signal.SIGINT, handle_sigint)

# ================= 新增：模式与深度思考控制函数 =================
def ensure_expert_mode():
    """初始化时确保进入专家模式并开启深度思考"""
    print(">>> 正在检查并切换到专家模式 + 深度思考...")
    # 1. 专家模式
    expert_radio = PAGE.query_selector('div[data-model-type="expert"][role="radio"]')
    if expert_radio:
        is_expert_selected = expert_radio.get_attribute('aria-checked') == 'true'
        if not is_expert_selected:
            print("  切换到专家模式...")
            expert_radio.click()
            PAGE.wait_for_timeout(500)  # 等待切换动画
        else:
            print("  已经是专家模式")
    else:
        print("  警告：未找到专家模式按钮")

    # 2. 深度思考
    deep_think_btn = PAGE.query_selector('div[role="button"][class*="ds-toggle-button"]')
    if deep_think_btn:
        # 检查是否已激活（一般会有 aria-pressed 属性，如果没有，则通过样式类判断）
        is_active = (
            deep_think_btn.get_attribute('aria-pressed') == 'true' or
            'ds-toggle-button--active' in deep_think_btn.get_attribute('class')
        )
        if not is_active:
            print("  开启深度思考...")
            deep_think_btn.click()
            PAGE.wait_for_timeout(500)
        else:
            print("  深度思考已开启")
    else:
        print("  警告：未找到深度思考按钮")

def set_mode_and_deepthink(mode: str, deep_think: str = None):
    """
    动态切换模式和深度思考
    mode: 'fast' 或 'expert'
    deep_think: 'on' 或 'off' (可选)
    """
    messages = []
    # 切换模式
    if mode == 'expert':
        target_radio = PAGE.query_selector('div[data-model-type="expert"][role="radio"]')
    elif mode == 'fast':
        target_radio = PAGE.query_selector('div[data-model-type="default"][role="radio"]')
    else:
        return f"无效的模式: {mode}"

    if not target_radio:
        return "未找到模式切换按钮"

    is_already = target_radio.get_attribute('aria-checked') == 'true'
    if not is_already:
        target_radio.click()
        PAGE.wait_for_timeout(500)
        messages.append(f"已切换到{'专家' if mode=='expert' else '快速'}模式")
    else:
        messages.append(f"已经是{'专家' if mode=='expert' else '快速'}模式")

    # 深度思考开关
    if deep_think is not None:
        deep_btn = PAGE.query_selector('div[role="button"][class*="ds-toggle-button"]')
        if not deep_btn:
            messages.append("未找到深度思考按钮")
            return "\n".join(messages)

        is_active = (
            deep_btn.get_attribute('aria-pressed') == 'true' or
            'ds-toggle-button--active' in deep_btn.get_attribute('class')
        )
        if deep_think == 'on' and not is_active:
            deep_btn.click()
            PAGE.wait_for_timeout(500)
            messages.append("深度思考已开启")
        elif deep_think == 'off' and is_active:
            deep_btn.click()
            PAGE.wait_for_timeout(500)
            messages.append("深度思考已关闭")
        else:
            messages.append(f"深度思考已经是{'开启' if deep_think=='on' else '关闭'}状态")

    return "\n".join(messages)

# ------------------------------------------------------------

def init_browser():
    global PLAYWRIGHT, BROWSER, CONTEXT, PAGE
    print(">>> 启动 Playwright 引擎...")
    try:
        PLAYWRIGHT = sync_playwright().start()
        state_file = os.path.join(os.path.dirname(__file__), "state.json")

        if not os.path.exists(state_file):
            # 没有登录状态，直接退出，让上层处理
            print(">>> 未找到登录状态，请先运行 'lg --login' 完成首次登录。")
            sys.exit(1)

        # 有登录状态，正常无头启动
        BROWSER = PLAYWRIGHT.chromium.launch(headless=False)
        CONTEXT = BROWSER.new_context(storage_state=state_file)
        PAGE = CONTEXT.new_page()
        PAGE.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=60000)

        # 等待输入框就绪
        try:
            PAGE.wait_for_selector("textarea", timeout=30000)
        except Exception:
            PAGE.reload(wait_until="domcontentloaded", timeout=30000)
            PAGE.wait_for_selector("textarea", timeout=30000)

        # ---- 新增：确保进入专家模式 + 深度思考 ----
        ensure_expert_mode()

        print(">>> 聊天页面就绪")
    except Exception as e:
        print(f">>> 启动失败: {e}")
        sys.exit(1)

def ask_deepseek(prompt_text):
    global STOP_REQUESTED
    STOP_REQUESTED = False   # 每次新请求重置停止标志
    print(f"\n>>> [BRIDGE] 开始注入指令 (DOM直取模式)...")
    
    # 1. 安全输入
    box = PAGE.wait_for_selector("textarea", timeout=5000)
    box.fill("")
    box.fill(prompt_text)
    PAGE.keyboard.press("Enter")
    time.sleep(1.5)
    
    # 2. 事务性执行：死循环监控状态，抵抗网络波动 (全新心跳监测机制)
    print(">>> [BRIDGE] 正在监控生成状态...")
    last_text_length = 0
    stable_ticks = 0
    
    while True:
        time.sleep(1)
        
        if STOP_REQUESTED:
            print(">>> [BRIDGE] 收到停止信号！")
            # 点击停止按钮（使用现有的多种选择器）
            stop_btn = PAGE.query_selector("div[role='button']:has-text('停止生成'), .ds-stop-button, [aria-label*='Stop' i], svg.stop-icon")
            if stop_btn and stop_btn.is_visible():
                stop_btn.click()
                print(">>> [BRIDGE] 已点击停止按钮。")
            STOP_REQUESTED = False
            break  # 跳出监控循环
        
        # 获取当前最新气泡的纯文本长度
        current_len = PAGE.evaluate("""() => {
            const msgs = document.querySelectorAll('.ds-message');
            return msgs.length > 0 ? msgs[msgs.length - 1].innerText.length : 0;
        }""")
        
        # 监测字数是否还在增长
        if current_len > 0 and current_len == last_text_length:
            stable_ticks += 1
        else:
            stable_ticks = 0
            last_text_length = current_len
            
        # 兼容最新版网页所有的停止按钮可能形态
        stop_btn = PAGE.query_selector("div[role='button']:has-text('停止生成'), .ds-stop-button, [aria-label*='Stop' i], svg.stop-icon")
        
        if stop_btn and stop_btn.is_visible():
            stable_ticks = 0 # 只要看到停止按钮，不管字数涨没涨，强制重置心跳
            continue
            
        # 如果长度连续 3 秒一动不动，且没有任何停止按钮，判定为输出彻底结束
        if stable_ticks >= 3:
            break
 
    # 3. 黄金标准提取层：原生递归解析，绝对零偏差保留格式
    print(">>> [BRIDGE] 生成完毕，正在执行底层 DOM 抽取...")
    time.sleep(0.5)
    
    # ⚠️ 注意这里使用的是 r""" (Python的原始字符串)，这非常关键，防止换行符被错误转义
    extracted_text = PAGE.evaluate(r"""() => {
        function extractMarkdown(node) {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent;
            if (node.nodeType !== Node.ELEMENT_NODE) return "";
            
            // 捕获 DeepThink 思考过程并包裹自定义标签，供后端正则精准剔除
            if (node.classList && (node.classList.contains('ds-markdown--think') || node.classList.contains('ds-think-content') || node.classList.contains('ds-thought-content'))) {
                return "\n<think>\n" + node.textContent + "\n</think>\n";
            }
            
            // 精准接管新版代码块容器
            if (node.classList && node.classList.contains('md-code-block')) {
                const langEl = node.querySelector('.md-code-block-header');
                const lang = langEl ? langEl.innerText.trim() : 'json';
                const pre = node.querySelector('pre');
                const code = pre ? pre.textContent : node.textContent;
                return "\n```" + lang + "\n" + code + "\n```\n";
            }
            
            // 兜底旧版的单纯 <pre>
            if (node.tagName === 'PRE') {
                return "\n```json\n" + node.textContent + "\n```\n";
            }
            
            let result = "";
            for (let child of node.childNodes) {
                result += extractMarkdown(child);
            }
            
            // 遇到块级元素自动追加换行，防止文字被挤在一行
            const blockTags = ['P', 'DIV', 'LI', 'H1', 'H2', 'H3', 'UL', 'OL'];
            if (blockTags.includes(node.tagName)) {
                return "\n" + result + "\n";
            }
            return result;
        }
        
        const messages = document.querySelectorAll('.ds-message');
        if (messages.length === 0) return "";
        const lastMsg = messages[messages.length - 1];
        
        let rawContent = extractMarkdown(lastMsg);
        
        // 正则清理因拼接产生的多余空白行
        return rawContent.replace(/\n{3,}/g, '\n\n').trim();
    }""")
    
    if not extracted_text:
        raise Exception("DOM 提取失败：提取到的文本为空。")
        
    print(">>> [BRIDGE] DOM 抽取成功！")
    
    # ======== 依然保留 DEBUG 日志观察效果 ========
    print("\n" + "="*20 + " BRIDGE EXTRACTED RAW TEXT " + "="*20)
    print(extracted_text)
    print("="*67 + "\n")
    # ============================================
    
    return extracted_text

@app.route('/v1/chat/completions', methods=['POST'])
def chat():
    data = request.json
    messages = data.get('messages', [])
    
    # 提取最后一条用户输入
    last_msg = messages[-1]['content'] if messages else ""
    
    # 👇 核心修复：判断是否是第一轮对话（即历史记录里还没有 assistant 的回复）
    has_assistant_reply = any(msg.get('role') == 'assistant' for msg in messages)
    
    if not has_assistant_reply:
        # 提取所有的 system 提示词（包括规则和当前工作目录）并拼接
        system_prompts = [msg['content'] for msg in messages if msg.get('role') == 'system']
        if system_prompts:
            combined_system = "\n\n".join(system_prompts)
            last_msg = f"System Rules:\n{combined_system}\n\nUser Input:\n{last_msg}"

    try:
        result_text = ask_deepseek(last_msg)
        return jsonify({
            "model": "deepseek-pure",
            "choices": [{"message": {"role": "assistant", "content": result_text}, "finish_reason": "stop"}]
        })
    except Exception as e:
        print(f">>> 错误: {e}")
        return jsonify({
            "model": "error",
            "choices": [{"message": {"role": "assistant", "content": f"提取失败: {e}"}, "finish_reason": "stop"}]
        })

@app.route('/v1/chat/delete', methods=['POST'])
def delete_chat():
    """接收 agent 退出时的信号，执行动态监听删除"""
    print("\n>>> [BRIDGE] 收到清理指令，正在销毁本次对话...")
    try:
        # 1. 找到左侧历史记录（兼容多种 URL 模式）
        history_link = PAGE.locator("a[href*='/a/chat/s/'], a[href*='/chat/']").first
        history_link.wait_for(state="visible", timeout=3000)
        history_link.hover()

        # 2. 点击 "..." 更多按钮
        more_btn = history_link.locator("div[role='button'], .ds-icon-button").last
        if not more_btn.is_visible():
             history_link.click(position={"x": 200, "y": 20})
        else:
             more_btn.click()
               
        # 3. 动态等待菜单出现并点击，【拒绝 time.sleep】
        delete_menu_item = PAGE.locator("div[role='menuitem'], div[class*='option']").filter(has_text=re.compile(r"删除|Delete")).first
        delete_menu_item.wait_for(state="visible", timeout=2000)
        delete_menu_item.click()

        # 4. 动态等待二次确认弹窗出现并点击，【拒绝 time.sleep】
        # DeepSeek 确认按钮文本为 "删除该对话"，不是单纯的"删除"
        confirm_btn = PAGE.locator("button").filter(has_text=re.compile(r"删除|确认|Confirm|Delete")).last
        confirm_btn.wait_for(state="visible", timeout=2000)
        confirm_btn.click()
        
        # 5. 动态等待弹窗消失！这代表删除彻底完成，触发真实信号！
        confirm_btn.wait_for(state="hidden", timeout=3000)

        print(">>> [BRIDGE] ✅ 本次会话已成功销毁，返回成功信号！")
        return jsonify({"status": "success"}) # 👈 这里就是向 Agent 发送的成功信号
        
    except Exception as e:
        print(f">>> [BRIDGE] ⚠️ 清理失败: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/v1/chat/stop', methods=['POST'])
def stop_chat():
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(">>> [BRIDGE] 收到停止指令。")
    return jsonify({"status": "success"})

# ================= 新增 API 端点：动态切换模式 =================
@app.route('/v1/chat/mode/set', methods=['POST'])
def set_mode_api():
    data = request.json
    mode = data.get('mode', 'expert')      # 'fast' 或 'expert'
    deep_think = data.get('deep_think')    # 'on', 'off', 或 None 表示不改变

    try:
        result = set_mode_and_deepthink(mode, deep_think)
        return jsonify({"status": "success", "message": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    init_browser()
    app.run(port=8000, threaded=False)