"""本地规则解析器测试（文本 → 任务 → VRP 命令 全链路）

重点验证用户反馈的原句：
  「配置S1上的G0/0/1接口为Trunk接口，允许vlan2、3通过」
在没有大模型的情况下也能解析出 link_type=trunk 与 allowed_vlans=[2,3]，
并生成正确的 Trunk 配置命令。

运行：python tests/test_rule_parser.py
      pytest tests/test_rule_parser.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from src.ai.rule_parser import RuleParser
from src.ai.command_gen import CommandGenerator

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def _pipeline(text):
    """文本 → 任务 → 命令，返回 (tasks, commands)"""
    rp = RuleParser()
    cg = CommandGenerator(llm_client=None)
    tasks = rp.parse(text)
    if not tasks:
        return tasks, []
    return tasks, [i["command"] for i in cg.generate_with_comments(tasks[0])]


def test_reported_trunk_sentence():
    """用户原句：不依赖 LLM 也要生成正确的 Trunk 配置"""
    tasks, cmds = _pipeline("配置S1上的G0/0/1接口为Trunk接口，允许vlan2、3通过")

    check("解析出 1 个任务", len(tasks) == 1, str(len(tasks)))
    if not tasks:
        return
    p = tasks[0]["parameters"]
    check("设备识别为 S1", tasks[0].get("device") == "S1", str(tasks[0].get("device")))
    check("action 为 config_interface",
          tasks[0].get("action") == "config_interface", str(tasks[0].get("action")))
    check("link_type 为 trunk", p.get("link_type") == "trunk", str(p))
    check("allowed_vlans 为 [2, 3]", p.get("allowed_vlans") == [2, 3], str(p))
    check("接口名保留 G0/0/1", p.get("interface") == "G0/0/1", str(p))

    check("命令含 port link-type trunk",
          "port link-type trunk" in cmds, str(cmds))
    check("命令含 port trunk allow-pass vlan 2 3",
          "port trunk allow-pass vlan 2 3" in cmds, str(cmds))
    check("接口名已展开为完整名称",
          "interface GigabitEthernet0/0/1" in cmds, str(cmds))


def test_access_sentence():
    """接入端口："加入 VLAN" 应识别为 access + default_vlan"""
    tasks, cmds = _pipeline("把S1的E0/0/1接口加入VLAN 10")
    p = tasks[0]["parameters"] if tasks else {}
    check("Access：default_vlan 为 10", p.get("default_vlan") == 10, str(p))
    check("Access：命令含 port link-type access",
          "port link-type access" in cmds, str(cmds))
    check("Access：命令含 port default vlan 10",
          "port default vlan 10" in cmds, str(cmds))


def test_interface_ip_sentence():
    """接口 IP"""
    tasks, cmds = _pipeline("在R1的G0/0/0配置IP 10.0.0.1/24")
    p = tasks[0]["parameters"] if tasks else {}
    check("IP：ip 参数正确", p.get("ip") == "10.0.0.1", str(p))
    check("IP：mask 参数为 24", p.get("mask") == 24, str(p))
    check("IP：命令含点分十进制掩码",
          "ip address 10.0.0.1 255.255.255.0" in cmds, str(cmds))


def test_svi_sentence():
    """VLAN 网关 / SVI"""
    tasks, cmds = _pipeline("在S1上配置VLAN 10的网关地址192.168.10.1/24")
    p = tasks[0]["parameters"] if tasks else {}
    check("SVI：action 为 config_vlanif",
          tasks and tasks[0].get("action") == "config_vlanif",
          str(tasks[0].get("action") if tasks else ""))
    check("SVI：vlan_id 为 10", p.get("vlan_id") == 10, str(p))
    check("SVI：命令进入 Vlanif10", "interface Vlanif10" in cmds, str(cmds))


def test_vlan_create_sentence():
    """批量创建 VLAN"""
    tasks, cmds = _pipeline("在S1上创建VLAN 10、20、30")
    p = tasks[0]["parameters"] if tasks else {}
    check("VLAN：vlan_id 为 [10,20,30]", p.get("vlan_id") == [10, 20, 30], str(p))
    check("VLAN：命令为 vlan batch", "vlan batch 10 20 30" in cmds, str(cmds))


def test_ospf_multi_device():
    """一条指令涉及两台设备时应生成两个任务"""
    rp = RuleParser()
    tasks = rp.parse("在R1和R2之间配置OSPF，area 0，R1的网段10.0.0.0/24")
    devices = [t.get("device") for t in tasks]
    check("OSPF：生成 2 个任务", len(tasks) == 2, str(len(tasks)))
    check("OSPF：设备为 R1 与 R2", devices == ["R1", "R2"], str(devices))
    if tasks:
        check("OSPF：area 为 0", tasks[0]["parameters"].get("area") == 0,
              str(tasks[0]["parameters"]))


def test_static_route_sentence():
    """静态路由"""
    tasks, cmds = _pipeline("在R1上配置静态路由，目的网段192.168.2.0/24，下一跳10.0.0.2")
    p = tasks[0]["parameters"] if tasks else {}
    check("路由：destination 正确", p.get("destination") == "192.168.2.0", str(p))
    check("路由：next_hop 正确", p.get("next_hop") == "10.0.0.2", str(p))
    check("路由：命令完整",
          "ip route-static 192.168.2.0 255.255.255.0 10.0.0.2" in cmds, str(cmds))


def test_show_sentence():
    """查询命令不应进入系统视图"""
    tasks, cmds = _pipeline("查看R1的路由表")
    check("查询：action 为 show_command",
          tasks and tasks[0].get("action") == "show_command",
          str(tasks[0].get("action") if tasks else ""))
    check("查询：命令为 display ip routing-table",
          cmds == ["display ip routing-table"], str(cmds))
    check("查询：不含 system-view", "system-view" not in cmds, str(cmds))


def test_unknown_sentence_returns_empty():
    """无法识别的指令返回空列表（由上层决定如何提示）"""
    rp = RuleParser()
    check("无意义文本返回空", rp.parse("今天天气怎么样") == [],
          str(rp.parse("今天天气怎么样")))
    check("空文本返回空", rp.parse("") == [])


def test_no_device_no_task():
    """缺少设备名时不应臆造设备"""
    rp = RuleParser()
    tasks = rp.parse("配置G0/0/1为Trunk接口，允许vlan 2通过")
    check("缺设备时不生成任务", tasks == [], str(tasks))


def main():
    print("=" * 60)
    print("本地规则解析器测试（文本 → VRP 命令 全链路）")
    print("=" * 60)
    for fn in (
        test_reported_trunk_sentence,
        test_access_sentence,
        test_interface_ip_sentence,
        test_svi_sentence,
        test_vlan_create_sentence,
        test_ospf_multi_device,
        test_static_route_sentence,
        test_show_sentence,
        test_unknown_sentence_returns_empty,
        test_no_device_no_task,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"规则解析测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
