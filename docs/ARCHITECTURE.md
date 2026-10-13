## 高层架构说明

### 模块划分

- **UI 层（Streamlit）**
  - `main.py`：提供 Web 界面与交互逻辑，含侧边栏设备管理与四个功能标签页。
  - 负责调用下层服务：设备发现、意图解析、命令生成、执行、诊断、拓扑等。

- **连接与会话层**
  - `DeviceManager`：设备信息管理、端口扫描与身份识别、Telnet 连接、会话恢复、命令执行。
  - `SessionPool`：缓存和复用会话，提升多设备场景的执行效率。

- **AI 能力层**
  - `LLMClient`：统一封装 OpenAI 兼容接口（DeepSeek / OpenAI / 第三方兼容）与本地模型（Ollama）。
  - `IntentParser`：自然语言 → 任务列表（JSON），含多格式容错解析。
  - `CommandGenerator`：任务 → VRP 命令序列；内置模板 + LLM 回退；提供带注释的命令预览。
  - `FaultDiagnosis`：设备状态 + 故障描述 → 诊断报告与修复建议。

- **拓扑层**
  - `InfoCollector`：并发采集拓扑相关命令输出。
  - `TopologyBuilder`：根据采集结果构建 NetworkX 拓扑图，归一化主机名与设备键。
  - `visualizer`：将拓扑图渲染为可视化图像在 Web 中展示。

- **执行与验证层**
  - `ConfigExecutor`：按任务分组（含设备名解析）调用命令生成器与设备管理器进行自动下发。
  - `TaskScheduler`：简单任务队列与后台执行框架（预留）。
  - `Tester`：连通性测试与配置验证。
  - `Reporter`：以 JSON 或文本形式生成任务执行报告。

- **基础设施层**
  - `DatabaseManager`：SQLite 持久化设备（含主机名/型号/版本，自动迁移）与任务。
  - `logger`：统一日志配置。
  - `prompt_loader`：加载和管理模型提示词模板。
  - `CLIParser`：基于 TextFSM 的 CLI 输出解析工具。

### 关键流程

#### 1. 自然语言指令解析

```
用户输入中文指令
  → IntentParser.parse()
      → 加载 config/prompts/intent_parser.txt，注入 {topology_context} 与 {user_input}
      → LLMClient.invoke()  ← 唯一一次大模型调用
      → _extract_json() 本地清洗（去代码块围栏 / 兼容数组、单对象、{"tasks":[...]}）
  → 结构化任务列表 [{device, action, parameters}]
```

#### 2. 命令预览与执行

```
结构化任务
  → CommandGenerator.generate_with_comments()   ← 本地规则，不调用大模型
      → 内置模板命中 → 本地生成命令
      → 未命中       → _generate_with_llm() 回退大模型
      → _comment_for() 推导逐条中文注释
  → ConfigExecutor.execute_tasks()
      → resolve_device_name() 解析设备名（R1 → OSPF-Core1）
      → DeviceManager.execute_config_commands() 逐条 timing 下发
      → 结果按设备分组返回，未匹配任务归入 __unresolved__
```

#### 3. eNSP 会话健壮性

```
执行前 recover_session()
  → 若处于分页暂停（---- More ----）→ 连续发送空格翻到底
  → 若处于配置模式（[hostname]）    → 发送 return 退出
执行中
  → 配置命令：逐条 send_command_timing（不依赖提示符正则）
  → 长输出：read_long_output() 自动翻页，空连接提前退出
执行后 _clean_output()
  → 去除分页提示 / ANSI 转义 / 命令回显 / 尾部提示符
```

### 架构示意图（Mermaid）

```mermaid
flowchart LR
    User[用户浏览器\nStreamlit 页面] --> UI[UI 层\nmain.py 侧边栏 + 4 个 Tab]

    UI --> SCAN[设备扫描发现\nscan_devices / scan_and_add]
    UI --> IP[IntentParser\n自然语言解析]
    UI --> CG[CommandGenerator\n命令生成 + 注释]
    UI --> EXE[ConfigExecutor\n配置执行]
    UI --> DIAG[FaultDiagnosis\n故障诊断]
    UI --> TOPO_UI[拓扑可视化\nvisualizer]
    UI --> CFGREAD[配置读取与备份\nexecute_command]
    UI --> TEST[Tester\n连通性/验证]

    IP --> LLM[LLMClient\nDeepSeek / OpenAI兼容 / Ollama]
    CG -->|命中内置模板| LOCAL[本地命令模板]
    CG -->|未命中回退| LLM
    DIAG --> LLM

    SCAN --> DM[DeviceManager\n连接 & 会话池 & 名称解析]
    EXE --> DM
    TEST --> DM
    CFGREAD --> DM
    TOPO_COLLECT[InfoCollector\n并发采集] --> DM
    DIAG --> DM

    DM --> ENSP[eNSP 虚拟设备\n(VRP)]

    TOPO_COLLECT --> TOPO_BUILD[TopologyBuilder\n拓扑构建]
    TOPO_BUILD --> TOPO_UI

    UI --> DB[DatabaseManager\n设备/任务持久化]
    UI --> LOG[Logger\n日志输出]
```

### 设计要点

| 要点 | 说明 |
| --- | --- |
| 大模型调用最小化 | 解析阶段调用一次；命令预览与常见动作类型均由本地模板完成，仅未知动作回退 LLM |
| 设备名解耦 | UI 与结果按任务原名展示，执行时内部解析为真实设备键 |
| 会话自愈 | 执行前后统一恢复分页与配置模式，避免 eNSP 卡死导致后续命令超时 |
| 幂等拓扑 | 每次构建前重置图，避免重复发现产生重复节点/边 |
| 向后兼容 | 数据库结构变更使用 `ALTER TABLE` 补列，旧库可自动迁移 |
