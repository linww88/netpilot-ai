"""自然语言意图解析模块""" 
import json 
import logging 
import re
from typing import List, Dict, Any 
from .llm_client import LLMClient 
from ..utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__) 

class IntentParser: 
    """将用户自然语言指令解析为结构化任务列表""" 
    
    def __init__(self, llm_client: LLMClient, topology_context: str = ""): 
        self.llm = llm_client 
        self.topology_context = topology_context 
        
    def parse(self, user_input: str) -> List[Dict[str, Any]]: 
        """解析指令，返回任务列表""" 
        try:
            prompt_template = load_prompt("intent_parser.txt") 
            prompt = prompt_template.format( 
                topology_context=self.topology_context or "（暂无拓扑信息，请仅根据用户指令中的设备名解析）", 
                user_input=user_input 
            ) 
            
            response = self.llm.invoke(prompt) 
            logger.info(f"Raw LLM response: {response!r}")
            # 提取 JSON 
            tasks = self._extract_json(response) 
            if not tasks:
                logger.warning(f"Intent parsing produced no tasks. Raw response: {response!r}")
            logger.info(f"Parsed tasks: {tasks}") 
            return tasks 
        except Exception as e: 
            logger.error(f"Intent parsing failed: {e}") 
            return [] 
    
    def _extract_json(self, text: str) -> List[Dict]: 
        """从 LLM 回复中提取 JSON 数组

        兼容多种常见返回格式：
        1. 纯 JSON 数组：[{...}]
        2. Markdown 代码块包裹：```json\n[...]\n```
        3. 前后夹杂说明文字：[...]
        4. 模型返回单个 JSON 对象而非数组：{...}
        """
        if not text:
            return []

        text = text.strip()

        # 1) 去掉 Markdown 代码块围栏
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        candidates = [block.strip() for block in fenced] + [text]

        for candidate in candidates:
            parsed = self._try_load(candidate)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
            if isinstance(parsed, dict):
                # 模型返回单个任务对象时包装为列表
                if any(k in parsed for k in ("device", "action", "parameters")):
                    return [parsed]
                # 兼容 {"tasks": [...]} 形式
                for key in ("tasks", "result", "data"):
                    inner = parsed.get(key)
                    if isinstance(inner, list):
                        return [item for item in inner if isinstance(item, dict)]

        # 2) 兜底：截取第一个 [ 到最后一个 ] 之间内容
        start = text.find('[') 
        end = text.rfind(']') + 1 
        if start != -1 and end > start: 
            parsed = self._try_load(text[start:end])
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]

        logger.warning(f"Failed to extract task JSON from response: {text[:300]!r}")
        return []

    @staticmethod
    def _try_load(fragment: str):
        """尝试解析 JSON 片段，失败返回 None"""
        try:
            return json.loads(fragment)
        except (json.JSONDecodeError, ValueError):
            return None
