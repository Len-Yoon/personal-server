(function (global) {
    "use strict";
    const prefix = (service) => `${service}:form-draft:v1:`;
    const bindings = [];
    const allowedNames = (form) => new Set((form.dataset.draftFields || "").split(/\s+/).filter(Boolean));
    const eligible = (field, names) => names.has(field.name)
        && !/password|token|secret|csrf|auth|session/i.test(field.name)
        && !["password", "hidden", "file", "submit", "button"].includes(field.type);
    const controls = (form) => [...form.elements].filter((field) => eligible(field, allowedNames(form)));
    const formKey = (form, service) => prefix(service) + JSON.stringify([
        global.location.pathname, form.getAttribute("action") || "",
        form.dataset.draftId || "", [...allowedNames(form)].sort(),
    ]);
    const snapshot = (form) => controls(form).map((field, index) => ({
        name: field.name, type: field.type, value: field.value,
        checked: Boolean(field.checked), index,
    }));
    const status = (form, message) => {
        let node = form.querySelector("[data-draft-status]");
        if (!node) {
            node = global.document.createElement("p");
            node.dataset.draftStatus = "";
            node.setAttribute("aria-live", "polite");
            form.append(node);
        }
        node.textContent = message;
    };
    const restore = (form, rows) => {
        if (!Array.isArray(rows)) return false;
        const names = allowedNames(form);
        const safe = rows.filter((row) => row && typeof row.name === "string"
            && typeof row.type === "string" && typeof row.value === "string"
            && eligible(row, names));
        // Submitted TOC candidates are created dynamically and absent after login.
        safe.filter((row) => row.name === "titles" && row.type === "checkbox").forEach((row) => {
            if (controls(form).some((field) => field.name === row.name && field.value === row.value)) return;
            const container = form.querySelector(".toc-candidates") || form;
            const label = global.document.createElement("label");
            const input = global.document.createElement("input");
            input.type = "checkbox"; input.name = "titles"; input.value = row.value;
            const text = global.document.createElement("span"); text.textContent = row.value;
            label.append(input, text); container.append(label); container.hidden = false;
            form.hidden = false;
        });
        const fields = controls(form);
        const assign = (field) => {
            const candidates = safe.filter((row) => row.name === field.name && row.type === field.type);
            const row = ["checkbox", "radio"].includes(field.type)
                ? candidates.find((item) => item.value === field.value) : candidates[0];
            if (!row) return;
            if (["checkbox", "radio"].includes(field.type)) field.checked = row.checked === true;
            else field.value = row.value;
        };
        fields.filter((field) => field.type.startsWith("select")).forEach((field) => {
            assign(field);
            field.dispatchEvent(new Event("change", {bubbles: true}));
        });
        fields.filter((field) => !field.type.startsWith("select")).forEach(assign);
        if (safe.length) {
            const details = form.closest("details");
            if (details) details.open = true;
            status(form, "로그인 이동 전에 제출한 내용을 복원했습니다. 확인 후 저장해주세요.");
        }
        return safe.length > 0;
    };
    const clearDrafts = (service) => {
        try {
            const storage = global.sessionStorage;
            const keys = Array.from({length: storage.length}, (_, index) => storage.key(index));
            keys.filter((key) => key && key.startsWith(prefix(service))).forEach((key) => storage.removeItem(key));
        } catch (_) { /* Storage is unavailable; never read or store credentials. */ }
    };
    const bindForm = (form, service, authenticated) => {
        const key = formKey(form, service);
        if (authenticated) {
            try {
                const saved = JSON.parse(global.sessionStorage.getItem(key) || "null");
                if (saved && saved.version === 1) restore(form, saved.fields);
            } catch (_) { /* Invalid or unavailable tab storage does not overwrite inputs. */ }
            bindings.push({form, key, baseline: JSON.stringify(snapshot(form))});
        }
        let busy = false;
        let savedOnServer = false;
        form.addEventListener("submit", async (event) => {
            if (event.defaultPrevented) return;
            event.preventDefault();
            if (busy || savedOnServer) return;
            if (!authenticated) {
                status(form, "로그인 후 작성해주세요.");
                return;
            }
            if (!form.checkValidity()) return;
            if (form.dataset.confirmMessage && !global.confirm(form.dataset.confirmMessage)) return;
            const requestBody = new FormData(form);
            try {
                global.sessionStorage.setItem(key, JSON.stringify({version: 1, fields: snapshot(form)}));
            } catch (_) { /* A failed draft write must prevent login navigation. */ }
            const disabledStates = [...form.elements].map((field) => [field, field.disabled]);
            disabledStates.forEach(([field]) => { field.disabled = true; });
            busy = true;
            try {
                const action = new URL(form.action, global.location.href);
                if (action.origin !== global.location.origin) throw new Error("invalid form target");
                const response = await global.fetch(action.href, {
                    method: "POST", body: requestBody,
                    credentials: "same-origin", headers: {"Accept": "application/json"},
                });
                const destination = new URL(response.url || global.location.href, global.location.href);
                if (isLoginResponse(response)) {
                    try {
                        global.sessionStorage.setItem(key, JSON.stringify({version: 1, fields: snapshot(form)}));
                    } catch (_) {
                        status(form, "초안을 임시 보관하지 못했습니다. 내용을 복사한 후 로그인해주세요.");
                        return;
                    }
                    redirectToLogin(form);
                    return;
                }
                if (!response.ok || destination.origin !== global.location.origin) {
                    status(form, "저장하지 못했습니다. 입력 내용은 유지됩니다. 확인 후 다시 시도해주세요.");
                    return;
                }
                savedOnServer = true;
                const binding = bindings.find((item) => item.form === form);
                if (binding) binding.baseline = JSON.stringify(snapshot(form));
                try { global.sessionStorage.removeItem(key); }
                catch (_) {
                    status(form, "저장은 완료했지만 임시 초안을 지우지 못했습니다. 다시 제출하지 말고 저장된 기록을 확인해주세요.");
                    return;
                }
                if (!saveOtherForms(form)) {
                    status(form, "저장은 완료했지만 다른 입력을 임시 보관하지 못했습니다. 내용을 복사한 후 저장된 기록을 확인해주세요. 다시 제출하지 마세요.");
                    return;
                }
                global.location.assign(destination.href);
            } catch (_) {
                status(form, "저장하지 못했습니다. 입력 내용은 유지됩니다. 잠시 후 다시 시도해주세요.");
            } finally {
                disabledStates.forEach(([field, disabled]) => { field.disabled = disabled; });
                busy = false;
            }
        });
    };
    const isLoginResponse = (response) => {
        const destination = new URL(response.url || global.location.href, global.location.href);
        return response.status === 401 || (destination.origin === global.location.origin
            && destination.pathname.replace(/\/+$/, "") === "/auth/login");
    };
    const redirectToLogin = (submitted) => {
        if (!saveOtherForms(submitted)) {
            const target = submitted || (bindings[0] && bindings[0].form);
            const message = "초안을 임시 보관하지 못했습니다. 내용을 복사한 후 로그인해주세요.";
            if (target) status(target, message);
            else global.alert(message);
            return false;
        }
        const currentPath = `${global.location.pathname}${global.location.search}`;
        global.location.assign(`/auth/login?next_path=${encodeURIComponent(currentPath)}`);
        return true;
    };
    const saveOtherForms = (submitted) => {
        try {
            for (const binding of bindings) {
                if (binding.form === submitted) continue;
                const fields = snapshot(binding.form);
                if (JSON.stringify(fields) === binding.baseline) continue;
                global.sessionStorage.setItem(binding.key, JSON.stringify({version: 1, fields}));
            }
            return true;
        } catch (_) {
            return false;
        }
    };
    const boot = () => {
        const body = global.document.body;
        const service = body.dataset.draftService;
        if (!["book-memo", "youtube-memo"].includes(service)) return;
        global.document.querySelectorAll("[data-draft-fields]").forEach((form) => {
            bindForm(form, service, body.dataset.writeAuthenticated === "true");
        });
        global.document.querySelectorAll('form[action="/auth/logout"]').forEach((form) => {
            form.addEventListener("submit", () => clearDrafts(service));
        });
    };
    global.MemoDrafts = {clearDrafts, redirectToLogin, isLoginResponse};
    global.document.addEventListener("DOMContentLoaded", boot);
})(window);
