"""拓扑刷新测试：验证「拓扑自动发现」不会残留上一份实验的拓扑

复现并验证修复的需求：
  "拓扑自动发现功能会经常保留上一份实验的拓扑而不刷新，
   要点击几次拓扑自动发现才会重新刷新新拓扑。"

涉及两个根因：
  1) TopologyBuilder 在图未重置时累加/残留旧节点与旧边 → 新增 clear() 并在 build() 前调用；
  2) 采集失败时旧图被继续展示 → build({}) 应清空图，界面据此提示"历史拓扑"。
另外验证 InfoCollector.wait_lldp_convergence 收敛等待逻辑（用假 DeviceManager）。

运行：python tests/test_topology_refresh.py
"""
import os
import sys
import json
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from src.topology.builder import TopologyBuilder
from src.topology.collector import InfoCollector

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


# ---------- 假的 DeviceManager：返回脚本化的 LLDP 输出序列 ----------
class ScriptedDM:
    """按调用轮次返回不同的 LLDP 输出，用于模拟邻居表逐步收敛"""

    def __init__(self, script):
        # script: {device: [out_round1, out_round2, ...]}
        self.script = script
        self.calls = {}

    def execute_command(self, name, cmd):
        idx = self.calls.get(name, 0)
        outs = self.script.get(name, [""])
        self.calls[name] = idx + 1
        if idx < len(outs):
            return outs[idx]
        return outs[-1]

    def execute_batch(self, device_names, commands, is_config=False):
        return {d: {c: "" for c in commands} for d in device_names}


def test_clear_resets_state():
    """clear() 应彻底清空 graph/devices/nodes/edges/hostname 索引"""
    b = TopologyBuilder()
    b.build({
        "R1": {
            "display current-configuration | include sysname": "sysname R1\n",
            "display lldp neighbor brief": "GE0/0/1   R2   GE0/0/2\n",
        },
        "R2": {
            "display current-configuration | include sysname": "sysname R2\n",
            "display lldp neighbor brief": "",
        },
    })
    check("构建后有节点", b.graph.number_of_nodes() == 2, str(b.graph.number_of_nodes()))

    b.clear()
    check("clear() 后 graph 为空", b.graph.number_of_nodes() == 0)
    check("clear() 后 edges 列表为空", b.edges == [])
    check("clear() 后 nodes 字典为空", b.nodes == {})
    check("clear() 后 devices 为空", b.devices == {})
    check("clear() 后 hostname 索引为空", b.hostname_to_key == {})


def test_rebuild_does_not_retain_old_topology():
    """第二次实验（不同设备/不同链路）不应残留第一次的节点与边"""
    b = TopologyBuilder()
    # 第一次实验：R1-R2
    b.build({
        "R1": {
            "display current-configuration | include sysname": "sysname R1\n",
            "display lldp neighbor brief": "GE0/0/1   R2   GE0/0/2\n",
        },
        "R2": {
            "display current-configuration | include sysname": "sysname R2\n",
            "display lldp neighbor brief": "GE0/0/2   R1   GE0/0/1\n",
        },
    })
    check("第一次实验：2 节点 1 边",
          b.graph.number_of_nodes() == 2 and b.graph.number_of_edges() == 1,
          f"{b.graph.number_of_nodes()} 节点 / {b.graph.number_of_edges()} 边")

    # 第二次实验：换成 S1-S3，且不再包含 R1/R2
    g = b.build({
        "S1": {
            "display current-configuration | include sysname": "sysname S1\n",
            "display lldp neighbor brief": "GE0/0/1   S3   GE0/0/2\n",
        },
        "S3": {
            "display current-configuration | include sysname": "sysname S3\n",
            "display lldp neighbor brief": "GE0/0/2   S1   GE0/0/1\n",
        },
    })
    check("第二次实验：不残留 R1", "R1" not in g.nodes(), str(sorted(g.nodes())))
    check("第二次实验：不残留 R2", "R2" not in g.nodes(), str(sorted(g.nodes())))
    check("第二次实验：不残留旧的 R1-R2 链路", not g.has_edge("R1", "R2"))
    check("第二次实验：仅 2 节点 1 边",
          g.number_of_nodes() == 2 and g.number_of_edges() == 1,
          f"{g.number_of_nodes()} 节点 / {g.number_of_edges()} 边")


def test_partial_rebuild_drops_missing_device():
    """第二次采集只拿到部分设备时，未采集到的旧设备必须消失（不是保留）"""
    b = TopologyBuilder()
    b.build({
        "R1": {
            "display current-configuration | include sysname": "sysname R1\n",
            "display lldp neighbor brief": "GE0/0/1   R2   GE0/0/2\n",
        },
        "R2": {
            "display current-configuration | include sysname": "sysname R2\n",
            "display lldp neighbor brief": "",
        },
        "R3": {
            "display current-configuration | include sysname": "sysname R3\n",
            "display lldp neighbor brief": "",
        },
    })
    check("初始：3 节点", b.graph.number_of_nodes() == 3, str(sorted(b.graph.nodes())))

    # 第二次只采集到 R1（R2、R3 掉线/未采集）
    g = b.build({
        "R1": {
            "display current-configuration | include sysname": "sysname R1\n",
            "display lldp neighbor brief": "",
        },
    })
    check("部分采集后：R2 被移除", "R2" not in g.nodes(), str(sorted(g.nodes())))
    check("部分采集后：R3 被移除", "R3" not in g.nodes(), str(sorted(g.nodes())))
    check("部分采集后：仅剩 1 节点", g.number_of_nodes() == 1, str(sorted(g.nodes())))


def test_failed_collection_empties_graph():
    """采集完全失败（build({})）时，图必须为空，避免继续展示旧拓扑"""
    b = TopologyBuilder()
    b.build({
        "R1": {
            "display current-configuration | include sysname": "sysname R1\n",
            "display lldp neighbor brief": "GE0/0/1   R2   GE0/0/2\n",
        },
        "R2": {
            "display current-configuration | include sysname": "sysname R2\n",
            "display lldp neighbor brief": "",
        },
    })
    check("失败前有节点", b.graph.number_of_nodes() > 0)

    g = b.build({})  # 模拟 usable 为空
    check("build({}) 后图被清空", g.number_of_nodes() == 0, str(g.number_of_nodes()))
    check("build({}) 后无残留边", g.number_of_edges() == 0)


def test_load_then_rebuild_no_residue():
    """从历史文件加载旧拓扑后，重新构建不应与历史数据混合"""
    tmpdir = tempfile.mkdtemp()
    b_old = TopologyBuilder(data_dir=tmpdir)
    b_old.build({
        "AC": {
            "display current-configuration | include sysname": "sysname AC\n",
            "display lldp neighbor brief": "GE0/0/1   ap1   GE0/0/1\n",
        },
        "ap1": {
            "display current-configuration | include sysname": "sysname ap1\n",
            "display lldp neighbor brief": "",
        },
    })
    b_old.save_topology()
    check("历史拓扑文件已生成", os.path.exists(os.path.join(tmpdir, "topology.json")))

    # 新会话：先加载历史，再重新发现新实验
    b_new = TopologyBuilder(data_dir=tmpdir)
    b_new.load_topology()
    check("加载历史后有 AC 节点", "AC" in b_new.graph.nodes(), str(sorted(b_new.graph.nodes())))

    g = b_new.build({
        "S1": {
            "display current-configuration | include sysname": "sysname S1\n",
            "display lldp neighbor brief": "GE0/0/1   S4   GE0/0/2\n",
        },
        "S4": {
            "display current-configuration | include sysname": "sysname S4\n",
            "display lldp neighbor brief": "",
        },
    })
    check("重新发现后：历史 AC 节点消失", "AC" not in g.nodes(), str(sorted(g.nodes())))
    check("重新发现后：历史 ap1 节点消失", "ap1" not in g.nodes(), str(sorted(g.nodes())))
    check("重新发现后：仅剩新实验节点",
          sorted(g.nodes()) == ["S1", "S4"], str(sorted(g.nodes())))


def test_lldp_convergence_waits_until_stable():
    """wait_lldp_convergence 应在邻居行数连续两轮一致后返回"""
    # 第 1 轮：R1 只有 0 行（未收敛）；第 2 轮：1 行；第 3 轮：1 行（稳定）
    header = "Local Intf   Neighbor Dev   Neighbor Intf\n"
    script = {
        "R1": ["", header + "GE0/0/1  R2  GE0/0/2\n", header + "GE0/0/1  R2  GE0/0/2\n"],
        "R2": ["", header + "GE0/0/2  R1  GE0/0/1\n", header + "GE0/0/2  R1  GE0/0/1\n"],
    }
    dm = ScriptedDM(script)
    col = InfoCollector(dm, {})
    counts = col.wait_lldp_convergence(["R1", "R2"], max_rounds=3, interval=0)
    check("收敛后行数统计正确（R1=1）", counts.get("R1") == 1, str(counts))
    check("收敛后行数统计正确（R2=1）", counts.get("R2") == 1, str(counts))


def test_lldp_convergence_stops_early():
    """若首轮即稳定（连续两轮一致），应在第 2 轮后提前结束"""
    header = "Local Intf   Neighbor Dev   Neighbor Intf\n"
    row = header + "GE0/0/1  R2  GE0/0/2\n"
    dm = ScriptedDM({"R1": [row, row, row, row]})
    col = InfoCollector(dm, {})
    col.wait_lldp_convergence(["R1"], max_rounds=5, interval=0)
    check("稳定后提前结束（调用次数=2）", dm.calls.get("R1") == 2, str(dm.calls))


def test_lldp_row_count_filters_header():
    """_count_lldp_rows 应排除表头与分隔线"""
    txt = ("Local Intf   Neighbor Dev   Neighbor Intf\n"
           "-------------------------------------------\n"
           "GE0/0/1      R2               GE0/0/2\n"
           "GE0/0/2      R3               GE0/0/1\n")
    n = InfoCollector._count_lldp_rows(txt)
    check("表头/分隔线被排除，仅计 2 行", n == 2, str(n))
    check("空文本返回 0", InfoCollector._count_lldp_rows("") == 0)


def main():
    print("=" * 60)
    print("拓扑刷新与 LLDP 收敛测试")
    print("=" * 60)

    for fn in (
        test_clear_resets_state,
        test_rebuild_does_not_retain_old_topology,
        test_partial_rebuild_drops_missing_device,
        test_failed_collection_empties_graph,
        test_load_then_rebuild_no_residue,
        test_lldp_convergence_waits_until_stable,
        test_lldp_convergence_stops_early,
        test_lldp_row_count_filters_header,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"拓扑刷新测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
