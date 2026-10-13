"""拓扑构建器测试：验证主机名归一化与链路推断

复现并验证修复：旧版中 LLDP 邻居上报的是 sysname（如 OSPF-Core1），
与设备键（如 R1）不一致，导致节点错位、链路连错。
运行：python tests/test_topology.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from src.topology.builder import TopologyBuilder

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def main():
    # 模拟采集数据：设备键 R1/R2，但 R1 的实际 sysname 是 OSPF-Core1
    collected = {
        "R1": {
            "display current-configuration | include sysname": "sysname OSPF-Core1\n",
            "display lldp neighbor brief": (
                "Local Intf      Neighbor Dev     Neighbor Intf\n"
                "GE0/0/1         R2               GE0/0/2\n"
                "GE0/0/2         R3               GE0/0/1\n"
            ),
        },
        "R2": {
            "display current-configuration | include sysname": "sysname R2\n",
            "display lldp neighbor brief": (
                "Local Intf      Neighbor Dev     Neighbor Intf\n"
                "GE0/0/2         OSPF-Core1       GE0/0/1\n"
            ),
        },
        "R3": {
            "display current-configuration | include sysname": "sysname R3\n",
            "display lldp neighbor brief": (
                "Local Intf      Neighbor Dev     Neighbor Intf\n"
                "GE0/0/1         OSPF-Core1       GE0/0/2\n"
            ),
        },
    }

    builder = TopologyBuilder()
    graph = builder.build(collected)

    print("=== 节点 ===")
    print(sorted(graph.nodes()))
    print("=== 边 ===")
    for u, v, d in graph.edges(data=True):
        print(f"  {u} -- {v}  ({d.get('local_port')} <-> {d.get('remote_port')})")

    print("\n=== 断言 ===")
    check("节点数为 3（R1/R2/R3）", graph.number_of_nodes() == 3, str(sorted(graph.nodes())))
    check("R1 的 hostname 正确解析为 OSPF-Core1",
          graph.nodes["R1"].get("hostname") == "OSPF-Core1",
          graph.nodes["R1"].get("hostname"))
    check("未凭空创建 'OSPF-Core1' 这种伪节点",
          "OSPF-Core1" not in graph.nodes(), str(sorted(graph.nodes())))
    check("R1--R2 链路存在", graph.has_edge("R1", "R2"))
    check("R1--R3 链路存在", graph.has_edge("R1", "R3"))
    check("不存在错误链路 R2--R3", not graph.has_edge("R2", "R3"))
    check("链路数为 2", graph.number_of_edges() == 2, str(graph.number_of_edges()))
    if graph.has_edge("R1", "R2"):
        d = graph["R1"]["R2"]
        check("R1--R2 端口信息正确",
              d.get("local_port") == "GE0/0/1" and d.get("remote_port") == "GE0/0/2",
              str(d))

    print("\n=== 重复构建幂等性 ===")
    g2 = builder.build(collected)
    check("重复构建不会累加边（幂等）", g2.number_of_edges() == 2, str(g2.number_of_edges()))

    print("\n=== 表头/分隔线过滤 ===")
    builder2 = TopologyBuilder()
    g3 = builder2.build({
        "R1": {
            "display current-configuration | include sysname": "sysname R1\n",
            "display lldp neighbor brief": "Local Intf  Neighbor Dev  Neighbor Intf\n------------\n",
        }
    })
    check("表头与分隔线不产生伪链路", g3.number_of_edges() == 0, str(g3.number_of_edges()))

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"拓扑测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 50)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
