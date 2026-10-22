"""本地规则解析器（不依赖大模型）

作用
----
把常见的中文网络配置指令，用正则确定性地解析为结构化任务，
再交给 `CommandGenerator` 的模板生成华为 VRP 命令。

为什么要它
--------
1. 未配置 `.env` 时，`IntentParser` 会抛 `LLMUnavailableError`，
   此时常见指令仍可用本地规则解析，保证基础功能可用；
2. 大模型返回空结果或参数缺失时，可作为兜底，避免"命令少了一半"；
3. 解析过程完全确定、可测试，不依赖网络。

覆盖范围（常见中考场景）
------------------------
- Trunk / Access / Hybrid 口配置（链路类型 + 放通 VLAN + PVID）
- 接口 IP 地址
- VLAN 三层接口（SVI / Vlanif）网关
- 创建 VLAN（单个 / 批量 / 区间）
- OSPF（进程、区域、network）
- 静态路由
- 查询命令（路由表 / 接口 / 版本 / 当前配置等）
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from .command_gen import CommandGenerator


# 设备名：字母前缀 + 数字，如 R1 / S1 / AR2 / LSW1 / SW3
_DEVICE_RE = r"[A-Za-z]{1,5}\d{1,3}"

# 接口：允许缩写与完整名，物理口必须带 x/y/z 斜杠
_IFACE_FULL_RE = re.compile(
    r"((?:GigabitEthernet|XGigabitEthernet|Ethernet|Eth|GE|Gi|G|E|Serial|S|MEth|NULL)"
    r"\s*\d+(?:/\d+){1,3}(?:\.\d+)?)",
    re.IGNORECASE,
)
_IFACE_VLANIF_RE = re.compile(r"\b(vlanif\s*\d+)\b", re.IGNORECASE)

_SHOW_MAP = [
    ("路由表", "display ip routing-table"),
    ("路由", "display ip routing-table"),
    ("接口状态", "display ip interface brief"),
    ("接口", "display ip interface brief"),
    ("版本", "display version"),
    ("当前配置", "display current-configuration"),
    ("配置", "display current-configuration"),
    ("mac", "display mac-address"),
    ("arp", "display arp"),
    ("vlan", "display vlan"),
    ("邻居", "display lldp neighbor brief"),
]


class RuleParser:
    """基于正则的本地意图解析器"""

    def __init__(self):
        self._vlan = CommandGenerator(llm_client=None)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def can_handle(self, text: str) -> bool:
        """能否解析（有可识别的意图关键字即可）"""
        return bool(self.parse(text))

    def parse(self, text: str) -> List[Dict[str, Any]]:
        """解析指令，返回任务列表（无法识别时返回空列表）"""
        if not text or not text.strip():
            return []
        raw = text.strip()
        t = raw.lower()

        devices = self._extract_devices(raw)
        iface = self._extract_interface(raw)

        # 1) 查询类：查看 / 显示 / 查询（且不含配置动词）
        if self._is_show(t):
            cmd = self._show_command(t)
            if cmd:
                return [self._task(devices[0] if devices else "", "show_command",
                                   {"command": cmd})]

        # 2) OSPF
        if "ospf" in t:
            return self._parse_ospf(raw, t, devices)

        # 3) 静态路由
        if "静态路由" in raw or "静态路" in raw or "route-static" in t:
            return self._parse_static_route(raw, t, devices)

        # 4) VLAN 三层接口（SVI / 网关）
        if self._is_vlanif(t) and devices:
            return self._parse_vlanif(raw, t, devices[0])

        # 5) 接口配置（Trunk / Access / Hybrid / IP）
        if iface and devices:
            return [self._parse_interface(raw, t, devices[0], iface)]

        # 6) 创建 VLAN
        if self._is_vlan_create(t) and devices:
            vids = self._extract_vlans(raw)
            if vids:
                return [self._task(devices[0], "config_vlan", {"vlan_id": vids})]

        return []

    # ------------------------------------------------------------------
    # 各意图的解析
    # ------------------------------------------------------------------
    def _parse_interface(self, raw: str, t: str, device: str,
                         iface: str) -> Dict[str, Any]:
        params: Dict[str, Any] = {"interface": iface}

        link_type = self._extract_link_type(t)
        if link_type:
            params["link_type"] = link_type

        vids = self._extract_vlans(raw)

        if link_type == "trunk":
            if vids:
                params["allowed_vlans"] = vids
        elif link_type == "access":
            if vids:
                params["default_vlan"] = vids[0]
        elif vids:
            # 未说链路类型但提到了 VLAN → 按接入处理
            params["default_vlan"] = vids[0]

        pvid = self._extract_pvid(raw, t)
        if pvid and link_type == "trunk":
            params["pvid"] = pvid

        ip, prefix = self._extract_ip(raw)
        if ip:
            params["ip"] = ip
            if prefix:
                params["mask"] = prefix

        desc = self._extract_description(raw)
        if desc:
            params["description"] = desc

        return self._task(device, "config_interface", params)

    def _parse_vlanif(self, raw: str, t: str, device: str) -> List[Dict[str, Any]]:
        vids = self._extract_vlans(raw)
        if not vids:
            m = re.search(r"vlan\s*(\d+)", t)
            if m:
                vids = [int(m.group(1))]
        if not vids:
            return []
        params: Dict[str, Any] = {"vlan_id": vids[0]}
        ip, prefix = self._extract_ip(raw)
        if ip:
            params["ip"] = ip
            if prefix:
                params["mask"] = prefix
        return [self._task(device, "config_vlanif", params)]

    def _parse_ospf(self, raw: str, t: str,
                    devices: List[str]) -> List[Dict[str, Any]]:
        if not devices:
            return []
        m = re.search(r"ospf\s*(\d+)", t)
        process = int(m.group(1)) if m else 1
        m = re.search(r"area\s*(\d+)", t)
        area = int(m.group(1)) if m else 0

        ips = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?", raw)
        network = ""
        for ip, prefix in ips:
            if prefix:
                network = f"{ip}/{prefix}"
                break

        params: Dict[str, Any] = {"process": process, "area": area}
        if network:
            params["network"] = network

        # 多设备时每台各生成一个任务（网段无法逐台区分，统一沿用）
        return [self._task(d, "config_ospf", dict(params)) for d in devices]

    def _parse_static_route(self, raw: str, t: str,
                            devices: List[str]) -> List[Dict[str, Any]]:
        if not devices:
            return []
        cidr = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})\s*/\s*(\d{1,2})", raw)
        ips = re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", raw)

        params: Dict[str, Any] = {}
        if cidr:
            params["destination"] = cidr.group(1)
            params["mask"] = int(cidr.group(2))
            rest = [ip for ip in ips if ip != cidr.group(1)]
            if rest:
                params["next_hop"] = rest[0]
        elif len(ips) >= 2:
            params["destination"], params["next_hop"] = ips[0], ips[1]
        else:
            return []

        return [self._task(devices[0], "config_static_route", params)]

    # ------------------------------------------------------------------
    # 抽取工具
    # ------------------------------------------------------------------
    def _extract_devices(self, raw: str) -> List[str]:
        """抽取指令中的设备名（按原文出现顺序去重）

        先剥离接口片段，避免把 G0/0/1 里的 G 误当设备；
        再收集所有"字母+数字"独立标识，并排除 vlan10 / area0 等非设备词。
        这样 "在R1和R2之间配置OSPF" 能同时得到 [R1, R2]。
        """
        cleaned = _IFACE_FULL_RE.sub(" ", raw)
        cleaned = _IFACE_VLANIF_RE.sub(" ", cleaned)

        found: List[str] = []
        # 注意：不能用 \b —— 中文也算 \w，中英文之间不存在词边界。
        # 这里用显式的「前后不是 ASCII 字母/数字」断言。
        pattern = rf"(?<![A-Za-z0-9])([A-Za-z]{{1,5}}\d{{1,3}})(?![A-Za-z0-9])"
        for m in re.finditer(pattern, cleaned):
            d = m.group(1)
            if self._is_device_token(d) and d not in found:
                found.append(d)
        return found

    @staticmethod
    def _is_device_token(token: str) -> bool:
        """排除 vlan10 / area0 / ospf1 等非设备标识"""
        low = token.lower()
        blocked = ("vlan", "area", "ospf", "bgp", "acl", "rip", "isis", "ge", "eth")
        if any(low.startswith(b) for b in blocked):
            return False
        # 设备名习惯首字母大写（R1 / S1 / AR2）
        return token[0].isupper() or token[0].isdigit()

    def _extract_interface(self, raw: str) -> Optional[str]:
        m = _IFACE_VLANIF_RE.search(raw)
        if m:
            return re.sub(r"\s+", "", m.group(1))
        m = _IFACE_FULL_RE.search(raw)
        if m:
            return re.sub(r"\s+", "", m.group(1))
        return None

    @staticmethod
    def _extract_link_type(t: str) -> Optional[str]:
        if "trunk" in t or "干道" in t:
            return "trunk"
        if "hybrid" in t or "混合" in t:
            return "hybrid"
        if "access" in t or "接入" in t:
            return "access"
        return None

    def _extract_vlans(self, raw: str) -> List[int]:
        """抽取指令中的 VLAN 号（"允许vlan2、3通过" → [2, 3]）"""
        ids: List[int] = []
        # 匹配 vlan 关键字之后的一段数字/分隔符
        for m in re.finditer(r"vlan\s*([0-9][0-9,，、\-~至到\s]*)", raw, re.IGNORECASE):
            ids.extend(self._vlan._parse_vlan_ids(m.group(1)))
        # 形如 "VLAN 10 的网关" / "创建VLAN10"
        for m in re.finditer(r"vlan\s*(\d+)", raw, re.IGNORECASE):
            ids.append(int(m.group(1)))
        seen: List[int] = []
        for v in ids:
            if 0 < v <= 4094 and v not in seen:
                seen.append(v)
        return sorted(seen)

    @staticmethod
    def _extract_pvid(raw: str, t: str) -> Optional[int]:
        m = re.search(r"pvid\s*(?:为|是|=)?\s*(?:vlan)?\s*(\d+)", t)
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_ip(raw: str) -> Tuple[Optional[str], Optional[int]]:
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})\s*/\s*(\d{1,2})", raw)
        if m:
            return m.group(1), int(m.group(2))
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})\s+(?:掩码|子网掩码)?\s*(255\.\d{1,3}\.\d{1,3}\.\d{1,3})", raw)
        if m:
            return m.group(1), None
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", raw)
        if m:
            return m.group(1), None
        return None, None

    @staticmethod
    def _extract_description(raw: str) -> Optional[str]:
        m = re.search(r"描述\s*(?:为|是|:|：)\s*[「\"']?([^，。；,;]+)[」\"']?", raw)
        return m.group(1).strip() if m else None

    @staticmethod
    def _is_show(t: str) -> bool:
        if not any(k in t for k in ("查看", "显示", "查询", "show", "看一")):
            return False
        # 含"配置/设为/改为/创建/加入"视为配置意图，不是查询
        if any(k in t for k in ("配置", "设为", "设置", "改为", "创建", "加入", "允许")):
            return False
        return True

    @staticmethod
    def _show_command(t: str) -> Optional[str]:
        for key, cmd in _SHOW_MAP:
            if key in t:
                return cmd
        return "display current-configuration"

    @staticmethod
    def _is_vlanif(t: str) -> bool:
        return any(k in t for k in ("vlanif", "svi", "三层接口", "网关", "网关地址"))

    @staticmethod
    def _is_vlan_create(t: str) -> bool:
        if not any(k in t for k in ("创建", "新建", "添加", "划分", "批量")):
            return False
        return "vlan" in t

    @staticmethod
    def _task(device: str, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "device": device,
            "action": action,
            "parameters": params,
            "parsed_by": "rule",
        }
