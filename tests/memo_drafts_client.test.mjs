import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {resolve} from "node:path";
import vm from "node:vm";
import test from "node:test";
import {randomUUID} from "node:crypto";

const service = process.env.MEMO_DRAFT_SERVICE;
assert.ok(["book-memo", "youtube-memo"].includes(service));
const script = readFileSync(resolve(service, "app/static/js/form-drafts.js"), "utf8");

class Element {
    constructor(name = "", type = "text", value = "") {
        this.name = name; this.type = type; this.value = value;
        this.checked = false; this.disabled = false; this.children = [];
        this.dataset = {}; this.listeners = new Map(); this.hidden = false;
    }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    append(...children) { this.children.push(...children); const node = children.find((child) => child.dataset?.draftStatus !== undefined); if (node) this.status = node; }
    querySelector(selector) { return selector === "[data-draft-status]" ? this.status : null; }
    setAttribute(name, value) { this[name] = value; }
    dispatchEvent(event) { this.listeners.get(event.type)?.(event); }
}

function fixture({authenticated = true, storage, action = "/videos/1/memos", id = "new"} = {}) {
    const values = storage || new Map();
    const location = {origin: "https://memo.example", pathname: "/items/1", search: "?page=2", href: "https://memo.example/items/1?page=2",
        assign(url) { this.assigned = url; }};
    const form = new Element();
    form.elements = [new Element("memo_title", "text", "draft title"), new Element("content", "textarea", "draft body\nsecond line")];
    form.dataset = {draftFields: "memo_title content", draftId: id};
    form.action = `https://memo.example${action}`; form.method = "post";
    form.getAttribute = (name) => name === "action" ? action : null;
    form.checkValidity = () => true;
    form.closest = () => null;
    form.querySelector = (selector) => selector === "[data-draft-status]" ? form.status
        : selector === "[data-draft-reload]" ? form.children.find((child) => child.dataset.draftReload !== undefined) : null;
    form.append = (element) => { form.children.push(element); if (element.name) form.elements.push(element); else if (element.dataset.draftStatus !== undefined) form.status = element; };
    const document = {body: {dataset: {draftService: service, writeAuthenticated: String(authenticated)}},
        querySelectorAll(selector) { return selector === "[data-draft-fields]" ? [form] : []; },
        createElement() { return new Element(); },
        listeners: new Map(), addEventListener(type, callback) { if (type === "DOMContentLoaded") this.ready = callback; else this.listeners.set(type, callback); }};
    const window = {document, location, sessionStorage: {
        get length() { return values.size; }, key(index) { return [...values.keys()][index]; },
        getItem(key) { return values.get(key) || null; }, setItem(key, value) { values.set(key, value); },
        removeItem(key) { values.delete(key); }}, confirm: () => true, crypto: {randomUUID},
        listeners: new Map(), addEventListener(type, callback) { this.listeners.set(type, callback); },
        async fetch() { return {status: 401, ok: false, url: "https://memo.example/write"}; }};
    class FormData {
        constructor(target) { this.fields = (target.elements || []).map(({name, value}) => [name, value]); }
        set(name, value) { this.fields = this.fields.filter(([key]) => key !== name); this.fields.push([name, value]); }
        get(name) { return this.fields.find(([key]) => key === name)?.[1]; }
    }
    class Event { constructor(type) { this.type = type; } }
    vm.runInNewContext(script, {window, document, URL, FormData, Event});
    const submit = () => form.listeners.get("submit")({defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }});
    return {window, document, form, values, submit, boot: () => document.ready()};
}

test("expired submission is restored after login without resubmitting", async () => {
    const first = fixture();
    first.window.fetch = async (_url, options) => {
        assert.equal(options.headers.Accept, "application/json");
        assert.equal(options.credentials, "same-origin");
        return {status: 401, ok: false, url: "https://memo.example/write"};
    };
    first.boot(); await first.submit();
    assert.equal(first.values.size, 1);
    assert.equal(first.window.location.assigned, "/auth/login?next_path=%2Fitems%2F1%3Fpage%3D2");
    const next = fixture({storage: first.values});
    next.form.elements.forEach((field) => { field.value = ""; });
    let writes = 0; next.window.fetch = async () => { writes += 1; };
    next.boot();
    assert.equal(next.form.elements[0].value, "draft title");
    assert.equal(next.form.elements[1].value, "draft body\nsecond line");
    assert.equal(writes, 0);
});

test("anonymous forms neither write nor restore private draft contents", async () => {
    const first = fixture(); first.boot(); await first.submit();
    const next = fixture({storage: first.values, authenticated: false});
    next.form.elements[1].value = "public default";
    let writes = 0; next.window.fetch = async () => { writes += 1; };
    next.boot(); await next.submit();
    assert.equal(next.form.elements[1].value, "public default");
    assert.equal(writes, 0);
});

test("failed and invalid submissions retain values and draft", async () => {
    for (const failure of [400, 403, 422, 500, "network", "validation"]) {
        const page = fixture(); page.boot();
        if (failure === "validation") page.form.checkValidity = () => false;
        page.window.fetch = async () => {
            if (failure === "network") throw new Error("offline");
            return {status: failure, ok: false, url: "https://memo.example/items/1"};
        };
        await page.submit();
        assert.equal(page.form.elements[1].value, "draft body\nsecond line");
        assert.equal(page.window.location.assigned, undefined);
        if (failure !== "validation") assert.equal(page.values.size, 1);
    }
});

test("success clears this form only and refuses login HTML as success", async () => {
    const other = fixture({id: "other"}); other.boot(); await other.submit();
    const page = fixture({storage: other.values}); page.boot();
    page.window.fetch = async () => ({status: 200, ok: true, url: "https://memo.example/auth/login?next_path=%2Fitems%2F1"});
    await page.submit(); assert.equal(page.values.size, 2);
    page.window.fetch = async () => ({status: 200, ok: true, url: "https://memo.example/items/1"});
    await page.submit(); assert.equal(page.values.size, 1);
    assert.match([...page.values.values()][0], /draft body/);
    assert.equal(page.window.location.assigned, "https://memo.example/items/1");
});

test("storage failure blocks expired-session navigation and preserves contents", async () => {
    const page = fixture(); page.window.sessionStorage.setItem = () => { throw new Error("quota"); };
    page.boot(); await page.submit();
    assert.equal(page.window.location.assigned, undefined);
    assert.equal(page.form.elements[1].value, "draft body\nsecond line");
    assert.match(page.form.status.textContent, /복사/);
});

test("password hidden authentication and unlisted fields never enter storage", async () => {
    const page = fixture();
    page.form.dataset.draftFields += " password token csrf_token secret";
    page.form.elements.push(new Element("password", "password", "password-secret"),
        new Element("token", "hidden", "token-secret"), new Element("csrf_token", "text", "csrf-secret"),
        new Element("secret", "hidden", "hidden-secret"), new Element("unlisted", "text", "unlisted-secret"));
    page.boot(); await page.submit();
    const stored = [...page.values.values()].join("");
    assert.doesNotMatch(stored, /password-secret|token-secret|csrf-secret|hidden-secret|unlisted-secret/);
});

test("different actions fields and stable ids cannot overwrite another form", async () => {
    const first = fixture(); first.boot(); await first.submit();
    for (const options of [{action: "/memos/1"}, {id: "chapter-1"}, {}]) {
        const page = fixture({...options, storage: first.values});
        if (!options.action && !options.id) page.form.dataset.draftFields = "content";
        page.boot(); await page.submit();
    }
    assert.equal(first.values.size, 4);
});

test("select change updates dynamic action before comment restoration", async () => {
    const first = fixture({id: "chapter-comment"});
    first.form.dataset.draftFields = "chapter_id comment";
    first.form.elements = [new Element("chapter_id", "select-one", "12"), new Element("comment", "textarea", "edited comment")];
    first.boot(); await first.submit();
    const page = fixture({id: "chapter-comment", storage: first.values});
    page.form.dataset.draftFields = "chapter_id comment";
    const select = new Element("chapter_id", "select-one", "");
    const comment = new Element("comment", "textarea", "old comment");
    select.addEventListener("change", () => { page.form.action = `/chapters/${select.value}/comment`; comment.value = "server comment"; });
    page.form.elements = [select, comment]; page.boot();
    assert.equal(page.form.action, "/chapters/12/comment"); assert.equal(comment.value, "edited comment");
});

test("logout removes only this service drafts", async () => {
    const page = fixture(); page.boot(); await page.submit();
    page.values.set("unrelated-state", "preserve");
    page.window.MemoDrafts.clearDrafts(service);
    assert.deepEqual([...page.values.keys()], ["unrelated-state"]);
});

test("login navigation also preserves another modified form but not untouched defaults", async () => {
    const page = fixture();
    const other = fixture({id: "other-form"}).form;
    const untouched = fixture({id: "untouched-form"}).form;
    page.document.querySelectorAll = (selector) => selector === "[data-draft-fields]" ? [page.form, other, untouched] : [];
    page.boot(); other.elements[1].value = "unsent other comment";
    await page.submit();
    assert.equal(page.values.size, 2);
    assert.match([...page.values.values()].join(""), /unsent other comment/);
    assert.doesNotMatch([...page.values.keys()].join(""), /untouched-form/);
});

test("another modified form storage failure blocks login navigation too", async () => {
    const page = fixture(); const other = fixture({id: "other-form"}).form;
    page.document.querySelectorAll = (selector) => selector === "[data-draft-fields]" ? [page.form, other] : [];
    page.boot(); other.elements[1].value = "unsent other comment";
    const setItem = page.window.sessionStorage.setItem;
    page.window.sessionStorage.setItem = (key, value) => {
        if (key.includes("other-form")) throw new Error("quota");
        setItem(key, value);
    };
    await page.submit();
    assert.equal(page.window.location.assigned, undefined);
    assert.equal(other.elements[1].value, "unsent other comment");
    assert.match(page.form.status.textContent, /복사/);
});

test("cross-origin targets and responses never receive contents or clear drafts", async () => {
    const page = fixture(); page.boot();
    let writes = 0;
    page.window.fetch = async () => { writes += 1; return {status: 200, ok: true, url: "https://outside.example/"}; };
    page.form.action = "https://outside.example/submit";
    await page.submit(); assert.equal(writes, 0); assert.equal(page.values.size, 1);
    page.form.action = "https://memo.example/submit";
    await page.submit(); assert.equal(writes, 1); assert.equal(page.values.size, 1);
    assert.equal(page.window.location.assigned, undefined);
});

test("dynamic TOC candidates and associated source restore without HTML injection", async () => {
    const first = fixture({id: "toc"}); first.form.dataset.draftFields = "toc_source titles";
    const selected = new Element("titles", "checkbox", "<b>first chapter</b>"); selected.checked = true;
    const unselected = new Element("titles", "checkbox", "second chapter");
    first.form.elements = [new Element("toc_source", "textarea", "source\nsecond line"), selected, unselected];
    first.boot(); await first.submit();
    const page = fixture({id: "toc", storage: first.values}); page.form.dataset.draftFields = "toc_source titles";
    page.form.hidden = true; page.form.elements = [new Element("toc_source", "textarea", "")];
    const container = new Element(); container.hidden = true;
    container.append = (label) => { container.children.push(label); page.form.elements.push(label.children[0]); };
    page.form.querySelector = (selector) => selector === ".toc-candidates" ? container : page.form.status;
    page.boot();
    assert.equal(page.form.elements[0].value, "source\nsecond line");
    assert.equal(page.form.elements[1].checked, true); assert.equal(page.form.elements[2].checked, false);
    assert.equal(container.children[0].children[1].textContent, "<b>first chapter</b>");
    assert.equal(container.hidden, false); assert.equal(page.form.hidden, false);
});

test("expired deferred request persists the latest submitted fields before redirect", async () => {
    const page = fixture(); page.boot(); let resolve;
    page.window.fetch = () => new Promise((done) => { resolve = done; });
    const pending = page.submit();
    page.form.elements[1].value += "\nadded while pending";
    resolve({status: 401, ok: false, url: "https://memo.example/write"}); await pending;
    const next = fixture({storage: page.values}); next.boot();
    assert.equal(next.form.elements[1].value, "draft body\nsecond line\nadded while pending");
});

test("request locks every associated field then restores each disabled state", async () => {
    const page = fixture(); const originalDisabled = new Element("metadata", "hidden", "fixed");
    originalDisabled.disabled = true; page.form.elements.push(originalDisabled); page.boot(); let resolve;
    page.window.fetch = () => new Promise((done) => { resolve = done; });
    const pending = page.submit();
    assert.equal(page.form.elements[0].disabled, true);
    assert.equal(page.form.elements[1].disabled, true);
    resolve({status: 422, ok: false, url: "https://memo.example/write"}); await pending;
    assert.equal(page.form.elements[0].disabled, false);
    assert.equal(page.form.elements[1].disabled, false);
    assert.equal(originalDisabled.disabled, true);
});

test("success also saves another dirty form or blocks navigation after confirmed save", async () => {
    for (const quotaFailure of [false, true]) {
        const page = fixture(); const other = fixture({id: "other-form"}).form;
        page.document.querySelectorAll = (selector) => selector === "[data-draft-fields]" ? [page.form, other] : [];
        page.boot(); other.elements[1].value = "new unsent other";
        const setItem = page.window.sessionStorage.setItem;
        page.window.sessionStorage.setItem = (key, value) => {
            if (quotaFailure && key.includes("other-form")) throw new Error("quota");
            setItem(key, value);
        };
        let writes = 0;
        page.window.fetch = async () => { writes += 1; return {status: 200, ok: true, url: "https://memo.example/items/1"}; };
        await page.submit();
        if (quotaFailure) {
            assert.equal(page.window.location.assigned, undefined);
            assert.match(page.form.status.textContent, /저장은 완료/);
            assert.match(page.form.status.textContent, /복사/);
            await page.submit(); assert.equal(writes, 1);
        } else {
            assert.equal(page.values.size, 1);
            assert.match([...page.values.values()][0], /new unsent other/);
        }
    }
});

test("latest draft write failure after a deferred 401 blocks login navigation", async () => {
    const page = fixture(); page.boot(); let resolve;
    page.window.fetch = () => new Promise((done) => { resolve = done; });
    const pending = page.submit(); page.form.elements[1].value += " new text";
    page.window.sessionStorage.setItem = () => { throw new Error("quota"); };
    resolve({status: 401, ok: false, url: "https://memo.example/write"}); await pending;
    assert.equal(page.window.location.assigned, undefined);
    assert.match(page.form.status.textContent, /복사/);
});

test("existing delete handlers preserve dirty drafts before 401 and login HTML redirects", async () => {
    for (const templatePath of ["home", service === "book-memo" ? "book_detail" : "video_detail"]) {
        const template = readFileSync(resolve(service, `app/templates/${templatePath}.html`), "utf8");
        const inline = template.match(/<script>([\s\S]*?)<\/script>/)?.[1];
        const redirect = inline.match(/const redirectToWriteLogin = \(\) => \{[\s\S]*?\n\};/)?.[0];
        const deletion = inline.slice(inline.indexOf('document.querySelectorAll(".protected-delete-form")'));
        for (const loginHtml of [false, true]) {
            const page = fixture(); const deletionForm = new Element();
            deletionForm.action = "https://memo.example/memos/1/delete";
            page.document.querySelectorAll = (selector) => selector === "[data-draft-fields]" ? [page.form]
                : selector === ".protected-delete-form" ? [deletionForm] : [];
            page.boot(); page.form.elements[1].value = "dirty before delete";
            const response = loginHtml ? {status: 200, ok: true, url: "https://memo.example/auth/login"}
                : {status: 401, ok: false, url: "https://memo.example/write"};
            page.window.fetch = async () => response;
            class FormData { constructor() {} }
            vm.runInNewContext(`${redirect}\n${deletion}`, {window: page.window, document: page.document,
                fetch: page.window.fetch, FormData, URL});
            await deletionForm.listeners.get("submit")({preventDefault() {}});
            assert.equal(page.values.size, 1);
            assert.match([...page.values.values()][0], /dirty before delete/);
            assert.equal(page.window.location.assigned, "/auth/login?next_path=%2Fitems%2F1%3Fpage%3D2");
        }
    }
});

test("delete success preserves dirty draft and quota failure never repeats a committed delete", async () => {
    for (const quota of [false, true]) {
        const page = fixture(); page.boot(); page.form.elements[1].value = "dirty after delete";
        const deletion = new Element(); deletion.action = "https://memo.example/memos/1/delete";
        page.window.MemoDrafts.bindDeleteForm?.(deletion);
        if (quota) page.window.sessionStorage.setItem = () => { throw new Error("quota"); };
        let deletes = 0;
        page.window.fetch = async () => { deletes++; return {status: 200, ok: true, url: "https://memo.example/items/1"}; };
        await deletion.listeners.get("submit")?.({preventDefault() {}});
        assert.equal(deletes, 1);
        if (quota) {
            assert.equal(page.window.location.assigned, undefined);
            assert.match(deletion.status.textContent, /삭제.*완료/);
            await deletion.listeners.get("submit")({preventDefault() {}});
            assert.equal(deletes, 1);
        } else {
            assert.match([...page.values.values()].join(""), /dirty after delete/);
            assert.equal(page.window.location.assigned, "https://memo.example/items/1");
        }
    }
});

test("lost response retries use the same request id through login and changed text gets a new id", async () => {
    const page = fixture(); page.boot(); const ids = [];
    page.window.fetch = async (_url, options) => { ids.push(options.body.get("request_id")); throw new Error("lost after commit"); };
    await page.submit(); await page.submit();
    assert.match(ids[0], /^[a-f0-9-]{36}$/);
    assert.equal(ids[1], ids[0]);
    const resumed = fixture({storage: page.values}); resumed.boot();
    resumed.window.fetch = async (_url, options) => { ids.push(options.body.get("request_id")); return {status: 401, ok: false}; };
    await resumed.submit(); assert.equal(ids[2], ids[0]);
    resumed.form.elements[1].value += " changed";
    await resumed.submit(); assert.notEqual(ids[3], ids[0]);
});

test("old draft edit version survives current hidden revision and conflict stays visible", async () => {
    const first = fixture(); const version = new Element("expected_version", "hidden", "1");
    first.form.elements.push(version); first.boot(); await first.submit();
    const resumed = fixture({storage: first.values}); const latest = new Element("expected_version", "hidden", "2");
    resumed.form.elements.push(latest); resumed.boot();
    assert.equal(latest.value, "1");
    resumed.window.fetch = async () => ({status: 409, ok: false, url: "https://memo.example/items/1"});
    await resumed.submit();
    assert.match(resumed.form.status.textContent, /다른 화면|충돌|최신/);
    assert.equal(latest.value, "1");
    assert.equal(resumed.window.location.assigned, undefined);
});

test("leaving a dirty editor triggers a browser guard without storing credentials", () => {
    const page = fixture(); page.boot(); page.form.elements[1].value = "unsaved";
    let prevented = false;
    page.window.listeners.get("beforeunload")?.({preventDefault() {prevented=true;}});
    assert.equal(prevented, true);
});

test("legacy drafts without a recorded version cannot masquerade as the latest edit", async () => {
    const first = fixture(); first.boot(); await first.submit();
    const resumed = fixture({storage: first.values}); const version = new Element("expected_version", "hidden", "3");
    resumed.form.elements.push(version); resumed.boot();
    assert.equal(version.value, "0");
});

test("conflict recovery explicitly discards only this draft and quota failures preserve it", async () => {
    for (const quota of [false, true]) {
        const page = fixture(); page.boot();
        page.window.fetch = async () => ({status: 409, ok: false, url: "https://memo.example/items/1"});
        await page.submit();
        page.values.set("unrelated-draft", "leave alone");
        const button = page.form.querySelector("[data-draft-reload]");
        assert.ok(button);
        if (quota) page.window.sessionStorage.removeItem = () => { throw new Error("storage failure"); };
        button.listeners.get("click")();
        assert.equal(page.values.get("unrelated-draft"), "leave alone");
        if (quota) {
            assert.equal(page.window.location.assigned, undefined);
            assert.equal(page.values.size, 2);
        } else {
            assert.equal(page.values.size, 1);
            assert.equal(page.window.location.assigned, "https://memo.example/items/1?page=2");
        }
    }
});

test("a new memo entered after a successful save uses a fresh request id", async () => {
    const page = fixture(); page.boot(); const ids = [];
    page.window.fetch = async (_url, options) => {
        ids.push(options.body.get("request_id"));
        return {status: 200, ok: true, url: "https://memo.example/items/1"};
    };
    await page.submit(); await page.submit();
    assert.equal(ids.length, 1);
    page.form.elements[1].value = "next memo";
    await page.submit();
    assert.equal(ids.length, 2);
    assert.notEqual(ids[0], ids[1]);
});

test("database busy preserves draft and revision and retries with the same request id", async () => {
    const page = fixture();
    page.form.elements.push(new Element("expected_version", "hidden", "7"));
    const ids = [];
    page.window.fetch = async (_url, options) => {
        ids.push(options.body.get("request_id"));
        assert.equal(options.body.get("expected_version"), "7");
        assert.equal(options.body.get("content"), "draft body\nsecond line");
        return {status: 503, ok: false, url: "https://memo.example/items/1"};
    };
    page.boot(); await page.submit();
    assert.match(page.form.status.textContent, /잠시 후 다시 저장/);
    assert.equal(page.values.size, 1);
    assert.equal(page.window.location.assigned, undefined);
    assert.ok(page.form.elements.every(field => !field.disabled));
    await page.submit();
    assert.equal(ids.length, 2);
    assert.equal(ids[0], ids[1]);
});
