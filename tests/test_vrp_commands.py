"""华为 VRP 命令生成正确性测试

复现并验证用户反馈的问题：
  输入「配置S1上的G0/0/1接口为Trunk接口，允许vlan2、3通过」，
  旧版只输出 system-view / interface G0/0/1 / quit 三条命令，
  **丢失了 port link-type trunk 与 port trunk allow-pass vlan 2 3**，
  导致配置完全不生效。

本测试同时覆盖接口名归一化、VLAN 列表解析、视图层级闭合等易错点。

运行：python tests/test_vrp_commands.py
      pytest tests/test_vrp_commands.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from src.ai.command_gen import CommandGenerator

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def _cmds(task):
    g = CommandGenerator(llm_client=None)
    return [i["command"] for i in g.generate_with_comments(task)]


def test_trunk_port_reported_case():
    """用户原例：Trunk 口必须包含 link-type 与 allow-pass"""
    cmds = _cmds({
        "action": "config_interface",
        "parameters": {"interface": "G0/0/1", "link_type": "trunk",
                       "allowed_vlans": [2, 3]},
    })
    check("Trunk：使用 VRP 完整接口名",
          "interface GigabitEthernet0/0/1" in cmds, str(cmds))
    check("Trunk：包含 port link-type trunk",
          "port link-type trunk" in cmds, str(cmds))
    check("Trunk：包含允许通过的 VLAN",
          "port trunk allow-pass vlan 2 3" in cmds, str(cmds))
    check("Trunk：link-type 在 allow-pass 之前",
          cmds.index("port link-type trunk") < cmds.index("port trunk allow-pass vlan 2 3"),
          str(cmds))
    check("Trunk：首条 system-view", cmds[0] == "system-view", str(cmds))
    check("Trunk：末条 quit", cmds[-1] == "quit", str(cmds))


def test_trunk_vlan_range():
    """VLAN 连续区间应压缩为 'a to b'"""
    cmds = _cmds({
        "action": "config_trunk",
        "parameters": {"interface": "G0/0/1", "allowed_vlans": "2-5"},
    })
    check("Trunk 区间：vlan 2 to 5",
          "port trunk allow-pass vlan 2 to 5" in cmds, str(cmds))
    check("Trunk 别名 action 自动补 link_type",
          "port link-type trunk" in cmds, str(cmds))


def test_access_port():
    """Access 口必须包含 link-type access 与 default vlan"""
    cmds = _cmds({
        "action": "config_interface",
        "parameters": {"interface": "E0/0/1", "link_type": "access",
                       "default_vlan": 10},
    })
    check("Access：使用 Ethernet 完整名",
          "interface Ethernet0/0/1" in cmds, str(cmds))
    check("Access：port link-type access",
          "port link-type access" in cmds, str(cmds))
    check("Access：port default vlan 10",
          "port default vlan 10" in cmds, str(cmds))


def test_vlan_only_implies_access():
    """只给 VLAN 未给类型时，按 Access 处理"""
    cmds = _cmds({
        "action": "config_interface",
        "parameters": {"interface": "E0/0/2", "vlan_id": 20},
    })
    check("仅 VLAN：推导出 access 类型",
          "port link-type access" in cmds, str(cmds))
    check("仅 VLAN：port default vlan 20",
          "port default vlan 20" in cmds, str(cmds))


def test_interface_ip_and_no_l2_leak():
    """三层 IP 配置，且 Trunk 场景不应出现 ip address"""
    cmds = _cmds({
        "action": "config_interface",
        "parameters": {"interface": "G0/0/0", "ip": "10.0.0.1", "mask": 24},
    })
    check("接口 IP：掩码 24 转点分十进制",
          "ip address 10.0.0.1 255.255.255.0" in cmds, str(cmds))

    trunk = _cmds({
        "action": "config_interface",
        "parameters": {"interface": "G0/0/1", "link_type": "trunk",
                       "allowed_vlans": [2, 3]},
    })
    check("二层 Trunk 口不生成 ip address",
          not any(c.startswith("ip address") for c in trunk), str(trunk))


def test_ip_with_prefix_notation():
    """ip 参数自带 /24 时应能拆分"""
    cmds = _cmds({
        "action": "config_interface",
        "parameters": {"interface": "G0/0/0", "ip": "10.0.0.1/24"},
    })
    check("IP 带前缀：正确拆分并转换掩码",
          "ip address 10.0.0.1 255.255.255.0" in cmds, str(cmds))


def test_svi_config():
    """VLAN 三层接口配置"""
    cmds = _cmds({
        "action": "config_vlanif",
        "parameters": {"vlan_id": 10, "ip": "192.168.10.1", "mask": 24},
    })
    check("SVI：先创建 vlan 10", "vlan 10" in cmds, str(cmds))
    check("SVI：进入 Vlanif10", "interface Vlanif10" in cmds, str(cmds))
    check("SVI：配置网关地址",
          "ip address 192.168.10.1 255.255.255.0" in cmds, str(cmds))


def test_vlan_batch():
    """批量创建 VLAN"""
    cmds = _cmds({"action": "config_vlan",
                  "parameters": {"vlan_id": [10, 20, 30]}})
    check("批量 VLAN：vlan batch",
          "vlan batch 10 20 30" in cmds, str(cmds))

    cmds2 = _cmds({"action": "config_vlan", "parameters": {"vlan_id": "2-5"}})
    check("VLAN 区间：2 to 5",
          "vlan batch 2 to 5" in cmds2, str(cmds2))


def test_ospf_view_closed_properly():
    """OSPF 进入 area 后必须 quit 两次，否则会停留在 OSPF 视图"""
    cmds = _cmds({
        "action": "config_ospf",
        "parameters": {"process": 1, "area": 0, "network": "10.0.0.0/24"},
    })
    check("OSPF：network 前缀转通配符",
          "network 10.0.0.0 0.0.0.255" in cmds, str(cmds))
    check("OSPF：两次 quit 闭合 area 与进程",
          cmds.count("quit") == 2, str(cmds))
    check("OSPF：最后一条是 quit", cmds[-1] == "quit", str(cmds))


def test_static_route_prefix():
    """静态路由支持 网段/前缀 写法"""
    cmds = _cmds({
        "action": "config_static_route",
        "parameters": {"destination": "192.168.2.0/24", "next_hop": "10.0.0.2"},
    })
    check("静态路由：/24 转掩码",
          "ip route-static 192.168.2.0 255.255.255.0 10.0.0.2" in cmds, str(cmds))


def test_interface_name_normalization():
    """接口名各种缩写都要展开为 VRP 完整名"""
    g = CommandGenerator(llm_client=None)
    cases = {
        "G0/0/1": "GigabitEthernet0/0/1",
        "g0/0/1": "GigabitEthernet0/0/1",
        "GE0/0/1": "GigabitEthernet0/0/1",
        "GE 0/0/1": "GigabitEthernet0/0/1",
        "GigabitEthernet0/0/1": "GigabitEthernet0/0/1",
        "E0/0/1": "Ethernet0/0/1",
        "Eth0/0/1": "Ethernet0/0/1",
        "S0/0/0": "Serial0/0/0",
        "XGE1/0/1": "XGigabitEthernet1/0/1",
        "Vlanif10": "Vlanif10",
        "LoopBack0": "LoopBack0",
    }
    ok = True
    detail = []
    for raw, expect in cases.items():
        got = g._normalize_interface(raw)
        if got != expect:
            ok = False
            detail.append(f"{raw}->{got}(期望 {expect})")
    check("接口名归一化全部正确", ok, "; ".join(detail) or "11 种写法均正确")

    check("接口名归一化幂等",
          g._normalize_interface(g._normalize_interface("G0/0/1"))
          == "GigabitEthernet0/0/1")


def test_vlan_parsing_variants():
    """VLAN 描述的各种分隔符都要能解析"""
    g = CommandGenerator(llm_client=None)
    cases = [
        ([2, 3], [2, 3]),
        ("2,3", [2, 3]),
        ("2、3", [2, 3]),
        ("2，3", [2, 3]),
        ("2 3", [2, 3]),
        ("vlan2、3", [2, 3]),
        ("2-5", [2, 3, 4, 5]),
        ("2 to 5", [2, 3, 4, 5]),
        ("2至5", [2, 3, 4, 5]),
        (10, [10]),
        (None, []),
    ]
    ok = True
    detail = []
    for raw, expect in cases:
        got = g._parse_vlan_ids(raw)
        if got != expect:
            ok = False
            detail.append(f"{raw!r}->{got}(期望 {expect})")
    check("VLAN 解析全部正确", ok, "; ".join(detail) or f"{len(cases)} 种写法均正确")


def test_warnings_when_params_uncovered():
    """模板未覆盖的参数，在无 LLM 时应给出告警而非静默丢弃"""
    g = CommandGenerator(llm_client=None)
    _, warns = g.generate_with_comments_ex({
        "action": "config_interface",
        "parameters": {"interface": "G0/0/1", "link_type": "trunk",
                       "allowed_vlans": [2, 3], "qos_profile": "PQ"},
    })
    check("未覆盖参数触发告警",
          any("qos_profile" in w for w in warns), str(warns))

    _, warns2 = g.generate_with_comments_ex({
        "action": "config_interface",
        "parameters": {"interface": "G0/0/1", "link_type": "trunk"},
    })
    check("Trunk 未指定 VLAN 时给出提示",
          any("VLAN 1" in w for w in warns2), str(warns2))


def test_subinterface_reported_case():
    """用户原例：单臂路由两个子接口必须都带 arp broadcast enable"""
    g = CommandGenerator(llm_client=None)
    # 模型当时的解析结果就是两个独立任务，这里按原样回放
    tasks = [
        {"action": "config_interface",
         "parameters": {"interface": "G0/0/1.2", "ip": "192.168.2.254",
                        "mask": 24, "encapsulation": "dot1q", "vlan_id": 2}},
        {"action": "config_interface",
         "parameters": {"interface": "G0/0/1.3", "ip": "192.168.3.254",
                        "mask": 24, "encapsulation": "dot1q", "vlan_id": 3}},
    ]
    all_cmds = []
    for t in tasks:
        all_cmds.extend(g.generate(t))

    check("子接口：dot1q 终结 vid 2 存在",
          "dot1q termination vid 2" in all_cmds, str(all_cmds))
    check("子接口：dot1q 终结 vid 3 存在",
          "dot1q termination vid 3" in all_cmds, str(all_cmds))
    check("子接口：共两条 arp broadcast enable（两个子接口都不能漏）",
          all_cmds.count("arp broadcast enable") == 2, str(all_cmds))
    check("子接口：子接口名展开为完整名",
          "interface GigabitEthernet0/0/1.2" in all_cmds
          and "interface GigabitEthernet0/0/1.3" in all_cmds, str(all_cmds))
    check("子接口：不得出现二层端口命令 port link-type",
          not any(c.startswith("port link-type") for c in all_cmds), str(all_cmds))
    check("子接口：不得出现 port default vlan",
          not any(c.startswith("port default vlan") for c in all_cmds), str(all_cmds))
    check("子接口：每条子接口都有 IP",
          sum(1 for c in all_cmds if c.startswith("ip address")) == 2, str(all_cmds))


def test_subinterface_single_task_multi_entries():
    """一个任务里带多条子接口（规则解析器 / 新提示词的输出形态）"""
    g = CommandGenerator(llm_client=None)
    cmds = g.generate({
        "action": "config_subinterface",
        "parameters": {
            "interface": "G0/0/1",
            "sub_interfaces": [
                {"vid": 2, "ip": "192.168.2.254", "mask": 24},
                {"vid": 3, "ip": "192.168.3.254", "mask": 24},
            ],
        },
    })
    check("子接口批量：两条 dot1q", sum(1 for c in cmds if c.startswith("dot1q")) == 2, str(cmds))
    check("子接口批量：两条 arp broadcast", cmds.count("arp broadcast enable") == 2, str(cmds))
    check("子接口批量：编号按 vid 推导为 .2/.3",
          "interface GigabitEthernet0/0/1.2" in cmds
          and "interface GigabitEthernet0/0/1.3" in cmds, str(cmds))
    check("子接口批量：每个子接口块以 quit 收尾",
          cmds.count("quit") == 2 and cmds[-1] == "quit", str(cmds))


def test_subinterface_vid_ip_lists():
    """vlan_id 与 ip 都是列表时按下标配对"""
    g = CommandGenerator(llm_client=None)
    cmds = g.generate({
        "action": "config_subinterface",
        "parameters": {"interface": "G0/0/1", "vlan_id": [2, 3],
                       "ip": ["192.168.2.254/24", "192.168.3.254/24"]},
    })
    check("子接口列表配对：vid 2 → 192.168.2.254",
          "ip address 192.168.2.254 255.255.255.0" in cmds, str(cmds))
    check("子接口列表配对：vid 3 → 192.168.3.254",
          "ip address 192.168.3.254 255.255.255.0" in cmds, str(cmds))
    check("子接口列表配对：两条 arp broadcast",
          cmds.count("arp broadcast enable") == 2, str(cmds))


def test_subinterface_arp_autofill():
    """大模型漏写 arp broadcast enable 时，兜底逻辑必须自动补齐"""
    g = CommandGenerator(llm_client=None)
    broken = [
        "system-view",
        "interface GigabitEthernet0/0/1.2",
        "dot1q termination vid 2",
        "ip address 192.168.2.254 255.255.255.0",
        "quit",
        "interface GigabitEthernet0/0/1.3",
        "dot1q termination vid 3",
        "ip address 192.168.3.254 255.255.255.0",
        "arp broadcast enable",
        "quit",
    ]
    fixed, added = g._ensure_dot1q_arp(broken)
    check("ARP 兜底：只补缺失的那一条", added == 1, f"added={added}")
    check("ARP 兜底：补齐后共两条",
          sum(1 for c in fixed if c == "arp broadcast enable") == 2, str(fixed))
    check("ARP 兜底：插入位置在 quit 之前",
          fixed.index("arp broadcast enable") == 4, str(fixed))
    check("ARP 兜底：不重复插入已有的一条",
          fixed.count("arp broadcast enable") == 2, str(fixed))

    # 正常命令不应被改动
    untouched, added2 = g._ensure_dot1q_arp(["system-view", "vlan 10", "quit"])
    check("ARP 兜底：非 dot1q 命令保持原样",
          untouched == ["system-view", "vlan 10", "quit"] and added2 == 0, str(untouched))


def test_subinterface_warns_switch_side():
    """单臂路由应提示交换机侧需要 Trunk 放通"""
    g = CommandGenerator(llm_client=None)
    _, warns = g.generate_with_comments_ex({
        "action": "config_subinterface",
        "parameters": {"interface": "G0/0/1",
                       "sub_interfaces": [{"vid": 2, "ip": "192.168.2.254", "mask": 24}]},
    })
    text = " ".join(warns)
    check("子接口：提示交换机侧配置 Trunk", "Trunk" in text, text)


def test_no_empty_commands():
    """任何生成的命令列表都不应包含空串"""
    g = CommandGenerator(llm_client=None)
    tasks = [
        {"action": "config_interface", "parameters": {}},
        {"action": "config_interface", "parameters": {"interface": "G0/0/1"}},
        {"action": "config_vlan", "parameters": {}},
        {"action": "config_static_route", "parameters": {}},
        {"action": "show_command", "parameters": {}},
        {"action": "config_ospf", "parameters": {}},
    ]
    ok = True
    detail = []
    for t in tasks:
        cmds = g.generate(t)
        if any(not str(c).strip() for c in cmds):
            ok = False
            detail.append(f"{t['action']}->{cmds}")
    check("无空命令产生", ok, "; ".join(detail) or f"{len(tasks)} 个场景均正常")


def main():
    print("=" * 60)
    print("华为 VRP 命令生成正确性测试")
    print("=" * 60)
    for fn in (
        test_trunk_port_reported_case,
        test_trunk_vlan_range,
        test_access_port,
        test_vlan_only_implies_access,
        test_interface_ip_and_no_l2_leak,
        test_ip_with_prefix_notation,
        test_svi_config,
        test_vlan_batch,
        test_ospf_view_closed_properly,
        test_static_route_prefix,
        test_interface_name_normalization,
        test_vlan_parsing_variants,
        test_warnings_when_params_uncovered,
        test_subinterface_reported_case,
        test_subinterface_single_task_multi_entries,
        test_subinterface_vid_ip_lists,
        test_subinterface_arp_autofill,
        test_subinterface_warns_switch_side,
        test_no_empty_commands,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"VRP 命令测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
