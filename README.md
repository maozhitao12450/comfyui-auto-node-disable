# ComfyUI Auto Node Disable

> 自动追踪 ComfyUI `custom_nodes/` 下各节点模块的使用情况，并在连续若干次工作流提交未被使用时，把它们整体移动到 `custom_nodes/.disabled/<原名>/` 下，等下次重启 ComfyUI 时真正卸载。

把冷门插件"关进小黑屋"，减少节点面板噪声、加快 ComfyUI 冷启动，避免手动挑挑拣拣。

---

## 目录

- [功能特性](#功能特性)
- [安装](#安装)
- [工作原理](#工作原理)
- [配置](#配置)
- [API 与前端面板](#api-与前端面板)
- [安全边界](#安全边界)
- [测试与验证](#测试与验证)
- [项目结构](#项目结构)
- [许可证](#许可证)

---

## 功能特性

- **零依赖**：仅使用 Python 标准库，不引入任何第三方包。
- **自动追踪**：通过 ComfyUI 的 `onprompt` 钩子记录每一次工作流入队实际使用的节点类。
- **滚动窗口决策**：连续 `threshold`（默认 30）次入队中某个 `custom_node` 提供的节点类全部未被使用，则视为可禁用。
- **原子化移动**：禁用采用"先持久化 `pending` → 再移动目录 → 最后落 `confirmed`"的三步流程；移动失败自动回滚状态，避免出现孤儿记录。
- **进程崩溃自愈**：启动时按文件存在与否自动对齐遗留的 `pending` 条目——路径仍在则回滚，路径已不在则升级为 `confirmed`。
- **审计可追溯**：每次入队的 `prompt_id` 透传到 `rounds` 条目；触发禁用的 `disabled` 条目会记录导致它的 `prompt_id`，事后可重建"哪个入队 → 哪个目录被禁用"的因果链。
- **干跑 (dry-run)**：开启后只写审计字段、不移动目录，便于先观察再放开。
- **可恢复**：被禁用的模块可通过 `restore_module` / 前端面板一键搬回 `custom_nodes/`。
- **可配置排除名单**：默认始终保留 `comfyui-auto-node-disable`、`ComfyUI-Manager` 等关键扩展，永不自动禁用。

---

## 安装

把本仓库克隆到 ComfyUI 的 `custom_nodes/` 目录下即可：

```bash
cd ComfyUI/custom_nodes/
git clone https://github.com/maozhitao12450/comfyui-auto-node-disable.git
```

或直接把仓库内的 `__init__.py`、`auto_disable.py`、`web/` 等文件复制到 `custom_nodes/comfyui-auto-node-disable/` 下。

> **重启 ComfyUI** 才会真正卸载已被禁用的模块（节点注册发生在启动阶段）。

不需要任何额外依赖，也不需要修改 ComfyUI 本身的代码。

---

## 工作原理

### 1. 反向构建 `custom_node → 节点类` 映射

插件启动时遍历 ComfyUI 全局 `NODE_CLASS_MAPPINGS`，读取每个节点类上的 `RELATIVE_PYTHON_MODULE` 属性（形如 `"custom_nodes.<module_name>"`），反推出：

```
custom_nodes/<module_name> → 它注册的节点类集合
```

> 同时兼容 `comfy_entrypoint` 返回 `ComfyExtension` 的 V3 注册方式——它们最终也会落到 `NODE_CLASS_MAPPINGS`。

### 2. 记录每次工作流入队

`PromptServer.add_on_prompt_handler` 钩子会拿到 `/prompt` API 的完整请求体：

```json
{
  "prompt": { "<node_id>": { "class_type": "...", "inputs": {...} } },
  "client_id": "...",
  "extra_data": { ... },
  "prompt_id": "..."
}
```

插件从中提取本次工作流实际使用的节点类名，写入状态文件的 `rounds` 滚动窗口（默认保留足够多轮次，并透传 `prompt_id`）。

### 3. 决策与禁用

每次入队后都立即做一次决策：

- 取最近 `threshold` 轮所有入队的节点类**并集**；
- 对每个已知的 `custom_node` 模块，若它提供的所有节点类与并集**互不相交**，即视为可禁用；
- 默认排除名单内的模块**永远跳过**。

执行动作时按下面三步走，保证"状态先于移动"：

```
1. 在 state.disabled[m] 上写 status="pending" 并立即落盘
2. 实际把目录移动到 custom_nodes/.disabled/<原名>/
3a. 移动成功 → status="confirmed"，落盘
3b. 移动失败 → 从 disabled 中移除该模块并落盘（回滚）
```

### 4. 进程崩溃自愈（`_reconcile_pending`）

启动时若读到 `status == "pending"` 的条目，按文件存在与否收敛：

- **原路径仍在** → 移动未发生，回滚该记录（避免孤儿状态）；
- **原路径已不在** → 移动实际成功，升级为 `confirmed`。

### 5. 状态文件位置

默认放在 ComfyUI 根目录下：

```
ComfyUI/
├── auto_node_disable_state.json   ← 插件的状态文件
├── custom_nodes/
│   ├── comfyui-auto-node-disable/
│   ├── ComfyUI-Manager/
│   ├── .disabled/                  ← 已被自动禁用的模块暂存区
│   │   └── <module_name>/
│   └── ...
```

---

## 配置

所有运行时配置都通过 HTTP API 或状态文件调整，**不需要重启 ComfyUI** 即可生效（仅 `restore` 需要重启加载节点）。

### 配置项

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `threshold` | `30` | 连续多少轮入队未使用即触发禁用；`0` 关闭自动禁用 |
| `dry_run` | `false` | `true` 时只写审计字段、不移动目录 |
| `exclude` | `["comfyui-auto-node-disable", "ComfyUI-Manager"]` | 永不自动禁用的模块名列表 |

### 通过 HTTP API 调整

```bash
# 查看当前状态
curl http://localhost:8188/auto_disable/status

# 调整阈值
curl -X POST http://localhost:8188/auto_disable/threshold \
     -H 'Content-Type: application/json' \
     -d '{"value": 5}'

# 更新排除名单
curl -X POST http://localhost:8188/auto_disable/exclude \
     -H 'Content-Type: application/json' \
     -d '{"names": ["comfyui-auto-node-disable", "ComfyUI-Manager", "my-essential-pack"]}'

# 恢复某个被禁用的模块
curl -X POST http://localhost:8188/auto_disable/restore \
     -H 'Content-Type: application/json' \
     -d '{"module": "some-disabled-pack"}'
```

> 调整后**需要重启 ComfyUI**，被恢复/被禁用的模块才会真正加载或卸载。

### 通过状态文件手动编辑

直接编辑 `ComfyUI/auto_node_disable_state.json`：

```json
{
  "threshold": 30,
  "dry_run": false,
  "exclude": ["comfyui-auto-node-disable", "ComfyUI-Manager"],
  "known_modules": { "...": "..." },
  "rounds": [],
  "disabled": {}
}
```

JSON 写入是**原子化**的（先写 `.tmp` 再 `os.replace`），可以安全地手工编辑。

---

## API 与前端面板

### HTTP 端点

| Method | Path | Body | 说明 |
| --- | --- | --- | --- |
| `GET` | `/auto_disable/status` | — | 返回当前状态的深拷贝快照 |
| `POST` | `/auto_disable/restore` | `{"module": "<name>"}` | 把 `.disabled/<name>` 搬回 `custom_nodes/` |
| `POST` | `/auto_disable/threshold` | `{"value": <int>}` | 调整决策阈值 |
| `POST` | `/auto_disable/exclude` | `{"names": [...]}` | 更新排除名单 |

### 前端面板

`web/js/autoDisable.js` 会在 ComfyUI 的 **Settings** 面板下注入一个菜单项，提供：

- 当前已知模块 / 已禁用模块列表；
- 调整阈值与排除名单；
- 一键恢复被禁用模块。

---

## 安全边界

为了让"目录移动"这种破坏性副作用可控，插件内置以下边界：

| 边界 | 说明 |
| --- | --- |
| **干跑模式** | `dry_run=true` 时只写审计字段，不动目录。先观察再放开。 |
| **`prompt_id` 审计** | 每条 `rounds` 与 `disabled` 都带上触发它的 `prompt_id`，方便事后回溯。 |
| **三步原子化** | 禁用决策必须**先持久化为 `pending`**，再移动目录，最后落 `confirmed`；顺序倒过来会导致"目录已搬走但状态没记录"。 |
| **移动失败回滚** | `_disable_module` 返回 `False` 时立即从 `disabled` 中移除该模块并落盘，避免"未移动却标 confirmed"。 |
| **进程崩溃自愈** | `_reconcile_pending` 启动时按文件存在与否收敛遗留 `pending` 条目。 |
| **去重** | 已在 `disabled`（任意 status）中的模块不会被二次决策。 |
| **JSON 原子写** | 状态文件先写 `.tmp` 再 `os.replace`，避免半写文件导致损坏。 |
| **线程安全** | 所有状态读写都在 `_state_lock` (RLock) 内串行化。 |
| **排除名单** | 关键模块（如 `ComfyUI-Manager`、本插件自身）默认永远不被禁用。 |

---

## 测试与验证

### 单元测试（pytest）

```bash
# 推荐：自动发现 tests/ 下所有测试
python -m pytest tests/ -v

# 也可用标准库 unittest
python -m unittest tests.test_auto_disable -v
```

测试覆盖 4 个用户指定维度：

1. 阈值判定（含 `threshold=0/-1/1/3` + `exclude` + `dry_run`）
2. 轮次不足时的行为
3. 恢复回退（异常路径 + disable/restore roundtrip）
4. 窗口修剪（`rounds` 超 `keep*4` 时被截断）

并补全了产品当前实现的额外安全边界：

- dry-run 干跑模式
- 三步原子化的 `pending → confirmed / rollback`
- 启动时 `_reconcile_pending` 对齐 pending 状态与文件位置
- 重复入队与 `prompt_id` 审计字段

### 热路径聚焦检查（verify_hot_path.py）

脱离 ComfyUI 宿主，跑通 5 个关键场景：

```bash
python verify_hot_path.py
```

| Scenario | 验证内容 |
| --- | --- |
| 1 | 干跑模式只写审计，不动目录 |
| 2 | 保存失败时状态与目录保持一致（移动不发生） |
| 3 | 重复入队只产生一条 confirmed 条目 |
| 4 | `restore_module` 能把目录搬回并清理状态 |
| 5 | 启动对齐：遗留 pending 按文件存在与否自动收敛 |

### 变更门禁（make gate）

静态门禁 + 修订级验收记录：

```bash
# Windows PowerShell 下
make check     # 仅校验
make gate      # 校验 + 写入 tools/check_log.jsonl
```

会做：

- Python / JSON 语法校验
- `auto_disable.py` 必填 API 表面检查
- 热路径函数存在性核对
- 前端 JS 语法 best-effort 校验（依赖 `node`）

---

## 项目结构

```
comfyui-auto-node-disable/
├── __init__.py              # 入口：注册 onprompt 钩子与 /auto_disable/* 路由
├── auto_disable.py          # 核心：决策、状态、移动、恢复
├── verify_hot_path.py       # 热路径 5 个场景的聚焦验证（脱离 ComfyUI 宿主）
├── pyproject.toml           # 项目元数据 + [tool.comfy] 扩展声明
├── Makefile                 # make check / make gate 门禁入口
├── tools/
│   └── change_check.py      # 静态门禁：语法 + API 表面 + 热路径核对
├── scripts/
│   └── diagnose.py          # 独立诊断脚本：复现与排查现场
├── tests/
│   └── test_auto_disable.py # pytest 测试网格（4 个用户维度 + 4 个补充安全边界）
└── web/
    └── js/
        └── autoDisable.js   # 前端 Settings 面板桥接
```

---

## 许可证

[MIT](./LICENSE) © 2026 zhitao
