"""设备管理器测试：验证配置命令下发与 LLDP 启用的健壮性

用假连接对象模拟 eNSP/Huawei VRP 的典型行为（包括 send_config_set 失败），
验证修复后的逐条下发 + timing 兜底逻辑。
运行：python tests/test_device_manager.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

import src.connection.device_manager as dm_module
from src.connection.device_manager import DeviceManager

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


class FakeConn:
    """模拟设备连接：send_config_set 会失败（复现真机问题），timing 模式可用"""

    def __init__(self, config_set_works=False, prompt="<R1>"):
        self.config_set_works = config_set_works
        self.prompt = prompt
        self.sent = []
        self.timing_sent = []

    def send_command(self, cmd, **kw):
        self.sent.append(cmd)
        return f"output of {cmd}"

    def send_command_timing(self, cmd, **kw):
        self.timing_sent.append(cmd)
        return f"timing output of {cmd}"

    def send_config_set(self, cmds, **kw):
        if not self.config_set_works:
            raise Exception("Pattern not detected: 'system-view' in output")
        self.sent.extend(cmds)
        return "config ok"

    def find_prompt(self):
        return self.prompt


def make_dm_with_conn(conn):
    dm = DeviceManager({"pool_size": 10, "timeout": 5})
    dm.devices["R1"] = {"name": "R1", "ip": "127.0.0.1", "port": 2000,
                        "device_type": "generic_telnet"}
    dm.sessions["R1"] = conn
    return dm


def main():
    print("=== 1. send_config_set 失败时用 timing 兜底 ===")
    conn = FakeConn(config_set_works=False)
    dm = make_dm_with_conn(conn)
    ok = dm.execute_config_commands("R1", ["system-view", "lldp enable"])
    check("配置下发返回成功", ok)
    check("timing 模式确实下发了 system-view", "system-view" in conn.timing_sent,
          str(conn.timing_sent))
    check("timing 模式确实下发了 lldp enable", "lldp enable" in conn.timing_sent,
          str(conn.timing_sent))

    print("\n=== 2. 正常路径也使用逐条 timing 下发（不依赖 send_config_set 提示符识别） ===")
    conn2 = FakeConn(config_set_works=True)
    dm2 = make_dm_with_conn(conn2)
    ok2 = dm2.execute_config_commands("R1", ["system-view", "lldp enable"])
    check("配置下发返回成功", ok2)
    # 修复后：配置命令统一走 send_command_timing，避免 send_config_set 在 eNSP 上识别提示符失败
    check("逐条 timing 下发了全部命令",
          "system-view" in conn2.timing_sent and "lldp enable" in conn2.timing_sent,
          str(conn2.timing_sent))

    print("\n=== 3. execute_command 查询失败时用 timing 兜底 ===")
    class QueryFailConn(FakeConn):
        def send_command(self, cmd, **kw):
            raise Exception("Pattern not detected: '<R3>' in output")

    conn3 = QueryFailConn()
    dm3 = make_dm_with_conn(conn3)
    out = dm3.execute_command("R1", "display clock")
    check("查询命令有兜底结果", out is not None, repr(out))
    check("timing 兜底执行了查询", "display clock" in conn3.timing_sent,
          str(conn3.timing_sent))

    print("\n=== 4. ensure_lldp_enabled：命令不支持时返回 False ===")
    class UnsupportedConn(FakeConn):
        def send_command_timing(self, cmd, **kw):
            self.timing_sent.append(cmd)
            return "Error: Unrecognized command found at '^' position."

        def find_prompt(self):
            return "<R1>"

        def write_channel(self, data):
            self.timing_sent.append(str(data).strip())

        def read_channel(self):
            # 模拟真实设备：命令写入后返回 "回显 + 报错 + 提示符"
            if self.timing_sent and "display lldp local" in self.timing_sent[-1]:
                return "display lldp local\nError: Unrecognized command found at '^' position.\n<R1>"
            return "\n<R1>"

    conn4 = UnsupportedConn(config_set_works=True)
    dm4 = make_dm_with_conn(conn4)
    r = dm4.ensure_lldp_enabled("R1")
    check("不支持 LLDP 时返回 False", r is False, str(r))

    print("\n=== 5. ensure_lldp_enabled：已启用时直接返回 True ===")
    class EnabledConn(FakeConn):
        def send_command_timing(self, cmd, **kw):
            self.timing_sent.append(cmd)
            return "LLDP is enabled globally. Local interface information:"

    conn5 = EnabledConn(config_set_works=True)
    dm5 = make_dm_with_conn(conn5)
    r5 = dm5.ensure_lldp_enabled("R1")
    check("已启用时返回 True", r5 is True, str(r5))
    check("已启用时不重复下发配置", not conn5.sent or "lldp enable" not in conn5.sent)

    print("\n=== 6. 配置后确保退出配置模式 ===")
    conn6 = FakeConn(config_set_works=True, prompt="<R1>")
    dm6 = make_dm_with_conn(conn6)
    dm6.execute_config_commands("R1", ["system-view", "lldp enable"])
    check("正常结束未残留配置模式", "return" not in conn6.timing_sent,
          str(conn6.timing_sent))

    class StuckConn(FakeConn):
        def __init__(self):
            super().__init__(config_set_works=True)
            self.return_sent = False

        def find_prompt(self):
            # 首次检查仍在配置模式，收到 return 后恢复为用户视图
            return "<R1>" if self.return_sent else "[R1]"

        def write_channel(self, data):
            if data.strip() == "return":
                self.return_sent = True
            self.timing_sent.append(data.strip())

    conn7 = StuckConn()
    dm7 = make_dm_with_conn(conn7)
    dm7.execute_config_commands("R1", ["system-view", "lldp enable"])
    check("检测到配置模式时发送 return", conn7.return_sent,
          str(conn7.timing_sent))

    print("\n=== 7. 移除所有设备 ===")
    dm8 = DeviceManager({})
    for _n, _p in (("R1", 2001), ("R2", 2002), ("S1", 2003)):
        dm8.add_device(_n, "127.0.0.1", _p)

    class ClosableConn:
        def __init__(self):
            self.closed = False

        def disconnect(self):
            self.closed = True

    live = ClosableConn()
    dm8.sessions["R1"] = live
    removed = dm8.remove_all_devices()
    check("remove_all_devices 返回移除数量", removed == 3, str(removed))
    check("设备表已清空", dm8.devices == {}, str(list(dm8.devices.keys())))
    check("会话表已清空", dm8.sessions == {}, str(list(dm8.sessions.keys())))
    check("移除前先断开了已有连接", live.closed is True, str(live.closed))
    check("空列表重复调用幂等", dm8.remove_all_devices() == 0, "应返回 0")

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"设备管理器测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 50)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
