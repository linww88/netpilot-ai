"""设备信息采集器""" 
import logging
import time
from typing import List, Dict, Any, Optional 
from ..connection.device_manager import DeviceManager 

logger = logging.getLogger(__name__) 

# LLDP 邻居表查询命令
_LLDP_CMD = "display lldp neighbor brief"

# 表头关键字（用于统计有效邻居行时排除）
_HEADER_KEYWORDS = (
    "system name", "local interface", "neighbor", "localintf",
    "neighbordev", "neighborintf", "systemname", "port id", "portid",
)


class InfoCollector: 
    """从设备采集原始信息""" 
    
    def __init__(self, device_manager: DeviceManager, topology_config: Optional[Dict[str, Any]] = None): 
        self.dm = device_manager 
        self.topology_config = topology_config or {}

    @staticmethod
    def _count_lldp_rows(text: str) -> int:
        """统计 LLDP 邻居有效行数（排除空行、表头与分隔线）"""
        if not text:
            return 0
        rows = 0
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if any(k in low for k in _HEADER_KEYWORDS):
                continue
            if set(s) <= set("-=+| "):
                continue
            rows += 1
        return rows

    def wait_lldp_convergence(self, device_names: List[str],
                              max_rounds: int = 3,
                              interval: float = 3.0) -> Dict[str, int]:
        """等待 LLDP 邻居信息收敛

        eNSP 中设备刚启动、或刚启用 LLDP 时，邻居表需要若干秒才会填充完整。
        若首次点击「拓扑发现」立即采集，往往只能拿到不完整（甚至为空）的邻居表，
        用户需要反复点击才能得到完整拓扑 —— 这正是"要点几次才刷新"的成因之一。

        本方法在正式采集前轮询 LLDP 邻居表，直到：
        - 各设备邻居行数连续两轮保持不变（视为已收敛），或
        - 达到最大轮次上限。

        返回：{设备名: 最终邻居行数}
        """
        if not device_names:
            return {}

        logger.info(
            f"Waiting for LLDP convergence on {len(device_names)} device(s) "
            f"(max {max_rounds} rounds, interval {interval}s)"
        )

        prev_counts: Optional[Dict[str, int]] = None
        counts: Dict[str, int] = {}

        for round_idx in range(max_rounds):
            counts = {}
            for name in device_names:
                try:
                    out = self.dm.execute_command(name, _LLDP_CMD)
                except Exception as e:
                    logger.warning(f"LLDP probe on {name} failed: {e}")
                    out = None
                counts[name] = self._count_lldp_rows(out or "")

            # 收敛判定：与上一轮完全一致（含全 0，表示确实没有邻居）
            if prev_counts is not None and counts == prev_counts:
                logger.info(f"LLDP converged after {round_idx + 1} round(s): {counts}")
                break

            prev_counts = dict(counts)
            if round_idx < max_rounds - 1 and interval > 0:
                time.sleep(interval)

        return counts

    def collect_all(self, device_names: List[str]) -> Dict[str, Dict[str, str]]: 
        """采集所有指定设备的信息""" 
        commands = self._get_collection_commands()
        # 如果 DeviceManager 支持批量接口，优先使用并发采集
        if hasattr(self.dm, "execute_batch"): 
            batch_results = self.dm.execute_batch(
                device_names=device_names,
                commands=commands,
                is_config=False,
            )
            return batch_results

        # 回退到逐设备串行采集
        results: Dict[str, Dict[str, str]] = {} 
        for name in device_names: 
            device_data: Dict[str, str] = {} 
            for cmd in commands: 
                output = self.dm.execute_command(name, cmd) 
                if output is not None: 
                    device_data[cmd] = output 
                else: 
                    logger.warning(f"Failed to execute {cmd} on {name}") 
            results[name] = device_data 
        return results 
    
    def _get_collection_commands(self) -> List[str]: 
        """获取要采集的命令列表（可从配置文件读取）""" 
        # 优先从拓扑配置中读取拓扑发现命令
        commands = self.topology_config.get('commands')
        if commands:
            return commands
        
        # 默认命令列表
        return [ 
            "display interface brief", 
            "display ip interface brief", 
            "display arp", 
            "display mac-address", 
            "display lldp neighbor brief", 
            "display current-configuration | include sysname" 
        ]
