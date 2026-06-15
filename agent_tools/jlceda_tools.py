import requests
import json
import os
import datetime
try:
    from .core import BaseTool
except ImportError:
    class BaseTool:
        pass

MCP_URL = "http://127.0.0.1:7655/mcp"

class JLCEDA_MasterTool(BaseTool):
    name = "jlceda_master"
    description = """[JLCEDA专用] 嘉立创EDA控制工具。唯一动作: invoke_api，传入 apiFullName + 额外参数即可调用任何内部API。网表自动落盘到~/jlceda_exports/。"""

    required_role = 1
    parameters_schema = {
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["invoke_api"],
                "description": "要执行的操作类型"
            },
            "query": {
                "type": "string",
                "description": "搜索关键词(用于search_api)"
            },
            "apiFullName": {
                "type": "string",
                "description": "完整API路径(如'eda.sch_ManufactureData.getBomFile')"
            },
            "keyword": {
                "type": "string",
                "description": "器件搜索关键词(用于component_select)"
            },
            "limit": {
                "type": "integer",
                "description": "返回器件上限(2-20,默认20)"
            },
            "components": {
                "type": "array",
                "description": "待放置器件列表[{uuid, libraryUuid}]"
            },
            "timeoutSeconds": {
                "type": "integer",
                "description": "放置超时(30-180秒,默认60)"
            }
        }
    }

    # ── 网表精简规则（通用白名单：覆盖所有常见器件类型的电路分析核心字段）──
    _KEEP_PROPS = {
        # ═══ 通用 ═══
        "Designator", "FootprintName", "Manufacturer Part", "Supplier Part",
        "Datasheet", "Description", "JLCPCB Part Class",
        # ═══ 无源器件 ═══
        # 电阻
        "Resistance", "Value", "Tolerance", "Power(Watts)", "Power",
        "Temperature Coefficient",
        # 电容
        "Capacitance", "Voltage Rated", "Dielectric",
        # 电感 / 磁珠
        "Inductance", "DC Resistance (DCR)", "DC Resistance(DCR)",
        "Impedance", "Impedance @ Frequency",
        "Current - Saturation (Isat)", "Current - Saturation(Isat)",
        "Saturation Current (Isat)",
        "Q @ Frequency", "Frequency - Self Resonant",
        # ═══ 分立半导体 ═══
        # 二极管 / TVS / ESD
        "Voltage - Forward(Vf@If)", "Voltage - DC Reverse(Vr)",
        "Reverse Stand-Off Voltage (Vrwm)", "Reverse Leakage Current (Ir)",
        "Clamping Voltage", "Voltage - Breakdown",
        "Peak Pulse Current (Ipp)", "Junction Capacitance",
        "Reverse Recovery Time (trr)", "Diode Configuration",
        # MOSFET
        "Drain to Source Voltage", "RDS(on)",
        "Gate Threshold Voltage (Vgs(th))",
        "Current - Continuous Drain(Id)",
        "Input Capacitance(Ciss)",
        "Reverse Transfer Capacitance (Crss@Vds)",
        "Pd - Power Dissipation",
        # BJT
        "Collector-Emitter Voltage", "Collector Current",
        "DC Current Gain (hFE)",
        # ═══ 电源 IC ═══
        "Input Voltage", "Output Voltage", "Output Current",
        "Voltage Dropout", "Output Type", "Output Configuration",
        "Frequency - Switching", "Topology",
        "Synchronous Rectifier", "Switch Tube (Built-In/External)",
        "Switch tube (built-in/external)",
        "Number of Outputs",
        # 电池管理
        "Battery Type", "Charge Current - Max",
        "Charging Saturation Voltage",
        "Number of Cells",
        # ═══ 晶振 ═══
        "Frequency", "Frequency Stability", "Load Capacitance",
        "Normal temperature Frequency Tolerance",
        # ═══ 连接器 ═══
        "Number of Pins", "Number of Contacts", "Connector Type",
        "Number of Rows", "Number of Ports",
        # ═══ 功放 / 音频 ═══
        "Output Power", "Speaker Channels",
        "Total Harmonic Distortion(THD)",
        # ═══ IC / 模块通用 ═══
        "Supply Voltage", "Operating Temperature",
        "Memory Size", "Interface", "Function",
        "Type", "Clock Frequency", "Number of Channels",
        "Number of Circuits", "Polarity", "Circuit",
        # ═══ 传感器 / GPS ═══
        "GNSS Type", "Sensitivity",
        "Time To First Fix", "Receive  Current",
        # ═══ 其他电路行为相关 ═══
        "Reset Active Level", "Reset Timeout",
        "Watchdog", "Manual Reset",
        "Interrupt Output", "Control Input Logic",
    }

    def _compact_netlist(self, raw_text: str) -> str:
        """将网表 JSON 精简：黑名单排除 + 引脚裁剪 + 保留顶层设计规则"""
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            return raw_text

        comps = data.get("components", {})
        if not comps:
            return raw_text

        compact = {}
        for cid, cdata in comps.items():
            props = cdata.get("props", {})
            pins = cdata.get("pinInfoMap", {})
            # 白名单过滤：只保留电路分析所需的关键字段
            slim_props = {k: v for k, v in props.items() if k in self._KEEP_PROPS}
            # 引脚裁剪：只保留 name / number / net
            slim_pins = {}
            for pn, pd in pins.items():
                slim_pins[pn] = {
                    "n": pd.get("name", ""),
                    "num": pd.get("number", pn),
                    "net": pd.get("net", ""),
                }
            compact[cid] = {"p": slim_props, "pins": slim_pins}

        summary = {
            "v": data.get("version", "?"),
            "total": len(comps),
            "comps": compact,
        }
        # 保留设计规则（电路分析用）
        for key in ("designRule", "differentialPair", "netClass", "equalLengthNetGroup"):
            if key in data:
                summary[key] = data[key]
        return json.dumps(summary, ensure_ascii=False, indent=2)

    def _send_rpc(self, tool_name: str, arguments: dict) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}
        }
        try:
            r = requests.post(MCP_URL, json=payload, timeout=120)  # 网表导出可能慢，加大超时
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                return f"RPC Error: {json.dumps(data['error'], ensure_ascii=False)}"
            content = data.get("result", {}).get("content", [])
            if content:
                return content[0].get("text", "OK (no text)")
            return json.dumps(data.get("result", {}), ensure_ascii=False)
        except requests.exceptions.ConnectionError:
            return "Error: MCP Hub 未运行 (:7655)"
        except Exception as e:
            return f"Error: {e}"

    def run(self, action: str, query: str = "", apiFullName: str = "",
            keyword: str = "", limit: int = 20, components: list = None,
            timeoutSeconds: int = 60, **kwargs) -> str:
        """所有**kwargs自动透传给嘉立创底层API,无需改代码"""

        if action == "invoke_api":
            if not apiFullName:
                return "Error: 'apiFullName' required"
            result = self._send_rpc("api_invoke", {"apiFullName": apiFullName, "params": kwargs})
            # 网表自动落盘
            if "getNetlistFile" in apiFullName or "getNetlist" in apiFullName.lower():
                return self._auto_file_large_result(result, "netlist", "json")
            return result

        return f"Unknown action: {action}"

    # ── TEL 格式转换器 ──
    def _to_tel_format(self, netlist_json: str) -> str:
        """将精简后的网表 JSON 转换为 TEL (Protel2) 格式"""
        try:
            data = json.loads(netlist_json)
        except (json.JSONDecodeError, TypeError):
            return netlist_json

        comps = data.get("comps", {})
        if not comps:
            return netlist_json

        lines = []
        lines.append("$PACKAGES")

        # ── 第一步：按封装+型号+参数分组，收集位号 ──
        groups = {}  # key: (封装, 型号, 参数值) → 位号列表
        for cid, cdata in comps.items():
            props = cdata.get("p", {})
            designator = props.get("Designator", "?")
            footprint = props.get("FootprintName", "?")
            # 提取型号和参数值
            part_number = props.get("Manufacturer Part", props.get("Supplier Part", ""))
            value = props.get("Value", props.get("Resistance", props.get("Capacitance", props.get("Inductance", ""))))
            if not part_number:
                part_number = value if value else "{Value}"
            param = value if value else "{Value}"

            key = (footprint, part_number, param)
            if key not in groups:
                groups[key] = []
            groups[key].append(designator)

        # 输出元器件分组行
        for (footprint, part_number, param), designators in sorted(groups.items(), key=lambda x: min(x[1])):
            designators.sort()
            if len(designators) > 1:
                # 换行续行：每行最多40个位号
                chunks = [designators[i:i+40] for i in range(0, len(designators), 40)]
                first_chunk = " ".join(chunks[0])
                if len(chunks) == 1:
                    lines.append(f"{footprint} ! {part_number} ! {param} ; {first_chunk}")
                else:
                    lines.append(f"{footprint} ! {part_number} ! {param} ; {first_chunk} ,")
                    for chunk in chunks[1:-1]:
                        lines.append(f"  {" ".join(chunk)} ,")
                    lines.append(f"  {" ".join(chunks[-1])}")
            else:
                lines.append(f"{footprint} ! {part_number} ! {param} ; {designators[0]}")

        # ── 第二步：构建网络连接表 ──
        lines.append("$NETS")
        net_map = {}  # net_name → [(designator, pin_number), ...]
        for cid, cdata in comps.items():
            designator = cdata.get("p", {}).get("Designator", "?")
            pins = cdata.get("pins", {})
            for pn, pd in pins.items():
                net_name = pd.get("net", "")
                pin_number = pd.get("num", pn)
                if net_name:
                    if net_name not in net_map:
                        net_map[net_name] = []
                    net_map[net_name].append(f"{designator}.{pin_number}")

        for net_name in sorted(net_map.keys()):
            nodes = net_map[net_name]
            node_str = " ".join(nodes)
            # 如果网络名包含特殊字符，加引号
            safe_name = f"'{net_name}'" if any(c in net_name for c in " .()[]{}/@!#$%^&*+=") else net_name
            lines.append(f"{safe_name} ; {node_str}")

        lines.append("$SCHEDULE")
        lines.append("$END")
        return "\n".join(lines)

    # ── 大数据自动落盘 ──
    _LARGE_OUTPUT_DIR = os.path.expanduser("~/jlceda_exports")

    def _auto_file_large_result(self, result: str, prefix: str, ext: str) -> str:
        """大数据自动落盘。网表走 TEL 转换器，其他直接保存；小数据直接返回"""
        if len(result) < 4096:
            return result

        os.makedirs(self._LARGE_OUTPUT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 提取原始 netlist JSON 文本（兼容多种外层包装）
        body = result
        raw = result
        try:
            inner = json.loads(result)
            raw = inner.get("result", {}).get("text", "")
            if not raw and "components" in inner:
                raw = result
        except Exception:
            pass

        # 网表 → TEL 格式
        if prefix == "netlist":
            compact = self._compact_netlist(raw)
            tel_body = self._to_tel_format(compact)
            fname = f"{prefix}_{ts}.tel"
            fpath = os.path.join(self._LARGE_OUTPUT_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(tel_body)
            size_kb = os.path.getsize(fpath) / 1024
            preview = tel_body[:500] + "..." if len(tel_body) > 500 else tel_body
            return f"[已保存到文件: {fpath} ({size_kb:.1f} KB)]\n{preview}"

        # 其他 → 直接保存
        fname = f"{prefix}_{ts}.{ext}"
        fpath = os.path.join(self._LARGE_OUTPUT_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(body)
        size_kb = os.path.getsize(fpath) / 1024
        preview = body[:500] + "..." if len(body) > 500 else body
        return f"[已保存到文件: {fpath} ({size_kb:.1f} KB)]\n{preview}"


# ── 便捷函数 ──

def export_bom(fileType: str = "csv") -> str:
    tool = JLCEDA_MasterTool()
    return tool.run("invoke_api", apiFullName="eda.sch_ManufactureData.getBomFile", fileType=fileType)

def export_netlist() -> str:
    tool = JLCEDA_MasterTool()
    return tool.run("invoke_api", apiFullName="eda.sch_ManufactureData.getNetlistFile")

def open_project(project_uuid: str) -> str:
    tool = JLCEDA_MasterTool()
    return tool.run("invoke_api", apiFullName="eda.dmt_Project.openProject")

def search_api(query: str) -> str:
    tool = JLCEDA_MasterTool()
    return tool.run("search_api", query=query)