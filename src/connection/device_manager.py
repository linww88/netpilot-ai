"""设备连接管理模块""" 
import logging 
import os
import re
import socket
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple 
from concurrent.futures import ThreadPoolExecutor, as_completed
from netmiko import ConnectHandler 
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException 
from .session_pool import SessionPool 

logger = logging.getLogger(__name__) 

# eNSP 默认端口扫描范围
DEFAULT_SCAN_START = 2000
DEFAULT_SCAN_END = 2100

# 设备型号关键字（华为 eNSP 常见，按特异性从高到低排列，需整词边界匹配）
_MODEL_KEYWORDS = (
    "S5700", "S3700", "S2700", "S6700", "S9300",
    "AR", "NE", "CE", "USG", "Router", "Switch",
)

# 型号匹配正则：要求以关键字开头且后接型号数字，避免把 software 里的 "are" 误判为 AR
_MODEL_RE = re.compile(
    r"\b((?:S|AR|NE|CE|USG)\d{3,5}[\w\-]*)\b",
    re.IGNORECASE,
)

# 提示符正则：<R1> 或 [R1] 或 [~R1]
_PROMPT_RE = re.compile(r"^[<\[]~?([^>\]~]+)[>\]]$")


class DeviceManager: 
    """管理所有网络设备的连接""" 
    
    def __init__(self, config: dict): 
        self.config = config 
        self.devices: Dict[str, dict] = {}  # name -> device info 
        self.sessions: Dict[str, Any] = {} 
        self.pool = SessionPool(max_size=config.get('pool_size', 10)) 
        self.scan_timeout = config.get('scan_timeout', 0.3)
        
    def add_device(self, name: str, ip: str, port: int = 2000) -> bool: 
        """添加设备信息（eNSP 专用：默认免密 Telnet）

        如果同名设备已存在，则更新其地址信息而非静默覆盖失败。
        """ 
        # eNSP 强制使用 generic_telnet 以跳过所有自动化登录探测
        device_type = 'generic_telnet'

        existing = self.devices.get(name, {})
        self.devices[name] = { 
            'name': name, 
            'ip': ip, 
            'port': port, 
            'device_type': device_type,
            'hostname': existing.get('hostname', ''),
            'model': existing.get('model', ''),
            'version': existing.get('version', ''),
        } 
        return True 

    # ------------------------------------------------------------------
    # 端口扫描与设备识别
    # ------------------------------------------------------------------
    @staticmethod
    def _port_is_open(ip: str, port: int, timeout: float = 0.3) -> bool:
        """检测 TCP 端口是否开放（用于发现 eNSP 映射的串口）"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                return sock.connect_ex((ip, port)) == 0
        except Exception:
            return False

    @staticmethod
    def _extract_model(text: str) -> str:
        """从 display version 回显中提取设备型号

        例如 "VRP (R) software, Version 5.170 (AR1220 V300R003C10SPC600)" -> "AR1220"
        需避免误匹配 "software" 中的字母组合。
        """
        if not text:
            return ""
        # 优先匹配括号内的型号（华为回显中最可靠的字段）
        m = re.search(r"\(([A-Za-z]+\d{3,5}[\w\-]*)\s", text)
        if m:
            return m.group(1)
        m = _MODEL_RE.search(text)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_hostname_from_banner(text: str) -> str:
        """从回显文本中提取提示符形式的设备名，如 <R1> / [R1]"""
        if not text:
            return ""
        for line in reversed(text.splitlines()):
            m = _PROMPT_RE.match(line.strip())
            if m:
                return m.group(1).strip()
        m = re.search(r"[<\[]~?([^>\]\s~]{1,64})[>\]]", text)
        return m.group(1).strip() if m else ""

    def is_ensp_device_port(self, ip: str, port: int, timeout: float = 2.0) -> bool:
        """判断某个开放端口背后是否为可交互的 eNSP 网络设备

        判断依据：连接后发送回车，能从设备读到类似提示符的内容（<hostname> / [hostname]）。
        普通服务端口不会有这种回显，因此可有效区分。
        """
        conn = None
        try:
            conn = ConnectHandler(
                device_type='generic_telnet',
                ip=ip,
                port=port,
                timeout=timeout,
                conn_timeout=timeout,
                global_delay_factor=1,
            )
            conn.write_channel("\n")
            import time
            time.sleep(0.4)
            try:
                banner = conn.read_channel()
            except Exception:
                banner = ""
            if self._extract_hostname_from_banner(banner):
                return True
            try:
                prompt = conn.find_prompt()
                if prompt and _PROMPT_RE.match(prompt.strip()):
                    return True
            except Exception:
                pass
            return False
        except Exception as e:
            logger.debug(f"Port {port} is not an interactive device: {e}")
            return False
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass

    def _probe_identity(self, ip: str, port: int) -> Dict[str, str]:
        """在扫描阶段临时连接并读取设备主机名/型号/版本，读完即断开"""
        identity = {"hostname": "", "model": "", "version": ""}
        conn = None
        try:
            conn = ConnectHandler(
                device_type='generic_telnet',
                ip=ip,
                port=port,
                timeout=5,
                conn_timeout=5,
            )
            conn.write_channel("\n")
            import time
            time.sleep(0.4)
            try:
                banner = conn.read_channel()
            except Exception:
                banner = ""

            # 主机名：优先 sysname，其次提示符
            try:
                out = conn.send_command_timing("display current-configuration | include sysname")
                m = re.search(r"sysname\s+(\S+)", out or "")
                if m:
                    identity["hostname"] = m.group(1)
            except Exception:
                pass
            if not identity["hostname"]:
                name_from_banner = self._extract_hostname_from_banner(banner)
                if name_from_banner:
                    identity["hostname"] = name_from_banner
            if not identity["hostname"]:
                try:
                    prompt = conn.find_prompt()
                    m = _PROMPT_RE.match((prompt or "").strip())
                    if m:
                        identity["hostname"] = m.group(1).strip()
                except Exception:
                    pass

            # 型号 / 版本
            try:
                ver_out = conn.send_command_timing("display version")
                if ver_out:
                    identity["model"] = self._extract_model(ver_out)
                    vm = re.search(r"[Vv]ersion\s+([\w\.\(\)]+)", ver_out)
                    if vm:
                        identity["version"] = vm.group(1)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Probe identity on {ip}:{port} failed: {e}")
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass
        return identity

    def scan_devices(
        self,
        ip: str = "127.0.0.1",
        start_port: int = DEFAULT_SCAN_START,
        end_port: int = DEFAULT_SCAN_END,
        max_workers: int = 50,
        probe_device: bool = True,
    ) -> List[Dict[str, Any]]:
        """扫描指定 IP 的端口范围，返回发现的设备列表

        Args:
            ip: 目标 IP（eNSP 默认本机 127.0.0.1）
            start_port / end_port: 端口范围（含端点）
            max_workers: 并发线程数
            probe_device: 是否进一步探测端口背后是否为可交互网络设备

        Returns:
            [{"port": 2000, "hostname": "R1", "model": "AR1220",
              "version": "...", "is_device": True}, ...]
        """
        if start_port > end_port:
            start_port, end_port = end_port, start_port
        start_port = max(1, min(int(start_port), 65535))
        end_port = max(1, min(int(end_port), 65535))

        open_ports: List[int] = []
        ports = list(range(start_port, end_port + 1))
        worker_count = min(max_workers, len(ports)) or 1

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(self._port_is_open, ip, p, self.scan_timeout): p
                for p in ports
            }
            for future in as_completed(future_map):
                p = future_map[future]
                try:
                    if future.result():
                        open_ports.append(p)
                except Exception:
                    pass

        open_ports.sort()
        logger.info(f"Scan {ip}:{start_port}-{end_port} found {len(open_ports)} open ports: {open_ports}")

        # 阶段二：并发探测每个开放端口（连接 + 读取身份），显著缩短总耗时
        def probe_port(p: int) -> Dict[str, Any]:
            entry: Dict[str, Any] = {
                "ip": ip,
                "port": p,
                "hostname": "",
                "model": "",
                "version": "",
                "is_device": True,
            }
            if probe_device:
                entry["is_device"] = self.is_ensp_device_port(ip, p, timeout=2.0)
                if entry["is_device"]:
                    entry.update(self._probe_identity(ip, p))
            return entry

        if not open_ports:
            return []

        results: List[Dict[str, Any]] = []
        probe_workers = min(max_workers, len(open_ports)) or 1
        with ThreadPoolExecutor(max_workers=probe_workers) as executor:
            future_map = {executor.submit(probe_port, p): p for p in open_ports}
            for future in as_completed(future_map):
                p = future_map[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.debug(f"Probe port {p} failed: {e}")
                    results.append({
                        "ip": ip, "port": p, "hostname": "", "model": "",
                        "version": "", "is_device": False,
                    })

        results.sort(key=lambda r: r["port"])
        return results

    def _read_device_identity(self, name: str) -> Dict[str, str]:
        """从已连接设备读取身份信息（主机名 / 型号 / 版本）

        返回 {"hostname": ..., "model": ..., "version": ...}，读取失败的字段为空串。
        """
        identity = {"hostname": "", "model": "", "version": ""}
        conn = self.connect(name)
        if not conn:
            return identity

        # 1) 主机名：优先 sysname，其次提示符
        try:
            out = conn.send_command_timing("display current-configuration | include sysname")
            m = re.search(r"sysname\s+(\S+)", out or "")
            if m:
                identity["hostname"] = m.group(1).strip()
        except Exception as e:
            logger.debug(f"sysname read failed on {name}: {e}")

        if not identity["hostname"]:
            try:
                prompt = conn.find_prompt()
                m = _PROMPT_RE.match((prompt or "").strip())
                if m:
                    identity["hostname"] = m.group(1).strip()
            except Exception as e:
                logger.debug(f"find_prompt failed on {name}: {e}")

        if not identity["hostname"]:
            identity["hostname"] = name

        # 2) 型号与版本
        try:
            ver_out = conn.send_command_timing("display version")
            if ver_out:
                identity["model"] = self._extract_model(ver_out)
                vm = re.search(r"[Vv]ersion\s+([\w\.\(\)]+)", ver_out)
                if vm:
                    identity["version"] = vm.group(1)
        except Exception as e:
            logger.debug(f"display version failed on {name}: {e}")

        return identity

    def refresh_device_name(self, name: str) -> Optional[str]:
        """读取设备的真实主机名；若与当前键不同则重命名设备（迁移连接与元数据）

        Returns:
            新的设备名；失败返回 None。
        """
        identity = self._read_device_identity(name)
        new_name = (identity.get("hostname") or "").strip() or name

        # 无论是否改名，都把主机名/型号写回设备信息
        if name in self.devices:
            self.devices[name].update({k: v for k, v in identity.items() if v})

        if new_name == name:
            return name

        # 目标名称已被其他设备占用
        if new_name in self.devices:
            logger.warning(f"Cannot rename {name} -> {new_name}: name already in use")
            return None

        # 迁移设备信息与连接
        info = self.devices.pop(name)
        info["name"] = new_name
        self.devices[new_name] = info

        if name in self.sessions:
            self.sessions[new_name] = self.sessions.pop(name)
            self.pool.remove(name)
            self.pool.add(new_name, self.sessions[new_name])

        logger.info(f"Device renamed: {name} -> {new_name}")
        return new_name

    def scan_and_add(
        self,
        ip: str = "127.0.0.1",
        start_port: int = DEFAULT_SCAN_START,
        end_port: int = DEFAULT_SCAN_END,
        selected_ports: Optional[List[int]] = None,
        name_prefix: str = "R",
        use_hostname: bool = True,
        scanned: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """扫描端口并批量添加设备（可由 UI 传入已扫描结果与勾选端口）

        Args:
            selected_ports: 若提供，则只添加这些端口；否则添加扫描到的全部设备端口。
            name_prefix: 自动命名前缀（如 "R"），与序号拼接。
            use_hostname: 优先使用设备真实主机名命名。
            scanned: 复用已有扫描结果，避免重复扫描。

        Returns:
            实际添加的设备信息列表。
        """
        if scanned is None:
            scanned = self.scan_devices(ip, start_port, end_port)

        candidates = [d for d in scanned if d.get("is_device")]
        if selected_ports is not None:
            selected_set = set(selected_ports)
            candidates = [d for d in candidates if d["port"] in selected_set]

        added: List[Dict[str, Any]] = []
        for idx, cand in enumerate(candidates, start=1):
            port = cand["port"]
            hostname = (cand.get("hostname") or "").strip()

            candidates_names: List[str] = []
            if use_hostname and hostname:
                candidates_names.append(hostname)
            # 前缀+序号 作为回退；序号取该候选在本次列表中的位置
            candidates_names.append(f"{name_prefix or 'DEV'}{idx}")
            # 最后用「前缀+端口」兜底，保证唯一
            candidates_names.append(f"{name_prefix or 'DEV'}{port}")

            dev_name = ""
            for cand_name in candidates_names:
                if cand_name not in self.devices:
                    dev_name = cand_name
                    break
                # 同名且地址相同 -> 已存在，跳过
                existing = self.devices[cand_name]
                if existing.get("ip") == ip and existing.get("port") == port:
                    dev_name = ""
                    break
            if not dev_name:
                continue  # 已存在该设备

            self.add_device(dev_name, ip, port)
            self.devices[dev_name].update({
                "hostname": hostname or dev_name,
                "model": cand.get("model", ""),
                "version": cand.get("version", ""),
            })
            added.append({"name": dev_name, **cand})

        logger.info(f"scan_and_add added {len(added)} devices: {[a['name'] for a in added]}")
        return added
    
    def remove_device(self, name: str) -> bool: 
        """移除设备并断开连接""" 
        if name in self.sessions: 
            self.disconnect(name) 
        return self.devices.pop(name, None) is not None 

    def remove_all_devices(self) -> int:
        """移除全部设备

        先逐个断开已建立的连接（避免遗留 Telnet 会话占用 eNSP 端口），
        再清空设备表。返回被移除的设备数量。
        """
        names = list(self.devices.keys())
        removed = 0
        for name in names:
            try:
                if name in self.sessions:
                    self.disconnect(name)
            except Exception as e:
                logger.warning(f"Error disconnecting {name} while removing all: {e}")
            if self.devices.pop(name, None) is not None:
                removed += 1
        # 兜底：清理可能残留的会话记录
        self.sessions.clear()
        logger.info(f"remove_all_devices removed {removed} devices")
        return removed

    def list_devices(self) -> List[dict]:
        """返回当前已注册设备信息列表（副本，供 UI/执行器消费）"""
        return [dict(info) for info in self.devices.values()]

    def resolve_device_name(self, name: str) -> Optional[str]:
        """把任务中的设备名解析为实际已配置的设备键

        AI 解析出的设备名（如 R1）可能与用户实际配置的设备键（如 OSPF-Core1）
        不一致，或大小写有差异。这里按优先级尝试匹配：
        1) 精确匹配设备键
        2) 忽略大小写匹配设备键
        3) 匹配设备的 hostname 字段（忽略大小写）
        4) 数字序号启发式：若请求形如 R1/R2，且存在唯一设备的键/hostname 中含相同数字，则匹配
        """
        if not name:
            return None
        if name in self.devices:
            return name
        lower = name.strip().lower()
        for key in self.devices:
            if key.lower() == lower:
                return key
        for key, info in self.devices.items():
            if (info.get("hostname") or "").strip().lower() == lower:
                return key

        # 4) 数字序号启发式：R1 -> 唯一含 "1" 的设备名
        m = re.fullmatch(r"([a-zA-Z]*)(\d+)", lower)
        if m:
            digits = m.group(2)
            matches = [
                key for key in self.devices
                if re.search(rf"\b{re.escape(m.group(1))}{digits}\b", key, re.IGNORECASE)
                or (self.devices[key].get("hostname") or "").lower().endswith(digits)
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def resolve_device_names(self, names: List[str]) -> Dict[str, str]:
        """批量解析设备名，返回 {原始名: 解析后的设备键}（未解析到的值为 None）"""
        return {n: self.resolve_device_name(n) for n in names}
    
    def connect(self, name: str) -> Optional[Any]: 
        """建立设备连接（支持按设备键或主机名解析）""" 
        # 统一解析设备名，兼容 AI 解析出的名称与实际配置名不一致的情况
        resolved = self.resolve_device_name(name)
        if resolved is None:
            logger.error(f"Device {name} not found")
            return None
        if resolved != name:
            logger.info(f"Device name '{name}' resolved to '{resolved}'")
        name = resolved

        if name in self.sessions: 
            return self.sessions[name] 
        
        device_info = self.devices.get(name) 
        if not device_info: 
            logger.error(f"Device {name} not found") 
            return None 
        
        # 尝试从池中获取或新建连接 
        session = self.pool.get(name) 
        if session: 
            self.sessions[name] = session 
            return session 
        
        try: 
            # 处理会话日志路径
            session_log = self.config.get('session_log')
            if session_log:
                # 规范化路径并转为绝对路径
                session_log = os.path.abspath(os.path.normpath(session_log))
                # 如果是目录，则生成文件名
                if session_log.endswith('/') or session_log.endswith('\\') or os.path.isdir(session_log) or not os.path.splitext(session_log)[1]:
                    if not os.path.exists(session_log):
                        os.makedirs(session_log)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    session_log = os.path.join(session_log, f"{name}_{timestamp}.log")

            # 构建连接参数
            # eNSP 专用：使用 generic_telnet 彻底跳过认证
            conn_params = {
                'device_type': device_info['device_type'], 
                'ip': device_info['ip'], 
                'port': device_info['port'], 
                'timeout': self.config.get('timeout', 10), # 缩短超时，快速反馈
                'session_log': session_log,
            }
            
            logger.info(f"Attempting RAW Telnet connection to {device_info['ip']}:{device_info['port']}")
            
            connection = ConnectHandler(**conn_params) 
            
            # 关键：发送回车以唤醒 eNSP 终端提示符
            # 使用 write_channel 绕过 send_command 的提示符等待逻辑
            logger.info("Sending initial newline to wake up device...")
            connection.write_channel("\n")
            import time
            time.sleep(1) # 等待设备响应
            
            # 获取当前提示符
            prompt = connection.find_prompt()
            logger.info(f"Device woke up. Current prompt: {prompt}")
            
            self.sessions[name] = connection 
            self.pool.add(name, connection) 
            logger.info(f"Successfully connected to {name}") 
            return connection 
        except Exception as e: 
            error_msg = f"Failed to connect to {name}: {str(e)}"
            logger.error(error_msg)
            logger.debug(traceback.format_exc())
            # 记录更详细的排查建议
            if "TCP connection to device failed" in str(e):
                logger.error(f"Troubleshooting: Please check if eNSP device {name} is started and Serial Port {device_info['port']} is correct.")
            elif "Authentication failed" in str(e) or "Authentication Failed" in str(e):
                logger.error(f"Troubleshooting: Password/Username incorrect for {name}.")
            return None 
    
    def disconnect(self, name: str) -> bool: 
        """断开设备连接""" 
        if name in self.sessions: 
            try: 
                self.sessions[name].disconnect() 
            except Exception as e: 
                logger.warning(f"Error disconnecting {name}: {e}") 
            finally: 
                del self.sessions[name] 
                self.pool.remove(name) 
            return True 
        return False 
    
    # ------------------------------------------------------------------
    # 会话状态恢复与分屏控制
    # ------------------------------------------------------------------
    def _is_paging(self, conn) -> bool:
        """检测会话是否卡在 '---- More ----' 分屏暂停状态"""
        try:
            prompt = conn.find_prompt()
        except Exception:
            return False
        return bool(prompt and "More" in prompt)

    def recover_session(self, conn, name: str = "") -> None:
        """把会话恢复到用户视图（<hostname>），并解除分屏暂停/配置模式

        eNSP 的 VRP 在长输出时会停在 '---- More ----'，此时提示符不再是 <hostname>，
        后续所有 send_command 都会因提示符不匹配而超时。这里统一做恢复：
        1) 若处于分屏状态，发送空格翻到底；
        2) 若处于配置模式（[hostname] 或 [~hostname]），发送 return 退出。
        """
        # 1) 解除分屏暂停
        for _ in range(120):
            if not self._is_paging(conn):
                break
            try:
                conn.write_channel(" ")
            except Exception:
                break
            import time
            time.sleep(0.05)

        # 2) 退出配置模式
        try:
            prompt = conn.find_prompt()
        except Exception:
            prompt = ""
        for _ in range(5):
            if not prompt or not prompt.strip().startswith("["):
                break
            try:
                conn.write_channel("return\n")
            except Exception:
                break
            import time
            time.sleep(0.3)
            try:
                prompt = conn.find_prompt()
            except Exception:
                break

    def read_long_output(self, conn, command: str, name: str = "", 
                         max_pages: int = 500, idle_limit: int = 8) -> str:
        """读取长输出命令的完整回显，自动翻页（处理 '---- More ----'）

        eNSP/VRP 在输出较长时会暂停并显示 '---- More ----'，此时必须发送空格继续，
        否则只能拿到第一页。本方法通过「读缓冲 + 遇 More 发空格 + 连续空闲判定结束」
        的方式拿到完整输出。
        """
        import time
        self.recover_session(conn, name)
        try:
            conn.write_channel(command + "\n")
        except Exception as e:
            logger.error(f"write_channel '{command}' on {name} failed: {e}")
            return ""

        buf = ""
        idle = 0
        empty_total = 0
        for _ in range(max_pages):
            try:
                chunk = conn.read_channel()
            except Exception:
                chunk = ""
            if chunk:
                idle = 0
                buf += chunk
                # 只要还停留在分屏暂停页，就继续翻页
                if "---- More ----" in chunk:
                    try:
                        conn.write_channel(" ")
                    except Exception:
                        break
                    time.sleep(0.15)
                continue
            idle += 1
            empty_total += 1
            # 连续多次读不到数据，且已回到用户视图提示符，则认为输出结束
            tail = buf[-60:]
            if idle >= idle_limit and "<" in tail and "More" not in tail:
                break
            # 全程一个字符都没读到，且已空转较久：不必再等下去（避免空连接阻塞）
            if not buf and empty_total >= idle_limit:
                logger.warning(f"No output received for '{command}' on {name}")
                break
            time.sleep(0.08)

        # 兜底：若仍卡在分屏，尝试退出
        if "---- More ----" in buf[-40:]:
            try:
                conn.write_channel("q")
            except Exception:
                pass
        self.recover_session(conn, name)
        return buf

    def execute_command(self, name: str, command: str) -> Optional[str]: 
        """在设备上执行查询命令并返回输出

        针对 eNSP/VRP 做了健壮性处理：
        - 执行前恢复会话（解除分屏 / 退出配置模式）
        - 长输出命令走自动翻页读取，避免只拿到第一页
        - 短命令走 timing 模式（不依赖提示符正则，适配主机名与配置名不一致的情况）
        """
        conn = self.connect(name) 
        if not conn: 
            return None

        long_output = any(k in command for k in (
            "current-configuration", "display version", "display interface",
            "display ip routing-table", "display arp", "display mac-address",
            "display lldp",
        ))

        if long_output:
            raw = self.read_long_output(conn, command, name)
            return self._clean_output(raw, command)

        # 恢复会话状态，避免因残留分屏/配置模式导致命令超时
        self.recover_session(conn, name)
        try: 
            output = conn.send_command_timing(
                command, read_timeout=20, last_read=1.0,
            )
            self.recover_session(conn, name)
            return self._clean_output(output, command)
        except Exception as e: 
            logger.error(f"Command '{command}' on {name} failed: {e}") 
            try:
                self.recover_session(conn, name)
            except Exception:
                pass
            return None

    @staticmethod
    def _clean_output(output: Optional[str], command: str) -> Optional[str]:
        """清理回显：去掉分屏提示、ANSI 控制序列、命令回显与尾部提示符"""
        if output is None:
            return None
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="ignore")

        # 去掉 '---- More ----' 及其后的退格对齐控制序列
        output = re.sub(r"\s*-+\s*More\s*-+.*?(?:\x1b\[\d+D\s*)+", "\n", output)
        output = re.sub(r"-+\s*More\s*-+", "", output)
        # 去掉 ANSI 转义序列（如 \x1b[42D）
        output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)
        # 去掉回显的 screen-length 等辅助命令行
        output = re.sub(r"^\s*screen-length[^\n]*\n?", "", output)

        # 去掉命令回显：eNSP 回显常形如 "<R1>display xxx" 或 "[R1]vlan 10"，
        # 提示符与命令可能出现在行首或跟在其它提示符之后（中间无换行）。
        cmd_pat = re.escape(command.strip())
        prompt_pat = r"[<\[]~?[\w\-\.]+[>\]]"
        # 1) 行首带提示符的命令回显
        output = re.sub(rf"(?m)^\s*{prompt_pat}\s*{cmd_pat}\s*$", "", output)
        # 2) 提示符后紧跟我方命令（去除整段 "提示符+命令"）
        output = re.sub(rf"{prompt_pat}\s*{cmd_pat}", "", output)
        # 3) 残留的裸命令回显（首行无提示符时）
        output = re.sub(rf"(?m)^\s*{cmd_pat}\s*$", "", output)
        # 4) 清理行首孤立提示符残留（如 "<R1>" 单独成串）
        output = re.sub(rf"{prompt_pat}", "", output)

        lines = output.splitlines()
        cleaned = []
        for line in lines:
            stripped = line.rstrip()
            # 跳过清理后变为空的行（原命令回显行）
            if not stripped.strip():
                continue
            cleaned.append(stripped)

        result = "\n".join(cleaned)
        return result.strip("\r\n")
    
    def execute_config_commands(self, name: str, commands: List[str]) -> bool: 
        """在设备上执行配置命令列表

        说明：eNSP 的 Huawei VRP 通过 generic_telnet 连接时，send_config_set 常常
        无法正确识别提示符（尤其是主机名与配置名不一致、残留分屏、或退出 system-view
        时），因此改为「先恢复会话状态 + 逐条 timing 下发」，最大程度保证配置落地。
        """
        conn = self.connect(name) 
        if not conn: 
            return False

        # 执行前先恢复会话（解除分屏 / 退出配置模式），避免首条命令就超时
        self.recover_session(conn, name)

        # 过滤空行与注释行
        cmds = [c.strip() for c in commands if c and c.strip() and not c.strip().startswith(("#", "//", "!"))]
        if not cmds:
            return True

        ok = True
        for cmd in cmds:
            try:
                conn.send_command_timing(
                    cmd, strip_prompt=False, strip_command=False, read_timeout=15,
                )
            except Exception as e:
                logger.warning(f"send_command_timing '{cmd}' on {name} failed ({e}); retrying via send_config_set")
                # 单条兜底：用 send_config_set 再试一次
                try:
                    conn.send_config_set([cmd], exit_config_mode=False)
                except Exception as e2:
                    logger.error(f"Config command '{cmd}' on {name} failed: {e2}")
                    ok = False

        # 执行后退出配置模式并恢复分屏设置
        self._ensure_privileged_mode(conn, name)
        logger.info(f"Config on {name} executed ({len(cmds)} commands), success={ok}")
        return ok

    def _ensure_privileged_mode(self, conn, name: str) -> None:
        """确保设备回到用户视图（<hostname>），避免卡在配置模式或分屏"""
        try:
            self.recover_session(conn, name)
        except Exception as e:
            logger.debug(f"Failed to normalize prompt on {name}: {e}")
    
    def get_all_connected(self) -> List[str]: 
        """返回当前已连接的设备列表""" 
        return list(self.sessions.keys())

    def execute_commands(
        self,
        name: str,
        commands: List[str],
        is_config: bool = False
    ) -> Dict[str, Optional[str]]:
        """
        在单个设备上执行多条命令。
        is_config=True 时按配置命令处理（send_config_set），否则 send_command。
        返回 {command: output or None}
        """
        results: Dict[str, Optional[str]] = {}
        conn = self.connect(name)
        if not conn:
            return {cmd: None for cmd in commands}

        for cmd in commands:
            if is_config:
                # 配置命令：直接复用 execute_config_commands 的健壮路径
                ok = self.execute_config_commands(name, [cmd])
                results[cmd] = "配置已下发" if ok else None
            else:
                results[cmd] = self.execute_command(name, cmd)

        return results

    def execute_batch(
        self,
        device_names: List[str],
        commands: List[str],
        is_config: bool = False,
        max_workers: int = 5,
    ) -> Dict[str, Dict[str, Optional[str]]]:
        """
        在多台设备上并发执行多条命令。
        返回结构:
        {
            "R1": {"display ip interface brief": "...", ...},
            "R2": {...},
        }
        任一条失败则对应 value 为 None。
        """
        results: Dict[str, Dict[str, Optional[str]]] = {}

        def worker(dev_name: str) -> Dict[str, Optional[str]]:
            conn = self.connect(dev_name)
            if not conn:
                return {cmd: None for cmd in commands}

            dev_res: Dict[str, Optional[str]] = {}
            for cmd in commands:
                if is_config:
                    ok = self.execute_config_commands(dev_name, [cmd])
                    dev_res[cmd] = "配置已下发" if ok else None
                else:
                    dev_res[cmd] = self.execute_command(dev_name, cmd)
            return dev_res

        # 限制最大并发，避免对 eNSP/本机造成过大压力
        worker_count = min(max_workers, len(device_names)) or 1
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_dev = {
                executor.submit(worker, dev): dev
                for dev in device_names
            }
            for future in as_completed(future_to_dev):
                dev_name = future_to_dev[future]
                try:
                    dev_res = future.result()
                except Exception as e:
                    logger.error(f"Batch execution error on {dev_name}: {e}")
                    dev_res = {cmd: None for cmd in commands}
                results[dev_name] = dev_res

        return results

    def ensure_lldp_enabled(self, name: str) -> bool:
        """
        确保指定设备已启用 LLDP。
        如果检测到未启用或无法确认状态，则尝试下发全局 lldp enable。
        返回 True 表示最终认为已启用（或命令下发成功），False 表示失败。
        """
        # 先确认设备可达
        conn = self.connect(name)
        if not conn:
            logger.error(f"Cannot connect to {name} for LLDP check")
            return False

        # 尝试通过查询命令判断 LLDP 是否可用
        check_cmd = "display lldp local"
        output = self.execute_command(name, check_cmd)
        if output:
            text = output.lower()
            # 明确报错：命令不存在 -> 无法启用
            if "unrecognized command" in text or "wrong parameter" in text:
                logger.warning(f"Device {name} does not support lldp command ('{check_cmd}')")
                return False
            # 已启用：输出中包含 LLDP 相关字段，且无未启用提示
            if "lldp" in text and "not enabled" not in text and "disabled" not in text:
                logger.info(f"LLDP already enabled on {name}")
                return True

        # 未能确定或明确未启用，尝试通过配置命令启用 LLDP（全局）
        logger.info(f"Enabling LLDP globally on {name} ...")
        ok = self.execute_config_commands(
            name,
            [
                "system-view",
                "lldp enable",
            ],
        )
        if not ok:
            logger.error(f"Failed to enable LLDP on {name}")
            return False

        # 再次做一次轻量校验（不强依赖结果）
        verify_output = self.execute_command(name, check_cmd)
        if verify_output:
            logger.info(f"LLDP enable verification output on {name}: {verify_output[:200]}...")
        return True
