"""故障诊断模块""" 
import json 
import logging 
from typing import Dict, Any 
from .llm_client import LLMClient, LLMUnavailableError 
from ..utils.prompt_loader import load_prompt 

logger = logging.getLogger(__name__) 

class FaultDiagnosis: 
    """分析故障并提供修复建议""" 
    
    def __init__(self, llm_client: LLMClient): 
        self.llm = llm_client 

    def is_available(self) -> bool:
        """AI 诊断能力是否可用（未配置 .env 时为 False）"""
        return bool(self.llm and self.llm.is_available())
        
    def diagnose(self, fault_description: str, device_outputs: Dict[str, str]) -> Dict[str, Any]: 
        """诊断故障并返回结构化建议

        当 LLM 未配置时抛出 LLMUnavailableError（由 UI 提示配置 .env），
        其余异常（如网络/接口错误）仍返回结构化错误信息，保证界面不崩。
        """ 
        prompt_template = load_prompt("fault_diagnosis.txt") 
        prompt = prompt_template.format( 
            fault_description=fault_description, 
            device_outputs=json.dumps(device_outputs, indent=2) 
        ) 

        try: 
            response = self.llm.invoke(prompt) 
            result = self._extract_json(response) 
            logger.info(f"Diagnosis result: {result}") 
            return result 
        except LLMUnavailableError: 
            # 未配置 Key：向上抛出，交由 UI 给出配置引导
            raise 
        except Exception as e: 
            logger.error(f"Fault diagnosis failed: {e}") 
            return {"analysis": "诊断过程发生错误，请检查日志", "suggestions": [], "risk_level": "Unknown"} 
    
    def _extract_json(self, text: str) -> Dict: 
        """从 LLM 回复中提取 JSON 诊断结果"""
        start = text.find('{') 
        end = text.rfind('}') + 1 
        if start != -1 and end > start: 
            json_str = text[start:end] 
            try: 
                return json.loads(json_str) 
            except json.JSONDecodeError: 
                logger.warning(f"Failed to parse JSON from LLM response: {json_str}")
        
        # 兜底处理：如果没找到 JSON 或解析失败，将全文作为分析内容返回
        return {"analysis": text, "suggestions": [], "risk_level": "Unknown"}
