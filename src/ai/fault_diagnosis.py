"""故障诊断模块

背景（用户反馈的错误结论）：
    S1 的 VLAN 配置实际是成功的（已 `vlan batch 2 3`、G0/0/1 为 trunk 且允许 vlan 2/3 通过），
    但故障分析却输出「S1 的 VLAN 配置未成功……仅存在默认 Vlanif1 接口……」

根因：
    旧版诊断只采集 `display ip routing-table` 与 `display ip interface brief` 两条 **三层** 命令。
    VLAN 属于二层配置，且 S1 没给这些 VLAN 配 IP，`display ip interface brief` 里根本不会出现
    它们 —— 模型把「输出里没看到」直接当成「设备上没有」，即典型的
    「证据缺失 = 否定证据」谬误。

修复分三层：
1. 采集层：按故障描述动态补齐二层/协议命令（display vlan / display port vlan /
   display interface brief / display current-configuration 等），并在提示词里声明每条命令
   的覆盖范围（哪条能证明什么、哪条天生看不到二层）。
2. 解析层：由程序把回显确定性地解析成「关键事实」（哪些 VLAN 存在、端口链路类型与允许
   列表、接口状态、配置命令行），模型只基于事实归因，不再靠上下文猜。
3. 提示词层：禁止「未采集 = 未配置」；强制输出证据缺口与置信度；要求尊重用户给定的前提
   （例如「不考虑 Vlanif 接口的配置」）。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMClient, LLMUnavailableError
from ..utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 采集命令与覆盖范围
# ---------------------------------------------------------------------------

#: 每条命令「能证明什么」。会随提示词一起交给模型，避免它把某个维度的缺席
#: 误解成设备上没有配置。
COMMAND_PURPOSE: Dict[str, str] = {
    "display vlan": "二层证据：已创建的 VLAN 列表及每个 VLAN 加入的端口（Ports 列为空=该 VLAN 已创建但无端口加入）",
    "display port vlan": "二层证据：各端口链路类型（access/trunk/hybrid）、PVID、Trunk 允许通过的 VLAN 列表",
    "display interface brief": "物理证据：各接口物理/协议状态（含管理口、空闲口）",
    "display ip interface brief": "三层证据：**仅列出配置了 IP 的三层接口**；未配 IP 的 VLAN/端口不会出现在这里",
    "display ip routing-table": "三层证据：路由表内容",
    "display current-configuration": "配置证据：设备当前完整配置（含 vlan batch、port link-type、ip address 等命令行）",
    "display ospf peer brief": "协议证据：OSPF 邻居状态",
    "display stp brief": "二层证据：生成树端口角色与状态",
    "display mac-address": "二层证据：MAC 地址表",
    "display arp": "三层证据：ARP 表",
    "display vrrp": "可靠性证据：VRRP 备份组状态",
    "display dhcp server statistics": "服务证据：DHCP 服务统计",
}

#: 每次诊断都采集的基础命令（覆盖二层存在性 + 端口归属 + 物理状态 + 三层状态）
BASE_COLLECT_COMMANDS: List[str] = [
    "display vlan",
    "display port vlan",
    "display interface brief",
    "display ip interface brief",
    "display ip routing-table",
]

#: 故障描述命中关键词时追加的命令
#: 说明：只要涉及 VLAN/Trunk/配置生效性问题，就必须拿到 current-configuration
#: 这条「唯一能直接证明配置存在」的证据。
CONDITIONAL_COMMANDS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "display current-configuration",
        ("配置", "未生效", "不生效", "是否成功", "是否生效", "vlan", "trunk", "access",
         "hybrid", "单臂", "dot1q", "子接口", "acl", "策略"),
    ),
    ("display ospf peer brief", ("ospf", "邻居", "路由协议", "动态路由")),
    ("display stp brief", ("stp", "生成树", "环路", "震荡")),
    ("display mac-address", ("mac", "mac地址", "地址表")),
    ("display arp", ("arp",)),
    ("display vrrp", ("vrrp", "主备", "双机")),
    ("display dhcp server statistics", ("dhcp",)),
]


def build_collect_commands(fault_description: str) -> List[str]:
    """按故障描述挑选采集命令。

    固定两条三层命令是旧版误判的根源，这里改为「基础二层+三层全景」，
    再按关键词补齐协议/配置类证据。
    """
    text = (fault_description or "").lower()
    commands = list(BASE_COLLECT_COMMANDS)
    for cmd, keywords in CONDITIONAL_COMMANDS:
        if cmd in commands:
            continue
        if any(kw.lower() in text for kw in keywords):
            commands.append(cmd)
    return commands


def build_collect_manifest(commands: List[str], device_outputs: Optional[Dict[str, Any]] = None) -> str:
    """生成「本次采集了什么 / 各自能证明什么 / 哪里看不到」的说明块。"""
    lines: List[str] = []
    for cmd in commands:
        lines.append(f"- `{cmd}` —— {COMMAND_PURPOSE.get(cmd, '设备回显')}")
    lines.append("")
    lines.append("采集边界（务必遵守）：")
    lines.append("- `display ip interface brief` 只显示已配置 IP 的三层接口；"
                 "未配 IP 的 VLAN 与二层端口不会出现在其中，"
                 "**不能**用它推断「VLAN 不存在」或「VLAN 未配置」。")
    lines.append("- VLAN 是否存在看 `display vlan`；端口属于哪个 VLAN、是否 Trunk 看 "
                 "`display port vlan`；配置是否真的写入看 `display current-configuration`。")
    if device_outputs:
        empty = [f"{dev}:`{cmd}`" for dev, res in device_outputs.items()
                 for cmd, out in (res or {}).items() if not out]
        if empty:
            lines.append("- 以下采集项回显为空/执行失败：" + "、".join(empty[:20]) +
                         "（属于「未采集到」，不得当作「未配置」）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 回显解析（确定性事实）
# ---------------------------------------------------------------------------

_STATUS = r"(?:\*?up|\*?down|#down|administratively\s+down|inactive|dormant)"

_VLAN_ROW = re.compile(r"^\s*(\d{1,4})\s+(common|super)\b\s*(.*)$", re.I)
_PORT_VLAN_ROW = re.compile(
    r"^\s*((?:X?GigabitEthernet|10GE|40GE|GE|XGE|Ethernet|Eth)\s*[\d/\.:]+)\s+"
    r"(access|trunk|hybrid)\s+(\d{1,4})\s*(.*)$",
    re.I,
)
_INTF_ROW = re.compile(
    r"^\s*([A-Za-z][\w/\-\.]*?)\s+(" + _STATUS + r")\s+(" + _STATUS + r")\b", re.I
)
_IP_INTF_ROW = re.compile(
    r"^\s*([A-Za-z][\w/\-\.]*?)\s+(\S+)\s+(" + _STATUS + r")\s+(" + _STATUS + r")\b", re.I
)
_IP_INTF_HDR = re.compile(r"Interface\s+IP\s+Address", re.I)


def _collapse(text: str, limit: int = 160) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s if len(s) <= limit else s[:limit] + "…"


def _clip(items: List[str], limit: int = 16) -> str:
    if len(items) <= limit:
        return "，".join(items)
    return "，".join(items[:limit]) + f"，…（共 {len(items)} 项）"


def parse_vlan_table(text: Optional[str]) -> Tuple[Optional[int], Dict[int, str]]:
    """解析 `display vlan`：返回 (VLAN 总数, {vid: 端口列表字符串})。"""
    if not text:
        return None, {}
    total: Optional[int] = None
    m = re.search(r"total number of vlans is\s*:?\s*(\d+)", text, re.I)
    if m:
        total = int(m.group(1))
    vlans: Dict[int, str] = {}
    for line in text.splitlines():
        m = _VLAN_ROW.match(line)
        if m:
            vlans[int(m.group(1))] = m.group(3).strip()
    return total, vlans


def parse_port_vlan(text: Optional[str]) -> Dict[str, Dict[str, str]]:
    """解析 `display port vlan`：{端口: {link_type, pvid, allow}}。"""
    result: Dict[str, Dict[str, str]] = {}
    if not text:
        return result
    for line in text.splitlines():
        m = _PORT_VLAN_ROW.match(line)
        if m:
            result[m.group(1)] = {
                "link_type": m.group(2).lower(),
                "pvid": m.group(3),
                "allow": m.group(4).strip() or "-",
            }
    return result


def parse_interface_brief(text: Optional[str]) -> Dict[str, Tuple[str, str]]:
    """解析 `display interface brief`：{接口: (物理状态, 协议状态)}。"""
    result: Dict[str, Tuple[str, str]] = {}
    if not text:
        return result
    for line in text.splitlines():
        if not line.strip() or line.lstrip().lower().startswith("interface"):
            continue
        m = _INTF_ROW.match(line)
        if m:
            result[m.group(1)] = (m.group(2), m.group(3))
    return result


def parse_ip_interface_brief(text: Optional[str]) -> Dict[str, Tuple[str, str, str]]:
    """解析 `display ip interface brief`：{接口: (IP/掩码, 物理, 协议)}。"""
    result: Dict[str, Tuple[str, str, str]] = {}
    if not text:
        return result
    for line in text.splitlines():
        if not line.strip() or _IP_INTF_HDR.search(line):
            continue
        m = _IP_INTF_ROW.match(line)
        if m:
            result[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return result


def parse_config_vlan_batch(text: Optional[str]) -> str:
    """提取 `vlan batch ...` 行（多条用 ; 连接）。"""
    found = [m.group(1).strip() for m in re.finditer(r"^\s*vlan batch\s+(.+)$", text or "", re.M)]
    return "; ".join(found)


def parse_config_blocks(text: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """解析 `display current-configuration` 的 interface 块，提取关键命令。"""
    blocks: Dict[str, Dict[str, Any]] = {}
    if not text:
        return blocks
    cur: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^interface\s+(\S+)$", line, re.I)
        if m:
            cur = m.group(1)
            blocks.setdefault(cur, {"lines": []})
            continue
        if line.startswith("#") or line.lower() == "return":
            cur = None
            continue
        if cur:
            blocks[cur]["lines"].append(line)

    for blk in blocks.values():
        for ln in blk["lines"]:
            low = ln.lower()
            if low.startswith("port link-type"):
                blk["link_type"] = ln.split()[-1].lower()
            elif low.startswith("port trunk allow-pass vlan"):
                blk["trunk_allow"] = ln[len("port trunk allow-pass vlan"):].strip()
            elif low.startswith("port default vlan"):
                blk["default_vlan"] = ln.split()[-1]
            elif low.startswith("port hybrid") and "vlan" in low:
                blk.setdefault("hybrid", []).append(ln)
            elif low.startswith("ip address"):
                parts = ln.split()
                if len(parts) >= 3:
                    blk.setdefault("ip", []).append(" ".join(parts[2:]))
            elif low.startswith("dot1q termination vid"):
                blk["dot1q_vid"] = ln.split()[-1]
            elif low.startswith("arp broadcast enable"):
                blk["arp_broadcast"] = True
            elif low == "shutdown":
                blk["shutdown"] = True
    return blocks


def _is_mgmt_interface(name: str) -> bool:
    """管理口 / 空接口：其 down 状态与业务转发无关，不能当故障判据。"""
    upper = name.upper()
    return upper.startswith(("METH", "NULL", "LOOP"))


#: 设备不认识该命令时的典型回显（路由器上执行 display vlan 就会这样）。
#: 必须与「命令没配」区分开：这只是"没有这个视角"，不是"没有这个配置"。
_UNSUPPORTED_HINTS = (
    "unrecognized command",
    "wrong parameter",
    "incomplete command",
    "too many parameters",
    "permission denied",
    "error:",
)


def _is_unsupported(out: Optional[str]) -> bool:
    """回显是否为「命令不支持/参数错误」这类短错误信息。"""
    low = (out or "").lower()
    if len(low) > 400:  # 正常回显（尤其配置）可能很长，不参与误判
        return False
    return any(h in low for h in _UNSUPPORTED_HINTS)


def summarize_device(
    dev: str,
    res: Dict[str, Optional[str]],
    expected: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """把单台设备的回显解析成 (关键事实, 证据缺口)。

    expected 指定「本次应该采集到的命令」：凡是没出现在 res 里的命令，
    都会被记成证据缺口，避免「根本没执行这条命令」被误读为「该配置不存在」。
    """
    res = res or {}
    expected = list(expected or BASE_COLLECT_COMMANDS)
    facts: List[str] = []
    gaps: List[str] = []

    vlan_out = res.get("display vlan")
    port_out = res.get("display port vlan")
    intf_out = res.get("display interface brief")
    ip_out = res.get("display ip interface brief")
    route_out = res.get("display ip routing-table")
    conf_out = res.get("display current-configuration")

    # 命令不被支持（典型：路由器上执行 display vlan）——单独记为「无此视角」，
    # 既不算事实，也不能被解读成「未配置」。
    unsupported = [cmd for cmd, out in res.items() if out and _is_unsupported(out)]
    for cmd in unsupported:
        gaps.append(f"`{cmd}` 该设备不支持/参数不被接受（可能不是交换机或版本不同）—— "
                    "该维度没有证据，不能据此判断 VLAN/端口配置是否存在")
    if "display vlan" in unsupported:
        vlan_out = None
    if "display port vlan" in unsupported:
        port_out = None
    if "display interface brief" in unsupported:
        intf_out = None
    if "display ip interface brief" in unsupported:
        ip_out = None
    if "display ip routing-table" in unsupported:
        route_out = None
    if "display current-configuration" in unsupported:
        conf_out = None

    # ---- 二层：VLAN 是否存在、端口归属 ----
    total, vlans = parse_vlan_table(vlan_out)
    if vlans:
        items = []
        for vid in sorted(vlans):
            ports = vlans[vid]
            items.append(f"VLAN {vid}（端口：{_collapse(ports, 110)}）" if ports
                         else f"VLAN {vid}（已创建，无端口加入）")
        tail = f"；VLAN 总数 {total}" if total is not None else ""
        facts.append("`display vlan` 解析：已创建 " + "，".join(items) + tail)
    elif vlan_out:
        facts.append("`display vlan` 回显未解析到 VLAN 行，原始片段：" + _collapse(vlan_out, 200))

    pv = parse_port_vlan(port_out)
    if pv:
        entries = [f"{name}=链路类型 {info['link_type']}、PVID {info['pvid']}、允许 VLAN {info['allow']}"
                   for name, info in pv.items()]
        facts.append("`display port vlan` 解析：" + _clip(entries))
    elif port_out:
        facts.append("`display port vlan` 回显未解析到端口行，原始片段：" + _collapse(port_out, 200))

    # ---- 物理状态（管理口单独标注，避免被误当判据）----
    ifs = parse_interface_brief(intf_out)
    if ifs:
        biz, mgmt = [], []
        for name, (phy, proto) in ifs.items():
            target = mgmt if _is_mgmt_interface(name) else biz
            target.append(f"{name}={phy}/{proto}")
        if biz:
            facts.append("`display interface brief` 业务接口状态：" + _clip(biz))
        if mgmt:
            facts.append("已标注的管理/空接口（与业务转发无关，不作故障判据）：" + _clip(mgmt, 6))
    elif intf_out:
        facts.append("`display interface brief` 回显未解析到接口行，原始片段：" + _collapse(intf_out, 200))

    # ---- 三层接口 ----
    ips = parse_ip_interface_brief(ip_out)
    if ips:
        entries = [f"{name}={addr}（{phy}/{proto}）" for name, (addr, phy, proto) in ips.items()]
        facts.append("`display ip interface brief`（只含已配 IP 的三层接口）：" + _clip(entries))
    elif ip_out:
        facts.append("`display ip interface brief` 回显未解析到接口行，原始片段：" + _collapse(ip_out, 200))

    if route_out:
        n = len(re.findall(r"^\s*(?:\d{1,3}\.){3}\d{1,3}/\d+", route_out, re.M))
        facts.append(f"`display ip routing-table` 解析：共 {n} 条路由")

    # ---- 配置证据（唯一能直接证明「配置是否写入」）----
    if conf_out:
        lines: List[str] = []
        batch = parse_config_vlan_batch(conf_out)
        if batch:
            lines.append(f"vlan batch {batch}")
        for name, blk in parse_config_blocks(conf_out).items():
            seg: List[str] = [name]
            if blk.get("link_type"):
                seg.append(f"link-type {blk['link_type']}")
            if blk.get("trunk_allow"):
                seg.append(f"allow-pass vlan {blk['trunk_allow']}")
            if blk.get("default_vlan"):
                seg.append(f"default vlan {blk['default_vlan']}")
            if blk.get("hybrid"):
                seg.append("; ".join(blk["hybrid"]))
            if blk.get("ip"):
                seg.append("ip " + ", ".join(blk["ip"]))
            if blk.get("dot1q_vid"):
                seg.append(f"dot1q vid {blk['dot1q_vid']}")
            if blk.get("arp_broadcast"):
                seg.append("arp broadcast enable")
            if blk.get("shutdown"):
                seg.append("shutdown")
            if len(seg) > 1:
                lines.append(" / ".join(seg))
        if lines:
            facts.append("`display current-configuration` 中的 VLAN/接口相关配置：" + "；".join(lines[:24]) +
                         ("；…" if len(lines) > 24 else ""))
        else:
            facts.append("`display current-configuration` 中未见任何 VLAN/接口相关命令行")

    # ---- 证据缺口：没执行 / 执行了但空回显，一律记为「未采集到」 ----
    for cmd in expected:
        if cmd not in res:
            gaps.append(f"`{cmd}` 本次未执行/未采集到 —— 该维度没有任何证据，不能据此判断「未配置」")
    for cmd, out in res.items():
        if not out:
            gaps.append(f"`{cmd}` 回显为空（执行失败或无输出）—— 不能据此判断「未配置」")

    return facts, gaps


def summarize_evidence(device_outputs: Dict[str, Any], commands: Optional[List[str]] = None) -> str:
    """把所有设备的确定性事实拼成提示词片段。"""
    if not device_outputs:
        return "（无设备输出）"
    blocks: List[str] = []
    for dev, res in device_outputs.items():
        facts, gaps = summarize_device(dev, res, expected=commands)
        lines = [f"设备 {dev}："]
        if facts:
            lines.append("  关键事实（由程序从回显中确定性解析，可信度最高）：")
            lines.extend(f"  - {f}" for f in facts)
        else:
            lines.append("  - 未能从回显中解析出任何事实（请仅依据原始回显谨慎判断）")
        if gaps:
            lines.append("  证据缺口：")
            lines.extend(f"  - {g}" for g in gaps)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _dump_outputs(device_outputs: Dict[str, Any], limit: int = 2600) -> str:
    """序列化原始回显，过长时首尾保留并标注截断，控制提示词体积。"""
    trimmed: Dict[str, Dict[str, str]] = {}
    for dev, res in (device_outputs or {}).items():
        trimmed[dev] = {}
        for cmd, out in (res or {}).items():
            if not out:
                trimmed[dev][cmd] = "（空回显/未采集到）"
                continue
            text = str(out)
            if len(text) <= limit:
                trimmed[dev][cmd] = text
            else:
                head, tail = text[: int(limit * 0.7)], text[-int(limit * 0.3):]
                trimmed[dev][cmd] = (
                    head + f"\n…（中间省略 {len(text) - len(head) - len(tail)} 字符，"
                           f"原文共 {len(text)} 字符）…\n" + tail
                )
    return json.dumps(trimmed, ensure_ascii=False, indent=2)


class FaultDiagnosis:
    """分析故障并提供修复建议"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def is_available(self) -> bool:
        """AI 诊断能力是否可用（未配置 .env 时为 False）"""
        return bool(self.llm and self.llm.is_available())

    def diagnose(
        self,
        fault_description: str,
        device_outputs: Dict[str, Any],
        collect_commands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """诊断故障并返回结构化建议

        当 LLM 未配置时抛出 LLMUnavailableError（由 UI 提示配置 .env），
        其余异常（如网络/接口错误）仍返回结构化错误信息，保证界面不崩。
        """
        commands = collect_commands or build_collect_commands(fault_description)
        prompt_template = load_prompt("fault_diagnosis.txt")
        prompt = prompt_template.format(
            fault_description=fault_description,
            device_outputs=_dump_outputs(device_outputs),
            collect_manifest=build_collect_manifest(commands, device_outputs),
            evidence_facts=summarize_evidence(device_outputs, commands),
        )

        try:
            response = self.llm.invoke(prompt)
            result = self._normalize_result(self._extract_json(response))
            logger.info(f"Diagnosis result: {result}")
            return result
        except LLMUnavailableError:
            # 未配置 Key：向上抛出，交由 UI 给出配置引导
            raise
        except Exception as e:
            logger.error(f"Fault diagnosis failed: {e}")
            return {
                "analysis": "诊断过程发生错误，请检查日志",
                "conclusion": "",
                "confidence": "低",
                "suggestions": [],
                "risk_level": "Unknown",
            }

    def _extract_json(self, text: str) -> Dict:
        """从 LLM 回复中提取 JSON 诊断结果"""
        if not isinstance(text, str):
            return {"analysis": str(text), "suggestions": []}
        start = text.find('{')
        end = text.rfind('}') + 1
        if start != -1 and end > start:
            json_str = text[start:end]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse JSON from LLM response: {json_str[:200]}")

        # 兜底处理：如果没找到 JSON 或解析失败，将全文作为分析内容返回
        return {"analysis": text, "suggestions": [], "confidence": "低"}

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [ln.strip() for ln in value.splitlines() if ln.strip()]
        if isinstance(value, (list, tuple)):
            out = []
            for item in value:
                if isinstance(item, str):
                    out.append(item.strip())
                elif isinstance(item, dict):
                    out.append(json.dumps(item, ensure_ascii=False))
                else:
                    out.append(str(item))
            return [x for x in out if x]
        return [str(value)]

    def _normalize_result(self, data: Any) -> Dict[str, Any]:
        """统一结果字段，兼容新旧提示词/模型输出的差异。"""
        result: Dict[str, Any] = dict(data) if isinstance(data, dict) else {}
        analysis = result.get("analysis") or result.get("分析") or ""
        if isinstance(analysis, (dict, list)):
            analysis = json.dumps(analysis, ensure_ascii=False)
        result["analysis"] = str(analysis).strip() or "模型未返回分析内容"

        conclusion = result.get("conclusion") or ""
        result["conclusion"] = str(conclusion).strip()

        confidence = str(result.get("confidence") or "").strip()
        result["confidence"] = confidence if confidence in ("高", "中", "低") else (confidence or "未标注")

        risk = str(result.get("risk_level") or "").strip()
        result["risk_level"] = risk or "未标注"

        result["evidence"] = self._as_list(result.get("evidence"))
        result["evidence_gaps"] = self._as_list(result.get("evidence_gaps"))
        result["suggestions"] = self._as_list(result.get("suggestions"))
        result["auto_fix_commands"] = self._as_list(result.get("auto_fix_commands"))
        result["verify_commands"] = self._as_list(result.get("verify_commands"))
        return result
