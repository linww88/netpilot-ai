"""配置执行器""" 
import logging 
from typing import List, Dict, Any 
from ..connection.device_manager import DeviceManager 
from ..ai.command_gen import CommandGenerator 

logger = logging.getLogger(__name__) 

class ConfigExecutor: 
    """执行配置任务""" 
    
    def __init__(self, device_manager: DeviceManager, command_gen: CommandGenerator): 
        self.dm = device_manager 
        self.cmd_gen = command_gen 
        
    def execute_tasks(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]: 
        """执行任务列表，返回每个设备的结果""" 
        results = {} 
        # 按设备分组任务（先把任务中的设备名解析为已注册设备的 key）
        device_tasks = {} 
        unresolved_tasks = []
        for task in tasks: 
            # 兼容字段名 device 或 target_device
            device = task.get("device") or task.get("target_device")
            if not device:
                logger.warning(f"Task skipped: No target device specified in {task}")
                unresolved_tasks.append({
                    "task": task, "commands": [], "success": False,
                    "error": "任务未指定目标设备"
                })
                continue
            # 名称解析：任务里可能写的是 hostname（如 OSPF-Core1）而设备 key 是 R1，
            # 或反之。统一解析为设备管理器中真实存在的 key。
            resolved = self.dm.resolve_device_name(device) if hasattr(self.dm, "resolve_device_name") else None
            if resolved is None:
                # 尝试从已注册设备里做一次宽松匹配（忽略大小写/空白）
                resolved = self._match_known_device(device)
            if resolved is None:
                logger.error(f"Target device not found: {device}")
                unresolved_tasks.append({
                    "task": task, "commands": [], "success": False,
                    "error": f"未找到目标设备：{device}"
                })
                continue
            if resolved != device:
                logger.info(f"Device name resolved: {device} -> {resolved}")
            task["_resolved_device"] = resolved
            if resolved not in device_tasks: 
                device_tasks[resolved] = [] 
            device_tasks[resolved].append(task) 
        
        # 为每个设备生成并执行命令 
        for device, task_list in device_tasks.items(): 
            device_results = [] 
            all_success = True 
            
            for task in task_list: 
                commands = self.cmd_gen.generate(task) 
                if commands: 
                    logger.info(f"Executing on {device}: {commands}") 
                    ok = self.dm.execute_config_commands(device, commands) 
                    device_results.append({ 
                        "task": task, 
                        "commands": commands, 
                        "success": ok 
                    }) 
                    if not ok: 
                        all_success = False 
                        logger.error(f"Execution failed on {device} for task: {task}")
                        break  # 失败后停止该设备后续配置
                else: 
                    device_results.append({ 
                        "task": task, 
                        "commands": [], 
                        "success": False, 
                        "error": "No commands generated" 
                    }) 
                    all_success = False 
                    logger.error(f"Command generation failed for task on {device}: {task}")
            
            results[device] = { 
                "success": all_success, 
                "tasks": device_results 
            } 
        
        # 记录无法解析设备名/未指定设备的任务，避免静默丢弃
        if unresolved_tasks:
            results["__unresolved__"] = {
                "success": False,
                "tasks": unresolved_tasks,
                "error": "以下任务未能匹配到已注册设备，请检查指令中的设备名"
            }
        return results

    def _match_known_device(self, name: str):
        """在已注册设备中做宽松匹配，返回匹配到的设备 key 或 None"""
        try:
            devices = self.dm.list_devices()
        except Exception:
            return None
        target = str(name).strip().lower()
        if not target:
            return None
        # 1) 忽略大小写的完整匹配（key / hostname / name）
        for d in devices:
            for field in ("name", "hostname", "key"):
                val = str(d.get(field) or "").strip().lower()
                if val and val == target:
                    return d.get("name") or d.get("key")
        # 2) 包含匹配（指令里写了部分名称，如 "Core1"）
        for d in devices:
            for field in ("name", "hostname"):
                val = str(d.get(field) or "").strip().lower()
                if val and (target in val or val in target):
                    return d.get("name") or d.get("key")
        return None

    def execute_raw_commands(self, device: str, commands: List[str]) -> bool:
        """直接下发原始命令列表"""
        return self.dm.execute_config_commands(device, commands)
