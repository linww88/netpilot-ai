"""DeepSeek 集成测试脚本
验证：配置读取 -> LLMClient 初始化 -> 真实调用 DeepSeek 接口 -> 各 AI 模块可用
运行：python tests/test_deepseek.py
"""
import os
import sys

# 让 src 可被导入
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import yaml
from dotenv import load_dotenv

load_dotenv(os.path.join(project_root, ".env"))

from src.ai.llm_client import LLMClient
from src.ai.intent_parser import IntentParser
from src.ai.command_gen import CommandGenerator
from src.ai.fault_diagnosis import FaultDiagnosis
from src.utils.logger import setup_logger

# 切到项目根目录，保证相对路径（./data/app.log 等）正确
os.chdir(project_root)

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -> {detail}" if detail else ""))
    return ok


def main():
    setup_logger()

    with open("config/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    llm_cfg = config["llm"]
    print("\n=== 1. 读取默认配置 ===")
    print(
        f"provider={llm_cfg.get('provider')}  model={llm_cfg.get('model')}  "
        f"base_url={llm_cfg.get('base_url')}  api_key_env={llm_cfg.get('api_key_env')}"
    )
    check("默认 provider 为 deepseek", llm_cfg.get("provider") == "deepseek", llm_cfg.get("provider"))
    check("默认 model 为 deepseek-chat", llm_cfg.get("model", "").startswith("deepseek"), llm_cfg.get("model"))
    check("默认 base_url 指向 deepseek", "deepseek" in (llm_cfg.get("base_url") or ""), llm_cfg.get("base_url"))

    key = os.getenv(llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "")
    check("已从环境变量读取到 API Key", bool(key), f"{key[:8]}...{key[-4:]}" if key else "缺失")

    print("\n=== 2. 初始化 LLMClient ===")
    llm = LLMClient(llm_cfg)
    check("LLMClient 初始化成功", llm.llm is not None, type(llm.llm).__name__)

    print("\n=== 3. 真实调用 DeepSeek 接口 ===")
    try:
        reply = llm.invoke("请只回复两个字：成功")
        ok = bool(reply and reply.strip())
        check("DeepSeek 接口连通并返回内容", ok, repr(reply[:60]))
    except Exception as e:
        check("DeepSeek 接口连通并返回内容", False, f"{type(e).__name__}: {e}")
        print("\n>>> 接口调用失败，跳过后续模块测试。")
        return summary()

    print("\n=== 4. IntentParser 意图解析 ===")
    parser = IntentParser(llm)
    tasks = parser.parse("在R1的G0/0/1配置IP 10.0.0.1/24")
    check("意图解析返回非空任务列表", bool(tasks), f"{len(tasks)} 个任务: {tasks}")

    print("\n=== 5. CommandGenerator 命令生成 ===")
    cg = CommandGenerator(llm)
    cmds = cg.generate({"action": "config_vlan", "device": "R1",
                        "parameters": {"vlan_id": 10, "name": "test"}, "target_devices": ["R1"]})
    check("命令生成返回非空命令列表", bool(cmds), f"{len(cmds)} 条: {cmds[:4]}")

    print("\n=== 6. FaultDiagnosis 故障诊断 ===")
    diag = FaultDiagnosis(llm)
    res = diag.diagnose("R1 无法 ping 通 R2，但接口状态是 UP",
                        {"R1": {"routing": "10.0.0.0/24 Direct", "interfaces": "GE0/0/1 up"}})
    check("故障诊断返回结构化结果", isinstance(res, dict) and bool(res.get("analysis")),
          f"risk_level={res.get('risk_level')}")

    print("\n=== 7. 缺少 Key 时的报错行为 ===")
    # 新版 langchain-openai 在「构造时」即校验凭证，缺少 Key 会立即抛错，
    # 这属于期望行为（快速失败），而非缺陷。
    try:
        fallback_cfg = dict(llm_cfg)
        fallback_cfg["api_key_env"] = "NON_EXISTENT_KEY_ENV"
        fallback_cfg["api_key"] = ""
        LLMClient(fallback_cfg)
        check("缺少 Key 时立即报错（快速失败）", False, "未报错，可能静默使用了环境中的其他凭证")
    except Exception as e:
        check("缺少 Key 时立即报错（快速失败）", "credential" in str(e).lower() or "api_key" in str(e).lower(),
              f"{type(e).__name__}: {str(e)[:70]}")

    print("\n=== 8. 命令解析兼容 JSON 数组格式 ===")
    cg2 = CommandGenerator(llm)
    parsed = cg2._parse_command_list('["system-view", "vlan 10", "name test", "quit"]')
    check("JSON 数组被正确拆分为多条命令", len(parsed) == 4, str(parsed))
    parsed2 = cg2._parse_command_list("system-view\nvlan 10\nquit")
    check("逐行格式仍正常解析", len(parsed2) == 3, str(parsed2))

    return summary()


def summary():
    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"测试结果: {passed}/{total} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  未通过: {name} -> {detail}")
    print("=" * 50)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
