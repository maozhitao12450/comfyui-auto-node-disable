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
- **缺失节点自动恢复**：提交工作流时若发现节点类在当前 `NODE_CLASS_MAPPINGS` 中不存在，会去 `.disabled/` 里查找能提供这些类名的模块并自动搬回 `custom_nodes/`，随后通过状态文件 + 前端弹出"请重启 ComfyUI"提示。
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

### 5. 反向能力：缺失节点自动恢复

当用户提交了一个工作流，其中包含 `class_type` 在当前 `NODE_CLASS_MAPPINGS` 中**找不到**的节点（多半是被自动禁用导致），插件会：

1. 用当前已注册节点类与工作流用到的节点类做差集，得到 `missing` 集合；
2. 扫描 `state["disabled"]`（`status == "confirmed"` 的条目）里 `node_classes` 与 `missing` 有交集的模块；
3. 用与禁用相同的三步原子流程把 `.disabled/<name>/` 搬回 `custom_nodes/<name>/`，写 `pending_restart` 条目；
4. 搬回成功后，浏览器端在 `api.queuePrompt` 响应后调用 `/auto_disable/pending_restart`，弹窗提示"请重启 ComfyUI"。

> 这一步是在 `_decide` 之前做的：如果刚好一次提交就触发了"恢复 + 再次禁用"的循环，会优先保证被搬回的模块留在原位。

### 6. 状态文件位置

默认放在 **插件目录** 内（与 `auto_disable.py` 同目录）：

```
ComfyUI/
├── custom_nodes/
│   ├── comfyui-auto-node-disable/
│   │   ├── auto_disable.py
│   │   ├── auto_node_disable_state.db     ← SQLite 状态文件（与代码同目录）
│   │   ├── auto_node_disable_state.db-wal ← WAL 模式辅助文件（运行时）
│   │   └── auto_node_disable_state.db-shm ← WAL 模式辅助文件（运行时）
│   ├── ComfyUI-Manager/
│   ├── .disabled/                          ← 已被自动禁用的模块暂存区
│   │   └── <module_name>/
│   └── ...
```

> 旧版（v0.x 及更早）状态文件位于 `ComfyUI/auto_node_disable_state.json`。
> 升级后第一次启动会自动迁移到该位置（以新为准），旧 JSON 文件会被归档为
> `auto_node_disable_state.json.migrated`。如需手动清理，删除旧位置的 JSON 即可。

### 7. 已知模块（`known_modules`）的同步策略

`state["known_modules"]` 记录所有“当前仍可用”的 `custom_node` 模块与它们提供的节点类。它通过以下流程保持同步：

- **启动时一次性扫描**：进程启动时首次 ``_load_state`` 调用会执行一次全量扫描，把结果写入 SQLite；之后 ``record_prompt`` 不再每轮重扫，避免每条 prompt 都遍历 ``NODE_CLASS_MAPPINGS``。
- **物理 disable 后从 `known_modules` 移除**：模块被搬到 `.disabled/` 后会从 `known_modules` 移除，避免下一轮决策把它再次视为待禁用候选。
- **自动恢复后回填 `known_modules`**：`restore_for_missing_classes` 触发模块移回 `custom_nodes/` 时会立即把该模块加入 `known_modules`，让下一轮决策能把它当作“已知且当前可用”。
- **运行时手动刷新**：通过 HTTP API 或 Python 入口调用 ``refresh_known_modules``，可强制重新扫描 ``NODE_CLASS_MAPPINGS``，适配“运行时新装/卸载模块”场景。

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

直接编辑 `custom_nodes/comfyui-auto-node-disable/auto_node_disable_state.json`：

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
| `POST` | `/auto_disable/pending_restart` | `{}` | 拉取并清空“待重启”提示列表（前端在 `api.queuePrompt` 之后调用） |
| `POST` | `/auto_disable/refresh_known` | `{}` | 运行时强制刷新 `state["known_modules"]`，适配“运行中新装/卸载模块”场景 |

### 前端面板

插件通过 `web/js/autoDisable.js` 在 ComfyUI 里暴露两个入口：

**1) Settings 对话框（齿轮菜单 → ComfyUI Settings → 搜索 "Auto Node Disable"）**

使用 `app.registerExtension({ settings: [...] })` 注册三个标准设置项，能被搜索、按 ID 引用，并自动持久化到浏览器本地存储：

| 设置项 ID | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `AutoNodeDisable.Threshold` | `number` | `30` | 连续未使用次数阈值；调整为 `0` 关闭自动禁用 |
| `AutoNodeDisable.Exclude` | `text` | `comfyui-auto-node-disable,ComfyUI-Manager` | 逗号分隔的永不自动禁用模块列表 |
| `AutoNodeDisable.DryRun` | `boolean` | `false` | 开启后只写审计字段、不移动目录 |

这三项变更都会通过 `onChange` 实时调用 `/auto_disable/threshold` / `/auto_disable/exclude` 后端 API 同步落盘。

**2) 顶栏 "Auto Disable" 按钮（点击后弹出浮窗）**

页面上找不到 Settings 时可以走这条路——脚本会向 ComfyUI 顶栏（`.comfy-menu` / `.litegraph-toolbar` / `body`）追加一个按钮，点击后弹出浮窗，包含：

- 当前阈值 / dry_run / 排除列表速览；
- 已自动禁用模块列表，每个模块带"恢复"按钮（恢复后需重启 ComfyUI）；
- 已知 custom_node 模块 JSON 折叠面板（调试用）。

> 浮窗是按需渲染的，首次点击时才拉取 `/auto_disable/status`，不会拖慢启动。

**3) 缺失节点自动恢复后的“请重启”提示**

脚本会一次性 `monkey patch` `api.queuePrompt`：每次提交工作流后异步调用 `/auto_disable/pending_restart`，若返回的列表非空，就调 `alert` 报示：

```
检测到缺失节点，已从 .disabled/ 自动恢复 N 个模块：module_a, module_b
请重启 ComfyUI 后这些节点才会被加载生效。
```

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

测试覆盖 5 个用户指定维度：

1. 阈值判定（含 `threshold=0/-1/1/3` + `exclude` + `dry_run`）
2. 轮次不足时的行为
3. 恢复回退（异常路径 + disable/restore roundtrip）
4. 窗口修剪（`rounds` 超 `keep*4` 时被截断）
5. 缺失节点自动恢复（submit 引用未注册类 → .disabled 匹配 → 原子恢复 → `pending_restart` 提示与消费；含拒绝/忽略/原子化等边界）

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

### 独立诊断（scripts/diagnose.py）

```bash
python scripts/diagnose.py
```

面向现场的诊断脚本，会依执行**正常 / 恢复 / 失败 / 审计**四个场景，打印中间状态与最终预期，供人与机器两边验证。主要检查：

- `_scan_known_modules` 能反推 `custom_node` 模块名与节点类集合；
- `rounds<threshold` 时决策对未用模块按全窗口评估（不“等满阈值”）；
- 状态在恢复、失败、审计三个场景下都不出现孤儿记录。

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
