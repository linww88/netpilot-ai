## 核心特性（Features）

本文件按功能域列出当前实现的能力。标注 `★ 新增` 的为本轮迭代新增或显著增强的能力。

---

### 一、自然语言与 AI

- **无 .env 的优雅降级** ★
  未配置 `.env` / API Key 时应用仍可正常启动运行，仅 AI 功能不可用：
  - `LLMClient` 进入降级模式（不抛异常），`is_available()` 返回 `False`；
  - 调用 AI 时抛出 `LLMUnavailableError`，页面给出醒目的 `.env` 配置引导；
  - 页面顶部提示 **仍可用的非 AI 功能**（设备扫描/连接、手动下发、拓扑发现、配置读取等）；
  - `local`（Ollama）provider 无需 Key。

- **自然语言 → 配置任务**
  输入一句话描述需求（例如"在 R1 和 R2 之间配置 OSPF，并在 R1 上创建 VLAN 10"），AI 自动解析为结构化任务清单，
  每项包含 `device`（目标设备）、`action`（动作类型）、`parameters`（关键参数）。
  - 可用动作类型：`config_interface`（含 Trunk/Access/Hybrid/IP）、`config_vlanif`（SVI 网关）、
    `config_subinterface`（dot1q 子接口 / 单臂路由）、
    `config_ospf`、`config_static_route`、`config_vlan`、`config_acl`、`show_command`；
    另支持 `config_trunk`、`config_access`、`config_svi`、`config_single_arm` 等语义化别名。
  - 一条指令涉及多台设备时，为每台设备各生成一个任务对象；
    **同一物理口的多个子接口合并为一个任务**（`sub_interfaces` 数组），不拆散。

- **鲁棒的结果解析** ★
  LLM 返回结果经本地清洗，兼容四种常见格式：纯 JSON 数组、Markdown 代码块包裹、
  前后夹杂说明文字、模型返回单个对象或 `{"tasks": [...]}` 包装形式。

- **本地规则解析（无需 AI）** ★
  `RuleParser` 用正则确定性解析常见中文指令，不依赖大模型与网络：
  - 覆盖 Trunk / Access / Hybrid 口、接口 IP、VLAN 三层接口（SVI）、
    单臂路由 dot1q 子接口（VLAN 与 IP 按下标配对）、
    创建 VLAN（单个/批量/区间）、OSPF（支持一条指令多设备）、静态路由、查询命令；
  - 未配置 `.env` 时直接启用，并提示"本次由本地规则解析完成"；
  - AI 已配置但未解析出任务时自动兜底，避免"解析失败就完全不可用"。

- **AI 配置命令生成** ★
  - **内置模板**（本地生成，稳定可复现）：
    - 接口：IP 地址、描述、链路类型（`access` / `trunk` / `hybrid`）、
      Access 默认 VLAN、Trunk 放通 VLAN 与 PVID、`shutdown` / `undo shutdown`；
    - **dot1q 终结子接口（单臂路由）**：`dot1q termination vid` + `ip address` +
      **`arp broadcast enable`**；多条子接口一次生成，`G0/0/1` + vid 2/3 自动推导为 `.2` / `.3`；
      该命令是 VRP 必需项，缺了 ARP 学不到、VLAN 间不通，故由模板统一补齐并做全局兜底；
    - VLAN 三层接口（`Vlanif` / SVI）网关地址；
    - VLAN 单个创建与批量创建（`vlan batch`，连续区间自动压缩为 `a to b`）；
    - OSPF（进程、区域、`network`，支持 `10.0.0.0/24` 前缀写法）、静态路由、ACL（含命名 ACL）、查询命令。
  - **名称归一化**：`G0/0/1`、`GE0/0/1`、`GE 0/0/1` 统一展开为 `GigabitEthernet0/0/1`，
    `E0/0/1` → `Ethernet0/0/1`，`S0/0/0` → `Serial0/0/0`，避免缩写不被 VRP 接受。
  - **VLAN 写法兼容**：`2`、`[2,3]`、`2,3`、`2、3`、`2，3`、`2 3`、`vlan2、3`、
    `2-5`、`2 to 5`、`2至5` 共 11 种写法。
  - **视图层级正确闭合**：进入几层子视图就有对应数量的 `quit`（如 OSPF 的 `area` 视图）。
  - **大模型回退**：未注册的动作类型调用 LLM 生成；模板未覆盖的参数也会回退 LLM，
    无 LLM 时在界面提示"哪些参数未被使用，结果可能不完整"，**不再静默丢配置**。
  - 命令生成解析兼容「每行一条」与「JSON 数组」两种返回格式。

- **命令注释预览** ★
  `generate_with_comments()` 在生成命令的同时产出逐条中文注释，**不额外调用大模型**：
  - 按命令前缀分类注释（`system-view` / `interface` / `ip address` / `vlan` / `ospf` / `area` / `network` /
    `ip route-static` / `acl` / `rule` / `description` / `display` / `undo` /
    `dot1q termination vid` / `arp broadcast enable` /
    `port link-type` / `port trunk allow-pass vlan` / `port default vlan` 等）；
  - `quit` 注释按**视图嵌套深度**推导（"退出至用户视图" / "返回系统视图" / "返回上一级视图"）；
  - 未知操作回退按动作类型给出语义化说明。

- **故障诊断与修复建议**
  - 输入故障现象或疑问（例如"R1 无法 ping 通 R2，但是接口状态是 UP"、"S1 的 VLAN 配置是否成功"）。
  - **按问题类型自动选取采集命令**：基础采集 `display vlan` / `display port vlan` /
    `display interface brief` / `display ip interface brief` / `display ip routing-table`；
    命中配置、VLAN、Trunk、OSPF、STP、MAC、ARP、VRRP、DHCP 等关键词时追加
    `display current-configuration` 等命令。
  - **确定性事实解析**：程序先把回显解析成事实（VLAN 是否创建、端口链路类型/PVID/允许列表、
    接口状态、配置命令行），AI 只基于事实归因；未采集到的维度记为「证据缺口」，
    不会当成「未配置」。
  - AI 输出**结论**（配置成功 / 配置失败 / 证据不足）、置信度、风险等级、逐条证据、
    证据缺口、修复步骤与复核命令；页面可展开查看采集到的证据原文。

- **多 Provider 支持**
  统一封装 `deepseek`（默认）、`openai`、`openai_compatible`（自定义 `base_url`）、`local`（Ollama）。
  API Key 支持「直接填值」或「读环境变量」两种方式。

---

### 二、设备发现与管理

- **端口扫描自动发现设备** ★
  - 自定义扫描 IP 与端口范围，默认 `127.0.0.1:2000-2100`。
  - **两阶段并发**：先并发 TCP 探测开放端口，再并发探测设备身份。
  - **设备识别**：连接后发送回车，能读到提示符（`<R1>` / `[R1]` / `[~R1]`）即判定为网络设备，
    可有效区分普通服务端口（如 Web 服务）。
  - 起止端口反序自动交换、越界自动收敛；结果按端口升序返回。

- **设备身份读取与自动命名** ★
  - 从设备回显中提取**主机名**（`sysname` / 提示符）、**型号**（如 `AR2200`，优先匹配括号内型号）与**版本**。
  - 命名优先级：**真实主机名 → 前缀+序号 → 前缀+端口**，保证唯一。
  - 支持「🔄 读取信息并重命名」批量刷新，重命名时自动迁移会话与数据库记录；同名冲突安全返回不覆盖。

- **设备名智能解析** ★
  指令中的设备名与实际设备键不一致时自动匹配，优先级：
  1. 精确匹配设备键；
  2. 忽略大小写匹配；
  3. 匹配设备 `hostname` 字段；
  4. 数字序号启发式（如 `R1` → 唯一以 `1` 结尾的设备键 / 主机名）。

- **设备信息与会话管理**
  - 通过 `generic_telnet` 免交互连接 eNSP 设备，内置会话池复用连接。
  - 设备卡片展示连接状态、类型、地址、主机名、型号与版本。
  - 支持单台 / 批量连接，支持移除单台设备。
  - **移除所有设备** ★：一键清空设备列表，先逐个断开已建立的会话再清内存与
    SQLite 记录；按钮需**二次确认**（确认 / 取消），避免误触丢失整份实验环境。

- **设备元信息持久化** ★
  SQLite `devices` 表新增 `hostname` / `model` / `version` 字段，
  启动时通过 `PRAGMA table_info` 检测并 `ALTER TABLE` 补列，**兼容旧数据库自动迁移**。

---

### 三、配置下发与执行

- **任务执行与结果反馈**
  - 从「待执行任务清单」选择解析结果，一键下发到相关设备。
  - 执行时自动把指令中的设备名解析为真实设备键。
  - 结果按设备分组展示：✅/❌ 状态、任务描述、命令列表、错误原因。
  - **未匹配到设备的任务单独提示**，不被静默丢弃；执行前也会先给出预检警告。

- **健壮的 eNSP 命令执行** ★
  - 配置命令统一走**逐条 timing 下发**，不依赖 `send_config_set` 的提示符正则识别（eNSP 上不可靠）。
  - 执行前后自动**恢复会话状态**：解除分页暂停（`---- More ----`）、退出配置模式（`return`）。
  - 长输出命令走**自动翻页读取**，避免只能拿到第一页。
  - 回显统一清洗：去除分页提示、ANSI 控制序列、命令回显与尾部提示符；空连接会提前退出避免阻塞。

- **手动 CLI 与多设备并发**
  - 手动 CLI：选择设备后逐行输入 VRP 命令下发。
  - 多设备远程执行：勾选多个已连接设备，并发执行多条操作/查询命令，按设备查看完整回显。

- **设备配置读取与备份** ★
  - 一键读取 `display current-configuration` 完整配置（自动处理分页）。
  - 未连接时自动尝试建立连接；失败时给出明确排查建议。
  - 支持保存为带时间戳的 `.cfg` 备份文件至 `data/config_backups/`。

---

### 四、拓扑

- **拓扑信息采集与构建**
  - 自动对多台设备执行关键信息采集（接口、IP、ARP、MAC、LLDP、sysname 等）。
  - 发现前自动检查并尝试启用各设备 LLDP 功能。
  - **LLDP 收敛等待** ★：正式采集前轮询邻居表，待各设备邻居行数连续两轮稳定后再采集，避免首次发现链路不完整。
  - 基于采集结果构建 NetworkX 拓扑图；**每次构建前完整重置图**（`clear()`），**重复发现幂等**且不残留上一次实验的节点与边。
  - LLDP 邻居主机名与设备键自动归一化，避免因命名差异产生孤立节点。
  - 过滤表头/分隔线，避免生成伪链路；支持保存 / 加载拓扑 JSON（UTF-8）。

- **拓扑刷新控制** ★
  - 「强制重新发现（忽略历史旧拓扑）」：发现前清空旧图，采集失败也不残留旧拓扑。
  - 「🧹 清空当前拓扑」：清除内存图并删除 `data/topology.json`。
  - 展示区区分「本次实时发现结果」与「历史加载的拓扑」，后者显示醒目警告，提示用户重新发现。

- **拓扑可视化**
  - 基于 Matplotlib + Streamlit 渲染：
    - 节点标注为「设备键 (主机名)」；
    - 边显示链路两端端口号（如 `GE0/0/1 <-> GE0/0/2`）；
    - 已配置中文字体，避免中文乱码。

---

### 五、基础设施

- **测试与验证辅助**
  - `ping`：查看丢包率；
  - `traceroute`：观测路径；
  - `show` 命令 + 关键字匹配，验证配置是否生效。

- **数据持久化与日志**
  - SQLite 持久化设备列表（含主机名/型号/版本）与任务记录。
  - 所有操作与错误记录到日志文件，便于排错与审计。

- **离线测试套件** ★
  提供不依赖真实设备与网络的测试，分 pytest 与脚本两种风格：

  *pytest 风格（`pytest tests/`，44 个用例）：*
  - `tests/test_vrp_commands.py`：VRP 命令正确性（Trunk/Access/SVI/OSPF/路由/名称与 VLAN 归一化）；
  - `tests/test_rule_parser.py`：本地规则解析（文本 → 任务 → 命令 全链路）；
  - `tests/test_topology_refresh.py`：拓扑清空/重建不残留、LLDP 收敛等待；
  - `tests/test_no_env.py`：无 .env 降级运行与友好提示；
  - `tests/test_app_startup.py`：set_page_config 顺序约束、**禁止 expander 嵌套**（AST + AppTest）。

  *脚本风格（`python tests/xxx.py`，100 项断言）：*
  - `tests/test_device_scan.py`（29 项）：端口扫描、身份识别、命名、数据库迁移等；
  - `tests/test_command_comments.py`（21 项）：命令生成与注释；
  - `tests/test_device_manager.py`（12 项）：连接、会话恢复、配置下发；
  - `tests/test_topology.py`（10 项）：拓扑构建与幂等性；
  - `tests/test_deepseek_mock.py`（14 项）：LLM 调用链路 mock 验证；
  - `tests/test_deepseek.py`（14 项）：LLM 契约（含无 Key 降级）与命令解析。
