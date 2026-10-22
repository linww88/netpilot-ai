"""Streamlit主应用入口""" 
import streamlit as st 
import yaml 
import os 
import sys
import time
from dotenv import load_dotenv 

# ⚠️ st.set_page_config() 必须是脚本中第一条 Streamlit 命令，
# 因此必须放在任何 st.* 调用（含 st.sidebar / st.session_state / st.error）之前。
st.set_page_config(page_title="NetPilot eNSP AI 网络助手", layout="wide", page_icon="🧠")

# 解决 ModuleNotFoundError: No module named 'src'
# 将项目根目录添加到 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.append(project_root)

from src.connection.device_manager import DeviceManager 
from src.topology.collector import InfoCollector 
from src.topology.builder import TopologyBuilder 
from src.topology.visualizer import visualize_topology 
from src.ai.llm_client import LLMClient, LLMUnavailableError, ENV_HINT 
from src.ai.intent_parser import IntentParser 
from src.ai.command_gen import CommandGenerator 
from src.ai.rule_parser import RuleParser
from src.ai.fault_diagnosis import FaultDiagnosis 
from src.executor.config_executor import ConfigExecutor 
from src.validator.tester import Tester 
from src.utils.logger import setup_logger 
from src.utils.db import DatabaseManager

# 加载环境变量 
load_dotenv() 

# 加载配置 
config_path = './config/config.yaml'
if not os.path.exists(config_path):
    st.error(f"未找到配置文件: {config_path}")
    st.stop()

with open(config_path, 'r', encoding='utf-8') as f: 
    config = yaml.safe_load(f) 

# 初始化日志 
setup_logger() 

# 初始化会话状态
if 'task_history' not in st.session_state:
    st.session_state['task_history'] = []
if 'last_parsed_tasks' not in st.session_state:
    st.session_state['last_parsed_tasks'] = None

# AI & 搜索相关会话配置
default_llm_cfg = config.get('llm', {})
llm_presets = config.get('llm_presets', [])
if 'llm_provider' not in st.session_state:
    st.session_state['llm_provider'] = default_llm_cfg.get('provider', 'deepseek')
if 'llm_model' not in st.session_state:
    st.session_state['llm_model'] = default_llm_cfg.get('model', 'deepseek-chat')
if 'llm_base_url' not in st.session_state:
    st.session_state['llm_base_url'] = default_llm_cfg.get('base_url', 'https://api.deepseek.com')
if 'llm_api_key_env' not in st.session_state:
    st.session_state['llm_api_key_env'] = default_llm_cfg.get('api_key_env', 'DEEPSEEK_API_KEY')
if 'llm_preset_name' not in st.session_state:
    st.session_state['llm_preset_name'] = "自定义（使用 config.yaml 配置）"
if 'use_web_search' not in st.session_state:
    # 预留开关：是否启用互联网聚合搜索增强
    st.session_state['use_web_search'] = False

# 侧边栏：AI & 搜索配置（放在设备管理前）
with st.sidebar:
    st.subheader("🤖 AI & 搜索配置")
    provider_labels = {
        "deepseek": "DeepSeek 官方接口（默认）",
        "openai": "在线 OpenAI / 兼容接口",
        "openai_compatible": "OpenAI 兼容 API（国内/第三方）",
        "local": "本地大模型 (Ollama)",
    }
    inverse_provider_labels = {v: k for k, v in provider_labels.items()}
    # 预置模型选项（包含一个“自定义”占位）
    preset_names = ["自定义"] + [p.get("name", f"预设 {i+1}") for i, p in enumerate(llm_presets)]
    current_preset = st.session_state.get('llm_preset_name', "自定义")
    try:
        preset_index = preset_names.index(current_preset)
    except ValueError:
        preset_index = 0
    selected_preset = st.selectbox(
        "预设模型（可选）",
        options=preset_names,
        index=preset_index,
        help="选择一个预配置的免费/本地/兼容模型，或选择“自定义”手动填写。",
    )
    st.session_state['llm_preset_name'] = selected_preset

    # 如果选中了具体预设，则用预设覆盖当前会话配置
    if selected_preset != "自定义":
        for p in llm_presets:
            if p.get("name") == selected_preset:
                st.session_state['llm_provider'] = p.get('provider', st.session_state['llm_provider'])
                st.session_state['llm_model'] = p.get('model', st.session_state['llm_model'])
                if 'base_url' in p:
                    st.session_state['llm_base_url'] = p.get('base_url', st.session_state['llm_base_url'])
                if 'api_key_env' in p:
                    st.session_state['llm_api_key_env'] = p.get('api_key_env', st.session_state['llm_api_key_env'])
                break

    current_provider = st.session_state['llm_provider']
    current_label = provider_labels.get(current_provider, provider_labels['deepseek'])
    selected_label = st.selectbox(
        "LLM 提供方",
        options=list(provider_labels.values()),
        index=list(provider_labels.values()).index(current_label),
        key="llm_provider_label",
    )
    st.session_state['llm_provider'] = inverse_provider_labels[selected_label]

    st.session_state['llm_model'] = st.text_input(
        "模型名称",
        value=st.session_state['llm_model'],
        help="例如 deepseek-chat、deepseek-reasoner、gpt-4o、qwen-max、glm-4 等",
    )

    if st.session_state['llm_provider'] in ('deepseek', 'openai', 'openai_compatible'):
        st.session_state['llm_base_url'] = st.text_input(
            "OpenAI 兼容 Base URL（可选）",
            value=st.session_state['llm_base_url'],
            help="例如 https://api.deepseek.com 或第三方兼容接口地址；留空使用默认。",
        )

    st.session_state['use_web_search'] = st.checkbox(
        "启用互联网聚合搜索增强（预留）",
        value=st.session_state['use_web_search'],
        help="开启后，解析/诊断逻辑可按需接入在线搜索能力（当前为预留开关）。",
    )

    # AI 配置状态提示（此处 llm_client 尚未创建，用环境变量做即时判断）
    _peek_key_env = st.session_state.get('llm_api_key_env', 'DEEPSEEK_API_KEY')
    _peek_provider = st.session_state.get('llm_provider', 'deepseek')
    if _peek_provider != 'local' and not (os.getenv(_peek_key_env, "") or "").strip():
        st.caption(f"⚠️ 未检测到环境变量 `{_peek_key_env}`，AI 功能将不可用（配置 .env 后重启）")
    else:
        st.caption("✅ AI 配置已就绪")

# 初始化核心组件（使用 st.cache_resource 确保在 Streamlit 重新运行时不会重复创建连接池等）
@st.cache_resource
def init_components(llm_overrides: dict):
    dm = DeviceManager(config['connection']) 
    # 合并配置：以 UI 选择为主
    merged_llm_cfg = config['llm'].copy()
    merged_llm_cfg.update(llm_overrides or {})
    llm = LLMClient(merged_llm_cfg) 
    ip = IntentParser(llm) 
    cg = CommandGenerator(llm) 
    exe = ConfigExecutor(dm, cg) 
    t = Tester(dm) 
    diag = FaultDiagnosis(llm) 
    db = DatabaseManager()
    topo_collector = InfoCollector(dm, config.get('topology', {}))
    topo_builder = TopologyBuilder()
    # 尝试加载已有拓扑（如果存在）
    topo_builder.load_topology()
    
    # 从数据库加载历史设备
    saved_devices = db.get_devices()
    for name, ip_addr, port, dev_type in saved_devices:
        dm.add_device(name, ip_addr, port)
        
    return dm, llm, ip, cg, exe, t, diag, db, topo_collector, topo_builder

llm_overrides = {
    "provider": st.session_state['llm_provider'],
    "model": st.session_state['llm_model'],
    "base_url": st.session_state['llm_base_url'],
    "api_key_env": st.session_state['llm_api_key_env'],
}
device_manager, llm_client, intent_parser, command_gen, executor, tester, diagnosis, db_manager, topo_collector, topo_builder = init_components(llm_overrides)

# 本地规则解析器（无状态、不依赖 AI，作为 AI 不可用/无结果时的兜底）
rule_parser = RuleParser()

# Streamlit 界面布局（set_page_config 已在文件顶部调用，此处只渲染标题）
st.title("🧠 NetPilot eNSP AI 网络助手") 

# AI 可用性提示（缺 .env / API Key 时给出明确引导，但应用仍可继续使用）
_ai_available = llm_client.is_available()
if not _ai_available:
    st.warning(
        "⚠️ **AI 功能当前不可用**（未检测到有效的大模型 API Key）。\n\n"
        "你仍然可以使用：**设备扫描/连接、手动命令下发、批量命令执行、设备配置读取、拓扑自动发现** 等功能。\n\n"
        "如需启用自然语言解析与故障诊断，请：\n"
        "1. 复制项目根目录的 `.env.example` 为 `.env`；\n"
        "2. 在 `.env` 中填写 `DEEPSEEK_API_KEY=sk-你的密钥`；\n"
        "3. 保存后重启应用。\n\n"
        f"（当前 LLM 配置：provider=`{llm_client.provider}`，model=`{llm_client.model}`，"
        f"环境变量=`{llm_client.api_key_env}`）"
    )

# 侧边栏：设备管理 
_scan_cfg = config.get('connection', {})
with st.sidebar: 
    st.header("🏢 设备管理") 

    # ---- 端口扫描自动发现 ----
    with st.expander("🔍 扫描添加设备（自动发现）", expanded=False):
        st.caption("扫描本机 eNSP 映射端口，自动识别设备并读取主机名命名。")
        scan_ip = st.text_input("扫描 IP", value=_scan_cfg.get('scan_ip', '127.0.0.1'), key="scan_ip")
        c1, c2 = st.columns(2)
        with c1:
            scan_start = st.number_input(
                "起始端口", min_value=1, max_value=65535,
                value=int(_scan_cfg.get('scan_start_port', 2000)), step=1, key="scan_start",
            )
        with c2:
            scan_end = st.number_input(
                "结束端口", min_value=1, max_value=65535,
                value=int(_scan_cfg.get('scan_end_port', 2100)), step=1, key="scan_end",
            )
        use_hostname = st.checkbox("用设备主机名命名（推荐）", value=True, key="scan_use_hostname")
        name_prefix = st.text_input(
            "命名前缀（未取到主机名时使用）", value="R", key="scan_prefix",
            help="例如前缀 R，将依次命名为 R1、R2……",
        )

        if st.button("🚀 开始扫描", use_container_width=True, key="btn_scan"):
            if scan_start > scan_end:
                st.error("起始端口不能大于结束端口")
            else:
                with st.spinner(f"正在扫描 {scan_ip}:{scan_start}-{scan_end} ..."):
                    try:
                        found = device_manager.scan_devices(
                            ip=scan_ip,
                            start_port=int(scan_start),
                            end_port=int(scan_end),
                            max_workers=int(_scan_cfg.get('scan_max_workers', 50)),
                        )
                        st.session_state['scan_results'] = found
                    except Exception as e:
                        st.error(f"扫描失败：{e}")
                        st.session_state['scan_results'] = []

        # 展示扫描结果，供勾选批量添加
        scan_results = st.session_state.get('scan_results')
        if scan_results is not None:
            dev_ports = [r for r in scan_results if r.get('is_device')]
            other_ports = [r for r in scan_results if not r.get('is_device')]
            if not scan_results:
                st.warning("该端口范围内未发现任何开放端口。请确认 eNSP 设备已启动并开启了端口映射。")
            else:
                st.success(
                    f"扫描完成：共 {len(scan_results)} 个开放端口，"
                    f"其中 {len(dev_ports)} 个识别为网络设备。"
                )
                if other_ports:
                    st.caption(
                        "以下开放端口未识别为设备（可能是其他服务）："
                        + ", ".join(str(r['port']) for r in other_ports)
                    )
                if dev_ports:
                    selectable = {}
                    for r in dev_ports:
                        label = f"端口 {r['port']}"
                        if r.get('hostname'):
                            label += f" · 主机名 {r['hostname']}"
                        if r.get('model'):
                            label += f" · {r['model']}"
                        selectable[label] = r['port']
                    chosen_labels = st.multiselect(
                        "选择要添加的设备", options=list(selectable.keys()),
                        default=list(selectable.keys()), key="scan_selected",
                    )
                    chosen_ports = [selectable[l] for l in chosen_labels]
                    if st.button("➕ 添加所选设备", type="primary",
                                 use_container_width=True, key="btn_scan_add"):
                        if not chosen_ports:
                            st.warning("请至少选择一个设备")
                        else:
                            added = device_manager.scan_and_add(
                                ip=scan_ip,
                                start_port=int(scan_start),
                                end_port=int(scan_end),
                                selected_ports=chosen_ports,
                                name_prefix=name_prefix or "DEV",
                                use_hostname=use_hostname,
                                scanned=scan_results,
                            )
                            for a in added:
                                db_manager.add_device(
                                    a['name'], scan_ip, a['port'], 'generic_telnet',
                                    hostname=a.get('hostname', ''),
                                    model=a.get('model', ''),
                                    version=a.get('version', ''),
                                )
                            if added:
                                names = ", ".join(a['name'] for a in added)
                                st.success(f"已添加 {len(added)} 台设备：{names}")
                            else:
                                st.info("所选设备均已存在，无需重复添加。")
                            st.rerun()

    st.divider()

    # ---- 手动添加 ----
    with st.form("add_device"): 
        st.caption("✍️ 手动添加")
        name = st.text_input("设备名", placeholder="例如: R1") 
        ip_addr = st.text_input("IP地址", value="127.0.0.1") 
        port = st.number_input("Telnet端口", value=2000, step=1) 
        submitted = st.form_submit_button("➕ 添加设备") 
        if submitted and name and ip_addr: 
            # 保存到内存
            device_manager.add_device(name, ip_addr, port) 
            # 保存到数据库
            device_info = device_manager.devices[name]
            db_manager.add_device(name, ip_addr, port, device_info['device_type'])
            st.success(f"设备 {name} 已添加到列表") 
            st.rerun()
    
    st.divider()
    st.subheader("📋 已添加设备") 
    devices_list = device_manager.devices.keys()
    if not devices_list:
        st.info("暂无设备，请先添加")
    else:
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            if st.button("🔄 读取信息并重命名", use_container_width=True, key="btn_refresh_all",
                         help="连接各设备，读取主机名后自动重命名，并刷新型号信息"):
                with st.spinner("正在读取设备信息..."):
                    renamed, failed = [], []
                    for dev_name in list(device_manager.devices.keys()):
                        try:
                            new_name = device_manager.refresh_device_name(dev_name)
                            if new_name and new_name != dev_name:
                                info = device_manager.devices[new_name]
                                db_manager.delete_device(dev_name)
                                db_manager.add_device(
                                    new_name, info['ip'], info['port'], info['device_type'],
                                    hostname=info.get('hostname', ''),
                                    model=info.get('model', ''),
                                    version=info.get('version', ''),
                                )
                                renamed.append(f"{dev_name}→{new_name}")
                            elif new_name:
                                info = device_manager.devices[dev_name]
                                db_manager.add_device(
                                    dev_name, info['ip'], info['port'], info['device_type'],
                                    hostname=info.get('hostname', ''),
                                    model=info.get('model', ''),
                                    version=info.get('version', ''),
                                )
                        except Exception as e:
                            failed.append(f"{dev_name}({e})")
                if renamed:
                    st.success("已重命名：" + ", ".join(renamed))
                if failed:
                    st.warning("以下设备读取失败：" + ", ".join(failed))
                st.rerun()
        with col_r2:
            if st.button("🔌 连接全部", use_container_width=True, key="btn_connect_all"):
                ok, bad = [], []
                for dev_name in list(device_manager.devices.keys()):
                    if device_manager.connect(dev_name):
                        ok.append(dev_name)
                    else:
                        bad.append(dev_name)
                if ok:
                    st.success(f"已连接 {len(ok)} 台：{', '.join(ok)}")
                if bad:
                    st.warning(f"连接失败：{', '.join(bad)}")

        connected_devices = device_manager.get_all_connected()
        for dev_name in list(devices_list): 
            status_label = "🟢 已连接" if dev_name in connected_devices else "🔴 未连接"
            info = device_manager.devices[dev_name]
            meta = ""
            if info.get('model'):
                meta = f" · {info['model']}"
            with st.expander(f"{status_label} | 🖥️ {dev_name}{meta}"):
                st.caption(f"类型: {info['device_type']} | 地址: {info['ip']}:{info['port']}")
                if info.get('hostname') and info['hostname'] != dev_name:
                    st.caption(f"主机名: {info['hostname']}")
                if info.get('version'):
                    st.caption(f"版本: {info['version']}")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("🔗 连接", key=f"conn_{dev_name}"): 
                        with st.spinner(f"正在连接 {dev_name}..."):
                            conn = device_manager.connect(dev_name) 
                            if conn: 
                                st.success(f"连接成功") 
                                st.rerun()
                            else: 
                                # 在 UI 中展示具体的失败原因
                                st.error(f"连接失败")
                                st.info("💡 请检查 eNSP 设备是否已启动，以及 Serial Port 端口号是否正确。")
                with col2:
                    if st.button("🗑️ 移除", key=f"del_{dev_name}"):
                        device_manager.remove_device(dev_name)
                        db_manager.delete_device(dev_name)
                        st.rerun()

                if st.button("🔄 读取此设备信息", key=f"refresh_{dev_name}"):
                    with st.spinner(f"正在读取 {dev_name} 的信息..."):
                        new_name = device_manager.refresh_device_name(dev_name)
                    if new_name is None:
                        st.warning("读取失败，请确认设备已连接。")
                    else:
                        info = device_manager.devices[new_name]
                        db_manager.delete_device(dev_name)
                        db_manager.add_device(
                            new_name, info['ip'], info['port'], info['device_type'],
                            hostname=info.get('hostname', ''),
                            model=info.get('model', ''),
                            version=info.get('version', ''),
                        )
                        if new_name != dev_name:
                            st.success(f"已重命名为 {new_name}")
                        else:
                            st.success("信息已刷新")
                        st.rerun()

# 主标签页 
tab1, tab2, tab3, tab4 = st.tabs(["💬 指令解析与执行", "🗺️ 拓扑自动发现", "⚙️ 手动配置下发", "🔍 故障分析排错"]) 

with tab1: 
    st.header("💬 自然语言指令解析") 
    st.info("输入您的配置需求，AI 将生成任务清单。解析后的任务可保存至历史记录以便后续执行。")
    user_input = st.text_area("配置需求 (例如: '在 R1 和 R2 之间配置 OSPF')", height=100) 
    
    col1, col2 = st.columns([1, 5])
    with col1:
        parse_btn = st.button("🚀 开始解析", use_container_width=True)
    
    if parse_btn: 
        if not user_input:
            st.warning("请输入指令内容")
        elif not _ai_available:
            # 未配置 .env：改用本地规则解析（覆盖常见指令），并提示能力边界
            _rule_tasks = rule_parser.parse(user_input)
            if _rule_tasks:
                st.session_state['last_parsed_tasks'] = _rule_tasks
                st.info(
                    "未配置 `.env`，本次由**本地规则解析**完成（支持 Trunk/Access 口、"
                    "接口 IP、VLAN、SVI、OSPF、静态路由、查询等常见指令）。\n\n"
                    "如需解析更复杂的表述，请配置 `.env` 启用 AI。"
                )
            else:
                st.error(
                    "AI 功能不可用，且本地规则未能识别该指令。请先配置 `.env`（见页面上方提示）。\n\n"
                    "在此期间，你仍可使用「⚙️ 手动配置下发」标签页直接下发命令。"
                )
        else:
            try:
                with st.spinner("AI 正在解析意图..."): 
                    tasks = intent_parser.parse(user_input)
                if not tasks:
                    # AI 没解析出来 → 用本地规则兜底
                    tasks = rule_parser.parse(user_input)
                    if tasks:
                        st.info("AI 未解析出任务，已自动改用**本地规则解析**。")
                if tasks:
                    st.session_state['last_parsed_tasks'] = tasks
                else:
                    st.warning("AI 未解析出任何任务，请调整指令表述后重试。")
            except LLMUnavailableError as e:
                st.error(f"AI 调用失败：{e}")
            except Exception as e:
                st.error(f"解析过程出错：{e}")
    
    if st.session_state['last_parsed_tasks']: 
        st.subheader("🤖 当前解析结果") 
        _tasks = st.session_state['last_parsed_tasks']

        view_mode = st.radio(
            "展示方式",
            options=["📋 结构化 + 命令预览", "🧾 仅 JSON"],
            horizontal=True,
            key="parse_view_mode",
        )

        if view_mode == "🧾 仅 JSON":
            st.json(_tasks)
        else:
            for idx, _task in enumerate(_tasks, 1):
                _dev = _task.get("device") or _task.get("target_device") or "未指定设备"
                _action = _task.get("action", "unknown")
                _resolved = device_manager.resolve_device_name(_dev)
                _badge = f"（已匹配设备：{_resolved}）" if _resolved else "（⚠️ 未找到该设备）"

                with st.expander(f"任务 {idx}｜{_dev}｜{_action}", expanded=True):
                    c1, c2 = st.columns([2, 3])
                    with c1:
                        st.markdown("**🖥️ 目标设备**")
                        st.markdown(f"`{_dev}` {_badge}")
                        st.markdown("**🧾 结构化 JSON**")
                        st.json(_task)
                    with c2:
                        st.markdown("**⌨️ 对应华为 VRP 配置命令**")
                        _cmds, _cmd_warns = command_gen.generate_with_comments_ex(_task)
                        if not _cmds:
                            st.info("未能生成命令（该操作类型可能需要人工确认或补充参数）。")
                        else:
                            _lines = []
                            for _i, _item in enumerate(_cmds, 1):
                                _cmd = _item.get("command", "")
                                _cmt = _item.get("comment", "")
                                _lines.append(f"{_i:>2}. {_cmd}"
                                              + (f"    # {_cmt}" if _cmt else ""))
                            st.code("\n".join(_lines), language="text")
                            for _w in _cmd_warns:
                                st.warning(_w)
                            # 注意：Streamlit 禁止 expander 嵌套 expander。
                            # 此处已位于「任务」expander 内部，故改用 popover
                            # （popover 可以嵌套，交互上也更紧凑）。
                            with st.popover(f"📖 命令逐条注释（共 {len(_cmds)} 条）",
                                            use_container_width=True):
                                for _i, _item in enumerate(_cmds, 1):
                                    _cmt = _item.get("comment", "")
                                    st.markdown(
                                        f"`{_i}` **{_item.get('command', '')}**"
                                        + (f" — {_cmt}" if _cmt else "")
                                    )

        with st.expander("🧩 全部任务的命令汇总", expanded=False):
            _all_lines = []
            for idx, _task in enumerate(_tasks, 1):
                _dev = _task.get("device") or _task.get("target_device") or "未指定设备"
                _all_lines.append(f"===== 任务 {idx}：{_dev} "
                                  f"（{_task.get('action')}）=====")
                _cmds = command_gen.generate_with_comments(_task)
                if not _cmds:
                    _all_lines.append("  (无可用命令)")
                for _item in _cmds:
                    _cmt = _item.get("comment", "")
                    _all_lines.append(f"  {_item.get('command', '')}"
                                      + (f"    # {_cmt}" if _cmt else ""))
                _all_lines.append("")
            st.code("\n".join(_all_lines).rstrip(), language="text")
            st.download_button(
                "⬇️ 下载命令脚本（.txt）",
                data="\n".join(_all_lines).rstrip(),
                file_name="ensp_commands.txt",
                mime="text/plain",
                use_container_width=True,
            )

        if st.button("💾 保存至待执行清单", type="primary"):
            from datetime import datetime
            task_entry = {
                "id": len(st.session_state['task_history']) + 1,
                "time": datetime.now().strftime("%H:%M:%S"),
                "input": user_input[:50] + "..." if len(user_input) > 50 else user_input,
                "tasks": st.session_state['last_parsed_tasks']
            }
            st.session_state['task_history'].append(task_entry)
            st.session_state['last_parsed_tasks'] = None
            st.success("任务已保存到 '配置执行' 标签页中")
            st.rerun()

with tab2:
    st.header("🗺️ 拓扑自动发现")
    all_devs = list(device_manager.devices.keys())
    if not all_devs:
        st.info("暂无设备，请先在侧边栏添加并连接设备")
    else:
        target_devs = st.multiselect("选择参与拓扑发现的设备", options=all_devs, default=all_devs, key="topo_devices")

        col_topo1, col_topo2 = st.columns([3, 1])
        with col_topo1:
            start_topo = st.button("🚀 开始拓扑发现", type="primary", use_container_width=True)
        with col_topo2:
            clear_topo = st.button("🧹 清空当前拓扑", use_container_width=True,
                                   help="清除内存与文件中的拓扑数据，避免与上一次实验的旧拓扑混淆")

        # 选项：是否强制重新发现（忽略历史加载的旧拓扑）
        force_refresh = st.checkbox(
            "强制重新发现（忽略历史旧拓扑）",
            value=True,
            help="勾选后，本次发现只反映实时采集结果；未采集到的旧设备不会残留。",
            key="topo_force_refresh",
        )
        wait_converge = st.checkbox(
            "等待 LLDP 收敛后再采集（推荐）",
            value=True,
            help="设备刚启动时邻居表可能未填充完整，开启后会自动等待若干秒，避免首次发现不完整。",
            key="topo_wait_converge",
        )

        # 清空拓扑
        if clear_topo:
            topo_builder.clear()
            try:
                _topo_file = os.path.join("data", "topology.json")
                if os.path.exists(_topo_file):
                    os.remove(_topo_file)
            except Exception as e:
                st.warning(f"删除拓扑文件失败：{e}")
            st.session_state.pop("topo_last_result", None)
            st.success("已清空当前拓扑数据，请重新执行拓扑发现。")
            st.rerun()

        if start_topo:
            if not target_devs:
                st.warning("请至少选择一个设备")
            else:
                # 强制刷新时，先清空旧图，确保失败也不会残留上一份实验的拓扑
                if force_refresh:
                    topo_builder.clear()

                # 步骤0：先检查连接，未连接的设备直接给出明确提示
                not_connected = [d for d in target_devs if d not in device_manager.get_all_connected()]
                if not_connected:
                    st.warning(
                        f"以下设备尚未连接，将尝试自动连接：{', '.join(not_connected)}"
                    )

                # 步骤1：确保参与拓扑发现的设备已启用 LLDP
                lldp_failed = []
                with st.spinner("正在检查并启用设备 LLDP 功能..."):
                    for dev in target_devs:
                        ok = device_manager.ensure_lldp_enabled(dev)
                        if not ok:
                            lldp_failed.append(dev)
                if lldp_failed:
                    st.warning(
                        f"以下设备的 LLDP 启用失败，其链路可能无法识别：{', '.join(lldp_failed)}。"
                        "请确认设备已启动、端口映射正确，或在 CLI 中手动执行 'system-view' + 'lldp enable'。"
                    )

                # 步骤1.5：等待 LLDP 邻居表收敛，避免首次采集不完整
                lldp_rows = {}
                if wait_converge:
                    with st.spinner("正在等待 LLDP 邻居信息收敛..."):
                        lldp_rows = topo_collector.wait_lldp_convergence(target_devs)
                    total_rows = sum(lldp_rows.values())
                    if total_rows == 0:
                        st.warning(
                            "LLDP 邻居表仍为空。可能原因：设备刚启动尚未收敛、未启用 LLDP，"
                            "或拓扑中确实没有 LLDP 链路。"
                        )

                # 步骤2：采集信息并构建拓扑
                with st.spinner("正在采集设备信息并构建拓扑..."):
                    collected = topo_collector.collect_all(target_devs)
                    # 若采集结果为空或全部失败，给出明确反馈
                    usable = {d: o for d, o in collected.items() if o and any(v for v in o.values())}
                    if not usable:
                        # 强制刷新已清空旧图，此处不会再展示上一份实验的拓扑
                        st.error(
                            "未能从任何设备采集到有效信息，无法构建拓扑。"
                            "请检查设备连接状态与端口映射后重试。"
                        )
                        st.session_state["topo_last_result"] = {
                            "ok": False, "nodes": 0, "edges": 0,
                            "devices": [], "times": len(st.session_state.get("topo_discover_count", [])) + 1,
                        }
                    else:
                        graph = topo_builder.build(usable)
                        topo_builder.save_topology()
                        n_nodes = graph.number_of_nodes()
                        n_edges = graph.number_of_edges()

                        # 记录本次发现结果，用于展示与陈旧性判断
                        st.session_state["topo_last_result"] = {
                            "ok": True,
                            "nodes": n_nodes,
                            "edges": n_edges,
                            "devices": sorted(graph.nodes()),
                            "lldp_rows": lldp_rows,
                        }
                        st.session_state["topo_discover_count"] = (
                            st.session_state.get("topo_discover_count", []) + [time.time()]
                        )

                        st.success(f"拓扑发现完成：识别到 {n_nodes} 台设备、{n_edges} 条链路。")

                        # 提示本次结果是否与期望的设备集合一致
                        missing = [d for d in target_devs if d not in graph]
                        if missing:
                            st.info(
                                f"以下已选设备本次未出现在拓扑中：{', '.join(missing)}。"
                                "若它们是刚启动的设备，可勾选「等待 LLDP 收敛」后重新发现。"
                            )
                        if n_edges == 0:
                            st.info(
                                "未识别到任何链路。请确认各设备已启用 LLDP 且已执行过数据采集；"
                                "也可稍等 LLDP 邻居信息收敛后再次点击发现。"
                            )

        # 拓扑展示区：区分「本次发现结果」与「历史加载的拓扑」
        last_result = st.session_state.get("topo_last_result")
        has_graph = bool(topo_builder.graph and topo_builder.graph.number_of_nodes() > 0)

        if has_graph:
            st.subheader("📍 当前拓扑视图")

            # 若当前图来自历史文件加载（本次会话尚未成功发现过），明确提示
            if not last_result or not last_result.get("ok"):
                st.warning(
                    "⚠️ 当前展示的是**历史保存的拓扑**（可能是上一次实验的残留），"
                    "并非本次实时采集结果。请点击「🚀 开始拓扑发现」重新发现，"
                    "或点击「🧹 清空当前拓扑」后重试。"
                )
            else:
                st.caption(
                    f"本次发现：{last_result['nodes']} 台设备、{last_result['edges']} 条链路"
                    f"（已选 {len(target_devs)} 台）"
                )

            fig = visualize_topology(topo_builder.graph)
            if fig:
                st.pyplot(fig)
        else:
            st.info("当前没有拓扑数据。请点击「🚀 开始拓扑发现」构建拓扑。")

with tab3: 
    st.header("⚙️ 任务执行与手动配置") 
    
    # 历史任务引用部分
    st.subheader("� 待执行任务清单")
    if not st.session_state['task_history']:
        st.info("暂无已解析的任务，请先在 '指令解析' 标签页中进行解析并保存。")
    else:
        # 选择要执行的历史任务
        task_options = {f"[{t['time']}] {t['input']}": t for t in st.session_state['task_history']}
        selected_task_key = st.selectbox("🎯 选择待执行任务", options=list(task_options.keys()))
        
        if selected_task_key:
            selected_task_data = task_options[selected_task_key]
            st.json(selected_task_data['tasks'])
            
            col1, col2 = st.columns(2)
            with col1:
                if st.button("🚀 立即执行该任务", type="primary", use_container_width=True):
                    # 执行前先解析设备名，提前暴露"任务设备名与已配置设备不匹配"的问题
                    unresolved = []
                    for _t in selected_task_data['tasks']:
                        _d = _t.get("device") or _t.get("target_device")
                        if not _d or device_manager.resolve_device_name(_d) is None:
                            unresolved.append(_d or "(未指定)")
                    if unresolved:
                        st.warning(
                            f"以下目标设备未在设备列表中找到，相关任务将无法执行：{', '.join(unresolved)}。"
                            "请在侧边栏确认设备名称，或使用“扫描添加设备”补充设备。"
                        )
                    with st.spinner("正在下发配置..."):
                        results = executor.execute_tasks(selected_task_data['tasks'])
                        st.session_state['last_exec_results'] = results
                        st.success("执行完成")
            with col2:
                if st.button("🗑️ 移除此任务", use_container_width=True):
                    st.session_state['task_history'] = [t for t in st.session_state['task_history'] if t['id'] != selected_task_data['id']]
                    st.rerun()

    if 'last_exec_results' in st.session_state:
        st.divider()
        st.subheader("📊 执行结果反馈")
        _res = st.session_state['last_exec_results']

        # 未匹配到设备的任务单独提示
        if "__unresolved__" in _res:
            st.error(_res["__unresolved__"].get("error", "存在未能匹配设备的任务"))
            for item in _res["__unresolved__"].get("tasks", []):
                st.markdown(f"- `{item.get('error', '')}` → 任务：{item.get('task')}")

        for dev_key, dev_res in _res.items():
            if dev_key == "__unresolved__":
                continue
            ok = dev_res.get("success")
            with st.expander(f"{'✅' if ok else '❌'} {dev_key}", expanded=not ok):
                if not dev_res.get("tasks"):
                    st.info("该设备没有任何可执行任务。")
                    continue
                for item in dev_res["tasks"]:
                    task = item.get("task", {})
                    desc = task.get("description") or task.get("intent") or str(task)
                    st.markdown(f"**任务**：{desc} {'✅' if item.get('success') else '❌'}")
                    if item.get("commands"):
                        st.code("\n".join(item["commands"]), language="text")
                    if item.get("error"):
                        st.warning(item["error"])
        with st.expander("🧾 原始执行结果 (JSON)"):
            st.json(_res)
        if st.button("🧹 清除执行记录"):
            del st.session_state['last_exec_results']
            st.rerun()

    st.divider()
    st.subheader("📝 手动 CLI 命令下发")
    all_devs = list(device_manager.devices.keys())
    if not all_devs:
        st.warning("请先在侧边栏添加设备")
    else:
        selected_dev = st.selectbox("🎯 选择目标设备", options=all_devs, key="manual_dev") 
        commands_text = st.text_area("📝 输入 VRP 命令（每行一条）", height=150, key="manual_cmd") 
        if st.button("📤 发送配置", use_container_width=True): 
            if commands_text: 
                cmd_list = [c.strip() for c in commands_text.split('\n') if c.strip()] 
                with st.spinner(f"正在向 {selected_dev} 发送命令..."):
                    success = device_manager.execute_config_commands(selected_dev, cmd_list) 
                    if success: 
                        st.success(f"配置已成功应用于 {selected_dev}") 
                    else: 
                        st.error("配置发送失败")

    st.divider()
    st.subheader("📡 多设备远程命令执行")
    connected_devs = device_manager.get_all_connected()
    if not connected_devs:
        st.info("当前暂无已连接设备，请先在侧边栏连接设备后再执行远程命令。")
    else:
        multi_devs = st.multiselect(
            "选择要执行命令的设备（仅显示已连接设备）",
            options=connected_devs,
            default=connected_devs,
            key="multi_exec_devs"
        )
        multi_cmds_text = st.text_area("🧾 输入操作/查询命令（每行一条）", height=120, key="multi_exec_cmds")
        if st.button("▶️ 执行远程命令", use_container_width=True):
            if not multi_devs:
                st.warning("请至少选择一个设备")
            elif not multi_cmds_text:
                st.warning("请输入至少一条命令")
            else:
                cmd_list = [c.strip() for c in multi_cmds_text.split('\n') if c.strip()]
                with st.spinner("正在多设备并发执行命令..."):
                    batch_out = device_manager.execute_batch(
                        device_names=multi_devs,
                        commands=cmd_list,
                        is_config=False,
                    )
                    st.session_state["last_multi_exec"] = {"devices": multi_devs, "commands": cmd_list, "results": batch_out}

    if "last_multi_exec" in st.session_state:
        st.subheader("📃 多设备命令执行结果")
        last = st.session_state["last_multi_exec"]
        for dev in last["devices"]:
            dev_res = last["results"].get(dev, {})
            with st.expander(f"🖥️ {dev}", expanded=False):
                if not dev_res:
                    st.info("未获取到任何输出（可能连接失败或命令执行异常）。")
                else:
                    for cmd in last["commands"]:
                        output = dev_res.get(cmd)
                        st.markdown(f"**$ {cmd}**")
                        if output is None:
                            st.text("执行失败或无输出")
                        else:
                            st.code(output)

    st.divider()
    st.subheader("📥 设备配置读取与备份")
    if not all_devs:
        st.info("请先在侧边栏添加设备")
    else:
        cfg_dev = st.selectbox("选择要读取配置的设备", options=all_devs, key="cfg_dev")
        col_cfg1, col_cfg2 = st.columns(2)
        with col_cfg1:
            if st.button("📑 读取当前配置", use_container_width=True):
                resolved_dev = device_manager.resolve_device_name(cfg_dev) or cfg_dev
                # 未连接则先尝试连接，避免直接读取失败
                if resolved_dev not in device_manager.get_all_connected():
                    with st.spinner(f"正在连接 {resolved_dev} ..."):
                        connected = device_manager.connect(resolved_dev)
                    if not connected:
                        st.error(
                            f"无法连接到设备 {resolved_dev}。请检查设备是否已启动、"
                            "端口映射是否正确（可在侧边栏“扫描添加设备”重新发现）。"
                        )
                        st.stop()
                with st.spinner(f"正在从 {resolved_dev} 读取当前配置（大配置需分页读取，请稍候）..."):
                    # display current-configuration 输出可能上千行，走 execute_command
                    # 内部会自动处理 ---- More ---- 分页与配置模式恢复
                    cfg_output = device_manager.execute_command(resolved_dev, "display current-configuration")
                    if not cfg_output:
                        st.error(
                            f"读取配置失败：未从 {resolved_dev} 获取到任何回显。"
                            "可能原因：会话停留在分页/配置模式，或设备未就绪。"
                            "可先点击侧边栏该设备的“🔄 刷新”重建会话后重试。"
                        )
                    else:
                        st.session_state["last_config_dump"] = {"device": resolved_dev, "content": cfg_output}
                        st.success(f"已读取 {resolved_dev} 配置，共 {len(cfg_output)} 字符。")
        with col_cfg2:
            if st.button("💾 保存配置到服务器", use_container_width=True):
                cfg_info = st.session_state.get("last_config_dump")
                if not cfg_info or cfg_info.get("device") != cfg_dev:
                    st.warning("请先读取该设备的当前配置。")
                else:
                    import datetime
                    import os as _os
                    backup_dir = "./data/config_backups"
                    _os.makedirs(backup_dir, exist_ok=True)
                    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{cfg_dev}_config_{ts}.cfg"
                    path = _os.path.join(backup_dir, filename)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(cfg_info["content"])
                    st.success(f"配置已保存到 {path}")

        if "last_config_dump" in st.session_state and st.session_state["last_config_dump"].get("device") == cfg_dev:
            st.subheader("📄 最近读取的设备配置")
            st.code(st.session_state["last_config_dump"]["content"])

with tab4: 
    st.header("🔍 网络故障分析排错") 
    st.info("AI 专家将协助您分析连通性问题并给出修复建议。")
    fault_desc = st.text_area("⚠️ 故障现象描述 (例如: 'R1 无法 ping 通 R2，但是 R1 接口状态是 UP')", height=100) 
    
    if st.button("🩺 开始 AI 诊断", type="primary"): 
        if not _ai_available:
            st.error(
                "AI 功能不可用，无法进行故障诊断。请先配置 `.env`（见页面上方提示）。"
            )
        elif fault_desc: 
            all_devs = list(device_manager.devices.keys())
            if not all_devs:
                st.warning("请先添加设备以供 AI 采集诊断数据")
            else:
                with st.spinner("正在采集诊断上下文信息 (路由表/接口状态)..."): 
                    # 并发采集所有设备的关键诊断信息
                    collect_cmds = [
                        "display ip routing-table",
                        "display ip interface brief",
                    ]
                    batch_results = device_manager.execute_batch(
                        device_names=all_devs,
                        commands=collect_cmds,
                        is_config=False,
                    )
                    device_outputs = {}
                    for dev, cmd_res in batch_results.items():
                        device_outputs[dev] = {
                            "routing": cmd_res.get("display ip routing-table"),
                            "interfaces": cmd_res.get("display ip interface brief"),
                        }

                    try:
                        with st.spinner("AI 专家正在分析根因并生成修复方案..."): 
                            diag_result = diagnosis.diagnose(fault_desc, device_outputs) 
                            st.session_state['diag_result'] = diag_result 
                    except LLMUnavailableError as e:
                        st.error(f"AI 诊断失败：{e}")
                    except Exception as e:
                        st.error(f"诊断过程出错：{e}")
        else:
            st.warning("请先描述故障现象")

    if 'diag_result' in st.session_state:
        res = st.session_state['diag_result']
        st.subheader("🔬 诊断分析报告")
        st.write(f"**分析详情**: {res.get('analysis', '无数据')}")
        st.write(f"**风险等级**: `{res.get('risk_level', 'Unknown')}`")
        
        st.subheader("🛠️ 修复建议命令")
        if res.get('suggestions'):
            st.code("\n".join(res['suggestions']))
            if st.button("🚀 自动应用修复配置"):
                # 这里可以根据建议自动执行，为了安全暂时仅展示
                st.info("自动应用功能待进一步确认安全策略后开启")
        else:
            st.info("AI 未能生成具体的修复命令，请手动核查")

# 底部页脚
st.markdown("---") 
st.caption("NetPilot eNSP AI 网络助手 v0.1.0 | Powered by DeepSeek & LangChain | © 2026")
