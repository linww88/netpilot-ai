"""LLM客户端封装，支持 DeepSeek / OpenAI 兼容接口 和 本地模型""" 
import os 
from typing import Any, Dict 
from langchain_openai import ChatOpenAI 
from langchain_community.llms import Ollama 

# 各 provider 的默认参数
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"

# provider 别名 -> (默认 base_url, 默认 api_key_env)
PROVIDER_DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openai_compatible": (None, "OPENAI_API_KEY"),
}


class LLMClient: 
    """统一的LLM调用接口""" 
    
    def __init__(self, config: Dict[str, Any]): 
        self.config = config 
        provider = config.get('provider', 'deepseek') 
        temperature = config.get('temperature', 0.2) 
        max_tokens = config.get('max_tokens', 1000) 

        # 根据 provider 决定默认值
        default_base_url, default_key_env = PROVIDER_DEFAULTS.get(
            provider, (DEFAULT_BASE_URL, DEFAULT_API_KEY_ENV)
        )
        model = config.get('model') or DEFAULT_MODEL
        base_url = config.get('base_url') or default_base_url
        api_key_env = config.get('api_key_env') or default_key_env
        # api_key 兼容两种写法：直接给值 或 给环境变量名
        api_key = config.get('api_key') or os.getenv(api_key_env, "")
        
        if provider in ('deepseek', 'openai', 'openai_compatible'): 
            # deepseek / openai_compatible 均走 OpenAI 兼容协议
            self.llm = ChatOpenAI( 
                model=model, 
                temperature=temperature, 
                max_tokens=max_tokens, 
                api_key=api_key,
                base_url=base_url,
            ) 
        elif provider == 'local': 
            # 本地大模型（例如 Ollama）
            self.llm = Ollama(model=model, temperature=temperature) 
        else: 
            raise ValueError(f"Unsupported LLM provider: {provider}") 
    
    def invoke(self, prompt: str, **kwargs) -> str: 
        """调用LLM并返回文本结果""" 
        response = self.llm.invoke(prompt, **kwargs) 
        
        if hasattr(response, 'content'):
            return str(response.content)
        return str(response)
