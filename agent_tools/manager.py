from .core import BaseTool, ToolLogger
from typing import Dict, Callable
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError


class ToolManager:
    def __init__(self, current_user_role: int = 3):
        self.tools: Dict[str, BaseTool] = {}
        self.current_role = current_user_role
        self.logger = ToolLogger()
        self.executor = ThreadPoolExecutor(max_workers=5) # 用于控制超时

    def register(self, tool: BaseTool):
        self.tools[tool.name] = tool

    def _create_proxy(self, tool: BaseTool):
        """生成一个代理函数，完全替代原来的普通函数，拦截所有调用"""
        def proxy(**kwargs) -> str:
            start_time = time.time()
            status = "FAILED"
            error_msg = ""
            result = ""

            try:
                # 【第一层：调用前置拦截 - Pre-flight】
                # 1. 权限校验
                if self.current_role < tool.required_role:
                    raise PermissionError(f"Role level {self.current_role} is lower than required {tool.required_role}.")
                
                # 2. 参数校验
                is_valid, val_msg = tool.validate_args(kwargs)
                if not is_valid:
                    raise ValueError(f"Schema Validation Failed: {val_msg}")

                # 【第二层：调用中执行与监控 - In-flight】
                # 带有超时的异步执行
                future = self.executor.submit(tool.run, **kwargs)
                result = future.result(timeout=tool.timeout)
                
                # 【第三层：结果校验 - Post-flight】
                if not isinstance(result, str):
                    result = str(result)
                status = "SUCCESS"
                
            except TimeoutError:
                error_msg = f"Timeout: Tool execution exceeded {tool.timeout} seconds."
                result = f"Error: {error_msg}"
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                result = f"Error: {error_msg}. Suggestion: Check parameters and security restrictions."
            finally:
                # 无论成功失败，记录企业级流水日志
                self.logger.log_action(
                    tool_name=tool.name,
                    args=kwargs,
                    status=status,
                    duration=time.time() - start_time,
                    error=error_msg
                )
            
            return result
            
        return proxy

    def get_agent_tools_dict(self) -> Dict[str, Callable]:
        """输出给 agent.py 的 tools 字典，无缝兼容你的现有代码"""
        return {name: self._create_proxy(tool) for name, tool in self.tools.items()}

    def generate_system_prompt_addition(self) -> str:
        """根据已注册的工具自动生成符合 SYSTEM_PROMPT 格式的文本"""
        lines = []
        for idx, (name, tool) in enumerate(self.tools.items(), 1):
            lines.append(f"{idx}. {name}")
            lines.append(f"   Description: {tool.description}")
            lines.append(f"   Parameters:")
            props = tool.parameters_schema.get("properties", {})
            for p_name, p_info in props.items():
                lines.append(f"   - {p_name} ({p_info.get('type')}): {p_info.get('description')}")
        return "\n".join(lines)