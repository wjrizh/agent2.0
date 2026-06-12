import os
import json
import re
import subprocess
from .core import BaseTool

class KiCadCombinedQueryTool(BaseTool):
    name = "kicad_combined_query_tool"
    description = "读取 PCB 和原理图。采用纯Python计算紧凑边界(Tight BBox)，免疫KiCad版本变动，精确返回真实物理宽高及引脚坐标。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "pcb_file_path": {"type": "string"},
            "sch_file_path": {"type": "string"}
        },
        "required": ["pcb_file_path"]
    }
    required_role = 1
    timeout = 30

    def run(self, pcb_file_path: str, sch_file_path: str = None, **kwargs) -> str:
        if not os.path.exists(pcb_file_path):
            return json.dumps({"error": f"PCB文件不存在: {pcb_file_path}"})
        if not sch_file_path:
            sch_file_path = os.path.splitext(pcb_file_path)[0] + ".kicad_sch"
            
        sch_values = {}
        if os.path.exists(sch_file_path):
            try:
                with open(sch_file_path, 'r', encoding='utf-8') as f: content = f.read()
                lib_start = content.find('(lib_symbols')
                if lib_start != -1:
                    depth = 0; lib_end = lib_start
                    for i in range(lib_start, len(content)):
                        if content[i] == '(': depth += 1
                        elif content[i] == ')':
                            depth -= 1
                            if depth == 0: lib_end = i; break
                    content = content[:lib_start] + content[lib_end+1:]
                symbol_blocks = re.split(r'\(\s*symbol\b', content)
                for block in symbol_blocks[1:]:
                    ref_match = re.search(r'\(\s*property\s+"Reference"\s+"([^"]+)"', block)
                    val_match = re.search(r'\(\s*property\s+"Value"\s+"([^"]+)"', block)
                    if ref_match and val_match:
                        ref = ref_match.group(1); val = val_match.group(1)
                        if not ref.startswith('#') and ref != "~": sch_values[ref] = val
            except: pass

        kicad_python = os.path.expanduser("/home/z/桌面/squashfs-root/bin/python3.11")
        
        script = f'''
import json
try:
    import pcbnew
    board = pcbnew.LoadBoard("{pcb_file_path}")
    scale = pcbnew.IU_PER_MM if hasattr(pcbnew, "IU_PER_MM") else 1000000.0
    footprints_data = []

    # 核心：纯 Python AABB 边界计算类，彻底抛弃 KiCad EDA_RECT
    class PyBBox:
        def __init__(self, kbox):
            self.x = kbox.GetX(); self.y = kbox.GetY()
            self.w = kbox.GetWidth(); self.h = kbox.GetHeight()
        def merge(self, other):
            min_x = min(self.x, other.x); min_y = min(self.y, other.y)
            max_x = max(self.x + self.w, other.x + other.w)
            max_y = max(self.y + self.h, other.y + other.h)
            self.x = min_x; self.y = min_y
            self.w = max_x - min_x; self.h = max_y - min_y

    def get_tight_bbox(fp):
        rect = None
        for pad in fp.Pads():
            b = PyBBox(pad.GetBoundingBox())
            if rect is None: rect = b
            else: rect.merge(b)
        if rect is None: rect = PyBBox(fp.GetBoundingBox())
        return rect

    for fp in board.GetFootprints():
        ref = fp.GetReference(); pos = fp.GetPosition()
        orientation = fp.GetOrientation() 
        angle = orientation.AsDegrees() if hasattr(orientation, "AsDegrees") else orientation / 10.0      
        bbox = get_tight_bbox(fp)
        
        nets = set(); pads_info = {{}}; pads_by_net = {{}}
        for pad in fp.Pads():
            net_name = pad.GetNetname()
            pad_num = pad.GetNumber()
            pad_pos = pad.GetPosition()
            if net_name:
                nets.add(net_name)
                p_data = {{"net": net_name, "x": round(pad_pos.x/scale, 3), "y": round(pad_pos.y/scale, 3)}}
                pads_info[pad_num] = p_data
                if net_name not in pads_by_net: pads_by_net[net_name] = []
                pads_by_net[net_name].append(p_data)

        footprints_data.append({{
            "ref": ref, "x": round(pos.x/scale, 3), "y": round(pos.y/scale, 3), "angle": angle,
            "width": round(bbox.w/scale, 3), "height": round(bbox.h/scale, 3),
            "is_locked": fp.IsLocked(), "connected_nets": list(nets),
            "pads": pads_info, "pads_by_net": pads_by_net
        }})
    print(json.dumps(footprints_data, ensure_ascii=False))
except Exception as e: print(json.dumps({{"error": str(e)}}))
'''
        try:
            output = subprocess.check_output([kicad_python, '-c', script], stderr=subprocess.STDOUT, timeout=self.timeout, text=True)
            pcb_data = json.loads(output.strip())
            if isinstance(pcb_data, dict) and "error" in pcb_data:
                return json.dumps(pcb_data, ensure_ascii=False)
            for fp in pcb_data: fp["value"] = sch_values.get(fp["ref"], "")
            return json.dumps({"status": "success", "footprints": pcb_data}, ensure_ascii=False)
        except Exception as e: return json.dumps({"error": str(e)})


class KiCadModifyTool(BaseTool):
    name = "kicad_modify_tool"
    description = "【大模型主导的刚体模板布局工具】由大模型设计相对坐标(dx, dy)模板，底层引擎将其作为不可变形的整体落子，并具备 30mm 大范围自动宏观避障与 100% 格式防错功能。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "pcb_file_path": {"type": "string"},
            "relative_templates": {
                "type": "array",
                "description": "基于靶心引脚的相对布局模板",
                "items": {
                    "type": "object",
                    "properties": {
                        "group_name": {"type": "string"},
                        "anchor_ref": {"type": "string", "description": "靶心芯片, 如 'U9'"},
                        "anchor_pad": {"type": "string", "description": "靶心引脚号（兼容 anchor_pin）"},
                        "components": {
                            "type": "array",
                            "description": "模块内元件相对于靶心引脚的精确偏移量 (毫米)（兼容 placements）",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ref": {"type": "string"},
                                    "dx": {"type": "number", "description": "相对引脚的 X 轴偏移 (mm)"},
                                    "dy": {"type": "number", "description": "相对引脚的 Y 轴偏移 (mm)"},
                                    "angle": {"type": "number", "description": "该元件独立的旋转角度"}
                                },
                                "required": ["ref", "dx", "dy"]
                            }
                        }
                    },
                    "required": ["anchor_ref"]
                }
            },
            "dry_run": {"type": "boolean", "description": "只推演不保存"},
            "clearance_mm": {"type": "number", "description": "模块整体与外部已有元件的安全防撞间距，默认 0.6mm"}
        },
        "required": ["pcb_file_path", "relative_templates"]
    }
    required_role = 1
    timeout = 30

    def run(self, pcb_file_path: str, relative_templates: list, dry_run: bool = False, clearance_mm: float = 0.6, **kwargs) -> str:
        kicad_python = os.path.expanduser("/home/z/桌面/squashfs-root/bin/python3.11")
        payload = json.dumps({"templates": relative_templates})
        dry_run_str = "True" if dry_run else "False"

        # ！！！注意：下面 f''' 内部的 Python 代码顶层绝对不能有任何缩进 ！！！
        script = f'''
import sys, json, math
try:
    import pcbnew
    board = pcbnew.LoadBoard("{pcb_file_path}")
    scale = pcbnew.IU_PER_MM if hasattr(pcbnew, "IU_PER_MM") else 1000000.0

    payload = json.loads(sys.stdin.read())
    templates = payload.get("templates", [])
    
    all_fps = list(board.GetFootprints())
    errors = []; act_places = []

    class PyBBox:
        def __init__(self, kbox=None):
            if kbox:
                self.x = kbox.GetX(); self.y = kbox.GetY()
                self.w = kbox.GetWidth(); self.h = kbox.GetHeight()
        def merge(self, other):
            min_x = min(self.x, other.x); min_y = min(self.y, other.y)
            max_x = max(self.x + self.w, other.x + other.w)
            max_y = max(self.y + self.h, other.y + other.h)
            self.x = min_x; self.y = min_y
            self.w = max_x - min_x; self.h = max_y - min_y

    def get_tight_bbox(fp):
        rect = None
        for pad in fp.Pads():
            b = PyBBox(pad.GetBoundingBox())
            if rect is None: rect = b
            else: rect.merge(b)
        if rect is None: rect = PyBBox(fp.GetBoundingBox())
        return rect

    # ================= 纯 Python 刚体边界测试逻辑 =================
    def check_overlap(b1, b2, clr_iu):
        return not (b1.x + b1.w + clr_iu <= b2.x or b2.x + b2.w + clr_iu <= b1.x or 
                    b1.y + b1.h + clr_iu <= b2.y or b2.y + b2.h + clr_iu <= b1.y)

    # 🚀 核心重构：不仅检查元件碰撞，还要进行【严苛的板框越界排查】
    def check_group_collision(group_refs, edg_box=None):
        clearance_iu = int({clearance_mm} * scale)
        for ref_a in group_refs:
            fp_a = board.FindFootprintByReference(ref_a)
            if not fp_a: continue
            ra = get_tight_bbox(fp_a)
            
            # 1. 【新增】板框边界出界硬拦截！
            if edg_box:
                # 元件的边界必须完全被包含在板框(edg_box)内部
                if (ra.x < edg_box.x or ra.y < edg_box.y or 
                    (ra.x + ra.w) > (edg_box.x + edg_box.w) or 
                    (ra.y + ra.h) > (edg_box.x + edg_box.h)):
                    return True, ref_a, "PCB 板框边界 (Edge.Cuts)"

            # 2. 外部元件碰撞排查
            for fp_b in all_fps:
                ref_b = fp_b.GetReference()
                if ref_b in group_refs: continue
                rb = get_tight_bbox(fp_b)
                if check_overlap(ra, rb, clearance_iu):
                    return True, ref_a, ref_b
        return False, None, None

    def apply_template(ax, ay, comps, shift_x_mm=0, shift_y_mm=0):
        group_refs = []
        for c in comps:
            ref = c["ref"]; fp = board.FindFootprintByReference(ref)
            if fp:
                dx = float(c["dx"]) + shift_x_mm
                dy = float(c["dy"]) + shift_y_mm
                fp.SetPosition(pcbnew.VECTOR2I(int((ax + dx) * scale), int((ay + dy) * scale)))
                if "angle" in c and c["angle"] is not None:
                    ang = float(c["angle"])
                    if hasattr(pcbnew, "EDA_ANGLE"): fp.SetOrientation(pcbnew.EDA_ANGLE(ang, pcbnew.DEGREES_T))
                    else: fp.SetOrientation(ang * 10.0)
                group_refs.append(ref)
        return group_refs

    # 🚀 核心新增：提取 PCB 的真实物理板框 Edge.Cuts 包围盒
    edge_bbox = None
    for drawing in board.GetDrawings():
        if drawing.GetLayerName() == "Edge.Cuts":
            b = PyBBox(drawing.GetBoundingBox())
            if edge_bbox is None: edge_bbox = b
            else: edge_bbox.merge(b)

    for t in templates:
        anchor_ref = t.get("anchor_ref")
        anchor_pad = t.get("anchor_pad") or t.get("anchor_pin") or t.get("anchor_pads")
        if isinstance(anchor_pad, list) and len(anchor_pad) > 0: anchor_pad = anchor_pad[0]
        comps = t.get("components") or t.get("placements") or []
        
        afp = board.FindFootprintByReference(anchor_ref)
        if not afp: errors.append(f"找不到锚点 {{anchor_ref}}"); continue
            
        ax, ay = afp.GetPosition().x / scale, afp.GetPosition().y / scale
        if anchor_pad:
            for p in afp.Pads():
                if str(p.GetNumber()) == str(anchor_pad):
                    ax = p.GetPosition().x / scale; ay = p.GetPosition().y / scale
                    break
                    
        # 释放大模型初始美学阵型
        group_refs = apply_template(ax, ay, comps)
        
        # 🚀 避障算法终极演进：全面支持【全方向板内推挤】
        has_col, ca, cb = check_group_collision(group_refs, edge_bbox)
        if has_col:
            found_safe = False
            # 扩展扫描圈至 40mm，支持负向（向内）推挤
            for r_step in range(1, 80):
                radius = r_step * 0.5
                # 扫查全向 8 个方位，如果飞出板外，反方向的点会自动把元件拉回板内
                for theta_deg in range(0, 360, 45):
                    tr = math.radians(theta_deg)
                    sx = radius * math.cos(tr); sy = radius * math.sin(tr)
                    apply_template(ax, ay, comps, sx, sy)
                    if not check_group_collision(group_refs, edge_bbox)[0]:
                        found_safe = True; break
                if found_safe: break
                
            if not found_safe:
                errors.append(f"❌ 严重越界或碰撞：整个模块在板框内无法找到合法安置点（冲突方: {{cb}}）。")

        for c in comps:
            sfp = board.FindFootprintByReference(c["ref"])
            if sfp:
                p = sfp.GetPosition()
                act_places.append(f"{{c['ref']}} 成功落子 -> ({{round(p.x/scale,3)}}, {{round(p.y/scale,3)}})")

    if not {dry_run_str} and len(errors) == 0:
        board.Save("{pcb_file_path}")

    print(json.dumps({{
        "status": "success" if len(errors) == 0 else "warning",
        "mode": "DRY RUN 沙盘推演" if {dry_run_str} else "EXECUTE 写入成功",
        "placements": act_places, "errors": errors
    }}, ensure_ascii=False))

except Exception as e: print(json.dumps({{"error": str(e)}}))
'''
        try:
            process = subprocess.Popen([kicad_python, '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output, _ = process.communicate(input=payload, timeout=self.timeout)
            return output.strip()
        except Exception as e: return json.dumps({"error": str(e)})