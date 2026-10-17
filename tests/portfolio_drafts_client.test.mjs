import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../portal-web/app/static/js/portfolio-drafts.js', import.meta.url), 'utf8');
const initialRevision = 'a'.repeat(64);
const savedRevision = 'b'.repeat(64);
function fixture({ draft = null, draftRevision = initialRevision, blocked = false,
                   response = { ok: true, status: 200, json: async () => ({ revision: savedRevision }) } } = {}) {
    const handlers = {};
    const data = new Map(draft === null ? [] : [['portfolio-editor:draft:v1', draft]]);
    if (draft !== null && draftRevision !== null) data.set('portfolio-editor:revision:v1', draftRevision);
    const content = { value: 'saved', addEventListener: (name, callback) => { handlers[name] = callback; } };
    const revision = { value: initialRevision };
    const button = { disabled: false };
    const status = { setAttribute() {}, textContent: '' };
    const conflictPanel = { hidden: true };
    const serverContent = { value: '' };
    const reviewButton = { disabled: false, addEventListener: (name, callback) => { handlers.review = callback; } };
    const nodes = { 'portfolio-conflict': conflictPanel, 'conflict-server-content': serverContent,
                    'portfolio-review-revision': reviewButton };
    const navigations = [];
    const form = {
        action: 'http://fixture/admin/save', elements: { namedItem: (name) => name === 'content' ? content : revision },
        querySelector: () => button, append() {},
        addEventListener: (name, callback) => { handlers[name] = callback; },
    };
    vm.runInNewContext(script, {
        document: { querySelector: () => form, createElement: () => status, getElementById: (id) => nodes[id] },
        window: { location: { assign: (url) => navigations.push(url) },
                  addEventListener: (name, callback) => { handlers[name] = callback; } },
        sessionStorage: {
            getItem(key) { if (blocked) throw Error('storage blocked'); return data.get(key) ?? null; },
            setItem(key, value) { if (blocked) throw Error('storage blocked'); data.set(key, value); },
            removeItem(key) { if (blocked) throw Error('storage blocked'); data.delete(key); },
        },
        FormData: class {}, fetch: typeof response === 'function' ? response : async () => response,
    });
    return { content, revision, button, status, conflictPanel, serverContent, data, navigations, handlers,
             submit: () => handlers.submit({ preventDefault() {} }) };
}

test('expired authentication preserves content and original revision before login navigation', async () => {
    const view = fixture({ response: { ok: false, status: 401 } });
    view.content.value = 'unfinished';
    await view.submit();
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'unfinished');
    assert.equal(view.data.get('portfolio-editor:revision:v1'), initialRevision);
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
    assert.equal(view.revision.value, initialRevision);
    assert.deepEqual(view.navigations, []);
    assert.equal(view.button.disabled, false);
});

test('successful save does not discard typing added while response is pending', async () => {
    let finish;
    const view = fixture({ response: () => new Promise((resolve) => { finish = resolve; }) });
    view.content.value = 'submitted';
    const pending = view.submit();
    view.content.value = 'later typing';
    finish({ ok: true, status: 200, json: async () => ({ revision: savedRevision }) });
    await pending;
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'later typing');
    assert.equal(view.data.get('portfolio-editor:revision:v1'), savedRevision);
    assert.deepEqual(view.navigations, []);
});

test('successful save clears draft then reloads', async () => {
    const view = fixture({ draft: 'ready' });
    await view.submit();
    assert.equal(view.data.size, 0);
    assert.deepEqual(view.navigations, ['/admin']);
});

test('old draft keeps its original revision rather than adopting the new page revision', () => {
    const oldRevision = 'c'.repeat(64);
    const restored = fixture({ draft: 'old unfinished work', draftRevision: oldRevision });
    assert.equal(restored.content.value, 'old unfinished work');
    assert.equal(restored.revision.value, oldRevision);
    const legacy = fixture({ draft: 'legacy unfinished work', draftRevision: null });
    assert.equal(legacy.revision.value, '');
});

test('conflict preserves draft and requires explicit review before adopting server revision', async () => {
    const view = fixture({ response: { ok: false, status: 409, json: async () => ({
        current_revision: savedRevision, current_content: '<script>server text</script>',
    }) } });
    view.content.value = 'my unfinished input';
    await view.submit();
    assert.equal(view.content.value, 'my unfinished input');
    assert.equal(view.revision.value, initialRevision);
    assert.equal(view.conflictPanel.hidden, false);
    assert.equal(view.serverContent.value, '<script>server text</script>');
    assert.equal(view.data.get('portfolio-editor:revision:v1'), initialRevision);
    assert.deepEqual(view.navigations, []);
    view.handlers.review();
    assert.equal(view.revision.value, savedRevision);
    assert.equal(view.data.get('portfolio-editor:revision:v1'), savedRevision);
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'my unfinished input');
    assert.deepEqual(view.navigations, []);
});

test('malformed conflict response never adopts untrusted revision or navigates', async () => {
    const view = fixture({ response: { ok: false, status: 409, json: async () => ({
        current_revision: '<script>bad</script>', current_content: 'server',
    }) } });
    view.content.value = 'keep me';
    await view.submit();
    assert.equal(view.revision.value, initialRevision);
    assert.equal(view.data.get('portfolio-editor:draft:v1'), 'keep me');
    assert.deepEqual(view.navigations, []);
});
