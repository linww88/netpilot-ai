"""拓扑构建器""" 
import networkx as nx 
import re 
import json
import logging
import os
from typing import Dict, Any, List, Tuple 

logger = logging.getLogger(__name__)

# LLDP 邻居信息表头关键字（用于过滤表头行）
_LLDP_HEADER_KEYWORDS = (
    "system name", "local interface", "neighbor", "localintf",
    "neighbordev", "neighborintf", "systemname", "port id", "portid",
    "chassis", "ttl", "expired",
)


class TopologyBuilder: 
    """根据采集的信息构建网络拓扑图""" 
    
    def __init__(self, data_dir="./data"): 
        self.data_dir = data_dir
        self.graph = nx.Graph()  # 节点可以是设备或子网 
        self.devices = {}  # name -> device info 
        self.nodes = {}    # 兼容旧版本 JSON 序列化
        self.edges = []    # 兼容旧版本 JSON 序列化
        # 主机名(hostname) -> 设备键(device key) 的映射，用于 LLDP 邻居归一化
        self.hostname_to_key: Dict[str, str] = {}
        
    def clear(self):
        """清空当前拓扑状态（内存中的图与序列化数据）

        用于「重新发现」场景：避免上一轮（或上一次实验）的图残留，
        导致界面继续展示陈旧拓扑。
        """
        self.graph = nx.Graph()
        self.devices = {}
        self.nodes = {}
        self.edges = []
        self.hostname_to_key = {}

    def build(self, collected_data: Dict[str, Dict[str, str]]) -> nx.Graph: 
        """从采集数据构建拓扑

        注意：本方法会**完全重置**内部状态（图、设备、主机名索引、序列化数据），
        即构建结果只反映本次传入的 collected_data，不会残留历史数据。
        """ 
        # 每次构建前重置图，避免重复累加导致的错乱（这是旧版最严重的问题之一）
        self.clear()

        self._parse_devices(collected_data) 
        self._infer_links(collected_data)
        # 更新 nodes 和 edges 列表用于持久化
        self._update_serialization_data()
        return self.graph 
    
    def _parse_devices(self, data: Dict[str, Dict[str, str]]): 
        """解析设备基本信息和接口""" 
        for dev_name, cmd_outputs in data.items(): 
            # 提取主机名（从 display current-configuration 中解析 sysname） 
            sysname_output = cmd_outputs.get("display current-configuration | include sysname", "") or ""
            match = re.search(r"sysname\s+(\S+)", sysname_output) 
            hostname = match.group(1) if match else dev_name 
            
            self.devices[dev_name] = { 
                "hostname": hostname, 
                "interfaces": []  # 稍后填充 
            } 
            # 建立 hostname -> device key 的反向索引（大小写不敏感）
            self.hostname_to_key[hostname.lower()] = dev_name
            self.hostname_to_key[dev_name.lower()] = dev_name

            # 添加设备节点 
            self.graph.add_node(dev_name, type="device", hostname=hostname) 
    
    def _resolve_device_key(self, neighbor_name: str) -> str:
        """将 LLDP 报出的邻居名称解析为拓扑中的设备键

        LLDP 邻居通常报告的是对端设备的 sysname（主机名），而拓扑节点用的是
        用户在界面上配置的设备键（如 R1）。两者可能不一致，需要归一化，
        否则会凭空创建出不存在的节点、并把链路连错（旧版的核心缺陷）。
        """
        if not neighbor_name:
            return neighbor_name
        key = neighbor_name.strip()
        # 去掉可能的域名后缀，如 R1.example.com
        key_no_domain = key.split(".")[0]
        for candidate in (key, key_no_domain):
            mapped = self.hostname_to_key.get(candidate.lower())
            if mapped:
                return mapped
        # 找不到映射则原样返回（保留为外部/未知设备节点）
        return key
    
    def _infer_links(self, data: Dict[str, Dict[str, str]]): 
        """推断设备间链路

        优先使用 LLDP 邻居信息（display lldp neighbor brief），
        若某链路两端都已知，则写入无向边并带上两端端口号。
        """
        for dev_name, cmd_outputs in data.items():
            lldp_output = cmd_outputs.get("display lldp neighbor brief", "") or ""
            if not lldp_output.strip():
                continue

            for line in lldp_output.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                low = stripped.lower()
                # 跳过表头、分隔线与提示
                if any(kw in low for kw in _LLDP_HEADER_KEYWORDS):
                    continue
                if set(stripped) <= set("-=+| "):
                    continue

                # 典型行格式（华为 VRP display lldp neighbor brief）：
                # LocalIntf   NeighborDev        NeighborIntf
                # GE0/0/1     R2                 GE0/0/2
                parts = re.split(r"\s+", stripped)
                if len(parts) < 2:
                    continue

                local_int = parts[0]
                neighbor_raw = parts[1]
                neighbor_int = parts[2] if len(parts) > 2 else ""

                # 接口名特征粗校验：形如 GE0/0/1 / GigabitEthernet0/0/1 / Eth0/0/1
                if not re.match(r"^[A-Za-z]", local_int) or "/" not in local_int:
                    continue

                neighbor_key = self._resolve_device_key(neighbor_raw)
                if neighbor_key == dev_name:
                    continue  # 忽略自环

                # 确保邻居节点存在（未采集到的对端也补一个节点）
                if neighbor_key not in self.graph:
                    self.graph.add_node(neighbor_key, type="device",
                                        hostname=neighbor_raw)

                # 同一条链路会被两端各描述一次（端口方向相反）。
                # 为避免「后写入者覆盖」导致端口信息不稳定，这里做确定性处理：
                # 只有当端点尚不存在该边时才写入；已存在则保留首次记录，
                # 从而保证同一份输入无论顺序如何，结果都一致。
                if self.graph.has_edge(dev_name, neighbor_key):
                    continue

                self.graph.add_edge(
                    dev_name, neighbor_key,
                    local_port=local_int,
                    remote_port=neighbor_int,
                )

    def _update_serialization_data(self):
        """将 Graph 转换为 nodes/edges 列表以便保存"""
        self.nodes = {node: data for node, data in self.graph.nodes(data=True)}
        self.edges = []
        for u, v, data in self.graph.edges(data=True):
            edge = {"source": u, "target": v}
            edge.update(data)
            self.edges.append(edge)

    def save_topology(self, filename="topology.json"):
        """保存拓扑到文件"""
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        path = os.path.join(self.data_dir, filename)
        # 确保父目录存在（data_dir 可能是相对路径）
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"nodes": self.nodes, "edges": self.edges}, f, indent=2, ensure_ascii=False)
        logger.info(f"Topology saved to {path}")

    def load_topology(self, filename="topology.json"):
        """从文件加载拓扑"""
        path = os.path.join(self.data_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.nodes = data.get("nodes", {})
                self.edges = data.get("edges", [])
                # 重新构建 graph
                self.graph = nx.Graph()
                for node_id, node_data in self.nodes.items():
                    self.graph.add_node(node_id, **node_data)
                for edge in self.edges:
                    edge = dict(edge)  # 复制，避免 pop 破坏 self.edges 数据
                    u = edge.pop("source")
                    v = edge.pop("target")
                    self.graph.add_edge(u, v, **edge)
            logger.info(f"Topology loaded from {path}")
        except FileNotFoundError:
            logger.error(f"Topology file {path} not found")
        except Exception as e:
            logger.error(f"Error loading topology: {e}")
