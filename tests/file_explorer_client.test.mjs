import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const template = readFileSync(resolve("portal-web/app/templates/files.html"), "utf8");
const clientScript = template.match(/<script>([\s\S]*?)<\/script>/)?.[1];

if (!clientScript) {
    throw new Error("파일함 클라이언트 스크립트를 찾을 수 없습니다.");
}

class TestElement {
    constructor({ classes = [], dataset = {} } = {}) {
        this.attributes = new Map();
        this.classList = new SetClassList(classes);
        this.dataset = dataset;
        this.hidden = false;
        this.listeners = new Map();
        this.children = [];
        this.value = "";
        this.textContent = "";
    }

    addEventListener(name, listener) {
        this.listeners.set(name, listener);
    }

    append(child) {
        this.children = this.children.filter((item) => item !== child);
        this.children.push(child);
    }

    dispatch(name, event = {}) {
        this.listeners.get(name)?.({
            preventDefault() {},
            stopPropagation() {},
            ...event,
        });
    }

    focus() {
        globalThis.document.activeElement = this;
    }

    blur() {}

    setAttribute(name, value) {
        this.attributes.set(name, value);
    }

    getAttribute(name) {
        return this.attributes.get(name);
    }
}

class SetClassList {
    constructor(classes) {
        this.values = new Set(classes);
    }

    add(value) {
        this.values.add(value);
    }

    contains(value) {
        return this.values.has(value);
    }

    remove(value) {
        this.values.delete(value);
    }

    toggle(value, force) {
        const enabled = force === undefined ? !this.values.has(value) : force;
        if (enabled) {
            this.values.add(value);
        } else {
            this.values.delete(value);
        }
        return enabled;
    }
}

function bootFileExplorer(savedView = "list") {
    const folder = new TestElement({
        classes: ["file-tile", "folder-tile", "folder-row"],
        dataset: { name: "자료", modified: "1" },
    });
    const report = new TestElement({
        classes: ["file-tile"],
        dataset: { name: "보고서.txt", modified: "2" },
    });
    const dropZone = new TestElement({ classes: ["drop-zone"] });
    dropZone.children = [folder, report];
    const elements = {
        ".drop-zone": dropZone,
        ".file-browser": new TestElement({ classes: ["file-browser"] }),
        "#file-upload-input": new TestElement(),
        "#selected-count": new TestElement(),
        "#clear-selection": new TestElement(),
        "#download-selection": new TestElement(),
        "#delete-selection": new TestElement(),
        "#new-folder": new TestElement(),
        "#pick-files": new TestElement(),
        "#file-search": new TestElement(),
        "#file-sort": new TestElement(),
        "#file-search-status": new TestElement(),
    };
    const icons = new TestElement({ dataset: { viewMode: "icons" } });
    const list = new TestElement({ dataset: { viewMode: "list" } });
    globalThis.document = {
        activeElement: null,
        querySelector(selector) {
            return elements[selector] || null;
        },
        querySelectorAll(selector) {
            if (selector === ".file-tile") {
                return dropZone.children;
            }
            if (selector === "[data-view-mode]") {
                return [icons, list];
            }
            return [];
        },
    };
    globalThis.localStorage = {
        value: savedView,
        getItem() {
            return this.value;
        },
        setItem(_key, value) {
            this.value = value;
        },
    };
    globalThis.window = { location: { href: "" } };

    eval(clientScript);
    return { dropZone, elements, folder, icons, list, report };
}

test("파일 탐색기 클라이언트 동작은 검색·정렬·보기 복원·키보드 선택을 유지한다", () => {
    const { dropZone, elements, folder, list, report } = bootFileExplorer();

    assert.equal(dropZone.classList.contains("list-view"), true);
    assert.equal(list.getAttribute("aria-pressed"), "true");

    elements["#file-search"].value = "보고";
    elements["#file-search"].dispatch("input");
    assert.equal(folder.hidden, true);
    assert.equal(report.hidden, false);
    assert.equal(elements["#file-search-status"].textContent, "1개 항목 표시");

    elements["#file-sort"].value = "modified";
    elements["#file-sort"].dispatch("change");
    assert.deepEqual(dropZone.children, [report, folder]);

    report.dispatch("keydown", { key: " " });
    assert.equal(report.getAttribute("aria-selected"), "true");
    report.dispatch("keydown", { key: "Escape" });
    assert.equal(report.getAttribute("aria-selected"), "false");

    report.dispatch("keydown", { key: "a", ctrlKey: true });
    assert.equal(report.getAttribute("aria-selected"), "true");

    elements["#file-search"].value = "";
    elements["#file-search"].dispatch("input");
    report.dispatch("keydown", { key: "ArrowDown" });
    assert.equal(globalThis.document.activeElement, folder);
});
