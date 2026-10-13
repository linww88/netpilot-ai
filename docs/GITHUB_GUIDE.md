## GitHub Fork 与代码上传指南

本项目（**NetPilot eNSP AI 网络助手**，仓库 `netpilot-ai`）基于 GitHub 开源项目 **eNSP AI Assistant** 二次开发而来。
本文档说明如何 **Fork 原仓库 → 在本地修订 → 上传到自己的 GitHub 仓库**，以及后续如何同步上游更新。

> 名词说明
> - **fork（派生/复刻）**：把别人的仓库完整复制一份到你的 GitHub 账号下，你拥有完全写权限，可自由修改，同时保留与原仓库的关联。
> - **origin**：你自己 Fork 出来的仓库（推送目标）。
> - **upstream**：原始的官方仓库（只读来源，用于拉取更新）。

---

### 一、前置准备

1. 注册/登录 GitHub 账号：<https://github.com>
2. 安装 Git：<https://git-scm.com/downloads>（Windows 安装时默认下一步即可）
3. 配置 Git 身份（只需一次）：

```bash
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"
```

4. （推荐）配置 SSH 或使用 HTTPS。
   - 使用 HTTPS：推送时会要求输入用户名 + **Personal Access Token**（见"六、常见问题"）。
   - 使用 SSH：执行 `ssh-keygen -t ed25519 -C "你的邮箱"` 并在 GitHub 的 Settings → SSH and GPG keys 中粘贴公钥。

---

### 二、Fork 原仓库（在 GitHub 网页上操作）

1. 打开原始项目页面，例如：
   `https://github.com/<原作者用户名>/ensp-ai-assistant`
2. 点击右上角的 **Fork** 按钮 → 选择你的账号 → **Create fork**。
3. 完成后你会跳转到：
   `https://github.com/<你的用户名>/ensp-ai-assistant`
   这就是你专属的副本，可以随意提交。

> 若原仓库不在 GitHub 或已无法访问，也可跳过 Fork，直接走"新建空仓库 + 推送本地代码"的方式（见第四节末的备选方案）。

---

### 三、重命名仓库为 netpilot-ai

Fork 出来的仓库默认沿用原名。改成新名字：

1. 进入你的 Fork 仓库 → **Settings**（设置）
2. 在 **Repository name** 一栏把 `ensp-ai-assistant` 改为 `netpilot-ai`
3. 点击 **Rename**

> 重命名会自动把旧地址重定向到新地址，GitHub 上已有的 star / fork 不受影响。

---

### 四、把代码拉到本地并关联远程

#### 4.1 克隆你 Fork 后的仓库

```bash
git clone https://github.com/<你的用户名>/netpilot-ai.git
cd netpilot-ai
```

#### 4.2 关联上游仓库（便于以后同步官方更新）

```bash
git remote add upstream https://github.com/<原作者用户名>/ensp-ai-assistant.git
git remote -v          # 确认：origin 是你的，upstream 是原作者的
```

预期输出：

```
origin    https://github.com/<你的用户名>/netpilot-ai.git (fetch)
origin    https://github.com/<你的用户名>/netpilot-ai.git (push)
upstream  https://github.com/<原作者用户名>/ensp-ai-assistant.git (fetch)
upstream  https://github.com/<原作者用户名>/ensp-ai-assistant.git (push)
```

#### 4.3 若已有本地项目（如你现在这份）

在项目根目录执行：

```bash
git init
git remote add origin https://github.com/<你的用户名>/netpilot-ai.git
git remote add upstream https://github.com/<原作者用户名>/ensp-ai-assistant.git
```

> **备选方案**：如果原仓库无法 Fork，可先在 GitHub 上 **New repository** 建一个名为 `netpilot-ai` 的空仓库（不要勾选 README），再按 `git remote add origin ...` 关联后直接推送。

---

### 五、提交本地修订并上传

#### 5.1 确认忽略规则

项目根目录的 `.gitignore` 已配置好，会自动排除：
`data/`（数据库、日志、备份、拓扑文件）、`.env`（含 API Key）、`.workbuddy/`、`__pycache__/` 等。

**务必确认 `.env` 不会被提交**（里面有 DeepSeek API Key）：

```bash
git status          # 检查待提交列表，不应出现 .env / data/
```

#### 5.2 添加、提交、推送

```bash
git add .
git commit -m "feat: 二次开发 NetPilot eNSP AI 网络助手（DeepSeek/拓扑刷新/命令注释等）"

git branch -M main            # 确保分支名为 main
git push -u origin main       # 首次推送并建立跟踪
```

之后每次修改只需：

```bash
git add .
git commit -m "你的提交说明"
git push
```

#### 5.3 保留上游历史（推荐）

如果希望你的仓库与上游共享 commit 历史（这样 GitHub 会显示 "forked from ..."，未来也能合并上游更新），
更规范的做法是：先 clone 上游 → 改 remote 为自己的 → 再推送：

```bash
git clone https://github.com/<原作者用户名>/ensp-ai-assistant.git netpilot-ai
cd netpilot-ai
git remote rename origin upstream
git remote add origin https://github.com/<你的用户名>/netpilot-ai.git
# 把本地这份项目的改动复制进来（保留 .git 目录），然后：
git add .
git commit -m "feat: 二次开发 ..."
git push -u origin main
```

---

### 六、后续同步上游更新（可选）

当原项目发布新版本时，可以把官方改动合并进来：

```bash
git fetch upstream                 # 拉取上游最新提交
git checkout main
git merge upstream/main            # 或 git rebase upstream/main
git push origin main               # 推回你自己的仓库
```

若发生冲突，按提示手动编辑冲突文件 → `git add` → `git commit` 即可。

---

### 七、常见问题（FAQ）

| 问题 | 解决办法 |
| --- | --- |
| 推送要密码，输入密码报 403 | GitHub 已禁用密码推送，需用 **Personal Access Token**：Settings → Developer settings → Personal access tokens → Generate new token（勾选 `repo`），用 Token 当密码 |
| `remote origin already exists` | 先删除：`git remote remove origin`，再重新 `add` |
| 误把 `.env`（含 Key）提交了 | 立即到 GitHub 删除该文件并在平台**吊销/重置 API Key**；然后 `git rm --cached .env`，确认 `.gitignore` 已含 `.env`，重新提交 |
| 文件太大推不上去（>100MB） | 先把大文件加入 `.gitignore`；已提交的用 `git rm --cached <file>` 移除；必要时用 Git LFS |
| 显示 "This branch is behind..." | 说明上游有新提交，按第六节 `fetch + merge upstream/main` |
| 想彻底重新开始 | 删除本地 `.git` 目录后重新 `git init`；或在 GitHub 建新仓库重推 |
| 换行符警告 `LF will be replaced by CRLF` | Windows 正常提示，可忽略；或设置 `git config --global core.autocrlf true` |

---

### 八、上传前后的检查清单

- [ ] `.env` 已加入 `.gitignore`，`git status` 中不出现
- [ ] `data/`、`.workbuddy/`、`__pycache__/` 未被提交
- [ ] `README.md` 已说明本项目与上游的二次开发关系
- [ ] 仓库名已改为 `netpilot-ai`
- [ ] `git remote -v` 中 `origin` 指向自己的仓库，`upstream` 指向原仓库
- [ ] 首次推送成功后，在 GitHub 网页确认文件与 commit 记录正常

---

### 九、本项目建议的仓库信息

| 项目 | 建议值 |
| --- | --- |
| 仓库名 | `netpilot-ai` |
| 中文名 | NetPilot eNSP AI 网络助手 |
| 描述（Description） | 基于 eNSP AI Assistant 二次开发：自然语言驱动华为 eNSP 设备配置、拓扑自动发现与故障诊断 |
| 主题标签（Topics） | `ensp` `huawei` `vrp` `ai` `llm` `deepseek` `network-automation` `streamlit` |
| 许可证 | 沿用上游 LICENSE（如为 MIT，请保留原作者版权声明） |

> **开源合规提示**：二次开发请遵守上游许可证条款，保留原作者的 LICENSE 与版权声明，
> 并在 README 中注明"基于 XXX 项目二次开发"，这是开源社区的基本礼仪。
