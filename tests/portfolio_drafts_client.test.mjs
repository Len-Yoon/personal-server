import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../portal-web/app/static/js/portfolio-drafts.js', import.meta.url), 'utf8');
function fixture({ draft = null, blocked = false, response = { ok: true, status: 200 } } = {}) {
    const handlers = {};
    const data = new Map(draft === null ? [] : [['portfolio-editor:draft:v1', draft]]);
    const content = { value: 'saved', addEventListener: (name, callback) => { handlers[name] = callback; } };
    const button = { disabled: false };
    const status = { setAttribute() {}, textContent: '' };
    const navigations = [];
    const form = {
        action: 'http://fixture/admin/save', elements: { namedItem: () => content },
        querySelector: () => button, append() {},
        addEventListener: (name, callback) => { handlers[name] = callback; },
    };
    vm.runInNewContext(script, {
        document: { querySelector: () => form, createElement: () => status },
        window: { location: { assign: (url) => navigations.push(url) },
                  addEventListener: (name, callback) => { handlers[name] = callback; } },
        sessionStorage: {
            getItem(key) { if (blocked) throw Error('storage blocked'); return data.get(key) ?? null; },
            setItem(key, value) { if (blocked) throw Error('storage blocked'); data.set(key, value); },
            removeItem(key) { if (blocked) throw Error('storage blocked'); data.delete(key); },
        },
        FormData: class {}, fetch: typeof response === 'function' ? response : async () => response,
    });
    return { content, button, status, data, navigations, handlers,
             submit: () => handlers.submit({ preventDefault() {} }) };
}

test('expired authentication preserves only content before login navigation', async () => {
    const view = fixture({ response: { ok: false, status: 401 } });
    view.content.value = 'unfinished';
    await view.submit();
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'unfinished');
    assert.deepEqual(view.navigations, ['/admin']);
    assert.equal(fixture({ draft: 'unfinished' }).content.value, 'unfinished');
});

test('storage failure on expired authentication prevents navigation', async () => {
    const view = fixture({ blocked: true, response: { ok: false, status: 401 } });
    view.content.value = 'unfinished';
    await view.submit();
    assert.equal(view.content.value, 'unfinished');
    assert.deepEqual(view.navigations, []);
    assert.match(view.status.textContent, /보관하지 못/);
    assert.equal(view.button.disabled, false);
});

test('failed save keeps draft and active editor', async () => {
    const view = fixture({ response: async () => { throw Error('lost response'); } });
    view.content.value = 'retry text';
    await view.submit();
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'retry text');
    assert.deepEqual(view.navigations, []);
    assert.equal(view.button.disabled, false);
});

test('successful save does not discard typing added while response is pending', async () => {
    let finish;
    const view = fixture({ response: () => new Promise((resolve) => { finish = resolve; }) });
    view.content.value = 'submitted';
    const pending = view.submit();
    view.content.value = 'later typing';
    finish({ ok: true, status: 200 });
    await pending;
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'later typing');
    assert.deepEqual(view.navigations, []);
});

test('successful save clears draft then reloads', async () => {
    const view = fixture({ draft: 'ready' });
    await view.submit();
    assert.equal(view.data.size, 0);
    assert.deepEqual(view.navigations, ['/admin']);
});
