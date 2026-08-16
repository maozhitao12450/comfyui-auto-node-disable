/**
 * ComfyUI Auto Node Disable - 前端桥接脚本
 * --------------------------------------------------
 * 在 ComfyUI 中暴露两类 UI 入口：
 *
 *  1) Settings 对话框（齿轮菜单 → ComfyUI Settings）
 *     使用 ``app.registerExtension({ settings: [...] })`` 注册三个标准设置项：
 *       - AutoNodeDisable.Threshold  : 连续未使用次数阈值（默认 30）
 *       - AutoNodeDisable.Exclude    : 永不自动禁用的模块名列表（逗号分隔）
 *       - AutoNodeDisable.DryRun     : 仅审计，不移动目录
 *     这三项会出现在 ComfyUI 的 Settings 对话框里，能被搜索、按 ID 引用，
 *     并通过 ``onChange`` 实时同步到后端状态文件。
 *
 *  2) 顶栏 "Auto Disable" 按钮（点击后弹出浮窗）
 *     用于展示当前已自动禁用的模块列表，并支持一键恢复。
 *     浮窗是按需渲染的，避免一上来就拉一次 /status。
 *
 * 后端 API（由 __init__.py 注册）：
 *   GET  /auto_disable/status
 *   POST /auto_disable/restore   {"module": "<name>"}
 *   POST /auto_disable/threshold {"value": <int>}
 *   POST /auto_disable/exclude   {"names": ["..."]}
 *   POST /auto_disable/pending_restart   取走并清空 待重启 提示列表
 *
 * 自动恢复流程：
 *   1. 提交工作流时若发现某个 class_type 在当前 NODE_CLASS_MAPPINGS 中缺失，
 *      后端去 .disabled 找能提供该类的模块并自动移回 custom_nodes。
 *   2. 这次恢复会写入 state.pending_restart，前端在 prompt 响应后调用
 *      pending_restart 端点拉取并弹 请重启 ComfyUI 提示。
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const ENDPOINT = "/auto_disable";

// ---------------------------------------------------------------------------
// 网络工具
// ---------------------------------------------------------------------------

async function fetchStatus() {
    try {
        const r = await api.fetchApi(`${ENDPOINT}/status`);
        if (!r.ok) {
            return null;
        }
        return await r.json();
    } catch (e) {
        console.warn("[auto_disable] status fetch failed:", e);
        return null;
    }
}

async function postJSON(path, body) {
    try {
        const r = await api.fetchApi(`${ENDPOINT}${path}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        return await r.json();
    } catch (e) {
        console.warn("[auto_disable] POST failed:", e);
        return { ok: false, error: String(e) };
    }
}

// ---------------------------------------------------------------------------
// 浮窗渲染（顶栏按钮触发）
// ---------------------------------------------------------------------------

function clearChildren(node) {
    while (node.firstChild) {
        node.removeChild(node.firstChild);
    }
}

function renderPanel(root, status) {
    clearChildren(root);

    const title = document.createElement("h3");
    title.textContent = "Auto Node Disable — 已禁用模块";
    title.style.margin = "0 0 8px 0";
    root.appendChild(title);

    const desc = document.createElement("p");
    desc.style.opacity = "0.8";
    desc.style.fontSize = "12px";
    desc.textContent =
        "每次提交工作流都会记录用到的节点；连续 N 次（阈值在 Settings 中配置）" +
        "都未出现的 custom_node 会被移动到 custom_nodes/.disabled/<名称>/ 里。" +
        "恢复后需要重启 ComfyUI 才生效。";
    root.appendChild(desc);

    // 当前阈值 / 排除列表 / dry-run 速览
    const summary = document.createElement("div");
    summary.style.fontSize = "12px";
    summary.style.opacity = "0.85";
    summary.style.margin = "8px 0";
    summary.innerHTML =
        `<div>当前阈值：<b>${status?.threshold ?? "?"}</b></div>` +
        `<div>dry_run：<b>${status?.dry_run ? "ON" : "OFF"}</b></div>` +
        `<div>排除列表：<code>${(status?.exclude || []).join(", ") || "(空)"}</code></div>`;
    root.appendChild(summary);

    // 已禁用模块列表
    const list = document.createElement("div");
    list.style.marginTop = "12px";
    const h = document.createElement("h4");
    h.textContent = "已自动禁用：";
    list.appendChild(h);
    const disabled = status?.disabled || {};
    const entries = Object.entries(disabled);
    if (entries.length === 0) {
        const empty = document.createElement("div");
        empty.style.opacity = "0.7";
        empty.textContent = "（暂无）";
        list.appendChild(empty);
    } else {
        for (const [name, info] of entries) {
            const row = document.createElement("div");
            row.style.display = "flex";
            row.style.justifyContent = "space-between";
            row.style.alignItems = "center";
            row.style.padding = "4px 0";
            row.style.borderBottom = "1px dashed #666";
            const left = document.createElement("div");
            left.style.fontFamily = "monospace";
            left.style.fontSize = "12px";
            left.textContent =
                `${name}  [${info?.status || "?"}]` +
                (info?.original_path ? `  (${info.original_path})` : "");
            const restore = document.createElement("button");
            restore.textContent = "恢复";
            restore.onclick = async () => {
                const r = await postJSON("/restore", { module: name });
                if (r && r.ok) {
                    restore.textContent = "已恢复（请重启 ComfyUI）";
                    restore.disabled = true;
                } else {
                    restore.textContent = "失败：" + (r?.error || "未知错误");
                }
            };
            row.appendChild(left);
            row.appendChild(restore);
            list.appendChild(row);
        }
    }
    root.appendChild(list);

    // 已知模块概览
    const km = document.createElement("details");
    km.style.marginTop = "12px";
    const sumEl = document.createElement("summary");
    sumEl.textContent =
        `已知 custom_node 模块（${Object.keys(status?.known_modules || {}).length}）`;
    km.appendChild(sumEl);
    const inner = document.createElement("pre");
    inner.style.fontSize = "11px";
    inner.style.maxHeight = "220px";
    inner.style.overflow = "auto";
    inner.style.background = "rgba(255,255,255,0.04)";
    inner.style.padding = "6px";
    inner.textContent = JSON.stringify(status?.known_modules || {}, null, 2);
    km.appendChild(inner);
    root.appendChild(km);

    // 刷新按钮
    const refresh = document.createElement("button");
    refresh.textContent = "刷新状态";
    refresh.style.marginTop = "10px";
    refresh.onclick = async () => {
        const s = await fetchStatus();
        renderPanel(root, s);
    };
    root.appendChild(refresh);
}

// ---------------------------------------------------------------------------
// 顶栏 "Auto Disable" 按钮
// ---------------------------------------------------------------------------

function attachTopButton() {
    if (document.getElementById("auto-disable-toggle")) {
        return;
    }
    // 兼容多种顶栏容器：ComfyUI 默认 .comfy-menu；
    // 某些主题 / lite 版本用 .litegraph-toolbar 或直接挂在 body。
    const menu =
        document.querySelector(".comfy-menu") ||
        document.querySelector(".litegraph-toolbar") ||
        document.body;
    if (!menu) {
        return;
    }
    const btn = document.createElement("button");
    btn.id = "auto-disable-toggle";
    btn.textContent = "Auto Disable";
    btn.title = "查看 / 恢复已被自动禁用的 custom_node 模块";
    btn.style.padding = "0 8px";
    btn.onclick = async () => {
        let panel = document.getElementById("auto-disable-panel");
        if (!panel) {
            panel = document.createElement("div");
            panel.id = "auto-disable-panel";
            panel.style.position = "fixed";
            panel.style.top = "60px";
            panel.style.right = "20px";
            panel.style.width = "420px";
            panel.style.maxHeight = "80vh";
            panel.style.overflow = "auto";
            panel.style.padding = "14px";
            panel.style.background = "#1f1f1f";
            panel.style.color = "#eee";
            panel.style.border = "1px solid #555";
            panel.style.borderRadius = "8px";
            panel.style.zIndex = "9999";
            panel.style.boxShadow = "0 8px 24px rgba(0,0,0,0.5)";
            document.body.appendChild(panel);
            panel._visible = false;
            panel.style.display = "none";
        }
        panel._visible = !panel._visible;
        panel.style.display = panel._visible ? "block" : "none";
        if (panel._visible) {
            const status = await fetchStatus();
            renderPanel(panel, status);
        }
    };
    menu.appendChild(btn);
}

// ---------------------------------------------------------------------------
// 自动重启提示：拦截 api.queuePrompt，提交后拉取待重启条目并弹窗
// ---------------------------------------------------------------------------

function showRestartNotice(items) {
    if (!items || items.length === 0) {
        return;
    }
    const names = items.map((it) => it.module).join(", ");
    const summary =
        `检测到缺失节点，已从 .disabled/ 自动恢复 ${items.length} 个模块：\n` +
        `${names}\n\n请重启 ComfyUI 后这些节点才会被加载生效。`;
    // 使用浏览器原生 alert 足够可靠；后续可换成 ComfyUI 自己的 toast / dialog
    // eslint-disable-next-line no-alert
    if (typeof window !== "undefined" && typeof window.alert === "function") {
        window.alert(summary);
    } else {
        console.warn("[auto_disable] pending restart:", items);
    }
}

async function consumePendingRestart() {
    try {
        const r = await api.fetchApi(`${ENDPOINT}/pending_restart`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
        if (!r || !r.ok) {
            return [];
        }
        const data = await r.json();
        return Array.isArray(data?.items) ? data.items : [];
    } catch (e) {
        console.warn("[auto_disable] pending_restart fetch failed:", e);
        return [];
    }
}

function wrapQueuePrompt() {
    // 只包一次，避免重复包装
    if (api.__auto_disable_wrapped__) {
        return;
    }
    const orig = api.queuePrompt;
    if (typeof orig !== "function") {
        return;
    }
    api.queuePrompt = async function patchedQueuePrompt(...args) {
        let result;
        try {
            result = await orig.apply(this, args);
        } catch (e) {
            // 即便提交失败也尝试消费待重启条目（避免堆积）
            throw e;
        } finally {
            // 不论成功失败都消费一次 pending_restart，保证状态收敛
            consumePendingRestart()
                .then((items) => showRestartNotice(items))
                .catch(() => {});
        }
        return result;
    };
    api.__auto_disable_wrapped__ = true;
}


// ---------------------------------------------------------------------------
// ComfyUI 扩展注册
// ---------------------------------------------------------------------------


app.registerExtension({
    name: "auto_node_disable",

    // 这些项会出现在 Settings 对话框里，能被搜索 / 排序 / 自动持久化。
    settings: [
        {
            id: "AutoNodeDisable.Threshold",
            type: "number",
            default: 30,
            name: "Auto Node Disable — 连续未使用次数阈值",
            tooltip:
                "连续 N 次入队未使用即触发自动禁用；" +
                "调到 0 可以彻底关闭自动禁用（dry_run 仍生效）。",
            attrs: { min: 0, max: 1000, step: 1 },
            onChange: async (value) => {
                const v = Number(value);
                if (!Number.isFinite(v) || v < 0) {
                    return;
                }
                await postJSON("/threshold", { value: v });
            },
        },
        {
            id: "AutoNodeDisable.Exclude",
            type: "text",
            default: "comfyui-auto-node-disable,ComfyUI-Manager",
            name: "Auto Node Disable — 永不自动禁用的模块",
            tooltip: "逗号分隔的模块名列表；本插件自身默认在列。",
            onChange: async (value) => {
                const names = (value || "")
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean);
                await postJSON("/exclude", { names });
            },
        },
        {
            id: "AutoNodeDisable.DryRun",
            type: "boolean",
            default: false,
            name: "Auto Node Disable — 仅审计不移动目录（dry-run）",
            tooltip:
                "开启后只写审计字段，不会真的把目录移动到 .disabled/。" +
                "适合先观察一段时间再放开。",
        },
    ],

    async setup() {
        // 包装 api.queuePrompt，使得每次提交后能拿到后端的 pending_restart 提示
        wrapQueuePrompt();
        // 等 UI 主体渲染完成后再挂顶栏按钮（部分主题懒渲染）
        const tryAttach = () => attachTopButton();
        setTimeout(tryAttach, 500);
        setTimeout(tryAttach, 2000);
        setTimeout(tryAttach, 4000);
    },
});
