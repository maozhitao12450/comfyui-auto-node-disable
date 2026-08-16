/**
 * ComfyUI Auto Node Disable - 前端桥接脚本
 * --------------------------------------------------
 * 在 UI 中暴露一个小菜单（位于 Settings 面板下），
 * 让用户可以：
 *   - 查看哪些 custom_node 模块当前被自动禁用
 *   - 调整阈值（默认 3）
 *   - 配置排除名单
 *   - 一键把某个被禁用的模块恢复到 custom_nodes/ 下
 *
 * 服务端已通过 onprompt 钩子自动追踪每次『队列提交』，前端主要负责
 * 提供用户可见的控制面板。后端 API：
 *   GET  /auto_disable/status
 *   POST /auto_disable/restore   {"module": "<name>"}
 *   POST /auto_disable/threshold {"value": 3}
 *   POST /auto_disable/exclude   {"names": ["..."]}
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const ENDPOINT = "/auto_disable";

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

function clearChildren(node) {
    while (node.firstChild) {
        node.removeChild(node.firstChild);
    }
}

function renderPanel(root, status) {
    clearChildren(root);

    const title = document.createElement("h3");
    title.textContent = "Auto Node Disable";
    title.style.margin = "0 0 8px 0";
    root.appendChild(title);

    const desc = document.createElement("p");
    desc.style.opacity = "0.8";
    desc.style.fontSize = "12px";
    desc.textContent =
        "每次提交工作流都会记录用到的节点；连续 N 次都未出现的 custom_node " +
        "会被移动到 custom_nodes/.disabled/<名称>/ 里。需要重启 ComfyUI 才生效。";
    root.appendChild(desc);

    // 阈值行
    const thRow = document.createElement("div");
    thRow.style.margin = "10px 0";
    thRow.style.display = "flex";
    thRow.style.alignItems = "center";
    thRow.style.gap = "8px";
    const thLabel = document.createElement("label");
    thLabel.textContent = "连续未使用次数阈值：";
    const thInput = document.createElement("input");
    thInput.type = "number";
    thInput.min = "0";
    thInput.value = String(status?.threshold ?? 3);
    thInput.style.width = "60px";
    const thBtn = document.createElement("button");
    thBtn.textContent = "保存";
    thBtn.onclick = async () => {
        const v = parseInt(thInput.value, 10);
        const r = await postJSON("/threshold", { value: v });
        if (r && r.ok) {
            thBtn.textContent = "已保存";
            setTimeout(() => (thBtn.textContent = "保存"), 1500);
        }
    };
    thRow.appendChild(thLabel);
    thRow.appendChild(thInput);
    thRow.appendChild(thBtn);
    root.appendChild(thRow);

    // 排除列表行
    const exRow = document.createElement("div");
    exRow.style.margin = "10px 0";
    exRow.style.display = "flex";
    exRow.style.alignItems = "center";
    exRow.style.gap = "8px";
    const exLabel = document.createElement("label");
    exLabel.textContent = "永不自动禁用的模块（逗号分隔）：";
    const exInput = document.createElement("input");
    exInput.type = "text";
    exInput.placeholder = "comfyui-auto-node-disable,ComfyUI-Manager";
    exInput.value = (status?.exclude || []).join(",");
    exInput.style.flex = "1";
    const exBtn = document.createElement("button");
    exBtn.textContent = "保存";
    exBtn.onclick = async () => {
        const names = exInput.value
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
        const r = await postJSON("/exclude", { names });
        if (r && r.ok) {
            exBtn.textContent = "已保存";
            setTimeout(() => (exBtn.textContent = "保存"), 1500);
        }
    };
    exRow.appendChild(exLabel);
    exRow.appendChild(exInput);
    exRow.appendChild(exBtn);
    root.appendChild(exRow);

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
            left.textContent = name + (info?.original_path ? `  (${info.original_path})` : "");
            const restore = document.createElement("button");
            restore.textContent = "恢复";
            restore.onclick = async () => {
                const r = await postJSON("/restore", { module: name });
                if (r && r.ok) {
                    restore.textContent = "已恢复（请重启 ComfyUI）";
                    restore.disabled = true;
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
    const summary = document.createElement("summary");
    summary.textContent = `已知 custom_node 模块（${Object.keys(status?.known_modules || {}).length}）`;
    km.appendChild(summary);
    const inner = document.createElement("pre");
    inner.style.fontSize = "11px";
    inner.style.maxHeight = "220px";
    inner.style.overflow = "auto";
    inner.style.background = "rgba(255,255,255,0.04)";
    inner.style.padding = "6px";
    inner.textContent = JSON.stringify(status?.known_modules || {}, null, 2);
    km.appendChild(inner);
    root.appendChild(km);
}

app.registerExtension({
    name: "auto_node_disable.settings",

    async setup() {
        // 在 Settings 面板里加一个 tab
        try {
            const dlg = await app.ui.settings.loadExtensionSettings?.();
        } catch (e) {
            // 不同版本 API 不同，回退到直接挂到 body 上
            console.warn("[auto_disable] settings dialog unavailable:", e);
        }

        // 简单实现：直接插入到一个浮动面板里（菜单按钮 -> 弹窗）
        const btnId = "auto-disable-toggle";
        if (document.getElementById(btnId)) {
            return;
        }

        // 找 ComfyUI 顶栏菜单，挂一个 "Auto Disable" 按钮
        const tryAttach = () => {
            const menu = document.querySelector(".comfy-menu");
            if (!menu || document.getElementById(btnId)) {
                return;
            }
            const btn = document.createElement("button");
            btn.id = btnId;
            btn.textContent = "Auto Disable";
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
                }
                const status = await fetchStatus();
                renderPanel(panel, status);
                panel._visible = !panel._visible;
                panel.style.display = panel._visible ? "block" : "none";
            };
            menu.appendChild(btn);
        };

        // 等 UI 渲染完成后再尝试挂载
        setTimeout(tryAttach, 500);
        setTimeout(tryAttach, 2000);
    },
});