"""故障诊断证据链测试（修复「VLAN 已配置却被判为未配置」）

复现用户反馈的问题：
  S1 实际已 `vlan batch 2 3`、G0/0/1 为 trunk 且允许 vlan 2/3 通过，
  但诊断输出「S1 的 VLAN 配置未成功……仅存在默认 Vlanif1 接口」。

根因：旧版只采集 `display ip routing-table` + `display ip interface brief` 两条三层命令，
VLAN（二层）根本不在采集范围内，模型把「没看到」当成「没配置」。

本测试守护三条契约：
  1. 采集命令必须覆盖二层证据（display vlan / display port vlan / current-configuration）；
  2. 程序必须能把回显解析成确定性事实，并把空回显标为「未采集到」而非「未配置」；
  3. 提示词必须包含「未采集 ≠ 未配置」「尊重用户前提」等约束，且事实被真正传给模型。

运行：python tests/test_fault_diagnosis.py
      pytest tests/test_fault_diagnosis.py
"""
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from src.ai.fault_diagnosis import (
    FaultDiagnosis,
    build_collect_commands,
    build_collect_manifest,
    parse_config_blocks,
    parse_config_vlan_batch,
    parse_interface_brief,
    parse_ip_interface_brief,
    parse_port_vlan,
    parse_vlan_table,
    summarize_device,
    summarize_evidence,
)
from src.utils.prompt_loader import load_prompt

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


# --------------------------------------------------------------------------
# 真实风格的 eNSP/VRP 回显样本
# --------------------------------------------------------------------------
S1_DISPLAY_VLAN = """The total number of vlans is : 3
--------------------------------------------------------------------------------
U: Up;         D: Down;         TG: Tagged;         UT: Untagged;
MP: Vlan-mapping;               ST: Static;
--------------------------------------------------------------------------------
VID  Type    Ports
--------------------------------------------------------------------------------
1    common  UT:GE0/0/1(U)     GE0/0/2(D)     GE0/0/3(D)
2    common  UT:GE0/0/2(U)
3    common
"""

S1_PORT_VLAN = """Port                    Link Type    PVID  Trunk VLAN List
-------------------------------------------------------------------------------
GigabitEthernet0/0/1    trunk        1     2-3
GigabitEthernet0/0/2    access       2     -
GigabitEthernet0/0/3    access       3     -
"""

S1_INTF_BRIEF = """Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/1        up      up           0%     0%          0          0
GigabitEthernet0/0/2        up      up           0%     0%          0          0
GigabitEthernet0/0/3        up      up           0%     0%          0          0
MEth0/0/1                   down    down         0%     0%          0          0
NULL0                       up      up(s)        0%     0%          0          0
"""

S1_IP_BRIEF = """Interface                         IP Address/Mask      Physical   Protocol
GigabitEthernet0/0/1              unassigned           up         up
Vlanif1                           unassigned           down       down
"""

S1_CONFIG = """#
sysname S1
#
vlan batch 2 3
#
interface Vlanif1
#
interface GigabitEthernet0/0/1
 port link-type trunk
 port trunk allow-pass vlan 2 to 3
#
interface GigabitEthernet0/0/2
 port link-type access
 port default vlan 2
#
interface GigabitEthernet0/0/3
 port link-type access
 port default vlan 3
#
return
"""

S1_OUTPUTS_OK = {
    "display vlan": S1_DISPLAY_VLAN,
    "display port vlan": S1_PORT_VLAN,
    "display interface brief": S1_INTF_BRIEF,
    "display ip interface brief": S1_IP_BRIEF,
    "display ip routing-table": "Route Flags: R - relay, D - download to fib\n"
                                "Destination/Mask    Proto   Pre  Cost  NextHop  Interface\n"
                                "0.0.0.0/0           Static  60   0     192.168.2.254  Vlanif1\n",
    "display current-configuration": S1_CONFIG,
}

# 旧版采集范围：只有两条三层命令（复现 bug 的输入）
S1_OUTPUTS_LEGACY = {
    "display ip interface brief": S1_IP_BRIEF,
    "display ip routing-table": "Destination/Mask    Proto   Pre  Cost  NextHop\n0.0.0.0/0  Static  60  0  192.168.2.254\n",
}

USER_QUESTION = "不考虑S1的vlanif接口的配置情况下请分析S1的vlan配置是否成功"


class CapturingLLM:
    """记录提示词并返回预设 JSON 的假 LLM"""

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def is_available(self):
        return True

    def invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.payload


# --------------------------------------------------------------------------
# 1. 采集命令选取
# --------------------------------------------------------------------------
def test_collect_commands_cover_layer2():
    cmds = build_collect_commands(USER_QUESTION)
    check("VLAN 问题采集 display vlan", "display vlan" in cmds, str(cmds))
    check("VLAN 问题采集 display port vlan", "display port vlan" in cmds, str(cmds))
    check("VLAN 问题采集 display interface brief", "display interface brief" in cmds, str(cmds))
    check("含「配置」关键词时采集 current-configuration",
          "display current-configuration" in cmds, str(cmds))
    check("无关协议命令不被拉起", "display ospf peer brief" not in cmds, str(cmds))


def test_collect_commands_light_for_ping():
    cmds = build_collect_commands("R1 无法 ping 通 R2，但是 R1 接口状态是 UP")
    check("ping 问题仍采集基础二维视图", "display vlan" in cmds and "display port vlan" in cmds, str(cmds))
    check("ping 问题不拉全量配置（控制耗时）",
          "display current-configuration" not in cmds, str(cmds))


def test_collect_commands_protocol_keywords():
    cmds = build_collect_commands("R1 和 R2 的 OSPF 邻居起不来")
    check("OSPF 关键词拉起邻居命令", "display ospf peer brief" in cmds, str(cmds))
    cmds2 = build_collect_commands("S1 疑似二层环路，STP 状态异常")
    check("STP 关键词拉起生成树命令", "display stp brief" in cmds2, str(cmds2))


# --------------------------------------------------------------------------
# 2. 解析器
# --------------------------------------------------------------------------
def test_parse_vlan_table():
    total, vlans = parse_vlan_table(S1_DISPLAY_VLAN)
    check("VLAN 总数解析", total == 3, str(total))
    check("VLAN 1/2/3 均被识别", set(vlans) == {1, 2, 3}, str(sorted(vlans)))
    check("VLAN 3 无端口", vlans.get(3) == "", repr(vlans.get(3)))
    check("VLAN 2 有端口", "GE0/0/2" in vlans.get(2, ""), repr(vlans.get(2)))


def test_parse_port_vlan():
    pv = parse_port_vlan(S1_PORT_VLAN)
    check("端口表解析出 3 个端口", len(pv) == 3, str(list(pv)))
    trunk = pv.get("GigabitEthernet0/0/1", {})
    check("G0/0/1 链路类型 trunk", trunk.get("link_type") == "trunk", str(trunk))
    check("G0/0/1 允许 VLAN 2-3", trunk.get("allow") == "2-3", str(trunk))
    check("G0/0/2 PVID 为 2", pv.get("GigabitEthernet0/0/2", {}).get("pvid") == "2",
          str(pv.get("GigabitEthernet0/0/2")))


def test_parse_interface_and_ip_brief():
    ifs = parse_interface_brief(S1_INTF_BRIEF)
    check("接口状态解析 5 条", len(ifs) == 5, str(len(ifs)))
    check("G0/0/1 为 up/up", ifs.get("GigabitEthernet0/0/1") == ("up", "up"),
          str(ifs.get("GigabitEthernet0/0/1")))
    check("MEth0/0/1 为 down/down", ifs.get("MEth0/0/1") == ("down", "down"),
          str(ifs.get("MEth0/0/1")))
    ips = parse_ip_interface_brief(S1_IP_BRIEF)
    check("三层接口解析跳过表头", "Interface" not in ips, str(list(ips)))
    check("Vlanif1 被识别为 down/down", ips.get("Vlanif1", ("", "", ""))[1:] == ("down", "down"),
          str(ips.get("Vlanif1")))


def test_parse_config_blocks():
    blocks = parse_config_blocks(S1_CONFIG)
    trunk = blocks.get("GigabitEthernet0/0/1", {})
    check("配置块识别 link-type trunk", trunk.get("link_type") == "trunk", str(trunk))
    check("配置块识别 allow-pass", trunk.get("trunk_allow") == "2 to 3", str(trunk))
    acc = blocks.get("GigabitEthernet0/0/2", {})
    check("配置块识别 default vlan", acc.get("default_vlan") == "2", str(acc))
    check("vlan batch 解析", parse_config_vlan_batch(S1_CONFIG) == "2 3",
          parse_config_vlan_batch(S1_CONFIG))


# --------------------------------------------------------------------------
# 3. 事实生成（核心：不能把「没采集」写成「没配置」）
# --------------------------------------------------------------------------
def test_facts_report_vlan_created():
    facts, gaps = summarize_device("S1", S1_OUTPUTS_OK)
    text = "\n".join(facts)
    check("事实中报出 VLAN 2 已创建", "VLAN 2（端口" in text, text[:120])
    check("事实中报出 VLAN 3 无端口", "VLAN 3（已创建，无端口加入）" in text, text[:200])
    check("事实中包含 trunk 与允许 VLAN",
          "trunk" in text and "允许 VLAN 2-3" in text, text[:200])
    check("事实中引用 current-configuration 的 vlan batch",
          "vlan batch 2 3" in text, text[:200])
    check("完整采集时无证据缺口", not gaps, str(gaps))


def test_management_port_marked_irrelevant():
    facts, _ = summarize_device("S1", S1_OUTPUTS_OK)
    text = "\n".join(facts)
    check("管理口被标注为与业务无关", "与业务转发无关" in text, text[:200])
    check("管理口单独成行、不与业务接口混列",
          "MEth0/0/1" not in text.split("与业务转发无关")[0].split("业务接口状态")[-1],
          text[:200])


def test_legacy_collection_records_gap_not_conclusion():
    """旧版只采两条三层命令时：必须显式记为「未采集到」，而不是暗示「未配置」"""
    facts, gaps = summarize_device("S1", S1_OUTPUTS_LEGACY)
    gap_text = "\n".join(gaps)
    check("缺 display vlan 记为证据缺口", "display vlan" in gap_text, gap_text)
    check("缺口说明禁止当作未配置", "不能据此判断" in gap_text, gap_text)
    check("事实中不出现「未创建/未配置」的否定断言",
          not any(w in "\n".join(facts) for w in ("未创建", "未配置")), "\n".join(facts))


def test_unsupported_command_not_counted_as_absent():
    """路由器不支持 display vlan 时，只能算「没有这个视角」，不能算「没有 VLAN」"""
    router_out = {
        "display vlan": "Error: Unrecognized command found at '^' position.",
        "display port vlan": "Error: Unrecognized command found at '^' position.",
        "display interface brief": "Interface                   PHY     Protocol\n"
                                   "GigabitEthernet0/0/0        up      up\n",
        "display ip interface brief": "Interface    IP Address/Mask   Physical  Protocol\n"
                                      "GigabitEthernet0/0/0   10.0.0.1/24   up   up\n",
        "display ip routing-table": "Destination/Mask  Proto  Pre  Cost  NextHop\n",
    }
    facts, gaps = summarize_device("R1", router_out)
    gap_text = "\n".join(gaps)
    check("命令不支持被识别为缺口", "该设备不支持" in gap_text, gap_text)
    check("命令不支持时给出「不能据此判断」警告", "不能据此判断" in gap_text, gap_text)
    check("不支持的命令不被当作事实解析",
          not any("未解析到 VLAN 行" in f for f in facts), "\n".join(facts))


def test_summarize_evidence_multi_device():
    text = summarize_evidence({"S1": S1_OUTPUTS_OK, "R1": {"display vlan": None}})
    check("多设备分段输出", "设备 S1：" in text and "设备 R1：" in text, text[:80])
    check("空回显设备给出缺口", "未采集到" in text, text[-200:])


def test_manifest_declares_blind_spot():
    manifest = build_collect_manifest(
        list(S1_OUTPUTS_OK.keys()),
        {"S1": {"display vlan": None, "display port vlan": S1_PORT_VLAN}},
    )
    check("说明 ip interface brief 只含三层接口",
          "只显示已配置 IP 的三层接口" in manifest, manifest[:200])
    check("提示 VLAN 证据应看 display vlan",
          "VLAN 是否存在看" in manifest, manifest[-260:])
    check("空回显命令在清单中标注",
          "display vlan" in manifest and "不得当作「未配置」" in manifest, manifest[-200:])


# --------------------------------------------------------------------------
# 4. 提示词契约
# --------------------------------------------------------------------------
def test_prompt_contracts():
    tpl = load_prompt("fault_diagnosis.txt")
    for key in ("{fault_description}", "{collect_manifest}", "{evidence_facts}", "{device_outputs}"):
        check(f"提示词含占位符 {key}", key in tpl)
    check("提示词禁止「没看到=没有」", "没看到" in tpl and "没有" in tpl)
    check("提示词要求输出证据缺口", "evidence_gaps" in tpl)
    check("提示词要求尊重用户前提", "不考虑" in tpl)
    check("提示词区分精确否定与缺口", "明确否定" in tpl)
    check("提示词要求是非题先给结论", "结论" in tpl)
    # 花括号必须转义，否则 .format 会抛 KeyError
    try:
        tpl.format(fault_description="x", collect_manifest="y", evidence_facts="z", device_outputs="{}")
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"    format 失败: {e}")
    check("提示词可被 format 渲染", ok)


# --------------------------------------------------------------------------
# 5. 端到端（假 LLM）
# --------------------------------------------------------------------------
def test_diagnose_passes_facts_to_llm():
    payload = json.dumps({
        "analysis": "S1 的 VLAN 配置成功：display vlan 显示 VLAN 2、3 已创建。",
        "conclusion": "配置成功",
        "confidence": "高",
        "risk_level": "低",
        "evidence": ["display vlan 显示 VLAN 2、3", "current-configuration 含 vlan batch 2 3"],
        "evidence_gaps": [],
        "suggestions": ["如需二层互通，把端口加入对应 VLAN"],
        "auto_fix_commands": [],
        "verify_commands": ["display vlan", "display port vlan"],
    }, ensure_ascii=False)
    llm = CapturingLLM(payload)
    diag = FaultDiagnosis(llm)
    res = diag.diagnose(USER_QUESTION, {"S1": S1_OUTPUTS_OK},
                        collect_commands=list(S1_OUTPUTS_OK.keys()))

    prompt = llm.prompts[0]
    check("提示词包含用户原始问题", USER_QUESTION in prompt)
    check("提示词包含确定性事实块", "关键事实（由程序从回显中确定性解析" in prompt)
    check("提示词事实含 VLAN 2", "VLAN 2" in prompt)
    check("提示词事实含 trunk 允许列表", "允许 VLAN 2-3" in prompt)
    check("提示词包含采集边界说明", "采集边界" in prompt)
    check("提示词含 display vlan 原始回显", "The total number of vlans is : 3" in prompt)
    check("结果保留 conclusion", res.get("conclusion") == "配置成功", str(res.get("conclusion")))
    check("结果置信度归一化", res.get("confidence") == "高", str(res.get("confidence")))
    check("结果证据列表被保留", len(res.get("evidence") or []) == 2, str(res.get("evidence")))
    check("结果字段齐全",
          all(k in res for k in ("analysis", "suggestions", "auto_fix_commands",
                                 "verify_commands", "risk_level")), str(list(res)))


def test_diagnose_legacy_output_shape():
    """兼容旧调用：{'R1': {'routing': ..., 'interfaces': ...}}"""
    payload = '{"analysis": "缺少证据", "confidence": "低"}'
    llm = CapturingLLM(payload)
    res = FaultDiagnosis(llm).diagnose("R1 无法 ping 通 R2",
                                       {"R1": {"routing": "", "interfaces": ""}})
    check("旧结构不抛异常", isinstance(res, dict) and res.get("analysis") == "缺少证据", str(res))
    check("旧结构缺失证据记入缺口", "未采集到" in llm.prompts[0], "提示词无缺口标记")
    check("缺省字段补全为空列表", res.get("suggestions") == [] and res.get("evidence_gaps") == [], str(res))


def test_diagnose_plain_text_fallback():
    llm = CapturingLLM("这不是 JSON，只是纯文本分析")
    res = FaultDiagnosis(llm).diagnose("S1 VLAN 是否配置成功", {"S1": S1_OUTPUTS_OK})
    check("非 JSON 回复降级为 analysis", "纯文本分析" in res.get("analysis", ""), str(res))
    check("降级结果字段仍齐全", res.get("confidence") == "低" and res.get("suggestions") == [], str(res))


def main():
    print("=" * 60)
    for fn in (
        test_collect_commands_cover_layer2,
        test_collect_commands_light_for_ping,
        test_collect_commands_protocol_keywords,
        test_parse_vlan_table,
        test_parse_port_vlan,
        test_parse_interface_and_ip_brief,
        test_parse_config_blocks,
        test_facts_report_vlan_created,
        test_management_port_marked_irrelevant,
        test_legacy_collection_records_gap_not_conclusion,
        test_unsupported_command_not_counted_as_absent,
        test_summarize_evidence_multi_device,
        test_manifest_declares_blind_spot,
        test_prompt_contracts,
        test_diagnose_passes_facts_to_llm,
        test_diagnose_legacy_output_shape,
        test_diagnose_plain_text_fallback,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"故障诊断测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
