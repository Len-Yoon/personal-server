(() => {
    const file = document.getElementById("import-file");
    const preview = document.getElementById("import-preview");
    const commit = document.getElementById("import-commit");
    const status = document.getElementById("import-status");
    let body = "", token = "", busy = false;
    const reset = () => { body = ""; token = ""; commit.hidden = true; commit.disabled = true; };
    file.addEventListener("change", () => { reset(); status.textContent = ""; });
    const send = async (path, headers) => {
        const response = await fetch(path, {method: "POST", credentials: "same-origin", body,
            headers: {"Content-Type": "application/json", "Accept": "application/json", ...headers}});
        if (response.status === 401) throw new Error("로그인이 만료되었습니다. 다시 로그인한 후 파일을 선택해주세요.");
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "가져오지 못했습니다. 다시 미리보기해주세요.");
        return result;
    };
    preview.addEventListener("click", async () => {
        if (busy) return;
        reset();
        const selected = file.files[0];
        if (!selected) { status.textContent = "JSON 파일을 선택해주세요."; return; }
        if (selected.size > 2 * 1024 * 1024) { status.textContent = "파일은 2 MiB 이내여야 합니다."; return; }
        busy = true; preview.disabled = true; file.disabled = true;
        try {
            body = await selected.text();
            const result = await send("/api/import/preview", {});
            token = result.preview_token;
            status.textContent = `전체 ${result.record_count}개 · 새 항목 ${result.new_count}개 · 기존 항목 ${result.skip_count}개 건너뛰기. 아직 저장하지 않았습니다.`;
            commit.hidden = false; commit.disabled = result.new_count === 0;
        } catch (error) { reset(); status.textContent = error.message; }
        finally { busy = false; preview.disabled = false; file.disabled = false; }
    });
    commit.addEventListener("click", async () => {
        if (busy || !token) return;
        busy = true; commit.disabled = true; preview.disabled = true; file.disabled = true;
        try {
            const result = await send("/api/import/commit", {"X-Import-Preview": token});
            reset(); status.textContent = `가져오기 완료: ${result.imported_count}개 저장 · ${result.skip_count}개 건너뛰기. 목록에서 확인해주세요.`;
        } catch (error) { reset(); status.textContent = error.message; }
        finally { busy = false; preview.disabled = false; file.disabled = false; }
    });
})();
