## 项目目录结构说明

```text
netpilot-ai/
├─ README.md                # 项目总览与使用说明（中文）
├─ requirements.txt         # Python 依赖列表
├─ .env.example             # 环境变量示例（LLM API Key 等）
├─ .env                     # 本地环境变量（不应提交到版本库）
│
├─ config/                  # 配置与提示词
│  ├─ config.yaml           # 应用主配置（连接/扫描、LLM、数据库、拓扑）
│  └─ prompts/              # 大模型提示词模板
│     ├─ intent_parser.txt      # 自然语言 → 结构化任务
│     ├─ command_gen.txt        # 任务 → VRP 命令
│     └─ fault_diagnosis.txt    # 故障诊断
│
├─ src/                     # 核心源码
│  ├─ main.py               # Streamlit Web 应用入口（侧边栏 + 4 个标签页）
│  │
│  ├─ ai/                   # AI 能力层
│  │  ├─ llm_client.py      # LLM 调用封装（DeepSeek / OpenAI 兼容 / Ollama）
│  │  ├─ intent_parser.py   # 自然语言 → 任务列表（含多格式容错解析）
│  │  ├─ command_gen.py     # 任务 → VRP 命令 + 带注释的命令预览
│  │  ├─ rule_parser.py     # 本地规则解析（不依赖 AI 的兜底解析器）
│  │  └─ fault_diagnosis.py # 故障诊断与修复建议
│  │
│  ├─ connection/           # 设备连接与会话管理
│  │  ├─ device_manager.py  # 设备信息、端口扫描、身份识别、名称解析、
│  │  │                     # 连接/会话恢复、命令执行、配置读取
│  │  └─ session_pool.py    # 连接会话池（复用连接）
│  │
│  ├─ topology/             # 拓扑采集与构建
│  │  ├─ collector.py       # 并发采集拓扑相关命令输出
│  │  ├─ builder.py         # 基于采集数据构建 NetworkX 拓扑图（幂等）
│  │  └─ visualizer.py      # 使用 matplotlib + Streamlit 可视化拓扑
│  │
│  ├─ executor/             # 配置执行与任务调度
│  │  ├─ config_executor.py # 按任务分组（含设备名解析）生成并下发配置
│  │  └─ task_scheduler.py  # 简单任务队列与后台执行（预留）
│  │
│  ├─ validator/            # 测试与验证
│  │  ├─ tester.py          # ping/traceroute/show 验证
│  │  └─ reporter.py        # 生成 JSON / 文本报告
│  │
│  └─ utils/                # 通用工具
│     ├─ db.py              # SQLite 数据库访问（设备元信息自动迁移）
│     ├─ logger.py          # 日志配置（stdout + data/app.log）
│     ├─ prompt_loader.py   # 提示词模板加载
│     └─ parsers.py         # TextFSM CLI 解析工具
│
├─ data/                    # 运行期数据（自动生成）
│  ├─ app.log               # 应用日志
│  ├─ ensp_ai.db            # SQLite 数据库（设备 / 任务）
│  ├─ topology.json         # 最近保存的拓扑结构
│  ├─ config_backups/       # 设备配置备份（<设备名>_config_<时间戳>.cfg）
│  └─ session_logs/         # 设备会话日志（按设备 + 时间命名）
│
├─ tests/                   # 离线测试套件（无需真实设备与网络）
│  ├─ test_device_scan.py       # 端口扫描与设备命名（29 项）
│  ├─ test_command_comments.py  # 命令生成与注释（21 项）
│  ├─ test_device_manager.py    # 连接/会话恢复/配置下发（12 项）
│  ├─ test_topology.py          # 拓扑构建与幂等性（10 项）
│  ├─ test_deepseek_mock.py     # LLM 调用链路 mock（14 项）
│  └─ test_deepseek.py          # 真实 DeepSeek 连通性（需 API Key，消耗额度）
│
└─ docs/                    # 项目文档
   ├─ README.md             # 文档总览与索引
   ├─ PRODUCT_OVERVIEW.md   # 产品说明索引入口
   ├─ INTRODUCTION.md       # 产品简介与场景
   ├─ FEATURES.md           # 核心特性列表
   ├─ ARCHITECTURE.md       # 高层架构、关键流程与模块划分
   ├─ USER_GUIDE.md         # 端到端使用说明（重点）
   ├─ CHANGELOG.md          # 修订说明与新增功能清单
   └─ STRUCTURE.md          # 本文件：目录结构与职责说明
```

### 关键 API 速查

**`src/connection/device_manager.py`**

| 方法 | 说明 |
| --- | --- |
| `scan_devices(ip, start_port, end_port, max_workers, probe_device)` | 两阶段并发端口扫描，返回开放端口与设备识别结果 |
| `scan_and_add(ip, start_port, end_port, selected_ports, name_prefix, use_hostname, scanned)` | 按主机名/前缀命名并批量添加设备 |
| `refresh_device_name(name)` | 读取主机名并重命名设备（迁移会话与元数据） |
| `resolve_device_name(name)` | 把任务中的设备名解析为真实设备键 |
| `list_devices()` | 返回已注册设备信息列表（副本） |
| `connect(name)` / `disconnect(name)` / `get_all_connected()` | 连接管理 |
| `execute_command(name, command)` | 执行查询命令（自动处理分页与长输出） |
| `execute_config_commands(name, commands)` | 逐条 timing 下发配置命令 |
| `execute_batch(device_names, commands, is_config)` | 多设备并发执行 |
| `ensure_lldp_enabled(name)` | 检查并尝试启用设备 LLDP |
| `recover_session(conn, name)` | 恢复会话（解除分页 / 退出配置模式） |

**`src/ai/command_gen.py`**

| 方法 | 说明 |
| --- | --- |
| `generate(task)` | 任务 → 命令列表（内置模板 + LLM 回退） |
| `generate_with_comments(task)` | 任务 → `[{command, comment}]`，供 UI 预览注释 |

**`src/executor/config_executor.py`**

| 方法 | 说明 |
| --- | --- |
| `execute_tasks(tasks)` | 按设备分组执行任务；未匹配设备归入 `__unresolved__` |
| `execute_raw_commands(device, commands)` | 直接下发原始命令列表 |

**`src/utils/db.py`**

| 方法 | 说明 |
| --- | --- |
| `add_device(name, ip, port, device_type, hostname, model, version)` | 同名则更新，否则插入 |
| `get_devices()` / `get_devices_with_meta()` | 读取设备列表（后者含主机名/型号/版本） |
| `delete_device(name)` | 删除设备 |

### 建议阅读顺序

1. `README.md`：快速了解项目、启动方式与基本使用。
2. `docs/INTRODUCTION.md`：理解产品定位和适用场景。
3. `docs/USER_GUIDE.md`：跟随步骤实际操作产品（**使用说明重点**）。
4. `docs/FEATURES.md`：掌握完整功能清单。
5. `docs/ARCHITECTURE.md`：了解整体架构、关键流程与模块间关系。
6. `docs/STRUCTURE.md`：查阅目录结构与关键 API（本文件）。
