# 修订说明与新增功能

本文档汇总 **NetPilot eNSP AI 网络助手**（`netpilot-ai`，基于 eNSP AI Assistant 二次开发）全部修订内容与新增功能，按主题分类，包含改动文件、问题根因、解决方案与验证结果。

- 修订性质：功能新增 + 缺陷修复 + 文档更新
- 涉及代码文件：10 个
- 新增测试文件：5 个（共 114 项离线测试）
- 代码规模：`src/` 约 2,700 行，`tests/` 约 1,300 行

---

## 目录

1. [大模型默认配置迁移至 DeepSeek](#1-大模型默认配置迁移至-deepseek)
2. [修复：自然语言指令无法解析](#2-修复自然语言指令无法解析)
3. [修复：拓扑自动发现无法运行 / 拓扑数据错乱](#3-修复拓扑自动发现无法运行--拓扑数据错乱)
4. [新增：端口扫描自动发现设备（2000–2100）](#4-新增端口扫描自动发现设备20002100)
5. [新增：设备信息读取与自动命名](#5-新增设备信息读取与自动命名)
6. [修复：设备配置读取失败](#6-修复设备配置读取失败)
7. [修复：指令解析与执行链路](#7-修复指令解析与执行链路)
8. [新增：命令注释预览](#8-新增命令注释预览)
9. [修复：拓扑自动发现残留上一次实验的拓扑](#9-修复拓扑自动发现残留上一次实验的拓扑)
10. [修复：全新克隆后无法启动（data 目录缺失）](#10-修复全新克隆后无法启动data-目录缺失)
11. [文档更新](#11-文档更新)
12. [测试与验证汇总](#12-测试与验证汇总)
13. [改动文件清单](#13-改动文件清单)
---

## 1. 大模型默认配置迁移至 DeepSeek

### 功能说明

将项目默认调用的大模型从原有配置改为 **DeepSeek 官方接口**，并建立可扩展的多 Provider 架构。

### 改动内容

| 文件 | 改动 |
| --- | --- |
| `config/config.yaml` | `llm` 段默认值改为 `provider: deepseek` / `model: deepseek-chat` / `base_url: https://api.deepseek.com` / `api_key_env: DEEPSEEK_API_KEY`；新增 `api_key` 字段（支持直接填值）；`llm_presets` 增加 DeepSeek Chat / Reasoner / 本地 Ollama / OpenAI 兼容预设 |
| `src/ai/llm_client.py` | 重写参数解析逻辑；新增 `PROVIDER_DEFAULTS` 映射按 provider 决定默认 `base_url` 与 `api_key_env`；`deepseek` 走 OpenAI 兼容分支 |
| `src/main.py` | 会话状态默认值改为 DeepSeek；侧边栏 Provider 下拉新增 DeepSeek 选项；页脚署名更新 |
| `.env` / `.env.example` | 新增环境变量模板，Key 通过 `DEEPSEEK_API_KEY` 注入 |

### 支持的 Provider

| provider | 说明 | 默认 base_url |
| --- | --- | --- |
| `deepseek`（默认） | DeepSeek 官方接口 | `https://api.deepseek.com` |
| `openai` | OpenAI 官方接口 | `https://api.openai.com/v1` |
| `openai_compatible` | 任意 OpenAI 兼容服务（需填 `base_url`） | 自定义 |
| `local` | 本地模型（Ollama） | — |

### 附带修复

修复 `src/ai/command_gen.py` 中的一个**真实缺陷**：提示词模板 `command_gen.txt` 使用 `{task}` 占位符，但旧代码用 `intent` / `target_devices` / `parameters` 等名称格式化，触发 `KeyError: 'task'` 并被 `except` 静默吞掉，导致**命令生成始终返回空列表**。

### 验证

- 真实接口测试 `tests/test_deepseek.py` 12/12 通过（HTTP 200）。
- 离线 Mock 测试 `tests/test_deepseek_mock.py` 14/14 通过。

---

## 2. 修复：自然语言指令无法解析

### 问题现象

输入「在 R1 和 R2 之间配置 OSPF」等指令后，解析结果为空数组 `[]`，"指令解析"功能实际不可用。

### 根因（两个独立原因）

**根因 A — 代码层：JSON 提取过于脆弱**

DeepSeek 返回的 JSON 常被 ` ```json ` 代码块围栏包裹，旧 `_extract_json` 直接用 `find('[')` 切割，遇到围栏或前后说明文字即解析失败。

**根因 B — 提示词层：拓扑为空导致模型拒答**

当 `topology_context` 为空时，模型认为指令中提到的 `R1` / `R2` 不存在，直接返回 `[]`。

### 解决方案

| 文件 | 改动 |
| --- | --- |
| `src/ai/intent_parser.py` | 重写 `_extract_json()`，新增 `_try_load()`，支持四种返回形态 |
| `config/prompts/intent_parser.txt` | 增加解析规则，避免模型拒答 |

**`_extract_json()` 兼容的四种形态：**

1. 纯 JSON 数组：`[{...}]`
2. Markdown 代码块包裹：` ```json\n[...]\n``` `
3. 前后夹杂说明文字：`[...]`
4. 模型返回单个对象或 `{"tasks": [...]}` 包装形式

**提示词新增规则：**

- 即使用户指令中提到的设备未出现在拓扑信息里，也必须根据指令文本中的设备名解析，**不得返回空数组**；
- 一条指令涉及多台设备时，为每台设备各生成一个任务对象；
- 只输出 JSON，禁止使用代码块包裹，禁止输出解释文字。

### 验证

- 4 类典型指令解析 4/4 成功；
- 同一指令连续调用 5 次，结果稳定 5/5。

---

## 3. 修复：拓扑自动发现无法运行 / 拓扑数据错乱

### 问题现象

点击"开始拓扑发现"后要么失败，要么生成包含伪节点、错误链路的拓扑数据。

### 根因（四个独立原因）

| # | 层次 | 问题 |
| --- | --- | --- |
| A | 设备层 | eNSP + `generic_telnet` 下 `send_config_set` 常报 `Pattern not detected`，导致 LLDP 启用失败 |
| B | 拓扑层（最严重） | LLDP 邻居上报的是**对端 sysname**（如 `OSPF-Core1`），而拓扑节点用的是**用户配置的设备键**（如 `R1`）；二者不一致导致凭空造出伪节点、链路连错 |
| C | 拓扑层 | `build()` 从不重置 `self.graph`，重复发现会累加节点与边 |
| D | UI 层 | `save_topology` 无编码参数；结果未向用户反馈节点/边数量，失败也显示"完成" |

### 解决方案

**`src/topology/builder.py`**

- `build()` 开头重置 `self.graph`（保证幂等）；
- 新增 `hostname_to_key` 反向索引与 `_resolve_device_key()`，对 LLDP 邻居名做**归一化**；
- LLDP 行解析增加表头/分隔线过滤与接口名校验，避免生成伪链路；
- 同一条边只写一次（确定性写入，避免两端互相覆盖）；
- `load_topology` 使用 dict 副本，避免 `pop` 破坏源数据；
- `save_topology` 显式指定 UTF-8。

**`src/topology/visualizer.py`**

- 配置中文字体（Microsoft YaHei / SimHei），避免中文乱码；
- 节点标签显示为「设备键 (hostname)」。

**`src/main.py`**

- 拓扑发现增加未连接设备提示、LLDP 失败清单、采集全失败报错；
- 成功时报告识别到的节点数与边数。

### 验证

- `tests/test_topology.py` 10/10 通过（覆盖 hostname 归一化、无伪节点、链路正确、幂等、表头过滤）。

---

## 4. 新增：端口扫描自动发现设备（2000–2100）

### 功能说明

自动扫描本机 eNSP 映射端口，识别其中的网络设备并批量添加，替代手工逐台配置设备名/IP/端口。

### 新增 API（`src/connection/device_manager.py`）

| 方法 | 说明 |
| --- | --- |
| `_port_is_open(ip, port, timeout)` | TCP 端口探测（`socket.connect_ex`） |
| `is_ensp_device_port(ip, port, timeout)` | 连接后发送回车，能读到提示符即判定为网络设备 |
| `_probe_identity(ip, port)` | 临时连接读取 hostname / model / version，读完断开 |
| `scan_devices(ip, start, end, max_workers, probe_device)` | **两阶段并发**扫描 |
| `scan_and_add(ip, start, end, selected_ports, name_prefix, use_hostname, scanned)` | 批量添加设备 |

### 关键设计

- **两阶段并发**：阶段一并发 TCP 探测开放端口；阶段二**并发**探测设备身份。
  优化后扫描耗时从 23 秒降至 **8.2 秒**。
- **设备识别**：通过"TCP 探测 + 提示符校验"两步，有效区分真实 eNSP 设备与普通服务端口（如 Web 服务）。
- **健壮性**：起止端口反序自动交换，越界自动收敛；结果按端口升序返回；非设备端口保留并标记 `is_device=False`。
- **去重**：已存在的设备自动跳过，不重复添加。

### 配置项（`config/config.yaml` → `connection`）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `scan_ip` | `127.0.0.1` | 扫描目标 IP |
| `scan_start_port` | `2000` | 起始端口 |
| `scan_end_port` | `2100` | 结束端口 |
| `scan_timeout` | `0.3` | 单端口探测超时（秒） |
| `scan_max_workers` | `50` | 扫描并发数 |

### UI 新增（`src/main.py` 侧边栏）

- 「🔍 扫描添加设备（自动发现）」折叠区：扫描 IP、起始/结束端口、用主机名命名开关、命名前缀、扫描按钮；
- 扫描结果展示：识别到的设备（端口 + 主机名 + 型号）多选勾选批量添加；非设备端口单独提示；无开放端口时给出排查建议；
- 新增「🔄 读取信息并重命名」与「🔌 连接全部」批量操作按钮。

### 验证（真实 eNSP 环境）

```
扫描 127.0.0.1:2000-2100 → 耗时 8.24s，发现 3 台设备：
  2000  OSPF-Core1  AR2200
  2001  R3          AR2200
  2002  R2          AR2200
```

- `tests/test_device_scan.py` 29/29 通过。

---

## 5. 新增：设备信息读取与自动命名

### 功能说明

读取 eNSP 设备的主机名、型号与版本，并**用真实主机名命名设备**，使界面中的设备名与实际设备一致。

### 新增 API

| 方法 | 说明 |
| --- | --- |
| `_extract_hostname_from_banner(text)` | 从回显/提示符提取 `<R1>` / `[R1]` / `[~R1]`，多行取最后一个 |
| `_extract_model(text)` | 从 `display version` 提取型号 |
| `_read_device_identity(name)` | 对已连接设备读取身份信息 |
| `refresh_device_name(name)` | 读取主机名并重命名设备（迁移会话、连接池与元数据） |
| `list_devices()` | 返回已注册设备信息列表（副本） |

### 命名优先级

1. **真实主机名**（`sysname`）
2. **前缀 + 序号**（如前缀 `R` → `R1`、`R2`……）
3. **前缀 + 端口**（保证唯一）

同名冲突时安全返回 `None`，不覆盖已有设备。

### 踩坑与修复

早期版本 `_MODEL_KEYWORDS` 包含 `"AR"` 且直接 `re.search`，把 `software` 中的 `are` 误匹配为型号。已改为**优先匹配括号内型号**（如 `(AR1220 V300R003C10SPC600)` → `AR1220`），回退使用带词边界的 `_MODEL_RE`。

### 数据库持久化（`src/utils/db.py`）

- `devices` 表新增 `hostname` / `model` / `version` 三列；
- `_init_db()` 使用 `PRAGMA table_info` 检测并用 `ALTER TABLE` 补列，**兼容旧数据库自动迁移**；
- `add_device()` 增加元信息参数，改为「同名则 UPDATE，否则 INSERT」；
- 新增 `get_devices_with_meta()`。

### UI 新增

- 每个设备卡片显示 hostname / model / version；
- 新增「🔄 读取此设备信息」单设备刷新按钮。

---

## 6. 修复：设备配置读取失败

### 问题现象

点击「读取当前配置」后无输出或报错，无法获取设备配置。

### 根因

**核心缺陷在 `_clean_output()`：提示符残留污染回显**

原实现使用：

```python
re.sub(r"\n[<\[]~?[\w\-]+[>\]]\s*$", "", result)
```

存在两个问题：

1. 缺少 `re.MULTILINE`，`$` 只匹配字符串末尾；
2. eNSP 回显实际形态是「提示符 + 命令同行、无换行分隔」（如 `<R1>display lldp local`），`\n` 前置条件根本不成立。

结果：提示符未被剥离，回显首行变成 `<R1>display xxx`，污染后续所有关键字判断（例如 `"unrecognized command" in text` 的判断失效）。

**次要缺陷：`read_long_output()` 空连接阻塞**

假连接（`read_channel` 恒空）下会循环 500 次 × 0.08 秒 ≈ 40 秒无输出阻塞。

### 解决方案

**重写 `_clean_output()` 的清理逻辑为 4 步：**

1. 删除行首「提示符 + 命令」整行；
2. 删除内嵌「提示符 + 命令」子串；
3. 删除裸命令回显行；
4. 删除残留孤立提示符；
5. 最后丢弃清理后变为空的行。

验证场景：`display current-configuration` / `display version` / `display clock` / 报错回显，全部正确，且报错文本完整保留。

**修复 `read_long_output()`：**

新增 `empty_total` 计数，全程未读到任何字符且空转达到 `idle_limit` 时提前退出并记录告警。

### UI 优化（`src/main.py`）

- 读取前先解析设备名；
- 未连接则自动尝试连接，失败时给出具体排查建议并终止；
- 成功时提示读取到的字符数；
- 失败时给出分页 / 配置模式 / 刷新会话等针对性建议。

### 验证（真实设备）

```
配置读取：OSPF-Core1 1305 字符 / R3 941 / R2 950，均含 sysname  ✅
```

---

## 7. 修复：指令解析与执行链路

### 问题现象

解析出的任务下发时报"设备不存在"，或部分任务被静默丢弃。

### 根因

AI 解析出的设备名（如 `R1`）与用户实际配置的设备键（如 `OSPF-Core1`）不一致，导致 `DeviceManager.connect()` 找不到设备直接失败。

### 解决方案

**新增设备名智能解析（`src/connection/device_manager.py`）**

`resolve_device_name(name)` 按优先级匹配：

1. 精确匹配设备键；
2. 忽略大小写匹配设备键；
3. 匹配设备的 `hostname` 字段；
4. **数字序号启发式**：若请求形如 `R1`，且存在唯一设备键/主机名以相同数字结尾，则匹配。

另提供 `resolve_device_names(names)` 批量解析。

**执行器修复（`src/executor/config_executor.py`）**

- `execute_tasks()` 在分组前先解析设备名，并记录 `_resolved_device`；
- 解析失败时回退 `_match_known_device()` 做宽松匹配（忽略大小写 / 包含匹配）；
- **无法匹配的任务不再静默丢弃**，统一归入 `results["__unresolved__"]` 并携带中文错误提示。

**UI 优化（`src/main.py`）**

- 「立即执行该任务」前先做名称解析预检，未匹配设备直接给出警告；
- 执行结果从裸 `st.json` 改为可读渲染：
  - 未匹配任务单独以错误区列出；
  - 每台设备一个可展开面板（成功/失败图标、任务描述、命令列表、错误原因）；
  - 原始 JSON 收进折叠区。

### 验证（真实设备）

```
名称解析：R1 → OSPF-Core1，R2 → R2，R3 → R3，OSPF-Core1 → OSPF-Core1  ✅
端到端：指令「在 R1 上创建 VLAN 10，并配置接口描述为 OFFICE」
        → 解析 1 任务 → 执行到 OSPF-Core1 → 确认 vlan 10 已生成  ✅
指令「给 R2 的 Gi0/0/0 配置 IP 10.0.0.1/24」
        → 执行成功，确认 ip address 10.0.0.1 255.255.255.0 已生效  ✅
未匹配设备 R99 → 正确进入 __unresolved__  ✅
```

---

## 8. 新增：命令注释预览

### 功能说明

自然语言指令解析后，除展示生成的 JSON 外，**同时展示对应的华为 VRP 配置命令并附带中文注释**，执行前即可核对将要下发的配置。

### 核心实现（`src/ai/command_gen.py`）

| 方法 | 说明 |
| --- | --- |
| `generate_with_comments(task)` | 返回 `[{"command": ..., "comment": ...}]`，**不额外调用大模型** |
| `_comment_for(cmd, action, params, all_cmds)` | 按命令语义与任务参数推导中文注释 |

**注释规则：**

- 按命令前缀分类：`system-view` / `interface` / `ip address` / `vlan` / `ospf` / `area` / `network` / `ip route-static` / `acl` / `rule` / `description` / `display` / `undo` 等；
- `quit` 注释按**视图嵌套深度**推导：统计该条之前已进入的视图层级，若其后无剩余 `quit` 则注释「退出至用户视图」，否则「返回上一级视图」；
- 未识别命令回退按动作类型给出语义化说明。

### 补齐内置命令模板

原先只有 `config_interface` / `config_ospf` / `config_static_route` 三个模板，
`config_vlan` / `config_acl` / `show_command` 会退化到 LLM 调用（甚至失败）。现已补齐：

| 模板 | 支持能力 |
| --- | --- |
| `_gen_vlan` | `vlan_id` 单值或列表（列表生成 `vlan batch a b c`），可选 `description` |
| `_gen_acl` | `acl 3000` + `rule ...`（自动补 `rule` 前缀） |
| `_gen_show` | 查询命令直接返回 `display xxx`，**不进入 system-view** |

### UI 新增（`src/main.py` 解析结果区）

- 新增「展示方式」切换：`📋 结构化 + 命令预览`（默认）／`🧾 仅 JSON`；
- 结构化视图：每个任务一张卡片，左右两栏——
  - 左栏：目标设备 + 匹配状态徽标（`已匹配设备：xxx` / `⚠️ 未找到该设备`）+ 结构化 JSON；
  - 右栏：**对应华为 VRP 配置命令**（按 `序号. 命令    # 注释` 逐行），并可展开「📖 命令逐条注释」；
- 新增「🧩 全部任务的命令汇总」折叠区，提供 **⬇️ 下载命令脚本（.txt）** 按钮。

### 效果示例

```
任务 1｜R1｜config_ospf
  1. system-view   # 进入系统视图（配置模式）
  2. ospf 1        # 启动 OSPF 进程 1
  3. quit          # 退出至用户视图
```

### 验证

- `tests/test_command_comments.py` 21/21 通过（六类命令、掩码转换、quit 深度注释、查询命令不含 system-view、输出结构严格为 `{command, comment}`）；
- 真实 DeepSeek API 端到端验证通过。

---

## 9. 修复：拓扑自动发现残留上一次实验的拓扑

### 问题现象

点击「开始拓扑发现」时，界面经常仍显示**上一次实验的拓扑**（旧设备、旧链路），
需要反复点击数次后才会刷新为新拓扑，界面与实时采集结果不一致。

### 根因（四个独立原因）

| # | 层次 | 问题 |
| --- | --- | --- |
| A | 缓存层 | `topo_builder` 由 `@st.cache_resource` 缓存，应用启动时 `init_components()` 会调用 `load_topology()` 载入上一次实验保存的 `data/topology.json`，于是打开页面就先展示旧拓扑 |
| B | 构建层 | 当本次采集失败（`usable` 为空）时，代码 `if not usable` 直接跳过 `build()`，旧图**从未被清空**，仍继续渲染 |
| C | 采集层 | eNSP 设备刚启动或刚启用 LLDP 时，邻居表需数秒才填充完整；首次点击立即采集往往只得空表，需反复点击才能拿到完整链路 |
| D | 展示层 | 界面无法区分「本次实时发现结果」与「历史加载的旧拓扑」，失败时也无从判断，用户只能凭感觉反复点击 |

### 解决方案

**`src/topology/builder.py` — 新增 `clear()` 并强制重置**

- 新增 `clear()` 方法，一次性重置 `graph` / `devices` / `nodes` / `edges` / `hostname_to_key`；
- `build()` 开头调用 `self.clear()`，保证**构建结果只反映本次传入的 `collected_data`**，不再残留历史节点与边（原第 3 节修复了"重复累加"，本次进一步覆盖"上一份实验残留"）。

**`src/topology/collector.py` — 新增 LLDP 收敛等待**

| 新增 | 说明 |
| --- | --- |
| `_count_lldp_rows(text)`（静态） | 统计 LLDP 邻居有效行数，排除空行、表头与分隔线 |
| `wait_lldp_convergence(device_names, max_rounds=3, interval=3.0)` | 正式采集前轮询各设备邻居表，直到**连续两轮行数一致**（视为收敛）或达到轮次上限，返回 `{设备名: 行数}` |

**`src/main.py` — tab2 拓扑页重构**

- 新增 **「🧹 清空当前拓扑」** 按钮：清空内存图并删除 `data/topology.json`（同时清除本次会话的历史结果记录）；
- 新增 **「强制重新发现（忽略历史旧拓扑）」** 复选框（默认开启）：发现前先 `topo_builder.clear()`，**即使本次采集失败也不会残留上一份实验的拓扑**；
- 新增 **「等待 LLDP 收敛后再采集（推荐）」** 复选框（默认开启）：调用 `wait_lldp_convergence()`，解决"首次点击不完整、要点击几次"的问题；
- 新增会话状态 `topo_last_result`，记录本次发现的 `{ok, nodes, edges, devices, lldp_rows}`，用于区分实时结果与历史拓扑；
- 展示区明确区分两种状态：
  - 本次会话**未成功发现**但图中仍有节点 → 显示醒目警告「⚠️ 当前展示的是**历史保存的拓扑**……请点击重新发现」；
  - 本次已成功发现 → 显示 `本次发现：N 台设备、M 条链路`；
- 补充反馈：未连接的设备、LLDP 启用失败清单、采集全失败报错、已选设备未出现在拓扑中的提示、链路为 0 时的排查建议。

### 验证

- 新增 `tests/test_topology_refresh.py` **28/28 通过**，覆盖：
  - `clear()` 彻底重置全部状态；
  - 第二次实验不残留第一次的节点、边与链路；
  - 部分采集时未采集到的旧设备被移除（而非保留）；
  - 采集失败（`build({})`）时图被清空；
  - 从历史文件加载后重新发现，历史节点全部消失；
  - `wait_lldp_convergence` 在连续两轮稳定后返回、并提前结束；
  - `_count_lldp_rows` 正确排除表头与分隔线。
- Streamlit 启动冒烟测试 HTTP 200，日志无异常。

---

## 10. 修复：全新克隆后无法启动（data 目录缺失）

### 问题现象

从 GitHub 克隆项目后直接运行，应用启动即报错崩溃：

```
FileNotFoundError: [Errno 2] No such file or directory: '.../data/app.log'
```

### 根因

`data/` 目录存放运行期数据（SQLite 数据库、日志、配置备份、拓扑文件），
已通过 `.gitignore` 排除，因此**仓库中不含 `data/` 目录**——这是正确的做法。
但代码中存在两处**未先创建目录就直接写入**的缺陷：

| 文件 | 缺陷代码 | 后果 |
| --- | --- | --- |
| `src/utils/logger.py` | `logging.FileHandler('./data/app.log')` | `main.py` 启动时立即调用 `setup_logger()`，目录不存在即抛 `FileNotFoundError`，**应用彻底无法启动** |
| `src/validator/reporter.py` | `open(f"{self.data_dir}/{filename}", "w")` | 生成报告时目录不存在会报错 |

其余模块（`db.py` / `builder.py` / `device_manager.py` / `main.py` 备份逻辑）
均已正确使用 `os.makedirs(..., exist_ok=True)`，问题仅集中于上述两处。

### 解决方案

**`src/utils/logger.py`**

- `setup_logger()` 中先 `os.makedirs("data", exist_ok=True)` 再创建 `FileHandler`；
- 日志文件路径改用 `os.path.join`，并显式指定 `encoding='utf-8'`。

**`src/validator/reporter.py`**

- `ReportGenerator.__init__()` 中 `os.makedirs(self.data_dir, exist_ok=True)`；
- 报告写入显式指定 `encoding="utf-8"` 与 `ensure_ascii=False`（避免中文乱码）。

**`.gitignore`**

- 规则由 `data/` 调整为 `data/*` + `!data/.gitkeep`，使仓库能保留一个占位文件
  `data/.gitkeep` 以维持目录结构，同时继续忽略其中所有运行期产物。

**`README.md`**

- 「启动应用」小节补充说明：首次运行会自动创建 `data/`，无需手动建目录；
- 常见问题表新增「启动报错 `File not found: data/app.log`」与「下载后 data/ 不存在」两条。

### 验证

- 在**不含 `data/` 目录**的干净目录中复现原报错 → 应用修复后 `setup_logger()` 与
  `ReportGenerator` 均正常运行，并自动创建出 `data/`。
- `git add` 验证：仅 `data/.gitkeep` 被跟踪，`app.log` / `ensp_ai.db` / `topology.json` /
  `config_backups/` 仍被忽略。
- 离线测试 114/114 通过。

---

## 11. 文档更新

| 文件 | 更新内容 |
| --- | --- |
| `README.md`（根） | 重写：补充全部新增特性、DeepSeek 默认配置表、快速开始、六步使用说明、测试命令、常见问题排查表 |
| `docs/USER_GUIDE.md` | 重写为 10 节端到端使用说明（环境准备 / 扫描添加设备 / 连接 / 指令解析双视图 / 执行 / 手动与批量命令 / 配置读取备份 / 拓扑发现 / 故障诊断 / 常见问题排查） |
| `docs/FEATURES.md` | 按功能域重排（AI / 设备发现管理 / 配置下发 / 拓扑 / 基础设施），新增项标注 `★ 新增` |
| `docs/ARCHITECTURE.md` | 补充「关键流程」三小节与「设计要点」表；更新 Mermaid 架构图（新增扫描、配置读取、本地模板节点，标注 LLM 回退路径） |
| `docs/STRUCTURE.md` | 更新目录树；新增「关键 API 速查表」（`device_manager` / `command_gen` / `config_executor` / `db`） |
| `docs/README.md` / `PRODUCT_OVERVIEW.md` / `INTRODUCTION.md` | 同步索引与概览 |
| `docs/CHANGELOG.md` | **本文件** |

---

## 12. 测试与验证汇总

### 离线测试套件（不依赖真实设备与网络）

| 测试文件 | 项数 | 覆盖范围 |
| --- | --- | --- |
| `tests/test_device_scan.py` | 29 | 端口扫描、范围纠正、非设备端口识别、主机名与型号解析、批量命名、选择性添加、去重、重命名与会话迁移、DB 元信息、旧库迁移 |
| `tests/test_command_comments.py` | 21 | 六类命令生成、掩码转换、quit 深度注释、查询命令、输出结构 |
| `tests/test_deepseek_mock.py` | 14 | LLM 调用链路 Mock |
| `tests/test_device_manager.py` | 12 | 配置下发兜底、查询兜底、LLDP 三态、退出配置模式、会话恢复 |
| `tests/test_topology.py` | 10 | hostname 归一化、无伪节点、链路正确、幂等、表头过滤 |
| `tests/test_topology_refresh.py` | 28 | 拓扑清空与重建不残留、部分采集移除旧设备、采集失败清空图、历史加载后重建无残留、LLDP 收敛等待、行数统计 |
| **合计** | **114** | **全部通过** |

### 真实环境验证

| 项目 | 结果 |
| --- | --- |
| 真实 DeepSeek 接口连通性 | 12/12 通过（HTTP 200） |
| eNSP 设备扫描（2000–2100） | 8.24 秒发现 3 台设备 |
| 设备配置读取 | 3/3 成功（1305 / 941 / 950 字符） |
| 设备名解析 | R1→OSPF-Core1、R2→R2、R3→R3 |
| 端到端配置下发 | VLAN 与接口 IP 均确认生效 |
| Streamlit 应用启动 | HTTP 200，日志 0 错误 |
| 拓扑刷新（新实验覆盖旧实验） | 28/28 离线测试通过，重建不残留历史节点与链路 |

---

## 13. 改动文件清单

### 代码文件（10 个）

| 文件 | 主要改动 |
| --- | --- |
| `config/config.yaml` | DeepSeek 默认配置、扫描参数、Provider 预设 |
| `src/ai/llm_client.py` | 多 Provider 架构、参数解析重写 |
| `src/ai/intent_parser.py` | JSON 提取容错（四种形态） |
| `src/ai/command_gen.py` | 修复 `{task}` KeyError、补齐 3 个模板、命令注释预览 |
| `src/connection/device_manager.py` | 端口扫描、设备识别、自动命名、名称解析、会话恢复、回显清理、配置读取 |
| `src/executor/config_executor.py` | 设备名解析、未匹配任务记录 |
| `src/topology/builder.py` | 图重置（新增 `clear()`）、hostname 归一化、链路去重、UTF-8 |
| `src/topology/collector.py` | 新增 LLDP 收敛等待 `wait_lldp_convergence()` 与邻居行数统计 |
| `src/topology/visualizer.py` | 中文字体、节点标签 |
| `src/utils/db.py` | 元信息字段、旧库自动迁移 |
| `src/main.py` | 侧边栏扫描 UI、设备卡片、解析双视图、执行结果渲染、配置读取 UI、拓扑页刷新/清空/收敛等待 |

### 提示词文件（2 个）

| 文件 | 改动 |
| --- | --- |
| `config/prompts/intent_parser.txt` | 新增解析规则（必须解析设备名、多设备分别生成、禁止代码块） |
| `config/prompts/command_gen.txt` | 占位符与代码调用对齐 |

### 测试文件（6 个，其中 5 个新增）

| 文件 | 状态 |
| --- | --- |
| `tests/test_device_scan.py` | 新增 |
| `tests/test_command_comments.py` | 新增 |
| `tests/test_deepseek_mock.py` | 新增 |
| `tests/test_topology.py` | 新增 |
| `tests/test_topology_refresh.py` | 新增 |
| `tests/test_device_manager.py` | 现有文件，断言同步当前实现 |
| `tests/test_deepseek.py` | 真实接口测试脚本 |

### 环境与文档

- `.env` / `.env.example`：DeepSeek API Key 注入模板
- `README.md`、`docs/*.md`：全量更新
- `docs/CHANGELOG.md`：本文件
