(() => {
    const form = document.getElementById("memo-trash-purge");
    if (!form) return;
    const boxes = Array.from(document.querySelectorAll(".memo-trash-select"));
    const selection = () => boxes.filter(box => box.checked).map(box => [Number(box.dataset.memoId), Number(box.dataset.version)]);
    const button = form.querySelector('button[type="submit"]');
    boxes.forEach(box => box.addEventListener("change", () => {
        const count = selection().length;
        button.disabled = count === 0 || count > 20;
    }));
    form.addEventListener("submit", event => {
        const selected = selection();
        if (!selected.length || selected.length > 20 || !window.confirm(`선택한 메모 ${selected.length}개와 수정 이력을 영구 삭제합니다. 복구할 수 없습니다. 계속하시겠습니까?`)) {
            event.preventDefault();
            return;
        }
        form.querySelector('[name="selection"]').value = JSON.stringify(selected);
    });
})();
