"""配置命令生成器

设计要点
--------
1. **内置模板优先**：常见操作（接口/trunk/access/SVI/VLAN/OSPF/静态路由/ACL/查询）
   全部由确定性模板生成，不依赖 LLM，输出稳定、可测试。
2. **模板只负责"它认识"的参数**：若任务参数中出现模板未覆盖的键，
   在有 LLM 的情况下回退给 LLM 生成，避免静默丢参数导致命令不完整。
3. **名称归一化**：把 `G0/0/1` / `GE0/0/1` / `g 0/0/1` 统一展开为
   `GigabitEthernet0/0/1`，避免下发到 eNSP 时因缩写不被接受而失败。
"""
import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple

from .llm_client import LLMClient, LLMUnavailableError
from ..utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


class CommandGenerator:
    """根据结构化任务生成具体华为命令序列"""

    # 接口名前缀别名 → VRP 完整名称（按长度降序匹配，保证 GE 先于 E 命中）
    _IFACE_ALIASES = [
        ("10gigabitethernet", "XGigabitEthernet"),
        ("xgigabitethernet", "XGigabitEthernet"),
        ("gigabitethernet", "GigabitEthernet"),
        ("vlan-interface", "Vlanif"),
        ("10ge", "XGigabitEthernet"),
        ("xge", "XGigabitEthernet"),
        ("cellular", "Cellular"),
        ("loopback", "LoopBack"),
        ("ethernet", "Ethernet"),
        ("vlanif", "Vlanif"),
        ("tunnel", "Tunnel"),
        ("serial", "Serial"),
        ("null", "NULL"),
        ("meth", "MEth"),
        ("pos", "Pos"),
        ("eth", "Ethernet"),
        ("ge", "GigabitEthernet"),
        ("gi", "GigabitEthernet"),
        ("lo", "LoopBack"),
        ("me", "MEth"),
        ("tu", "Tunnel"),
        ("g", "GigabitEthernet"),
        ("e", "Ethernet"),
        ("s", "Serial"),
    ]

    # 各 action 模板实际消费的参数键（用于检测"被静默丢弃"的参数）
    _CONSUMED_KEYS = {
        "interface": {
            "interface", "iface", "port", "interface_name", "port_name",
            "ip", "ip_address", "mask", "netmask", "prefix", "prefix_length",
            "description", "desc", "name",
            "link_type", "port_type", "mode", "port_mode", "type", "linktype",
            "default_vlan", "access_vlan", "vlan_id", "vlan",
            "allowed_vlans", "allow_pass_vlans", "trunk_vlans", "vlans",
            "pvid", "trunk_pvid",
            "shutdown", "no_shutdown", "undo_shutdown",
            "speed", "duplex", "mtu",
        },
        "vlanif": {
            "vlan_id", "vlan", "vlanif", "interface", "iface", "id",
            "ip", "ip_address", "mask", "netmask", "prefix", "prefix_length",
            "description", "desc", "shutdown", "undo_shutdown",
        },
        "vlan": {
            "vlan_id", "vlan", "id", "vlan_ids", "vlans", "vlan_range",
            "description", "desc", "name",
        },
        "ospf": {
            "process", "process_id", "ospf", "router_id", "router-id",
            "area", "area_id", "network", "network_address", "networks",
            "wildcard", "wildcard_mask",
        },
        "static_route": {
            "destination", "dest", "network", "mask", "netmask", "prefix",
            "next_hop", "nexthop", "gateway", "preference", "description", "desc",
        },
        "acl": {"acl_id", "acl", "acl_number", "rules", "rule", "acl_type", "type", "name"},
        "show": {"command", "cmd", "commands"},
    }

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        # 内置模板（简化版）
        self.templates = {
            "config_interface": self._gen_interface,
            "config_interface_ip": self._gen_interface,
            "config_trunk": self._gen_interface,
            "config_trunk_port": self._gen_interface,
            "config_access": self._gen_interface,
            "config_access_port": self._gen_interface,
            "config_vlanif": self._gen_vlanif,
            "config_svi": self._gen_vlanif,
            "config_vlan_interface": self._gen_vlanif,
            "config_ospf": self._gen_ospf,
            "config_static_route": self._gen_static_route,
            "config_vlan": self._gen_vlan,
            "config_acl": self._gen_acl,
            "show_command": self._gen_show,
        }

        # action 名 → 内部模板族（用于参数键校验）
        self._action_family = {
            "config_interface": "interface",
            "config_interface_ip": "interface",
            "config_trunk": "interface",
            "config_trunk_port": "interface",
            "config_access": "interface",
            "config_access_port": "interface",
            "config_vlanif": "vlanif",
            "config_svi": "vlanif",
            "config_vlan_interface": "vlanif",
            "config_vlan": "vlan",
            "config_ospf": "ospf",
            "config_static_route": "static_route",
            "config_acl": "acl",
            "show_command": "show",
        }

    def is_available(self) -> bool:
        """AI 生成能力是否可用（内置模板不依赖 AI，未配置 .env 时仍可用）"""
        return bool(self.llm and self.llm.is_available())

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def generate(self, task: Dict[str, Any]) -> List[str]:
        """为单个任务生成命令列表"""
        action = task.get("action")
        params = self._prepare_params(action, task.get("parameters") or {})

        if action in self.templates:
            commands = self.templates[action](params)
            leftover = self._leftover_keys(action, params)
            if leftover and self.is_available():
                # 模板不认识这些参数 → 交给 LLM 生成完整命令，避免静默丢配置
                logger.info("action=%s 存在模板未覆盖的参数 %s，回退 LLM 生成", action, leftover)
                llm_cmds = self._generate_with_llm({**task, "parameters": params})
                if llm_cmds:
                    return llm_cmds
            return commands

        # 未知操作，使用 LLM 生成
        return self._generate_with_llm(task)

    def generate_with_comments(self, task: Dict[str, Any]) -> List[Dict[str, str]]:
        """生成「命令 + 注释」列表，用于在界面上直观展示将要下发的配置。

        返回值形如：
            [{"command": "system-view", "comment": "进入系统视图"},
             {"command": "vlan 10", "comment": "创建 VLAN 10"}]
        """
        items, _ = self.generate_with_comments_ex(task)
        return items

    def generate_with_comments_ex(
        self, task: Dict[str, Any]
    ) -> Tuple[List[Dict[str, str]], List[str]]:
        """同 generate_with_comments，额外返回告警列表（供 UI 提示"可能不完整"）。"""
        action = task.get("action")
        raw_params = task.get("parameters") or {}
        params = self._prepare_params(action, raw_params)
        commands = self.generate(task)

        warnings: List[str] = []
        leftover = self._leftover_keys(action, params)
        if leftover and not self.is_available():
            warnings.append(
                "以下参数未被内置模板使用，生成结果可能不完整："
                + "、".join(sorted(leftover))
                + "（配置 .env 后可由 AI 生成完整命令）"
            )

        # trunk 口未指定放通 VLAN 时给出提示（VRP 默认只放通 VLAN 1）
        # 注意：以"最终生成的命令"为准，而不是原始参数键，
        # 否则模型用 vlan / vlan_id 表达放通列表时会误报。
        link_type = self._norm_link_type(self._pick(params, "link_type", "port_type",
                                                    "mode", "port_mode", "type", "linktype"))
        has_allow_pass = any(
            c.strip().lower().startswith("port trunk allow-pass vlan")
            for c in commands
        )
        if link_type == "trunk" and not has_allow_pass:
            warnings.append("trunk 口未指定允许通过的 VLAN，VRP 默认仅放通 VLAN 1。")

        result: List[Dict[str, str]] = []
        for idx, cmd in enumerate(commands):
            result.append({
                "command": cmd,
                "comment": self._comment_for(cmd, action, params, commands, idx),
            })
        return result, warnings

    # ------------------------------------------------------------------
    # 参数预处理与校验
    # ------------------------------------------------------------------
    def _prepare_params(self, action: Optional[str], params: Dict[str, Any]) -> Dict[str, Any]:
        """根据 action 名补齐隐含参数（如 config_trunk 隐含 link_type=trunk）"""
        p = dict(params or {})

        if action in ("config_trunk", "config_trunk_port"):
            if not self._pick(p, "link_type", "port_type", "mode", "port_mode", "type"):
                p["link_type"] = "trunk"
        elif action in ("config_access", "config_access_port"):
            if not self._pick(p, "link_type", "port_type", "mode", "port_mode", "type"):
                p["link_type"] = "access"
        return p

    def _leftover_keys(self, action: Optional[str], params: Dict[str, Any]) -> set:
        """返回模板未消费、且值非空的参数键"""
        family = self._action_family.get(action or "")
        if not family:
            return set()
        consumed = self._CONSUMED_KEYS.get(family, set())
        leftover = set()
        for k, v in (params or {}).items():
            if k in consumed:
                continue
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            if isinstance(v, (list, dict, tuple, set)) and len(v) == 0:
                continue
            leftover.add(k)
        return leftover

    @staticmethod
    def _pick(params: Dict[str, Any], *keys: str) -> Any:
        """按顺序取第一个"有值"的参数"""
        for k in keys:
            if k in params and params[k] not in (None, "", [], {}):
                return params[k]
        return None

    # ------------------------------------------------------------------
    # 归一化工具
    # ------------------------------------------------------------------
    def _normalize_interface(self, name: Any) -> str:
        """把接口缩写展开为 VRP 完整名：G0/0/1 → GigabitEthernet0/0/1

        已是完整名称时原样返回（幂等）。
        """
        if name is None:
            return ""
        s = str(name).strip().replace(" ", "")
        if not s:
            return ""
        low = s.lower()
        for alias, full in self._IFACE_ALIASES:  # 已按长度降序排列
            if low.startswith(alias):
                rest = s[len(alias):]
                if rest and rest[0].isdigit():
                    return full + rest
        return s

    @staticmethod
    def _norm_link_type(value: Any) -> Optional[str]:
        """归一化链路类型：trunk / access / hybrid，无法识别返回 None"""
        if value is None:
            return None
        v = str(value).strip().lower()
        if not v:
            return None
        if "trunk" in v or "干道" in v:
            return "trunk"
        if "hybrid" in v or "混合" in v:
            return "hybrid"
        if "access" in v or "接入" in v:
            return "access"
        return None

    def _parse_vlan_ids(self, value: Any) -> List[int]:
        """把各种写法的 VLAN 描述解析为去重升序的整数列表

        支持：2 / "2" / [2,3] / "2,3" / "2、3" / "2 3" / "2-5" / "2 to 5" /
              "2至5" / "vlan2、3"
        """
        if value is None:
            return []
        if isinstance(value, bool):
            return []
        if isinstance(value, int):
            return [value] if 0 < value <= 4094 else []
        if isinstance(value, (list, tuple, set)):
            out: List[int] = []
            for v in value:
                out.extend(self._parse_vlan_ids(v))
            return sorted(set(out))

        s = str(value).strip()
        if not s:
            return []
        # 统一分隔符
        s = re.sub(r"^(?:vlan|vlanif|vlan-id)\s*", "", s, flags=re.IGNORECASE)
        s = s.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")

        ids = set()
        # 先处理区间：2-5 / 2~5 / 2 to 5 / 2至5 / 2到5
        for m in re.finditer(r"(\d+)\s*(?:-|~|—|to|至|到)\s*(\d+)", s, re.IGNORECASE):
            a, b = int(m.group(1)), int(m.group(2))
            lo, hi = min(a, b), max(a, b)
            if lo <= 0 or hi > 4094:
                continue
            ids.update(range(lo, hi + 1))
        # 去掉区间片段后，再取剩余裸数字
        rest = re.sub(r"(\d+)\s*(?:-|~|—|to|至|到)\s*(\d+)", " ", s, flags=re.IGNORECASE)
        for tok in re.split(r"[,\s]+", rest):
            if tok.isdigit():
                n = int(tok)
                if 0 < n <= 4094:
                    ids.add(n)
        return sorted(ids)

    @staticmethod
    def _vlan_segments(ids: List[int]) -> str:
        """把 VLAN 列表压缩为 VRP 可用的片段串

        [2, 3]        -> "2 3"
        [2, 3, 4]     -> "2 to 4"
        [2, 3, 5, 10] -> "2 3 5 10"
        """
        if not ids:
            return ""
        ids = sorted(set(ids))
        segs: List[str] = []
        start = prev = ids[0]
        run = 1
        for cur in ids[1:]:
            if cur == prev + 1:
                run += 1
                prev = cur
                continue
            segs.append(f"{start} to {prev}" if run >= 3 else
                        " ".join(str(x) for x in range(start, prev + 1)))
            start = prev = cur
            run = 1
        segs.append(f"{start} to {prev}" if run >= 3 else
                    " ".join(str(x) for x in range(start, prev + 1)))
        return " ".join(segs)

    def _to_netmask(self, mask: Any) -> Optional[str]:
        """掩码归一化：24 / "24" / "/24" → 255.255.255.0；点分十进制原样返回"""
        if mask is None or str(mask).strip() == "":
            return None
        s = str(mask).strip().lstrip("/")
        if s.isdigit():
            prefix = int(s)
            if 0 <= prefix <= 32:
                return self._prefix_to_netmask(prefix)
            return None
        return s

    # ------------------------------------------------------------------
    # 各操作的命令模板
    # ------------------------------------------------------------------
    def _gen_interface(self, params: Dict) -> List[str]:
        """生成接口配置命令

        覆盖：IP 地址、描述、链路类型（access/trunk/hybrid）、
              access 默认 VLAN、trunk 放通 VLAN / PVID、shutdown、速率双工。
        """
        iface = self._pick(params, "interface", "iface", "port",
                           "interface_name", "port_name")
        if not iface:
            return []

        iface = self._normalize_interface(iface)
        ip = self._pick(params, "ip", "ip_address")
        mask = self._to_netmask(self._pick(params, "mask", "netmask",
                                          "prefix", "prefix_length"))
        description = self._pick(params, "description", "desc", "name")
        link_type = self._norm_link_type(
            self._pick(params, "link_type", "port_type", "mode",
                       "port_mode", "type", "linktype")
        )
        default_vlan = self._pick(params, "default_vlan", "access_vlan",
                                  "vlan", "vlan_id")
        allowed = self._pick(params, "allowed_vlans", "allow_pass_vlans",
                             "trunk_vlans", "vlans")
        pvid = self._pick(params, "pvid", "trunk_pvid")

        # 兼容模型把"放通 VLAN"写成 vlan / vlan_id / 单个 vlan 的情况，
        # 避免这些键被判定为"已消费"却实际未参与命令生成。
        if allowed is None:
            allowed = self._pick(params, "vlan", "vlan_id")

        commands = ["system-view", f"interface {iface}"]

        if description:
            commands.append(f"description {description}")

        # 未显式指定链路类型，但给了接入 VLAN → 视为 access 口
        if link_type is None and default_vlan is not None:
            link_type = "access"

        if link_type in ("access", "trunk", "hybrid"):
            commands.append(f"port link-type {link_type}")

            if link_type == "access" and default_vlan is not None:
                vids = self._parse_vlan_ids(default_vlan)
                if vids:
                    commands.append(f"port default vlan {vids[0]}")

            elif link_type == "trunk":
                if pvid is not None:
                    pv = self._parse_vlan_ids(pvid)
                    if pv:
                        commands.append(f"port trunk pvid vlan {pv[0]}")
                vids = self._parse_vlan_ids(allowed)
                if vids:
                    commands.append(f"port trunk allow-pass vlan {self._vlan_segments(vids)}")

            elif link_type == "hybrid":
                vids = self._parse_vlan_ids(allowed)
                if vids:
                    commands.append(
                        f"port hybrid tagged vlan {self._vlan_segments(vids)}"
                    )
                if default_vlan is not None:
                    dv = self._parse_vlan_ids(default_vlan)
                    if dv:
                        commands.append(
                            f"port hybrid untagged vlan {self._vlan_segments(dv)}"
                        )

        if ip and mask:
            commands.append(f"ip address {ip} {mask}")
        elif ip and not mask:
            # 只给了 IP（可能自带 /24 前缀）
            ip_str = str(ip)
            if "/" in ip_str:
                addr, _prefix = ip_str.split("/", 1)
                nm = self._to_netmask(_prefix)
                if nm:
                    commands.append(f"ip address {addr.strip()} {nm}")
                else:
                    commands.append(f"ip address {addr.strip()}")
            else:
                commands.append(f"ip address {ip_str}")

        # shutdown 处理：True → shutdown；False → undo shutdown
        shutdown = params.get("shutdown")
        undo_shutdown = params.get("undo_shutdown", params.get("no_shutdown"))
        if shutdown is True or str(shutdown).lower() in ("true", "1", "up", "enable"):
            commands.append("shutdown")
        elif shutdown is False or undo_shutdown:
            commands.append("undo shutdown")

        commands.append("quit")
        return self._dedupe_adjacent(commands)

    def _gen_vlanif(self, params: Dict) -> List[str]:
        """生成 VLAN 三层接口（SVI / Vlanif）配置命令"""
        vlan_id = self._pick(params, "vlan_id", "vlan", "vlanif", "id")
        iface = self._pick(params, "interface", "iface")
        if iface:
            iface = self._normalize_interface(iface)
            m = re.search(r"(\d+)", str(iface))
            vlan_num = int(m.group(1)) if m else None
        else:
            ids = self._parse_vlan_ids(vlan_id)
            vlan_num = ids[0] if ids else None
            iface = f"Vlanif{vlan_num}" if vlan_num else None

        if not iface:
            return []

        ip = self._pick(params, "ip", "ip_address")
        mask = self._to_netmask(self._pick(params, "mask", "netmask",
                                           "prefix", "prefix_length"))
        description = self._pick(params, "description", "desc")

        commands = ["system-view"]
        if vlan_num:
            commands.extend([f"vlan {vlan_num}", "quit"])
        commands.append(f"interface {iface}")
        if description:
            commands.append(f"description {description}")
        if ip and mask:
            commands.append(f"ip address {ip} {mask}")
        elif ip:
            commands.append(f"ip address {ip}")
        if params.get("shutdown") is False or params.get("undo_shutdown"):
            commands.append("undo shutdown")
        commands.append("quit")
        return commands

    def _gen_ospf(self, params: Dict) -> List[str]:
        """生成 OSPF 配置命令

        修正：进入 area 视图后需 quit 两次才能回到系统视图
        （旧实现只 quit 一次，会停在 OSPF 视图，导致后续命令层级错乱）。
        """
        process = self._pick(params, "process", "process_id", "ospf") or 1
        area = self._pick(params, "area", "area_id")
        area = 0 if area is None else area
        wildcard = self._pick(params, "wildcard", "wildcard_mask") or "0.0.0.255"
        router_id = self._pick(params, "router_id", "router-id")

        networks = params.get("networks")
        net_list: List[Any] = []
        if isinstance(networks, (list, tuple)):
            net_list.extend(networks)
        elif networks:
            net_list.append(networks)

        single = self._pick(params, "network", "network_address")
        if single:
            if isinstance(single, (list, tuple)):
                net_list.extend(single)
            elif isinstance(single, str) and ("," in single or "，" in single):
                net_list.extend(re.split(r"[,，\s]+", single))
            else:
                net_list.append(single)

        commands = ["system-view", f"ospf {process}"]
        if router_id:
            commands.append(f"router id {router_id}")

        if net_list:
            commands.append(f"area {area}")
            for net in net_list:
                net_s = str(net).strip()
                if not net_s:
                    continue
                # network 支持 "10.0.0.0/24" 写法
                if "/" in net_s:
                    addr, prefix = net_s.split("/", 1)
                    wc = self._wildcard_from_prefix(prefix)
                    commands.append(f"network {addr.strip()} {wc}")
                elif net_s.count(" ") >= 1:
                    commands.append(f"network {net_s}")
                else:
                    commands.append(f"network {net_s} {wildcard}")
            commands.append("quit")  # 退出 area

        commands.append("quit")  # 退出 ospf 进程
        return commands

    def _gen_static_route(self, params: Dict) -> List[str]:
        """生成静态路由命令"""
        dest = self._pick(params, "destination", "dest", "network")
        if not dest:
            return []
        dest_s = str(dest).strip()

        mask = self._to_netmask(self._pick(params, "mask", "netmask", "prefix"))
        if "/" in dest_s:
            dest_s, prefix = dest_s.split("/", 1)
            dest_s = dest_s.strip()
            if not mask:
                mask = self._to_netmask(prefix)

        next_hop = self._pick(params, "next_hop", "nexthop", "gateway")
        preference = self._pick(params, "preference")
        description = self._pick(params, "description", "desc")

        parts = ["ip route-static", dest_s]
        if mask:
            parts.append(mask)
        if next_hop:
            parts.append(str(next_hop))
        if preference:
            parts.append(f"preference {preference}")
        if description:
            parts.append(f"description {description}")

        return ["system-view", " ".join(parts)]

    def _gen_vlan(self, params: Dict) -> List[str]:
        """生成 VLAN 配置命令

        支持：
        - vlan_id: 10                -> vlan 10 + description + quit
        - vlan_id: [10, 20, 30]      -> vlan batch 10 20 30
        - vlan_id: "2-5"             -> vlan batch 2 to 5
        - vlan_id: [10, 20] + 各自的 description 不适用，批量时忽略 description
        """
        vlan_raw = self._pick(params, "vlan_id", "vlan", "id",
                              "vlan_ids", "vlans", "vlan_range")
        description = self._pick(params, "description", "desc", "name")
        ids = self._parse_vlan_ids(vlan_raw)

        commands = ["system-view"]
        if not ids:
            return commands

        if len(ids) == 1:
            commands.append(f"vlan {ids[0]}")
            if description:
                commands.append(f"description {description}")
            commands.append("quit")
        else:
            commands.append(f"vlan batch {self._vlan_segments(ids)}")
        return commands

    def _gen_acl(self, params: Dict) -> List[str]:
        """生成 ACL 配置命令（含规则）"""
        acl_id = self._pick(params, "acl_id", "acl", "acl_number")
        rules = self._pick(params, "rules", "rule") or []
        acl_type = self._pick(params, "acl_type", "type")
        acl_name = self._pick(params, "name")

        if isinstance(rules, str):
            rules = [rules]

        commands = ["system-view"]
        if acl_id is not None or acl_name:
            if acl_name:
                suffix = ""
                if acl_type:
                    t = str(acl_type).lower()
                    if t.startswith("adv") or "高级" in t:
                        suffix = " advance"
                    elif t.startswith("basic") or "基本" in t:
                        suffix = " basic"
                commands.append(f"acl name {acl_name}{suffix}")
            else:
                commands.append(f"acl {acl_id}")
            for r in rules:
                r = str(r).strip()
                if not r:
                    continue
                commands.append(r if r.lower().startswith("rule") else f"rule {r}")
            commands.append("quit")
        return commands

    def _gen_show(self, params: Dict) -> List[str]:
        """查询命令：无需进入系统视图，直接下发 display 命令"""
        cmd = self._pick(params, "command", "cmd")
        if isinstance(cmd, (list, tuple)):
            return [str(c) for c in cmd if str(c).strip()]
        if isinstance(cmd, str) and ("\n" in cmd):
            return [line.strip() for line in cmd.splitlines() if line.strip()]
        return [str(cmd)] if cmd else []

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _dedupe_adjacent(commands: List[str]) -> List[str]:
        """去除相邻重复命令（保持顺序），避免出现连续两个 quit/system-view"""
        out: List[str] = []
        for c in commands:
            if out and out[-1] == c:
                continue
            out.append(c)
        return out

    def _wildcard_from_prefix(self, prefix: Any) -> str:
        """前缀长度 → 通配符掩码：24 → 0.0.0.255"""
        try:
            p = int(str(prefix).strip().lstrip("/"))
        except (TypeError, ValueError):
            return "0.0.0.255"
        mask = (0xffffffff << (32 - p)) & 0xffffffff if 0 <= p <= 32 else 0xffffff00
        inv = ~mask & 0xffffffff
        return f"{(inv >> 24) & 0xff}.{(inv >> 16) & 0xff}.{(inv >> 8) & 0xff}.{inv & 0xff}"

    def _prefix_to_netmask(self, prefix: int) -> str:
        """将前缀长度转换为点分十进制掩码"""
        mask = (0xffffffff << (32 - prefix)) & 0xffffffff
        return f"{(mask >> 24) & 0xff}.{(mask >> 16) & 0xff}.{(mask >> 8) & 0xff}.{mask & 0xff}"

    # ------------------------------------------------------------------
    # 命令注释
    # ------------------------------------------------------------------
    def _comment_for(self, cmd: str, action: str, params: Dict[str, Any],
                     all_cmds: List[str], idx: Optional[int] = None) -> str:
        """为单条 VRP 命令生成中文注释（基于命令语义与任务参数）"""
        c = (cmd or "").strip()
        low = c.lower()

        # —— 通用视图切换 ——
        if low == "system-view":
            return "进入系统视图（配置模式）"
        if low == "quit":
            depth = self._view_depth(all_cmds, idx)
            # depth 为当前已进入的视图层数：1=系统视图，2=接口/协议等子视图
            if depth <= 1:
                return "退出至用户视图"
            if depth == 2:
                return "返回系统视图"
            return "返回上一级视图"
        if low == "return":
            return "直接返回用户视图"
        if low == "undo" or low.startswith("undo "):
            if low == "undo shutdown":
                return "开启接口（取消关闭）"
            return f"删除/取消配置：{c[5:].strip()}"
        if low == "shutdown":
            return "关闭接口"
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
        if low.startswith("port link-type "):
            lt = c[len("port link-type "):].strip()
            label = {"trunk": "Trunk（干道）", "access": "Access（接入）",
                     "hybrid": "Hybrid（混合）"}.get(lt.lower(), lt)
            return f"设置端口链路类型为 {label}"
        if low.startswith("port default vlan "):
            return f"将端口加入 VLAN {c[len('port default vlan '):].strip()}（untagged）"
        if low.startswith("port trunk allow-pass vlan "):
            return f"允许 VLAN {c[len('port trunk allow-pass vlan '):].strip()} 通过（tagged）"
        if low.startswith("port trunk pvid vlan "):
            return f"设置 Trunk 口 PVID 为 VLAN {c[len('port trunk pvid vlan '):].strip()}"
        if low.startswith("port hybrid tagged vlan "):
            return f"Hybrid 口以 tagged 方式放通 VLAN {c[len('port hybrid tagged vlan '):].strip()}"
        if low.startswith("port hybrid untagged vlan "):
            return f"Hybrid 口以 untagged 方式放通 VLAN {c[len('port hybrid untagged vlan '):].strip()}"
        if low.startswith("vlan batch "):
            return f"批量创建 VLAN：{c[len('vlan batch '):].strip()}"
        if low.startswith("vlan "):
            return f"创建 VLAN {c[len('vlan '):].strip()}"
        if low.startswith("ospf "):
            return f"启动 OSPF 进程 {c[len('ospf '):].strip()}"
        if low.startswith("router id "):
            return f"设置 Router ID 为 {c[len('router id '):].strip()}"
        if low.startswith("area "):
            return f"进入 OSPF 区域 {c[len('area '):].strip()}"
        if low.startswith("network "):
            return f"宣告网段：{c[len('network '):].strip()}"
        if low.startswith("ip route-static "):
            return f"配置静态路由：{c[len('ip route-static '):].strip()}"
        if low.startswith("acl name "):
            return f"创建/进入命名 ACL：{c[len('acl name '):].strip()}"
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
            "config_interface_ip": "接口 IP 配置",
            "config_trunk": "Trunk 口配置",
            "config_trunk_port": "Trunk 口配置",
            "config_access": "Access 口配置",
            "config_access_port": "Access 口配置",
            "config_vlanif": "VLAN 三层接口配置",
            "config_svi": "VLAN 三层接口配置",
            "config_vlan_interface": "VLAN 三层接口配置",
            "config_ospf": "OSPF 配置",
            "config_static_route": "静态路由配置",
            "config_vlan": "VLAN 配置",
            "config_acl": "ACL 配置",
        }.get(action)
        if action_hint:
            return f"{action_hint}命令"
        return ""

    @staticmethod
    def _view_depth(all_cmds: List[str], idx: Optional[int]) -> int:
        """计算执行到 all_cmds[idx]（不含）时，当前所处的视图层数"""
        if idx is None:
            return 1
        depth = 0
        entry_prefixes = (
            "interface ", "vlan ", "acl ", "ospf ", "area ", "bgp ",
            "ip pool ", "ipv6 pool ", "aaa", "vlanif ",
        )
        for c in all_cmds[:idx]:
            cl = c.strip().lower()
            if cl == "system-view":
                depth = 1
            elif cl.startswith(entry_prefixes):
                # "vlan batch 10 20" 不进入 VLAN 视图
                if cl.startswith("vlan batch"):
                    continue
                depth += 1
            elif cl == "return":
                depth = 0
            elif cl == "quit":
                depth = max(0, depth - 1)
        return depth

    # ------------------------------------------------------------------
    # LLM 生成
    # ------------------------------------------------------------------
    def _generate_with_llm(self, task: Dict) -> List[str]:
        """调用LLM生成未知类型的命令

        当 LLM 未配置时抛出 LLMUnavailableError（由 UI 提示配置 .env），
        以便区分"模型不可用"与"模型返回为空"两种情况。
        """
        prompt_template = load_prompt("command_gen.txt")
        # 模板使用 {task} 占位符，这里传入完整的任务描述
        task_desc = (
            f"目标设备：{task.get('target_devices') or task.get('device') or '未指定'}；"
            f"操作类型：{task.get('action')}；"
            f"参数：{json.dumps(task.get('parameters', {}), ensure_ascii=False)}"
        )
        prompt = prompt_template.format(task=task_desc)
        try:
            response = self.llm.invoke(prompt)
            return self._parse_command_list(response)
        except LLMUnavailableError:
            # 未配置 Key：向上抛出，交由 UI 给出配置引导
            raise
        except Exception as e:
            logger.error(f"LLM command generation failed: {e}")
            return []

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------
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
