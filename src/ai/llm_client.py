"""LLM客户端封装，支持 DeepSeek / OpenAI 兼容接口 和 本地模型

设计要点（无 .env 也能运行）：
- 未配置 API Key 时，本类进入 **降级模式**（available=False），不会抛异常导致应用崩溃；
- 此时项目其余功能（设备扫描/连接、命令模板下发、拓扑发现、配置读取等）均可正常使用；
- 只有在真正需要 AI（自然语言解析、故障诊断、LLM 生成命令）时，才抛出
  `LLMUnavailableError`，由上层捕获并提示用户配置 .env。
"""
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

# 需要 API Key 的 provider（local 类型不需要）
_KEY_REQUIRED_PROVIDERS = ("deepseek", "openai", "openai_compatible")

# 统一的配置引导文案（供各层复用）
ENV_HINT = (
    "未检测到有效的大模型 API Key，AI 功能暂不可用。\n\n"
    "请按以下步骤配置后重试：\n"
    "1) 复制项目根目录的 `.env.example` 为 `.env`；\n"
    "2) 在 `.env` 中填写：`DEEPSEEK_API_KEY=sk-你的密钥`；\n"
    "3) 保存后重启应用（Streamlit 需重新运行）。\n\n"
    "提示：未配置时，设备扫描/连接、命令模板下发、拓扑发现、配置读取等非 AI 功能仍可正常使用。"
)


class LLMUnavailableError(RuntimeError):
    """LLM 未配置或不可用时抛出（携带面向用户的中文提示）"""

    def __init__(self, message: str = ENV_HINT):
        super().__init__(message)


class LLMClient: 
    """统一的LLM调用接口（支持未配置 Key 的降级运行）""" 
    
    def __init__(self, config: Dict[str, Any]): 
        self.config = config or {}
        provider = self.config.get('provider', 'deepseek') 
        temperature = self.config.get('temperature', 0.2) 
        max_tokens = self.config.get('max_tokens', 1000) 

        # 根据 provider 决定默认值
        default_base_url, default_key_env = PROVIDER_DEFAULTS.get(
            provider, (DEFAULT_BASE_URL, DEFAULT_API_KEY_ENV)
        )
        model = self.config.get('model') or DEFAULT_MODEL
        base_url = self.config.get('base_url') or default_base_url
        api_key_env = self.config.get('api_key_env') or default_key_env
        # api_key 兼容两种写法：直接给值 或 给环境变量名
        api_key = self.config.get('api_key') or os.getenv(api_key_env, "")

        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env

        needs_key = provider in _KEY_REQUIRED_PROVIDERS
        has_key = bool((api_key or "").strip())
        # 是否可用：本地模型始终可用；云端模型需有 Key
        self.available = (not needs_key) or has_key
        self.llm = None

        if not self.available:
            # 降级模式：不构造底层客户端，避免 Missing credentials 异常导致应用启动失败
            self.unavailable_reason = (
                f"未配置 API Key（provider={provider}，环境变量 {api_key_env} 为空）"
            )
            return

        try:
            if provider in _KEY_REQUIRED_PROVIDERS: 
                # deepseek / openai / openai_compatible 均走 OpenAI 兼容协议
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
                # 未知 provider：进入降级模式而非直接崩溃
                self.available = False
                self.unavailable_reason = f"不支持的 LLM provider：{provider}"
        except Exception as e: 
            # 任何初始化异常（如 base_url 非法）都不应使整个应用崩溃
            self.available = False
            self.unavailable_reason = f"LLM 客户端初始化失败：{e}"

    @property
    def unavailable_reason(self) -> str:
        """降级原因（可用时为空字符串）"""
        return getattr(self, "_unavailable_reason", "")

    @unavailable_reason.setter
    def unavailable_reason(self, value: str) -> None:
        self._unavailable_reason = value or ""

    def is_available(self) -> bool:
        """AI 功能是否可用（供 UI 判断并给出提示）"""
        return bool(self.available and self.llm is not None)

    def invoke(self, prompt: str, **kwargs) -> str: 
        """调用LLM并返回文本结果

        未配置 Key（降级模式）时抛出 LLMUnavailableError，
        由上层捕获并提示用户配置 .env（不返回空值以免被误认为模型无输出）。
        """
        if not self.is_available():
            reason = self.unavailable_reason or "LLM 未配置"
            raise LLMUnavailableError(f"{reason}\n\n{ENV_HINT}")

        response = self.llm.invoke(prompt, **kwargs) 
        
        if hasattr(response, 'content'):
            return str(response.content)
        return str(response)

    def describe_status(self) -> str:
        """返回当前 LLM 配置状态的可读描述（用于界面展示）"""
        if self.is_available():
            return f"✅ AI 已就绪（provider={self.provider}，model={self.model}）"
        return f"⚠️ AI 不可用：{self.unavailable_reason}"
