(function (global) {
    "use strict";
    const prefix = (service) => `${service}:form-draft:v1:`;
    const bindings = [];
    let navigating = false;
    const revisionFields = (form) => [...form.elements].filter((field) =>
        field.type === "hidden" && ["expected_version", "expected_versions"].includes(field.name));
    const draftData = (binding) => ({version: 1, fields: snapshot(binding.form),
        revisions: revisionFields(binding.form).map(({name, value}) => ({name, value})),
        requestId: binding.requestId || "", requestPayload: binding.requestPayload || ""});
    const navigate = (url) => { navigating = true; global.location.assign(url); };
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
    const offerConflictRecovery = (binding) => {
        const form = binding.form;
        if (form.querySelector("[data-draft-reload]")) return;
        const button = global.document.createElement("button");
        button.type = "button"; button.dataset.draftReload = "";
        button.textContent = "이 폼 초안 버리고 최신 원본 보기";
        button.addEventListener("click", () => {
            if (!global.confirm("작성 내용을 복사했나요? 이 폼의 초안을 버리고 최신 원본을 불러옵니다.")) return;
            try {
                if (!saveOtherForms(form)) throw new Error("storage unavailable");
                global.sessionStorage.removeItem(binding.key);
            } catch (_) {
                status(form, "초안을 지우지 못했습니다. 입력은 유지됩니다. 내용을 복사하고 다시 시도해주세요.");
                return;
            }
            navigate(global.location.href);
        });
        form.append(button);
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
        const binding = {form, key, baseline: JSON.stringify(snapshot(form)), requestId: "", requestPayload: ""};
        if (authenticated) {
            try {
                const saved = JSON.parse(global.sessionStorage.getItem(key) || "null");
                if (saved && saved.version === 1) {
                    restore(form, saved.fields);
                    revisionFields(form).forEach((field) => {
                        const row = (Array.isArray(saved.revisions) ? saved.revisions : []).find((item) => item?.name === field.name);
                        field.value = row && typeof row.value === "string" ? row.value
                            : field.name === "expected_version" ? "0" : "";
                    });
                    if (typeof saved.requestId === "string" && /^[a-f0-9-]{36}$/i.test(saved.requestId)
                        && typeof saved.requestPayload === "string") {
                        binding.requestId = saved.requestId; binding.requestPayload = saved.requestPayload;
                    }
                }
            } catch (_) { /* Invalid or unavailable tab storage does not overwrite inputs. */ }
            bindings.push(binding);
        }
        let busy = false;
        let savedOnServer = false;
        form.addEventListener("submit", async (event) => {
            if (event.defaultPrevented) return;
            event.preventDefault();
            if (busy) return;
            if (savedOnServer) {
                if (JSON.stringify(snapshot(form)) === binding.baseline) return;
                savedOnServer = false; binding.requestId = ""; binding.requestPayload = "";
            }
            if (!authenticated) {
                status(form, "로그인 후 작성해주세요.");
                return;
            }
            if (!form.checkValidity()) return;
            if (form.dataset.confirmMessage && !global.confirm(form.dataset.confirmMessage)) return;
            const payload = JSON.stringify([form.action, snapshot(form), revisionFields(form).map(({name, value}) => [name, value])]);
            if (!binding.requestId || binding.requestPayload !== payload) {
                try { binding.requestId = global.crypto.randomUUID(); binding.requestPayload = payload; }
                catch (_) { status(form, "저장 요청을 준비하지 못했습니다. 내용을 복사하고 다시 시도해주세요."); return; }
            }
            let requestField = [...form.elements].find((field) => field.name === "request_id");
            if (!requestField) {
                requestField = global.document.createElement("input"); requestField.type = "hidden";
                requestField.name = "request_id"; form.append(requestField);
            }
            requestField.value = binding.requestId;
            const requestBody = new FormData(form);
            requestBody.set("request_id", binding.requestId);
            try {
                global.sessionStorage.setItem(key, JSON.stringify(draftData(binding)));
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
                        global.sessionStorage.setItem(key, JSON.stringify(draftData(binding)));
                    } catch (_) {
                        status(form, "초안을 임시 보관하지 못했습니다. 내용을 복사한 후 로그인해주세요.");
                        return;
                    }
                    redirectToLogin(form);
                    return;
                }
                if (!response.ok || destination.origin !== global.location.origin) {
                    status(form, response.status === 409
                        ? "저장 충돌이 발생했습니다. 다른 화면에서 수정했거나 요청 내용이 변경되었습니다. 입력을 복사하고 새로고침해 최신 기록을 확인해주세요."
                        : "저장하지 못했습니다. 입력 내용은 유지됩니다. 확인 후 다시 시도해주세요.");
                    if (response.status === 409) offerConflictRecovery(binding);
                    return;
                }
                savedOnServer = true;
                binding.baseline = JSON.stringify(snapshot(form));
                try { global.sessionStorage.removeItem(key); }
                catch (_) {
                    status(form, "저장은 완료했지만 임시 초안을 지우지 못했습니다. 다시 제출하지 말고 저장된 기록을 확인해주세요.");
                    return;
                }
                if (!saveOtherForms(form)) {
                    status(form, "저장은 완료했지만 다른 입력을 임시 보관하지 못했습니다. 내용을 복사한 후 저장된 기록을 확인해주세요. 다시 제출하지 마세요.");
                    return;
                }
                navigate(destination.href);
            } catch (_) {
                status(form, "저장 결과를 확인하지 못했습니다. 입력은 유지되며 같은 요청으로 다시 확인할 수 있습니다.");
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
        navigate(`/auth/login?next_path=${encodeURIComponent(currentPath)}`);
        return true;
    };
    const saveOtherForms = (submitted) => {
        try {
            for (const binding of bindings) {
                if (binding.form === submitted) continue;
                const fields = snapshot(binding.form);
                if (JSON.stringify(fields) === binding.baseline) continue;
                global.sessionStorage.setItem(binding.key, JSON.stringify(draftData(binding)));
            }
            return true;
        } catch (_) {
            return false;
        }
    };
    const bindDeleteForm = (form) => {
        let busy = false;
        let completed = false;
        let destination = "";
        const finish = () => {
            if (!saveOtherForms()) {
                status(form, "삭제는 완료했지만 작성 중 입력을 임시 보관하지 못했습니다. 내용을 복사한 후 이동해주세요. 다시 삭제하지 마세요.");
                return;
            }
            navigate(destination);
        };
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (busy) return;
            if (completed) { finish(); return; }
            if (!global.confirm(form.dataset.confirmMessage || "삭제할까요?")) return;
            busy = true;
            try {
                const target = new URL(form.action, global.location.href);
                if (target.origin !== global.location.origin) throw new Error("invalid delete target");
                const response = await global.fetch(target.href, {method: "POST", body: new FormData(form), credentials: "same-origin"});
                if (isLoginResponse(response)) { redirectToLogin(); return; }
                const url = new URL(response.url || global.location.href, global.location.href);
                if (!response.ok || url.origin !== global.location.origin) throw new Error("delete failed");
                completed = true; destination = url.href; finish();
            } catch (_) { status(form, "삭제 결과를 확인하지 못했습니다. 현재 기록을 확인한 후 다시 시도해주세요."); }
            finally { busy = false; }
        });
    };
    const boot = () => {
        const body = global.document.body;
        const service = body.dataset.draftService;
        if (!["book-memo", "youtube-memo"].includes(service)) return;
        global.document.querySelectorAll("[data-draft-fields]").forEach((form) => {
            bindForm(form, service, body.dataset.writeAuthenticated === "true");
        });
        global.document.querySelectorAll('form[action="/auth/logout"]').forEach((form) => {
            form.addEventListener("submit", () => { navigating = true; clearDrafts(service); });
        });
        global.addEventListener("beforeunload", (event) => {
            if (!navigating && bindings.some((binding) => JSON.stringify(snapshot(binding.form)) !== binding.baseline)) {
                saveOtherForms(); event.preventDefault(); event.returnValue = "";
            }
        });
        global.document.addEventListener("click", (event) => {
            const link = event.target.closest?.("a[href]");
            if (!link || !bindings.some((binding) => JSON.stringify(snapshot(binding.form)) !== binding.baseline)) return;
            if (!saveOtherForms()) {
                event.preventDefault(); status(bindings[0].form, "입력을 임시 보관하지 못했습니다. 내용을 복사한 후 이동해주세요.");
            }
        });
    };
    global.MemoDrafts = {clearDrafts, redirectToLogin, isLoginResponse, bindDeleteForm};
    global.document.addEventListener("DOMContentLoaded", boot);
})(window);
