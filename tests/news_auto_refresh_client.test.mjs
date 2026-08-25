import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const template = readFileSync(resolve("crawler-worker/app/templates/search.html"), "utf8");
const clientScript = template.match(/<script id="news-auto-refresh">([\s\S]*?)<\/script>/)?.[1];

if (!clientScript) {
    throw new Error("뉴스 자동 갱신 스크립트를 찾을 수 없습니다.");
}

class TestElement {
    constructor(tagName = "div") {
        this.tagName = tagName.toUpperCase();
        this.attributes = new Map();
        this.children = [];
        this.className = "";
        this.dataset = {};
        this.href = "";
        this.textContent = "";
    }

    append(child) {
        if (child.isFragment) {
            this.children.push(...child.children);
            return;
        }
        this.children.push(child);
    }

    replaceChildren(child) {
        this.children = [];
        this.append(child);
    }

    setAttribute(name, value) {
        this.attributes.set(name, value);
    }

    querySelectorAll(selector) {
        const matches = [];
        const visit = (element) => {
            if (selector === "a.article-link" && element.tagName === "A" && element.className === "article-link") {
                matches.push(element);
            }
            element.children.forEach(visit);
        };
        this.children.forEach(visit);
        return matches;
    }
}

function allText(element) {
    return [element.textContent, ...element.children.map(allText)].join(" ");
}

test("자동 새로고침은 UTC 기사 시각을 KST 분 단위로 표시한다", async () => {
    const container = new TestElement();
    container.dataset.category = "KR_WORLD";
    container.dataset.autoRefreshSeconds = "60";
    const newsList = new TestElement("section");
    const existingLink = new TestElement("a");
    existingLink.className = "article-link";
    existingLink.href = "https://example.com/market";
    newsList.append(existingLink);
    const status = new TestElement();
    let refreshNews;

    globalThis.document = {
        hidden: false,
        querySelector(selector) {
            return {
                "[data-auto-refresh-seconds]": container,
                "[data-news-list]": newsList,
                "[data-refresh-status]": status,
            }[selector] || null;
        },
        createElement(tagName) {
            return new TestElement(tagName);
        },
        createDocumentFragment() {
            const fragment = new TestElement();
            fragment.isFragment = true;
            return fragment;
        },
    };
    globalThis.window = {
        location: { origin: "https://news.example.com" },
        setInterval(callback) {
            refreshNews = callback;
        },
    };
    globalThis.fetch = async () => ({
        ok: true,
        async json() {
            return {
                count: 1,
                cache: { hit: false },
                articles: [
                    {
                        title_ko: "시장 충격 발생",
                        provider: "Investing.com RSS",
                        source: "Investing.com 한국어",
                        collected_at: "2026-07-10T15:58:15.761236+00:00",
                        published_at: "Fri, 10 Jul 2026 15:58:15 +0000",
                        topics: ["세계동향"],
                        summary: "기존 보관 기사에서 중요 뉴스로 갱신됨",
                        url: "https://example.com/market",
                        nasdaq_relevance: { level: "alert", reasons: ["시장 급변"] },
                    },
                ],
            };
        },
    });

    eval(clientScript);
    await refreshNews();

    assert.equal(newsList.children.length, 1);
    assert.equal(newsList.children[0].dataset.newsAlertStatus, "alert");
    const cardText = allText(newsList.children[0]);
    assert.match(cardText, /시장 충격 발생/);
    assert.match(cardText, /기존 보관 기사에서 중요 뉴스로 갱신됨/);
    assert.match(cardText, /텔레그램 알림 대상/);
    assert.match(cardText, /분류 사유: 시장 급변/);
    assert.equal((cardText.match(/2026-07-11 00:58/g) || []).length, 2);
    assert.doesNotMatch(cardText, /2026년|2026-07-10T15:58:15|00:58:15|KST/);
});

test("자동 새로고침은 호스트 로컬 시간대와 관계없이 naive ISO 시각을 UTC 기준 KST로 표시한다", async () => {
    const originalTimezone = process.env.TZ;
    process.env.TZ = "America/New_York";

    try {
        const container = new TestElement();
        container.dataset.category = "KR_WORLD";
        container.dataset.autoRefreshSeconds = "60";
        const newsList = new TestElement("section");
        const status = new TestElement();
        let refreshNews;

        globalThis.document = {
            hidden: false,
            querySelector(selector) {
                return {
                    "[data-auto-refresh-seconds]": container,
                    "[data-news-list]": newsList,
                    "[data-refresh-status]": status,
                }[selector] || null;
            },
            createElement(tagName) {
                return new TestElement(tagName);
            },
            createDocumentFragment() {
                const fragment = new TestElement();
                fragment.isFragment = true;
                return fragment;
            },
        };
        globalThis.window = {
            location: { origin: "https://news.example.com" },
            setInterval(callback) {
                refreshNews = callback;
            },
        };
        globalThis.fetch = async () => ({
            ok: true,
            async json() {
                return {
                    count: 1,
                    cache: { hit: false },
                    articles: [{
                        title_ko: "naive ISO 기사",
                        provider: "RSS",
                        source: "RSS",
                        collected_at: "2026-07-09T01:02:03",
                    }],
                };
            },
        });

        eval(clientScript);
        await refreshNews();

        const cardText = allText(newsList.children[0]);
        assert.match(cardText, /naive ISO 기사/);
        assert.match(cardText, /2026-07-09 10:02/);
        assert.doesNotMatch(cardText, /2026-07-09 14:02/);
    } finally {
        if (originalTimezone === undefined) {
            delete process.env.TZ;
        } else {
            process.env.TZ = originalTimezone;
        }
    }
});

test("자동 새로고침은 해석할 수 없는 기사 시각을 표시하지 않는다", async () => {
    const container = new TestElement();
    container.dataset.category = "KR_WORLD";
    container.dataset.autoRefreshSeconds = "60";
    const newsList = new TestElement("section");
    const status = new TestElement();
    let refreshNews;

    globalThis.document = {
        hidden: false,
        querySelector(selector) {
            return {
                "[data-auto-refresh-seconds]": container,
                "[data-news-list]": newsList,
                "[data-refresh-status]": status,
            }[selector] || null;
        },
        createElement(tagName) {
            return new TestElement(tagName);
        },
        createDocumentFragment() {
            const fragment = new TestElement();
            fragment.isFragment = true;
            return fragment;
        },
    };
    globalThis.window = {
        location: { origin: "https://news.example.com" },
        setInterval(callback) {
            refreshNews = callback;
        },
    };
    globalThis.fetch = async () => ({
        ok: true,
        async json() {
            return {
                count: 1,
                cache: { hit: false },
                articles: [{
                    title_ko: "시간 오류 기사",
                    provider: "RSS",
                    source: "RSS",
                    collected_at: "2026-07-09 01:02:03 UTC",
                    published_at: "2026-99-99T01:02:03+00:00",
                }],
            };
        },
    });

    eval(clientScript);
    await refreshNews();

    const cardText = allText(newsList.children[0]);
    assert.match(cardText, /시간 오류 기사/);
    assert.doesNotMatch(cardText, /2026-|2026-07-09 01:02:03 UTC|01:02:03|UTC|KST/);
});
