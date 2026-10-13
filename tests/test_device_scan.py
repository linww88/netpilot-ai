"""设备扫描与自动命名测试

覆盖：端口扫描、设备识别、主机名解析、批量添加、重命名、DB 元信息持久化。
不依赖真实 eNSP：通过打桩 `_port_is_open` / 连接对象模拟设备。
运行：python tests/test_device_scan.py
"""
import os
import sys
import sqlite3
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

import src.connection.device_manager as dm_module
from src.connection.device_manager import DeviceManager, DEFAULT_SCAN_START, DEFAULT_SCAN_END
from src.utils.db import DatabaseManager

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


class FakeConn:
    """模拟 netmiko 连接对象"""

    def __init__(self, prompt="<R1>", sysname="R1", version_out=None):
        self.prompt = prompt
        self.sysname = sysname
        self.version_out = version_out or (
            "Huawei Versatile Routing Platform Software\n"
            "VRP (R) software, Version 5.170 (AR1220 V300R003C10SPC600)\n"
        )
        self.commands = []
        self.disconnected = False

    def write_channel(self, data):
        return None

    def read_channel(self):
        return f"\r\n{self.prompt}"

    def find_prompt(self):
        return self.prompt

    def send_command_timing(self, cmd, **kw):
        self.commands.append(cmd)
        if "sysname" in cmd:
            return f"sysname {self.sysname}\n"
        if "display version" in cmd:
            return self.version_out
        return ""

    def disconnect(self):
        self.disconnected = True


def patch_scan(dm, open_ports, conns_by_port):
    """打桩：指定开放端口，并让指定端口返回可用连接"""
    dm._port_is_open = staticmethod(lambda ip, port, timeout=0.3: port in open_ports)

    def fake_connect(device_type=None, ip=None, port=None, **kw):
        conn = conns_by_port.get(port)
        if conn is None:
            raise Exception(f"cannot connect to {port}")
        return conn

    dm_module.ConnectHandler = fake_connect
    return dm


def main():
    dm = DeviceManager({"pool_size": 10, "scan_timeout": 0.05})

    # ---------------- 1. 扫描范围与端口发现 ----------------
    print("=== 1. 端口扫描 ===")
    open_ports = [2000, 2003, 2001, 2099, 3001]
    conns = {
        2000: FakeConn(prompt="<R1>", sysname="R1"),
        2001: FakeConn(prompt="<R2>", sysname="R2"),
        2003: FakeConn(prompt="<R3>", sysname="R3"),
        2099: FakeConn(prompt="<OSPF-Core1>", sysname="OSPF-Core1"),
    }
    patch_scan(dm, set(open_ports), conns)

    scanned = dm.scan_devices("127.0.0.1", 2000, 2100)
    ports = [r["port"] for r in scanned]
    check("只返回范围内端口（3001 被排除）", 3001 not in ports, str(ports))
    check("结果按端口升序", ports == sorted(ports), str(ports))
    check("发现 4 个开放端口", len(scanned) == 4, str(ports))
    check("全部识别为设备", all(r["is_device"] for r in scanned), str([r["is_device"] for r in scanned]))
    check("主机名正确解析", [r["hostname"] for r in scanned] == ["R1", "R2", "R3", "OSPF-Core1"],
          str([r["hostname"] for r in scanned]))
    check("型号正确解析", all(r["model"] == "AR1220" for r in scanned),
          str([r["model"] for r in scanned]))

    # ---------------- 2. 非设备端口识别 ----------------
    print("\n=== 2. 非设备端口被排除 ===")
    class NonDeviceConn(FakeConn):
        def read_channel(self):
            return "HTTP/1.1 400 Bad Request\r\nServer: nginx"

        def find_prompt(self):
            return ""

    dm2 = DeviceManager({"pool_size": 10, "scan_timeout": 0.05})
    patch_scan(dm2, {2000, 2005}, {2000: FakeConn(), 2005: NonDeviceConn()})
    scanned2 = dm2.scan_devices("127.0.0.1", 2000, 2100)
    dev_ports = [r["port"] for r in scanned2 if r["is_device"]]
    check("非设备端口未被识别为设备", dev_ports == [2000], str(dev_ports))
    check("非设备端口仍出现在结果中（标记 is_device=False）",
          any(r["port"] == 2005 and not r["is_device"] for r in scanned2),
          str([(r["port"], r["is_device"]) for r in scanned2]))

    # ---------------- 3. 范围自动纠正 ----------------
    print("\n=== 3. 范围纠正与边界 ===")
    dm3 = DeviceManager({"pool_size": 10})
    patch_scan(dm3, {2050}, {2050: FakeConn(sysname="X")})
    r = dm3.scan_devices("127.0.0.1", 2100, 2000)  # 故意反序
    check("起始>结束 时自动交换", [x["port"] for x in r] == [2050], str([x["port"] for x in r]))

    # ---------------- 4. 主机名命名批量添加 ----------------
    print("\n=== 4. scan_and_add 命名 ===")
    dm4 = DeviceManager({"pool_size": 10})
    patch_scan(dm4, {2000, 2001}, {
        2000: FakeConn(sysname="Core-SW1"),
        2001: FakeConn(sysname="Core-SW2"),
    })
    added = dm4.scan_and_add("127.0.0.1", 2000, 2100, use_hostname=True)
    names = [a["name"] for a in added]
    check("使用主机名命名", names == ["Core-SW1", "Core-SW2"], str(names))
    check("设备已加入 devices", set(dm4.devices) == {"Core-SW1", "Core-SW2"}, str(list(dm4.devices)))
    check("端口映射正确", dm4.devices["Core-SW1"]["port"] == 2000,
          str(dm4.devices["Core-SW1"]["port"]))

    # ---------------- 5. 前缀命名（未取到主机名时） ----------------
    print("\n=== 5. 无主机名时用前缀命名 ===")
    dm5 = DeviceManager({"pool_size": 10})
    patch_scan(dm5, {2000, 2001}, {
        2000: FakeConn(sysname=""),  # 取不到 sysname
        2001: FakeConn(sysname=""),
    })
    added5 = dm5.scan_and_add("127.0.0.1", 2000, 2100, use_hostname=True, name_prefix="R")
    names5 = [a["name"] for a in added5]
    check("回退为前缀+序号命名", names5 == ["R1", "R2"], str(names5))

    # ---------------- 6. 只添加勾选端口 ----------------
    print("\n=== 6. 选择性添加 ===")
    dm6 = DeviceManager({"pool_size": 10})
    patch_scan(dm6, {2000, 2001, 2002}, {
        2000: FakeConn(sysname="A"),
        2001: FakeConn(sysname="B"),
        2002: FakeConn(sysname="C"),
    })
    scanned6 = dm6.scan_devices("127.0.0.1", 2000, 2100)
    added6 = dm6.scan_and_add("127.0.0.1", 2000, 2100,
                              selected_ports=[2000, 2002], scanned=scanned6)
    check("只添加勾选的端口", sorted(a["name"] for a in added6) == ["A", "C"],
          str([a["name"] for a in added6]))

    # ---------------- 7. 重复添加去重 ----------------
    print("\n=== 7. 重复添加去重 ===")
    added6b = dm6.scan_and_add("127.0.0.1", 2000, 2100,
                               selected_ports=[2000], scanned=scanned6)
    check("同一地址不重复添加", added6b == [], str(added6b))

    # ---------------- 8. 重命名（读取主机名） ----------------
    print("\n=== 8. refresh_device_name 重命名 ===")
    dm8 = DeviceManager({"pool_size": 10})
    dm8.add_device("R1", "127.0.0.1", 2000)
    conn8 = FakeConn(prompt="<OSPF-Core1>", sysname="OSPF-Core1")
    dm8.sessions["R1"] = conn8
    new_name = dm8.refresh_device_name("R1")
    check("重命名为真实主机名", new_name == "OSPF-Core1", str(new_name))
    check("旧键已移除", "R1" not in dm8.devices, str(list(dm8.devices)))
    check("新键存在且端口保留", dm8.devices.get("OSPF-Core1", {}).get("port") == 2000,
          str(dm8.devices.get("OSPF-Core1", {})))
    check("会话已迁移", "OSPF-Core1" in dm8.sessions and "R1" not in dm8.sessions,
          str(list(dm8.sessions)))

    # ---------------- 9. 重命名冲突保护 ----------------
    print("\n=== 9. 重命名冲突保护 ===")
    dm9 = DeviceManager({"pool_size": 10})
    dm9.add_device("R1", "127.0.0.1", 2000)
    dm9.add_device("R2", "127.0.0.1", 2001)
    dm9.sessions["R1"] = FakeConn(prompt="<R2>", sysname="R2")  # 与已有 R2 冲突
    res9 = dm9.refresh_device_name("R1")
    check("冲突时返回 None 且不改名", res9 is None and "R1" in dm9.devices and "R2" in dm9.devices,
          f"res={res9}, keys={list(dm9.devices)}")

    # ---------------- 10. 主机名解析边界 ----------------
    print("\n=== 10. 提示符解析边界 ===")
    check("解析 <R1>", DeviceManager._extract_hostname_from_banner("\r\n<R1>") == "R1")
    check("解析 [R1]", DeviceManager._extract_hostname_from_banner("[R1]") == "R1")
    check("解析 [~R1]", DeviceManager._extract_hostname_from_banner("[~R1]") == "R1")
    check("解析 多行取最后一个提示符",
          DeviceManager._extract_hostname_from_banner("<R1>\r\nsome output\r\n<R2>") == "R2")
    check("无提示符返回空", DeviceManager._extract_hostname_from_banner("hello world") == "")

    # ---------------- 11. DB 元信息持久化 ----------------
    print("\n=== 11. 数据库元信息 ===")
    tmp_dir = tempfile.mkdtemp(prefix="ensp_scan_test_")
    try:
        db_path = os.path.join(tmp_dir, "t.db")
        db = DatabaseManager(db_path=db_path)
        db.add_device("Core-SW1", "127.0.0.1", 2000, "generic_telnet",
                      hostname="Core-SW1", model="S5700", version="V200R003")
        rows = db.get_devices_with_meta()
        check("新增设备含元信息", len(rows) == 1 and rows[0][4] == "Core-SW1" and rows[0][5] == "S5700",
              str(rows))
        # 更新同一条记录而非重复插入
        db.add_device("Core-SW1", "127.0.0.1", 2005, "generic_telnet",
                      hostname="Core-SW1", model="S5700", version="V200R005")
        rows2 = db.get_devices_with_meta()
        check("同名设备为更新而非插入", len(rows2) == 1 and rows2[0][2] == 2005, str(rows2))
        check("版本已更新", rows2[0][6] == "V200R005", str(rows2[0][6]))
    finally:
        # Windows 下 sqlite 文件句柄需确保释放，忽略清理异常
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    print("\n=== 12. 旧库兼容迁移 ===")
    tmp_dir2 = tempfile.mkdtemp(prefix="ensp_scan_test2_")
    try:
        db_path = os.path.join(tmp_dir2, "old.db")
        # 建旧结构
        with sqlite3.connect(db_path) as c:
            c.execute("CREATE TABLE devices (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "name TEXT NOT NULL, ip TEXT NOT NULL, port INTEGER NOT NULL, type TEXT NOT NULL)")
            c.execute("INSERT INTO devices (name, ip, port, type) VALUES ('R1','127.0.0.1',2000,'generic_telnet')")
            c.commit()
        db = DatabaseManager(db_path=db_path)  # 应自动补列
        rows = db.get_devices_with_meta()
        check("旧库自动补列后可读", len(rows) == 1 and rows[0][0] == "R1", str(rows))
    finally:
        try:
            import shutil
            shutil.rmtree(tmp_dir2, ignore_errors=True)
        except Exception:
            pass

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"扫描功能测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 50)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
