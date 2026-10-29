import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import test from "node:test";

// Run the shipped draft and preview scripts against the same DOM fixture.
class Element {
    constructor(tag = "div") { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = new Map(); this.checked = false; this.value = ""; this.textContent = ""; }
    set innerHTML(value) { assert.equal(value, ""); this.children = []; }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    appendChild(child) { this.append(child); }
    setAttribute() {}
    getAttribute(name) { return name === "action" ? "/books" : null; }
    closest() { return null; }
    addEventListener(name, callback) { const list = this.listeners.get(name) || []; list.push(callback); this.listeners.set(name, list); }
    dispatchEvent(event) { for (const callback of this.listeners.get(event.type) || []) callback(event); if (event.bubbles) this.parent?.dispatchEvent(event); }
    querySelectorAll(selector) {
        const matches = (item) => selector === "input[name='titles']" ? item.name === "titles" : selector === "[data-toc-selection-count]" ? item.dataset.tocSelectionCount !== undefined : false;
        return this.children.flatMap((child) => [ ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

for (const detail of [false, true]) test(`${detail ? "detail" : "search"} preview keeps selection on failure and shows counts/partial/empty`, async () => {
    const nodes = Object.fromEntries(["source", "button", "build", "count", "status", "form", "candidates"].map((name) => [name, new Element()]));
    const fields = Object.fromEntries(["isbn", "title", "external_id", "source"].map((name) => [name, {value: name === "isbn" ? "9781234567890" : ""}]));
    nodes.form.querySelector = (selector) => ({".load-result-toc": nodes.button, ".toc-status": nodes.status, ".toc-candidates": nodes.candidates}[selector] || fields[selector.match(/name='([^']+)'/)?.[1]] || null);
    const ids = {"#toc-source": "source", "#toc-fetch-button": "button", "#toc-build-button": "build", "#toc-count": "count", "#toc-status": "status", "#toc-candidate-form": "form", "#toc-candidates": "candidates"};
    nodes.form.dataset = {draftFields: "titles", draftId: "preview"};
    nodes.form.append(nodes.candidates);
    Object.defineProperty(nodes.form, "elements", {get: () => nodes.candidates.querySelectorAll("input[name='titles']")});
    const document = {body: {dataset: {draftService: "book-memo", writeAuthenticated: "true"}}, listeners: [], addEventListener(type, callback) { if (type === "DOMContentLoaded") this.listeners.push(callback); }, createElement: (tag) => new Element(tag), querySelector: (id) => nodes[ids[id]], querySelectorAll: () => [nodes.form]};
    let next = {candidates: ["1장 시작", "일반 제목", "<img src=x onerror=alert(1)>"], source: "YES24", source_url: "https://www.yes24.com/product/goods/123", truncated: true};
    let failed = false; let requests = 0;
    const fetch = async () => { requests++; return {ok: !failed, json: async () => next}; };
    class Event { constructor(type, options = {}) { this.type = type; this.bubbles = options.bubbles; } }
    let source = readFileSync(`book-memo/app/templates/${detail ? "book_detail" : "home"}.html`, "utf8");
    source = detail ? source.slice(source.indexOf('    const tocSource ='), source.indexOf('\ndocument.querySelectorAll(".protected-delete-form")')) : source.slice(source.indexOf('    const shouldSelectTocByDefault ='), source.indexOf('    document.querySelectorAll(".library-comment-form")'));
    const key = "book-memo:form-draft:v1:" + JSON.stringify(["/", "/books", "preview", ["titles"]]);
    const saved = new Map([[key, JSON.stringify({version: 1, fields: [{name: "titles", type: "checkbox", value: "1장 시작", checked: false}, {name: "titles", type: "checkbox", value: "일반 제목", checked: true}]})]]);
    const window = {document, location: {pathname: "/", origin: "https://memo.example", href: "https://memo.example/"}, addEventListener() {}, sessionStorage: {getItem: (key) => saved.get(key) || null, setItem: (key, value) => saved.set(key, value), removeItem: (key) => saved.delete(key)}};
    const context = vm.createContext({document, window, fetch, URLSearchParams, URL, Event});
    vm.runInContext(readFileSync("book-memo/app/static/js/form-drafts.js", "utf8"), context);
    vm.runInContext(source, context);
    for (const listener of document.listeners) listener();
    assert.equal(nodes.candidates.querySelector("[data-toc-selection-count]").textContent, "선택 1개 / 전체 2개", "restored choices get selection controls without refetching");
    assert.equal(requests, 0, "preview must be explicitly requested");
    const click = async () => nodes.button.listeners.get("click")[0]();
    await click();
    const count = () => nodes.candidates.querySelector("[data-toc-selection-count]").textContent;
    assert.equal(count(), "선택 1개 / 전체 3개");
    assert.match(nodes.status.textContent, /앞 500개/);
    assert.equal(nodes.candidates.querySelectorAll("input[name='titles']")[2].value, next.candidates[2]);
    assert.equal(nodes.candidates.querySelectorAll("input[name='titles']")[0].checked, false, "successful preview must keep restored deselection");
    assert.equal(nodes.candidates.querySelectorAll("input[name='titles']")[1].checked, true);
    const controls = nodes.candidates.children[0];
    controls.children.find((node) => node.textContent === "전체 선택").dispatchEvent(new Event("click"));
    assert.equal(count(), "선택 3개 / 전체 3개");
    nodes.candidates.querySelectorAll("input[name='titles']")[1].checked = false;
    nodes.candidates.querySelectorAll("input[name='titles']")[1].dispatchEvent(new Event("change", {bubbles: true}));
    assert.equal(count(), "선택 2개 / 전체 3개");
    failed = true; await click();
    assert.equal(count(), "선택 2개 / 전체 3개");
    assert.equal(nodes.button.disabled, false);
    failed = false; next = {candidates: []}; await click();
    assert.match(nodes.status.textContent, /목차가 없습니다/);
    assert.equal(nodes.candidates.querySelectorAll("input[name='titles']").length, 0);
});
