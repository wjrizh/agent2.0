# file_editor.py — 重构版 UpdateFileTool: 锚点定位 → 区间 Patch → 原子写入
import os
import uuid
import hashlib
import shutil
import difflib
from datetime import datetime
from .core import BaseTool

# ========== 模块级可配置常量 ==========
_FUZZY_THRESHOLD = 0.9       # 宽松模式相似度阈值
_FUZZY_WINDOW = 10            # 锚点安全窗口 ±N 行
_LINES_WARN_RATIO = 3         # 行数差异警告倍数
_LARGE_FILE_THRESHOLD = 10000 # 大文件模式触发行数
_BACKUP_DIR = os.path.expanduser("~/.ligong_backups")  # 备份集中目录
_MAX_BACKUPS_PER_FILE = 20    # 每个文件最大备份数

# ========== 辅助函数 ==========

def _secure_path(relative_path: str) -> str:
    return os.path.abspath(os.path.join(os.getcwd(), relative_path))

def _is_junk(x: str) -> bool:
    """宽松模式：忽略纯空白和 Python 注释行"""
    s = x.strip()
    return not s or s.startswith('#')

def _normalize_line(line: str) -> str:
    """归一化：去首尾空格、\r、\t→4空格"""
    return line.rstrip('\r').strip().replace('\t', '    ')

def _normalize_lines(lines: list) -> list:
    return [_normalize_line(l) for l in lines]

def _strict_anchor_match(file_lines: list, search_lines: list) -> tuple | None:
    """严格模式：连续 N 行完全精确匹配，返回 (start, end) 或 None"""
    n = len(search_lines)
    total = len(file_lines)
    matches = []
    for i in range(total - n + 1):
        if file_lines[i:i+n] == search_lines:
            matches.append((i, i + n))
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        return ('multiple', matches)  # 降级信号
    return None

def _fuzzy_anchor_match(file_lines: list, search_lines: list, threshold: float) -> tuple | None:
    """宽松模式：归一化后按整体行序列计算相似度，返回 (start, end, similarity, candidates)"""
    n = len(search_lines)
    total = len(file_lines)
    if n == 0 or n > total:
        return None

    norm_file = _normalize_lines(file_lines)
    norm_search = _normalize_lines(search_lines)

    best_score = 0.0
    best_idx = -1
    candidates = []

    for i in range(total - n + 1):
        window = norm_file[i:i+n]
        # 跳过纯空白段（非空白行占比 < 50%）
        non_blank = sum(1 for l in window if l.strip()) / n
        if non_blank < 0.5:
            continue
        sm = difflib.SequenceMatcher(isjunk=_is_junk, a=window, b=norm_search)
        score = sm.ratio()
        if score > best_score:
            best_score = score
            best_idx = i
        candidates.append((i, i + n, round(score, 4)))

    # Top-3 候选按分数降序
    candidates.sort(key=lambda x: -x[2])
    top_candidates = candidates[:3]

    if best_score >= threshold and best_idx >= 0:
        # 检查是否有多个候选也 ≥ 阈值
        above = [c for c in top_candidates if c[2] >= threshold]
        if len(above) >= 2:
            return ('multiple', above)  # 多个高相似候选
        return (best_idx, best_idx + n, round(best_score, 4), top_candidates)
    return (None, top_candidates)  # 无命中

def _validate_window(anchor_start: int, anchor_end: int, total_lines: int, window: int, replace_lines_count: int) -> tuple:
    """区间校验，返回 (start, end, expanded)"""
    start = max(0, anchor_start - window)
    end = min(total_lines, anchor_end + window)
    expanded = False

    # replace_block 超出窗口时自动扩展
    needed = anchor_start + replace_lines_count + 5
    if needed > end:
        end = min(total_lines, needed)
        expanded = True

    # 扩展后超文件总行数
    if end > total_lines:
        return ('error', f"扩展后替换区间超出文件总行数({total_lines}行)，无法完成替换")

    return (start, end, expanded)

def _char_diff_check(original_line: str, search_line: str) -> str:
    """单行字符级 ndiff 校验，返回差异类型"""
    diff = list(difflib.ndiff([original_line], [search_line]))
    non_escape = False
    has_escape = False
    for token in diff:
        if token.startswith('- ') or token.startswith('+ '):
            ch = token[2:]
            if ch in ('\\', '"'):
                has_escape = True
            else:
                non_escape = True
    if non_escape and has_escape:
        return 'mixed'
    elif non_escape:
        return 'code_logic'
    elif has_escape:
        return 'escape_only'
    return 'exact'

def _cleanup_backups(path_hash: str):
    """清理指定 path_hash 的备份，保留最新 _MAX_BACKUPS_PER_FILE 个"""
    try:
        backups = sorted([
            f for f in os.listdir(_BACKUP_DIR)
            if f.endswith('.bak') and path_hash in f
        ])
        while len(backups) > _MAX_BACKUPS_PER_FILE:
            os.remove(os.path.join(_BACKUP_DIR, backups.pop(0)))
    except OSError as e:
        print(f"[backup] 清理旧备份失败: {e}")


def _create_backup(safe_path: str, old_start: int, old_end: int,
                   old_total_len: int, new_total_len: int) -> str | None:
    """备份修改前的源文件（在替换写入之前调用）。返回备份路径，失败返回 None。"""
    try:
        os.makedirs(_BACKUP_DIR, exist_ok=True)
    except OSError as e:
        print(f"[backup] 创建备份目录失败: {e}")
        return None

    real_path = os.path.realpath(safe_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path_hash = hashlib.blake2b(real_path.encode(), digest_size=4).hexdigest()
    filename = os.path.basename(safe_path)
    line_info = f"total_{old_total_len}→{new_total_len}_range_{old_start}-{old_end}"
    bak_name = f"{timestamp}__{path_hash}__{filename}__{line_info}.bak"
    bak_path = os.path.join(_BACKUP_DIR, bak_name)

    try:
        shutil.copy2(safe_path, bak_path)
    except OSError as e:
        print(f"[backup] 备份失败: {e}，尝试降级重试")
        try:
            existing = sorted([
                f for f in os.listdir(_BACKUP_DIR)
                if f.endswith('.bak') and path_hash in f
            ])
            if existing:
                os.remove(os.path.join(_BACKUP_DIR, existing[0]))
                shutil.copy2(safe_path, bak_path)
        except OSError:
            return None

    _cleanup_backups(path_hash)
    return bak_path

def _generate_candidate_context(file_lines: list, candidates: list) -> list:
    """为候选区域补充前后各 1 行上下文"""
    enriched = []
    for c in candidates:
        start, end, score = c[0], c[1], c[2]
        ctx_before = file_lines[start-1].rstrip('\n') if start > 0 else '(文件开头)'
        ctx_after = file_lines[end].rstrip('\n') if end < len(file_lines) else '(文件末尾)'
        enriched.append({
            'start': start, 'end': end, 'similarity': score,
            'context_before': ctx_before[:80],
            'context_after': ctx_after[:80]
        })
    return enriched


# ========== 重构版 UpdateFileTool ==========

class UpdateFileTool(BaseTool):
    name = "update_file"
    description = "Update a file by searching for a specific block of code and replacing it. Fails if search_block is not unique unless replace_all is true."
    required_role = 2
    parameters_schema = {
        "required": ["path", "search_block", "replace_block"],
        "properties": {
            "path": {"type": "string", "description": "File path."},
            "search_block": {"type": "string", "description": "The exact string currently in the file. Must be unique."},
            "replace_block": {"type": "string", "description": "The new string to replace it with."},
            "replace_all": {"type": "boolean", "description": "Set to true to change every instance of search_block in the file. Default is false."},
            "fuzzy_threshold": {"type": "number", "description": "Override fuzzy matching threshold. Default 0.9."},
            "fuzzy_window": {"type": "integer", "description": "Override anchor safety window ±N lines. Default 10."}
        }
    }

    def run(self, path: str, search_block: str, replace_block: str,
            replace_all: bool = False,
            fuzzy_threshold: float = None,
            fuzzy_window: int = None) -> str:

        trace_id = uuid.uuid4().hex[:12]
        threshold = fuzzy_threshold if fuzzy_threshold is not None else _FUZZY_THRESHOLD
        window = fuzzy_window if fuzzy_window is not None else _FUZZY_WINDOW

        safe_path = _secure_path(path)

        # ---- 前置校验 ----
        # [引用外部 _READ_FILES 集合]
        from .builtin_tools import _READ_FILES
        if safe_path not in _READ_FILES:
            return f"Error: You MUST use the 'read_file' tool on '{path}' before attempting to edit it."

        # 空块检查
        search_stripped = search_block.strip()
        if not search_stripped:
            return "Error: 锚点块为空/仅含空白字符，无法定位"

        # 文件读取异常兜底
        try:
            with open(safe_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            return "Error: 读取文件失败：文件不存在"
        except PermissionError:
            return "Error: 读取文件失败：无读取权限"

        file_lines = content.split('\n')
        search_lines = search_block.split('\n')
        replace_lines = replace_block.split('\n')
        total_lines = len(file_lines)

        print(f"[update_file] trace={trace_id} | 文件: {path} | 总行数: {total_lines} | 锚点行数: {len(search_lines)}")

        # ---- 大文件模式 ----
        if total_lines > _LARGE_FILE_THRESHOLD:
            print(f"[update_file] trace={trace_id} | 触发大文件模式 (>{_LARGE_FILE_THRESHOLD}行)")

        # ---- 阶段1: 锚点定位 ----
        mode = 'strict'
        anchor_result = _strict_anchor_match(file_lines, search_lines)

        if anchor_result is None:
            # 严格模式无匹配 → 降级宽松模式
            mode = 'fuzzy'
            print(f"[update_file] trace={trace_id} | 严格模式无匹配，降级至宽松模式 (阈值={threshold})")
            fuzzy_result = _fuzzy_anchor_match(file_lines, search_lines, threshold)

            if isinstance(fuzzy_result[0], str) and fuzzy_result[0] == 'multiple':
                if not replace_all:
                    candidates = _generate_candidate_context(file_lines, fuzzy_result[1])
                    return (
                        f"Error: 宽松模式匹配到 {len(fuzzy_result[1])} 个高相似候选区域，锚点不唯一。"
                        f"请补全更精确的锚点后重试，或设置 replace_all=true 全量替换。\n"
                        f"候选区域: {candidates}"
                    )
                # replace_all=True：取最高相似度候选开始替换（多匹配走循环）
                fuzzy_result = (fuzzy_result[1][0][0], fuzzy_result[1][0][1], fuzzy_result[1][0][2], [])
                anchor_start, anchor_end, similarity, _ = fuzzy_result

            if fuzzy_result[0] is None:
                # 无命中
                top_candidates = _generate_candidate_context(file_lines, fuzzy_result[1])
                return (
                    f"Error: 未找到匹配区域（宽松模式最高相似度: {fuzzy_result[1][0][2] if fuzzy_result[1] else 'N/A'}）。"
                    f"请检查 search_block 是否存在于文件中。\n"
                    f"Top-3 候选: {top_candidates}"
                )

            anchor_start, anchor_end, similarity, _ = fuzzy_result
            print(f"[update_file] trace={trace_id} | 宽松模式命中 | 区间: {anchor_start}-{anchor_end} | 相似度: {similarity}")

        elif isinstance(anchor_result[0], str) and anchor_result[0] == 'multiple':
            # 严格模式多匹配
            if replace_all:
                # replace_all=True：收集所有严格模式匹配位置，从后往前替换
                multi_matches = anchor_result[1]
                multi_matches.sort(key=lambda x: -x[0])  # 从后往前
                file_lines_orig = file_lines[:]
                replaced_count = 0
                for m_start, m_end in multi_matches:
                    diff = len(replace_lines) - (m_end - m_start)
                    file_lines[m_start:m_end] = replace_lines
                    replaced_count += 1
                    # 调整后续匹配的偏移（前面已排好序，不影响已处理的）
                    for j in range(len(multi_matches)):
                        if multi_matches[j][0] < m_start:
                            multi_matches[j] = (multi_matches[j][0] + diff, multi_matches[j][1] + diff)
                mode = 'replace_all'
                anchor_start = multi_matches[-1][0]  # 最前一个匹配的最终位置
                anchor_end = multi_matches[-1][1]
                similarity = 1.0
                print(f"[update_file] trace={trace_id} | replace_all 模式 | 替换了 {replaced_count} 处严格匹配")
            else:
                mode = 'fuzzy'
                print(f"[update_file] trace={trace_id} | 严格模式匹配到 {len(anchor_result[1])} 处，降级至宽松模式")
                fuzzy_result = _fuzzy_anchor_match(file_lines, search_lines, threshold)
                if fuzzy_result[0] is None or (isinstance(fuzzy_result[0], str) and fuzzy_result[0] == 'multiple'):
                    candidates = _generate_candidate_context(file_lines,
                        fuzzy_result[1] if not isinstance(fuzzy_result[0], str) else [])
                    return f"Error: 锚点不唯一，严格+宽松模式均无法定位唯一区域。候选: {candidates}"
                anchor_start, anchor_end, similarity, _ = fuzzy_result
                print(f"[update_file] trace={trace_id} | 宽松模式命中 | 区间: {anchor_start}-{anchor_end} | 相似度: {similarity}")
        else:
            anchor_start, anchor_end = anchor_result
            similarity = 1.0
            print(f"[update_file] trace={trace_id} | 严格模式精确命中 | 区间: {anchor_start}-{anchor_end}")

        # ---- 阶段2: 区间锁定 ----
        validation = _validate_window(anchor_start, anchor_end, total_lines, window, len(replace_lines))
        if isinstance(validation[0], str) and validation[0] == 'error':
            return f"Error: {validation[1]}"
        patch_start, patch_end, expanded = validation
        if expanded:
            print(f"[update_file] trace={trace_id} | replace_block 行数超出安全区间，自动扩展至 {patch_end} 行")

        # ---- 阶段3: 单行字符级校验 ----
        char_diff_type = 'n/a'
        if len(search_lines) == 1:
            char_diff_type = _char_diff_check(file_lines[anchor_start], search_lines[0])
            if char_diff_type == 'code_logic':
                print(f"[update_file] trace={trace_id} | ⚠ 单行锚点存在非转义代码差异")
            elif char_diff_type == 'escape_only':
                print(f"[update_file] trace={trace_id} | 单行锚点仅存在转义字符差异，正常执行")

        # replace_block 为空处理
        if not replace_block.strip():
            print(f"[update_file] trace={trace_id} | ⚠ replace_block 为空，将执行删除锚点区间操作")

        # ---- 阶段4: 切片替换 ----
        if mode == 'replace_all':
            new_lines = file_lines
            old_len = total_lines
            new_len = len(file_lines)
            ratio = max(old_len, new_len) / min(old_len, new_len) if min(old_len, new_len) > 0 else 1.0
        else:
            new_lines = file_lines[:anchor_start] + replace_lines + file_lines[anchor_end:]
            old_len = anchor_end - anchor_start
            new_len = len(replace_lines)
            ratio = max(old_len, new_len) / min(old_len, new_len) if min(old_len, new_len) > 0 else float('inf')
        if ratio > _LINES_WARN_RATIO:
            print(f"[update_file] trace={trace_id} | ⚠ 行数差异超 {_LINES_WARN_RATIO} 倍（原{old_len}行→新{new_len}行）")

        # ---- 备份（替换前备份原文件）----
        bak_path = _create_backup(
            safe_path=safe_path,
            old_start=anchor_start + 1,   # 转换为 1-based
            old_end=anchor_end,
            old_total_len=total_lines,
            new_total_len=len(new_lines)
        )
        if bak_path:
            print(f"[update_file] trace={trace_id} | 备份已生成: {bak_path}")

        # ---- 原子写入 ----
        tmp_path = safe_path + f".swp.{trace_id}"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(new_lines))
            os.replace(tmp_path, safe_path)
        except (PermissionError, OSError) as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if bak_path and os.path.exists(bak_path):
                os.remove(bak_path)
            return f"Error: 写入文件失败：{e}，已回滚至备份文件 {bak_path}"

        # ---- 操作摘要 ----
        summary = (
            f"替换完成：原区间 {anchor_start}-{anchor_end} 行 ({old_len}行) → 新内容 {new_len}行，"
            f"差异倍数 {ratio:.2f}"
            + (f"，已触发行数警告" if ratio > _LINES_WARN_RATIO else f"，未触发行数警告")
        )
        print(f"[update_file] trace={trace_id} | {summary}")

        return (
            f"Successfully updated '{path}'. Replaced 1 occurrence(s).\n"
            f"[mode={mode}, similarity={similarity:.4f}, trace={trace_id}, "
            f"char_diff={char_diff_type}, backup={bak_path}]\n"
            f"{summary}"
        )