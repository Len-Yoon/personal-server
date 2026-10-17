(() => {
    "use strict";
    const form = document.querySelector('form[action="/admin/save"]');
    if (!form) return;
    const content = form.elements.namedItem("content");
    const revision = form.elements.namedItem("expected_revision");
    if (!revision) return;
    const validRevision = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
    const button = form.querySelector('button[type="submit"]');
    const key = "portfolio-editor:draft:v1";
    const revisionKey = "portfolio-editor:revision:v1";
    const conflictPanel = document.getElementById("portfolio-conflict");
    const serverContent = document.getElementById("conflict-server-content");
    const reviewButton = document.getElementById("portfolio-review-revision");
    let conflictRevision = null;
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    form.append(status);
    let navigating = false;
    let submitting = false;
    let baseline = content.value;
    try {
        const draft = sessionStorage.getItem(key);
        if (draft !== null) {
            content.value = draft;
            const savedRevision = sessionStorage.getItem(revisionKey);
            // Never attach an old draft to a newer page revision automatically.
            revision.value = validRevision(savedRevision) ? savedRevision : "";
            status.textContent = "이전 작성 내용을 복원했습니다. 확인 후 저장해주세요.";
        }
    } catch (_) {
        status.textContent = "임시 보관을 사용할 수 없습니다. 이동 전 내용을 복사해주세요.";
    }
    const preserve = () => {
        try {
            sessionStorage.setItem(key, content.value);
            sessionStorage.setItem(revisionKey, revision.value);
            return sessionStorage.getItem(key) === content.value && sessionStorage.getItem(revisionKey) === revision.value;
        } catch (_) { return false; }
    };
    content.addEventListener("input", preserve);
    reviewButton.addEventListener("click", () => {
        if (!validRevision(conflictRevision)) return;
        revision.value = conflictRevision;
        conflictRevision = null;
        reviewButton.disabled = true;
        preserve();
        status.textContent = "최신 내용과 비교한 뒤 작성 내용을 정리하고 저장해주세요.";
    });
    window.addEventListener("beforeunload", (event) => {
        if (!navigating && content.value !== baseline && !preserve()) {
            event.preventDefault();
            event.returnValue = "";
        }
    });
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (submitting) return;
        submitting = true;
        button.disabled = true;
        const submitted = content.value;
        try {
            const response = await fetch(form.action, {
                method: "POST", body: new FormData(form), credentials: "same-origin",
                headers: {Accept: "application/json"},
            });
            if (response.status === 401) {
                if (!preserve()) {
                    status.textContent = "입력을 보관하지 못했습니다. 내용을 복사한 뒤 새 탭에서 로그인해주세요.";
                    return;
                }
                navigating = true;
                window.location.assign("/admin");
                return;
            }
            if (response.status === 409) {
                const conflict = await response.json();
                if (!validRevision(conflict.current_revision) || typeof conflict.current_content !== "string") throw new Error("invalid conflict response");
                conflictRevision = conflict.current_revision;
                serverContent.value = conflict.current_content;
                conflictPanel.hidden = false;
                reviewButton.disabled = false;
                preserve();
                status.textContent = "다른 창에서 저장한 내용이 있습니다. 아래 최신 내용과 비교해주세요. 작성 내용은 유지됩니다.";
                return;
            }
            if (!response.ok) throw new Error("save failed");
            const saved = await response.json();
            if (!validRevision(saved.revision)) throw new Error("invalid save response");
            revision.value = saved.revision;
            conflictRevision = null;
            conflictPanel.hidden = true;
            baseline = submitted;
            if (content.value !== submitted) {
                preserve();
                status.textContent = "저장 중 추가한 내용이 남아 있습니다. 다시 저장해주세요.";
                return;
            }
            try { sessionStorage.removeItem(key); sessionStorage.removeItem(revisionKey); } catch (_) {
                status.textContent = "저장 완료. 임시 내용 정리가 불가능하여 현재 화면을 유지합니다.";
                return;
            }
            navigating = true;
            window.location.assign("/admin");
        } catch (_) {
            preserve();
            status.textContent = "저장 결과를 확인할 수 없습니다. 입력은 유지됩니다. 다시 확인해주세요.";
        } finally {
            submitting = false;
            button.disabled = false;
        }
    });
})();
