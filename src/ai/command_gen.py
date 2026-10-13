"""配置命令生成器""" 
import json 
import logging 
from typing import List, Dict, Any 
from .llm_client import LLMClient 
from ..utils.prompt_loader import load_prompt 

logger = logging.getLogger(__name__) 

class CommandGenerator: 
    """根据结构化任务生成具体华为命令序列""" 
    
    def __init__(self, llm_client: LLMClient): 
        self.llm = llm_client 
        # 内置模板（简化版） 
        self.templates = { 
            "config_interface": self._gen_interface, 
            "config_ospf": self._gen_ospf, 
            "config_static_route": self._gen_static_route, 
            "config_vlan": self._gen_vlan,
            "config_acl": self._gen_acl,
            "show_command": self._gen_show,
        } 
    
    def generate(self, task: Dict[str, Any]) -> List[str]: 
        """为单个任务生成命令列表""" 
        action = task.get("action") 
        params = task.get("parameters", {}) 
        
        if action in self.templates: 
            return self.templates[action](params) 
        else: 
            # 未知操作，使用LLM生成 
            return self._generate_with_llm(task) 

    # ------------------------------------------------------------------
    # 命令 + 注释 预览（供 UI 展示：既看到 JSON，也看到对应华为命令）
    # ------------------------------------------------------------------
    def generate_with_comments(self, task: Dict[str, Any]) -> List[Dict[str, str]]:
        """生成「命令 + 注释」列表，用于在界面上直观展示将要下发的配置。

        返回值形如：
            [{"command": "system-view", "comment": "进入系统视图"},
             {"command": "vlan 10", "comment": "创建 VLAN 10"}]

        说明：内置模板可给出精准注释；未知操作走 LLM 生成时只能拿到命令本身，
        此时注释退化为空字符串（UI 会自动省略）。
        """
        action = task.get("action")
        params = task.get("parameters", {}) or {}
        commands = self.generate(task)

        result: List[Dict[str, str]] = []
        for cmd in commands:
            result.append({
                "command": cmd,
                "comment": self._comment_for(cmd, action, params, commands),
            })
        return result

    def _comment_for(self, cmd: str, action: str, params: Dict[str, Any],
                     all_cmds: List[str]) -> str:
        """为单条 VRP 命令生成中文注释（基于命令语义与任务参数）"""
        c = (cmd or "").strip()
        low = c.lower()

        # —— 通用视图切换 ——
        if low == "system-view":
            return "进入系统视图（配置模式）"
        if low == "quit":
            # 统计本条 quit 之前已经「进入」了几层视图（system-view/接口/协议等）
            depth = 1 if any(c.strip().lower() == "system-view" for c in all_cmds) else 0
            idx = -1
            for i, c in enumerate(all_cmds):
                if c is cmd or c == cmd:
                    idx = i
                    break
            prior = all_cmds[:idx] if idx > 0 else []
            entered = 0
            for c in prior:
                cl = c.strip().lower()
                if cl in ("system-view",) or cl.startswith(
                    ("interface ", "vlan ", "acl ", "ospf ", "area ", "bgp ")
                ):
                    entered += 1
                elif cl == "quit":
                    entered = max(0, entered - 1)
            # 本次 quit 后是否还有更深层级未退出
            remaining_quits = [c for c in all_cmds[idx + 1:] if c.strip().lower() == "quit"] if idx >= 0 else []
            if entered >= 1 and not remaining_quits:
                return "退出至用户视图"
            return "返回上一级视图"
        if low == "return":
            return "直接返回用户视图"
        if low == "undo" or low.startswith("undo "):
            return f"删除/取消配置：{c[5:].strip()}"
        if low == "save":
            return "保存当前配置"

        # —— 按操作类型补充业务语义 ——
        if low.startswith("interface "):
            iface = c[len("interface "):].strip()
            return f"进入接口 {iface} 视图"
        if low.startswith("ip address "):
            parts = c.split()
            if len(parts) >= 4:
                return f"配置接口 IP 地址 {parts[2]} {parts[3]}"
            return "配置接口 IP 地址"
        if low.startswith("description "):
            return f"设置描述信息：{c[len('description '):].strip()}"
        if low.startswith("vlan batch "):
            return f"批量创建 VLAN：{c[len('vlan batch '):].strip()}"
        if low.startswith("vlan "):
            return f"创建 VLAN {c[len('vlan '):].strip()}"
        if low.startswith("ospf "):
            return f"启动 OSPF 进程 {c[len('ospf '):].strip()}"
        if low.startswith("area "):
            return f"进入 OSPF 区域 {c[len('area '):].strip()}"
        if low.startswith("network "):
            return f"宣告网段：{c[len('network '):].strip()}"
        if low.startswith("ip route-static "):
            return f"配置静态路由：{c[len('ip route-static '):].strip()}"
        if low.startswith("acl "):
            return f"创建/进入 ACL {c[len('acl '):].strip()}"
        if low.startswith("rule "):
            return f"配置 ACL 规则：{c[len('rule '):].strip()}"
        if low.startswith("lldp"):
            return "启用/配置 LLDP 邻居发现"
        if low.startswith("display "):
            return f"查询：{c[len('display '):].strip()}"

        # —— 兜底：结合操作类型给出语义化说明 ——
        action_hint = {
            "config_interface": "接口配置",
            "config_ospf": "OSPF 配置",
            "config_static_route": "静态路由配置",
            "config_vlan": "VLAN 配置",
            "config_acl": "ACL 配置",
        }.get(action)
        if action_hint:
            return f"{action_hint}命令"
        return ""
    
    def _gen_interface(self, params: Dict) -> List[str]: 
        """生成接口配置命令""" 
        iface = params.get("interface") 
        ip = params.get("ip") 
        mask = params.get("mask") 
        # 掩码转换 
        if mask and str(mask).isdigit(): 
            mask = self._prefix_to_netmask(int(mask)) 
        
        commands = ["system-view", f"interface {iface}"] 
        if ip and mask: 
            commands.append(f"ip address {ip} {mask}") 
        commands.append("quit") 
        return commands 
    
    def _gen_ospf(self, params: Dict) -> List[str]: 
        """生成OSPF配置命令""" 
        process = params.get("process", 1) 
        area = params.get("area", 0) 
        network = params.get("network") 
        wildcard = params.get("wildcard", "0.0.0.255") 
        
        commands = ["system-view", f"ospf {process}"] 
        if network: 
            commands.append(f"area {area}") 
            commands.append(f"network {network} {wildcard}") 
        commands.append("quit") 
        return commands 
    
    def _gen_static_route(self, params: Dict) -> List[str]: 
        """生成静态路由命令""" 
        dest = params.get("destination") 
        mask = params.get("mask") 
        next_hop = params.get("next_hop") 
        if mask and str(mask).isdigit(): 
            mask = self._prefix_to_netmask(int(mask)) 
        commands = ["system-view", f"ip route-static {dest} {mask} {next_hop}"] 
        return commands 
    
    def _gen_vlan(self, params: Dict) -> List[str]:
        """生成 VLAN 配置命令

        支持两种写法：
        - vlan_id: 10          -> vlan 10
        - vlan_id: [10, 20]    -> vlan batch 10 20
        """
        vlan_id = params.get("vlan_id") or params.get("vlan") or params.get("id")
        description = params.get("description")
        commands = ["system-view"]
        if isinstance(vlan_id, (list, tuple)):
            ids = " ".join(str(v) for v in vlan_id)
            commands.append(f"vlan batch {ids}")
        elif vlan_id is not None:
            commands.append(f"vlan {vlan_id}")
            if description:
                commands.append(f"description {description}")
            commands.append("quit")
        return commands

    def _gen_acl(self, params: Dict) -> List[str]:
        """生成 ACL 配置命令（含规则）"""
        acl_id = params.get("acl_id") or params.get("acl")
        rules = params.get("rules") or params.get("rule") or []
        if isinstance(rules, str):
            rules = [rules]
        commands = ["system-view"]
        if acl_id is not None:
            commands.append(f"acl {acl_id}")
            for r in rules:
                r = str(r)
                commands.append(r if r.lower().startswith("rule") else f"rule {r}")
            commands.append("quit")
        return commands

    def _gen_show(self, params: Dict) -> List[str]:
        """查询命令：无需进入系统视图，直接下发 display 命令"""
        cmd = params.get("command") or params.get("cmd")
        return [str(cmd)] if cmd else []
    
    def _generate_with_llm(self, task: Dict) -> List[str]: 
        """调用LLM生成未知类型的命令""" 
        try:
            prompt_template = load_prompt("command_gen.txt") 
            # 模板使用 {task} 占位符，这里传入完整的任务描述
            task_desc = (
                f"目标设备：{task.get('target_devices') or task.get('device') or '未指定'}；"
                f"操作类型：{task.get('action')}；"
                f"参数：{task.get('parameters', {})}"
            )
            prompt = prompt_template.format(task=task_desc)
            response = self.llm.invoke(prompt) 
            # 尝试解析返回的命令列表 
            return self._parse_command_list(response) 
        except Exception as e:
            logger.error(f"LLM command generation failed: {e}")
            return []
    
    def _prefix_to_netmask(self, prefix: int) -> str: 
        """将前缀长度转换为点分十进制掩码""" 
        mask = (0xffffffff << (32 - prefix)) & 0xffffffff 
        return f"{(mask >> 24) & 0xff}.{(mask >> 16) & 0xff}.{(mask >> 8) & 0xff}.{mask & 0xff}" 
    
    def _parse_command_list(self, text: str) -> List[str]: 
        """从LLM回复中提取命令列表

        兼容两种返回格式：
        1. 每行一条命令（提示词要求的格式）
        2. JSON 数组，如 ["system-view", "vlan 10", "quit"]
        """
        text = (text or "").strip()
        if not text:
            return []

        # 优先尝试解析 JSON 数组
        json_cmds = self._try_parse_json_array(text)
        if json_cmds:
            return json_cmds

        lines = text.split('\n') 
        commands = [] 
        for line in lines: 
            line = line.strip() 
            # 过滤掉 Markdown 代码块围栏与注释
            if line.startswith('```') or line in ('[', ']'):
                continue
            # 去掉行内的注释与 Markdown 符号
            if line and not line.startswith(('#', '//', '!', '*')): 
                commands.append(line) 
        return commands

    def _try_parse_json_array(self, text: str) -> List[str]:
        """若回复是 JSON 数组，则解析为命令列表；否则返回空列表"""
        start = text.find('[')
        end = text.rfind(']')
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
        return []
