"""离线 Mock 验证：不调用真实接口，验证 DeepSeek 配置链路是否完整跑通
覆盖：配置读取 -> LLMClient 参数装配 -> IntentParser / CommandGenerator / FaultDiagnosis 全链路
运行：python tests/test_deepseek_mock.py
"""
import os
import sys
import json

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

import yaml
from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, ".env"))

from src.ai.llm_client import LLMClient
from src.ai.intent_parser import IntentParser
from src.ai.command_gen import CommandGenerator
from src.ai.fault_diagnosis import FaultDiagnosis

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


# ---- 模拟 DeepSeek 的返回内容 ----
MOCK_INTENT = '[{"device": "R1", "action": "config_interface", "parameters": {"interface": "G0/0/1", "ip": "10.0.0.1", "mask": "24"}}]'
MOCK_CMDS = "system-view\nvlan 10\nname test\nquit"
MOCK_DIAG = json.dumps({
    "analysis": "接口 UP 但无法互通，疑似对端未配置同网段地址或缺少路由。",
    "confidence": "中",
    "risk_level": "Medium",
    "suggestions": ["检查对端接口地址", "确认路由配置"],
    "auto_fix_commands": ["system-view", "ip route-static 10.0.0.0 24 10.0.0.2"],
}, ensure_ascii=False)


def make_mock_llm(payload_by_prompt):
    """构造一个假 LLMClient，按 prompt 关键字返回预设内容"""
    class MockLLM(LLMClient):
        def __init__(self):
            self.config = {}
            self.llm = "mock"
            self.calls = 0

        def invoke(self, prompt, **kwargs):
            self.calls += 1
            for kw, payload in payload_by_prompt.items():
                if kw in prompt:
                    return payload
            return "mock-response"

    return MockLLM()


def main():
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    llm_cfg = config["llm"]

    print("=== 1. 配置默认值为 DeepSeek ===")
    check("provider=deepseek", llm_cfg.get("provider") == "deepseek", llm_cfg.get("provider"))
    check("model=deepseek-chat", llm_cfg.get("model") == "deepseek-chat", llm_cfg.get("model"))
    check("base_url=api.deepseek.com", "api.deepseek.com" in llm_cfg.get("base_url", ""), llm_cfg.get("base_url"))
    check("api_key_env=DEEPSEEK_API_KEY", llm_cfg.get("api_key_env") == "DEEPSEEK_API_KEY", llm_cfg.get("api_key_env"))

    print("\n=== 2. LLMClient 参数装配（不发起请求）===")
    # 用 __new__ 跳过后端构造，直接检查参数解析逻辑
    probe = LLMClient.__new__(LLMClient)
    cfg = dict(llm_cfg)
    provider = cfg.get("provider", "deepseek")
    from src.ai.llm_client import PROVIDER_DEFAULTS, DEFAULT_MODEL
    d_base, d_env = PROVIDER_DEFAULTS.get(provider, (None, None))
    resolved_base = cfg.get("base_url") or d_base
    resolved_env = cfg.get("api_key_env") or d_env
    check("provider 走 OpenAI 兼容分支", provider in ("deepseek", "openai", "openai_compatible"), provider)
    check("base_url 解析正确", resolved_base == "https://api.deepseek.com", resolved_base)
    check("api_key_env 解析正确", resolved_env == "DEEPSEEK_API_KEY", resolved_env)
    check("默认模型常量为 deepseek-chat", DEFAULT_MODEL == "deepseek-chat", DEFAULT_MODEL)

    print("\n=== 3. IntentParser 链路（Mock）===")
    parser = IntentParser(make_mock_llm({"网络配置专家": MOCK_INTENT}))
    tasks = parser.parse("在R1的G0/0/1配置IP 10.0.0.1/24")
    check("解析出 1 个任务", len(tasks) == 1, str(tasks))
    check("任务 action 正确", tasks and tasks[0].get("action") == "config_interface",
          tasks[0].get("action") if tasks else "")

    print("\n=== 4. CommandGenerator 链路 ===")
    cg = CommandGenerator(make_mock_llm({"VRP设备配置专家": MOCK_CMDS}))
    # 已内置模板：config_ospf 无需 LLM
    cmds = cg.generate({"action": "config_ospf", "parameters": {"process": 1, "area": 0, "network": "10.0.0.0"}})
    check("内置模板生成 OSPF 命令", "ospf 1" in " ".join(cmds), str(cmds))
    # 未知动作 -> 走 LLM（Mock）
    cmds2 = cg.generate({"action": "config_vlan", "device": "R1", "parameters": {"vlan_id": 10}})
    check("未知动作走 LLM 生成命令", bool(cmds2), str(cmds2))

    print("\n=== 5. FaultDiagnosis 链路（Mock）===")
    diag = FaultDiagnosis(make_mock_llm({"故障诊断专家": MOCK_DIAG}))
    res = diag.diagnose("R1 无法 ping 通 R2", {"R1": {"routing": "", "interfaces": ""}})
    check("诊断为结构化 JSON", isinstance(res, dict) and "analysis" in res, f"risk={res.get('risk_level')}")
    check("含修复建议命令", bool(res.get("auto_fix_commands")), str(res.get("auto_fix_commands")))

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"Mock 测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 50)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
