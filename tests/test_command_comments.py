"""命令注释生成测试：验证 generate_with_comments 的输出结构与注释质量

运行：python tests/test_command_comments.py
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


def main():
    # 内置模板不依赖 LLM，可传 None
    g = CommandGenerator(llm_client=None)

    print("=== 1. 接口配置：命令与注释一一对应 ===")
    items = g.generate_with_comments({
        "action": "config_interface",
        "parameters": {"interface": "GigabitEthernet0/0/1",
                       "ip": "10.0.0.1", "mask": "24"},
    })
    cmds = [i["command"] for i in items]
    check("生成 4 条命令", len(items) == 4, str(cmds))
    check("首条为 system-view", cmds[0] == "system-view", str(cmds))
    check("掩码 24 已转点分十进制",
          "ip address 10.0.0.1 255.255.255.0" in cmds, str(cmds))
    check("每条命令都有注释", all(i["comment"] for i in items),
          str([i["comment"] for i in items]))
    check("末条 quit 注释为退出至用户视图",
          items[-1]["comment"] == "退出至用户视图", items[-1]["comment"])

    print("\n=== 2. OSPF：区域与网段注释 ===")
    ospf = g.generate_with_comments({
        "action": "config_ospf",
        "parameters": {"process": 1, "area": 0,
                       "network": "10.0.0.0", "wildcard": "0.0.0.255"},
    })
    texts = {i["command"]: i["comment"] for i in ospf}
    check("ospf 1 注释含进程号", "1" in texts.get("ospf 1", ""), texts.get("ospf 1"))
    check("area 0 注释含区域号", "0" in texts.get("area 0", ""), texts.get("area 0"))
    check("network 注释含网段",
          "10.0.0.0" in texts.get("network 10.0.0.0 0.0.0.255", ""),
          texts.get("network 10.0.0.0 0.0.0.255"))

    print("\n=== 3. 静态路由：掩码转换与注释 ===")
    sr = g.generate_with_comments({
        "action": "config_static_route",
        "parameters": {"destination": "192.168.2.0", "mask": "24",
                       "next_hop": "10.0.0.2"},
    })
    check("含 ip route-static 命令",
          any("ip route-static" in i["command"] for i in sr), str(sr))
    check("静态路由命令有注释", all(i["comment"] for i in sr), str(sr))

    print("\n=== 4. VLAN：单个与批量 ===")
    v1 = g.generate_with_comments({
        "action": "config_vlan",
        "parameters": {"vlan_id": 10, "description": "OFFICE"},
    })
    v1c = [i["command"] for i in v1]
    check("含 vlan 10", "vlan 10" in v1c, str(v1c))
    check("含 description OFFICE", "description OFFICE" in v1c, str(v1c))
    check("含 system-view 起始", v1c[0] == "system-view", str(v1c))

    v2 = g.generate_with_comments({
        "action": "config_vlan", "parameters": {"vlan_id": [10, 20, 30]},
    })
    check("批量生成 vlan batch",
          any(i["command"].startswith("vlan batch") for i in v2), str(v2))

    print("\n=== 5. ACL：规则生成与注释 ===")
    a = g.generate_with_comments({
        "action": "config_acl",
        "parameters": {"acl_id": 3000,
                       "rules": ["permit ip source 10.0.0.0 0.0.0.255"]},
    })
    ac = [i["command"] for i in a]
    check("含 acl 3000", "acl 3000" in ac, str(ac))
    check("规则补全 rule 前缀",
          any(c.startswith("rule permit") for c in ac), str(ac))

    print("\n=== 6. 查询命令：不进入系统视图 ===")
    sh = g.generate_with_comments({
        "action": "show_command",
        "parameters": {"command": "display ip routing-table"},
    })
    check("只生成 1 条命令", len(sh) == 1, str(sh))
    check("不含 system-view",
          all(i["command"] != "system-view" for i in sh), str(sh))
    check("命令为 display 查询", sh[0]["command"] == "display ip routing-table",
          str(sh))
    check("查询命令有注释", bool(sh[0]["comment"]), str(sh))

    print("\n=== 7. 输出结构符合 UI 消费约定 ===")
    ok_struct = all(
        isinstance(i, dict) and set(i.keys()) == {"command", "comment"}
        for i in items
    )
    check("每项均为 {command, comment}", ok_struct, str(items[0]))

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"命令注释测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 50)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
