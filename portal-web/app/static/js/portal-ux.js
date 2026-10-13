/* Shared UI helpers. Server-side authentication and storage policies remain authoritative. */
(function (root) {
    "use strict";
    function uploadError(status, detail) {
        const messages = {
            401: "파일함 인증이 만료되었습니다. 다시 인증해주세요.",
            403: "파일함 접근 권한을 확인해주세요.",
            409: "이미 같은 이름의 파일이 있습니다.",
            413: "업로드 용량 제한을 초과했습니다.",
            422: "업로드할 파일과 대상 폴더를 확인해주세요.",
            429: "요청이 많습니다. 잠시 후 다시 시도해주세요.",
        };
        const allowed = ["파일 이름이 비어 있습니다.", "확장자가 없는 파일은 업로드할 수 없습니다.",
            "허용되지 않은 파일 형식입니다.", "보안 정책상 차단된 파일 형식입니다.",
            "숨김 이름은 사용할 수 없습니다.", "허용되지 않는 경로입니다.",
            "심볼릭 링크 경로는 허용되지 않습니다.", "업로드할 폴더가 아닙니다.",
            "폴더를 찾을 수 없습니다."];
        if (status === 400 && typeof detail === "string") {
            if (allowed.includes(detail) || /^(파일은 \d+MB 이하만 업로드할 수 있습니다\.|한 번에 최대 \d+개 파일만 업로드할 수 있습니다\.|일괄 업로드 파일의 합계는 \d+MB 이하만 허용됩니다\.)$/.test(detail)) {
                return detail;
            }
        }
        return messages[status] || (status === 400 ? "파일 이름·형식·대상 폴더를 확인해주세요." : "서버에서 업로드를 처리하지 못했습니다. 잠시 후 다시 시도해주세요.");
    }

    function createUploader(options) {
        let busy = false;
        return function uploadFiles(files, targetPath) {
            if (busy || !files.length) return Promise.resolve(false);
            busy = true;
            const {dropZone, progress, status, feedback, input, button} = options;
            feedback.hidden = false;
            progress.value = 0;
            status.textContent = "전송 준비 중…";
            dropZone?.classList.add("uploading");
            if (input) input.disabled = true;
            if (button) button.disabled = true;
            const form = new FormData();
            form.append("path", targetPath);
            files.forEach(file => form.append("uploads", file));
            return new Promise(resolve => {
                const xhr = new XMLHttpRequest();
                const finish = (message, success = false) => {
                    busy = false;
                    status.textContent = message;
                    dropZone?.classList.remove("uploading");
                    if (input) { input.disabled = false; input.value = ""; }
                    if (button) button.disabled = false;
                    resolve(success);
                    if (success) root.location.reload();
                };
                xhr.open("POST", "/files/uploads");
                xhr.setRequestHeader("Accept", "application/json");
                xhr.timeout = 600000;
                xhr.upload.onprogress = event => {
                    if (event.lengthComputable && event.total > 0) {
                        const percent = Math.min(100, Math.floor(event.loaded / event.total * 100));
                        progress.value = percent;
                        status.textContent = percent === 100 ? "전송 완료 · 서버 저장 확인 중…" : `전송 중 ${percent}%`;
                    } else {
                        progress.removeAttribute("value");
                        status.textContent = "전송 중…";
                    }
                };
                xhr.onload = () => {
                    let payload;
                    try { payload = JSON.parse(xhr.responseText); } catch (_) { payload = {}; }
                    if (!payload || typeof payload !== "object") payload = {};
                    const destination = new URL(xhr.responseURL || "/files/uploads", root.location.href);
                    if (xhr.status === 200 && payload.uploaded === true && destination.origin === root.location.origin && destination.pathname === "/files/uploads") {
                        progress.value = 100;
                        finish("업로드 및 저장 완료", true);
                    } else {
                        finish(uploadError(xhr.status, payload.detail));
                    }
                };
                const uncertain = "연결이 끊겨 저장 결과를 확인하지 못했습니다. 파일 목록을 확인한 뒤 다시 시도해주세요.";
                xhr.onerror = () => finish(uncertain);
                xhr.ontimeout = () => finish("응답 시간이 초과되었습니다. 파일 목록에서 저장 여부를 먼저 확인해주세요.");
                xhr.onabort = () => finish(uncertain);
                try { xhr.send(form); } catch (_) { finish(uncertain); }
            });
        };
    }

    function bindSearchRetries(doc) {
        doc.addEventListener("click", async event => {
            const button = event.target.closest("[data-search-retry]");
            if (!button || button.disabled) return;
            const group = button.closest("[data-search-source]");
            if (!group) return;
            button.disabled = true;
            const status = group.querySelector('[role="status"]');
            status.textContent = "이 서비스 다시 검색 중…";
            const controller = new AbortController();
            const timeout = root.setTimeout(() => controller.abort(), 10000);
            try {
                const query = new URLSearchParams({source: group.dataset.searchSource, q: group.dataset.searchQuery});
                const response = await root.fetch(`/search/retry?${query}`, {signal: controller.signal, headers: {Accept: "text/html"}});
                if (!response.ok || response.redirected || !(response.headers.get("Content-Type") || "").includes("text/html")) throw new Error("retry failed");
                const fragment = doc.createElement("template");
                fragment.innerHTML = await response.text();
                const replacement = fragment.content.firstElementChild;
                if (fragment.content.childElementCount !== 1 || !replacement?.matches("section.search-group") || replacement.dataset.searchSource !== group.dataset.searchSource) throw new Error("invalid retry response");
                group.replaceWith(replacement);
            } catch (_) {
                status.textContent = "응답을 확인하지 못했습니다. 다시 시도해주세요.";
                button.disabled = false;
            } finally {
                root.clearTimeout(timeout);
            }
        });
    }
    root.PortalUX = {createUploader, uploadError, bindSearchRetries};
    if (typeof module !== "undefined") module.exports = root.PortalUX;
})(typeof window !== "undefined" ? window : globalThis);
