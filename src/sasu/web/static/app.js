// 사수 웹 화면. 라이브러리 없이 DOM을 직접 만든다 (인터넷이 막힌 PC에서도 동작).
// 코드·주석·SQL은 저장소에서 온 글이므로 항상 textContent로 넣는다 (innerHTML 금지).
"use strict";

// ── 작은 도구 ────────────────────────────────────────────

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "open" || k === "disabled" || k === "checked") el[k] = true;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (sel) => document.querySelector(sel);
// replaceChildren은 null을 "null" 글자로 넣으므로 빈 값을 걸러서 넣는다
function setKids(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}
const fmt = (n) => (n ?? 0).toLocaleString("ko-KR");
const SVG_NS = "http://www.w3.org/2000/svg";
// 한글 설명(업무명). 서버가 아직 주지 않으면 null — 화면은 없을 때도 그대로 동작한다
const koOf = (o) => (o && (o.label_ko || o.desc_ko || o.desc)) || null;
const HANGUL = /[가-힣]/;
// 검색어 0건 안내: ‘{입력}’에 맞는 …가 없어요
const noMatch = (input, what) => `‘${input}’에 맞는 ${what} 없어요`;

// Lucide 스프라이트(index.html)의 <symbol id="i-이름">을 쓴다
function icon(name, cls) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("class", cls ? `icon ${cls}` : "icon");
  s.setAttribute("aria-hidden", "true");
  s.setAttribute("focusable", "false");
  const u = document.createElementNS(SVG_NS, "use");
  u.setAttribute("href", `#i-${name}`);
  s.append(u);
  return s;
}

// `명령`처럼 백틱으로 감싼 부분을 <code>로 보여 준다 (textContent만 씀)
function withCode(text) {
  return String(text).split(/`([^`]+)`/).map((part, i) => (i % 2 ? h("code", { class: "cmd" }, part) : part));
}

class ApiError extends Error {
  constructor(message, code, status, title, body) {
    super(message);
    this.code = code;
    this.status = status;
    this.title = title;
    this.body = body;
  }
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(init.json);
    delete init.json;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("사수 서버에 연결할 수 없어요.", "offline", 0,
      "사수 서버에 연결할 수 없어요", "`sasu serve`를 실행한 터미널 창이 열려 있는지 확인하세요.");
  }
  let data = null;
  try { data = await res.json(); } catch { /* 본문 없음 */ }
  if (!res.ok) {
    if (res.status >= 500) {
      throw new ApiError(`처리하다가 오류가 났어요 (${res.status})`, "server", res.status,
        `처리하다가 오류가 났어요 (${res.status})`, "터미널 창에 오류 내용이 있어요. 반복되면 그 내용을 함께 알려 주세요.");
    }
    throw new ApiError((data && data.detail) || `요청을 처리하지 못했어요 (${res.status})`, data && data.code, res.status);
  }
  return data;
}

// 화면 읽기 프로그램용 상주 live 영역 하나에 알린다
function announce(text) {
  const live = $("#live");
  live.textContent = "";
  setTimeout(() => { live.textContent = text; }, 30);
}

function toast(message) {
  document.querySelectorAll(".toast").forEach((el) => el.remove());
  const el = h("div", { class: "toast" }, message);
  document.body.append(el);
  announce(message);
  setTimeout(() => el.remove(), Math.max(2600, 1200 + message.length * 70));
}

function loading(text = "불러오는 중…") {
  announce(text);
  return h("div", { class: "loading" }, icon("loader-circle", "spin"), text);
}

function emptyState(iconName, title, body, ...actions) {
  return h("div", { class: "card empty" }, icon(iconName, "i40"), h("b", {}, title),
    body ? h("div", {}, withCode(body)) : null,
    actions.length ? h("div", { class: "row" }, actions) : null);
}

function errorBox(err, retry) {
  if (err.code === "no_project") return noProject();
  if (err.title) {
    return emptyState("circle-alert", err.title, err.body,
      retry ? h("button", { class: "btn", type: "button", onclick: retry }, icon("refresh-cw"), "다시 시도") : null);
  }
  return h("div", { class: "notice bad" }, icon("circle-alert"), h("div", {}, withCode(err.message || String(err))));
}

function noProject() {
  const cmd = "sasu index <프로젝트 폴더>";
  return emptyState("folder-git-2", "아직 분석한 프로젝트가 없어요",
    "터미널에서 프로젝트 폴더를 분석하면 지도와 질문이 생겨요. `sasu index <프로젝트 폴더>` · 예제로 해 보려면 `sasu fetch-sample`",
    h("button", { class: "btn", type: "button", onclick: () => copy(cmd) }, icon("copy"), "명령 복사"),
    h("button", { class: "btn", type: "button", onclick: () => location.reload() }, icon("refresh-cw"), "새로 고침"));
}

function fileName(p) {
  return (p || "").split("/").pop();
}

// 경로 앞부분을 줄인다: …/bbs/web/EgovArticleController.java:149
function shortLoc(location) {
  const parts = (location || "").split("/");
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : location;
}

// `.`·`/` 뒤에서만 줄이 바뀌게 <wbr>을 넣는다
function wbr(text) {
  const out = [];
  for (const piece of String(text).split(/(?<=[./])/)) out.push(piece, h("wbr"));
  out.pop();
  return out;
}

// "경로:줄" 또는 "경로:시작-끝"을 누르면 코드 서랍을 연다 (키보드로도 열리게 button)
function locLink(location, label, title) {
  if (!location) return null;
  return h("button", {
    class: "loc", type: "button", title: title || `코드 보기: ${location}`,
    onclick: (e) => { e.stopPropagation(); openCode(location); },
  }, label || location);
}

function locLabel(location) {
  const loc = parseLocation(location);
  return loc ? `${fileName(loc.path)}:${loc.start}` : fileName(location);
}

function parseLocation(location) {
  const m = /^(.*?):(\d+)(?:-(\d+))?$/.exec(location || "");
  if (!m) return null;
  return { path: m[1], start: Number(m[2]), end: Number(m[3] || m[2]) };
}

function copy(text, done = "복사했어요") {
  const ok = () => toast(done);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(ok, () => fallbackCopy(text, ok));
  } else fallbackCopy(text, ok);
}
function fallbackCopy(text, ok) {
  const ta = h("textarea", { style: "position:fixed;opacity:0" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  try { document.execCommand("copy"); ok(); } catch { toast("복사하지 못했어요"); }
  ta.remove();
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* 저장소를 못 쓰는 환경 */ }
  return null;
}

function recentList(key) {
  try { return JSON.parse(store(key) || "[]"); } catch { return []; }
}
function pushRecent(key, value) {
  const list = recentList(key).filter((x) => JSON.stringify(x) !== JSON.stringify(value));
  list.unshift(value);
  store(key, JSON.stringify(list.slice(0, 5)));
}

// 분석 시각을 로컬 시각 "10-11 22:36"으로
function stamp(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 저장 시각(UTC)을 로컬 날짜 "2026-10-15"로 (UTC로 자르면 하루 전이 되던 문제)
function localDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const focusableSel = 'a[href], button:not([disabled]), input:not([disabled]):not([tabindex="-1"]), select, textarea, [tabindex]:not([tabindex="-1"])';
function trapFocus(container, e) {
  if (e.key !== "Tab") return;
  const items = [...container.querySelectorAll(focusableSel)].filter((el) => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// ── 등급·근거 표시 (5절) ─────────────────────────────────

const GRADE = {
  사실: { cls: "fact", icon: "circle-check", name: "사실", desc: "코드·SQL에서 직접 확인한 내용이에요." },
  답변: { cls: "answer", icon: "user-round-check", name: "답변", desc: "전임자가 준 답변에 있는 내용이에요. 기억에 기댄 것이라 코드와 다를 수 있어요." },
  추정: { cls: "guess", icon: "circle-dashed", name: "추정", desc: "사수가 근거를 보고 짐작한 내용이에요. 고치기 전에 확인하세요." },
  모름: { cls: "unknown", icon: "circle-question-mark", name: "모름", desc: "근거가 없어 답하지 않았어요. 아래 질문을 전임자에게 물어보세요." },
  질문: { cls: "question", icon: "message-circle-question-mark", name: "물어볼 질문", desc: "전임자에게 그대로 보낼 수 있는 질문이에요." },
};
const LEVEL = {
  "확인된 사실": { name: "코드·SQL", cls: "fact", icon: "code-xml" },
  "인수인계 답변": { name: "받은 답변", cls: "answer", icon: "user-round-check" },
  기록: { name: "주석·커밋", cls: "record", icon: "git-commit-horizontal" },
};

function gradeChip(label, extra) {
  const g = GRADE[label] || GRADE.추정;
  return h("span", { class: `grade ${g.cls}${extra ? " " + extra : ""}`, title: g.desc }, icon(g.icon), g.name);
}

function gradeLegendLine() {
  return h("div", { class: "card legend-line" },
    ["사실", "답변", "추정", "모름", "질문"].map((k) => gradeChip(k)),
    h("span", { class: "muted" }, "사수는 코드·SQL·받은 답변만 근거로 답하고, 근거가 없으면 '모름'이라고 말합니다."));
}

// ── 코드 서랍 (7.6) ──────────────────────────────────────

let drawer = null;  // { el, scrim, prev }

function drawerWidth() {
  return Math.round(Math.max(480, Math.min(560, window.innerWidth - 886)));
}

async function openCode(location) {
  const loc = parseLocation(location);
  if (!loc) return;
  const prev = drawer ? drawer.prev : document.activeElement;
  closeDrawer(false);
  const wide = window.innerWidth >= 1280;
  const titleId = "drawer-title";
  const where = `${fileName(loc.path)}:${loc.start}`;
  const body = h("div", { class: "code", tabindex: "0", "aria-label": "코드" }, loading("코드를 읽는 중…"));
  const qchips = h("div", { class: "qchips hidden" });
  const title = h("h2", { id: titleId, tabindex: "-1" }, "코드 보기 · ", h("code", { title: `${loc.path}:${loc.start}` }, where));
  const el = h("aside", wide
    ? { class: "drawer", "aria-labelledby": titleId }
    : { class: "drawer as-modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
  h("header", {},
    title,
    h("button", { class: "btn sm", type: "button", onclick: () => copy(`${loc.path}:${loc.start}`, "위치를 복사했어요") }, icon("copy", "i14"), "위치 복사"),
    h("button", { class: "btn sm", type: "button", onclick: () => askWithTarget(`${loc.path}:${loc.start}`) }, icon("message-square-quote", "i14"), "이 코드로 묻기"),
    h("span", { class: "grow" }),
    h("button", { class: "btn sm", type: "button", onclick: () => closeDrawer() }, icon("x", "i14"), "닫기")),
  qchips, body);
  let scrim = null;
  if (wide) {
    document.documentElement.style.setProperty("--drawer-w", `${drawerWidth()}px`);
    document.body.classList.add("with-drawer");
  } else {
    scrim = h("div", { class: "scrim", onclick: () => closeDrawer() });
    el.addEventListener("keydown", (e) => trapFocus(el, e));
    document.body.append(scrim);
  }
  document.body.append(el);
  drawer = { el, scrim, prev };
  title.focus();
  try {
    const end = Math.max(loc.end, loc.start + 40);
    const data = await api(`/api/code?path=${encodeURIComponent(loc.path)}&start=${loc.start}&end=${end}`);
    if (!drawer || drawer.el !== el) return;
    setKids(body, ...data.lines.map((line, i) => {
      const n = data.start + i;
      const focus = n >= loc.start && n <= loc.end;
      return h("div", { class: focus ? "focus" : null, "data-line": n === loc.start ? "focus" : null },
        h("span", { class: "ln" }, n), line || " ");
    }));
    const focus = body.querySelector('[data-line="focus"]');
    if (focus) body.scrollTop = Math.max(0, focus.offsetTop - body.clientHeight / 3);
    // 좋음: 이 범위의 질문
    const last = data.start + data.lines.length - 1;
    const inRange = Q.data ? Q.data.items.filter((q) => q.file_path === loc.path && q.line >= data.start && q.line <= last) : [];
    if (inRange.length) {
      setKids(qchips, "이 범위의 질문", ...inRange.map((q) => questionBadge(q)));
      qchips.classList.remove("hidden");
    }
  } catch (err) {
    let box;
    if (err.status === 404 && /저장소 폴더/.test(err.message)) {
      box = emptyState("folder-git-2", "원본 코드 폴더를 찾지 못했어요",
        "분석한 뒤 폴더를 옮겼거나 지웠을 수 있어요. 지금 위치에서 `sasu index`를 다시 실행하세요. 분석 결과와 질문은 그대로 볼 수 있어요.",
        h("button", { class: "btn", type: "button", onclick: () => copy(`${loc.path}:${loc.start}`, "위치를 복사했어요") }, icon("copy"), "위치 복사"));
    } else if (err.status === 404) {
      box = emptyState("files", "이 파일이 원본 폴더에 없어요", "분석한 뒤 지웠거나 이름을 바꿨을 수 있어요.");
    } else box = errorBox(err);
    setKids(body, box);
  }
}

function closeDrawer(restore = true) {
  if (!drawer) return;
  const { el, scrim, prev } = drawer;
  el.remove();
  if (scrim) scrim.remove();
  document.documentElement.style.setProperty("--drawer-w", "0px");
  document.body.classList.remove("with-drawer");
  drawer = null;
  if (restore && prev && document.contains(prev)) prev.focus();
}

// ── 모달 (포커스 가두기·복귀) ────────────────────────────

const modals = [];
function modal(title, content, actions) {
  const prev = document.activeElement;
  const titleId = `modal-${Date.now()}`;
  let back;
  const close = () => {
    back.remove();
    modals.splice(modals.indexOf(close), 1);
    if (prev && document.contains(prev)) prev.focus();
  };
  const box = h("div", { class: "card modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("h2", { id: titleId, tabindex: "-1" }, title), ...content,
    h("div", { class: "actions" }, ...actions(close)));
  box.addEventListener("keydown", (e) => trapFocus(box, e));
  back = h("div", { class: "modal-back", onmousedown: (e) => { if (e.target === back) close(); } }, box);
  document.body.append(back);
  modals.push(close);
  (box.querySelector(".actions .primary") || box.querySelector("h2")).focus();
  return close;
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // 맨 위에 열린 것 하나만 닫는다
  if (modals.length) { modals[modals.length - 1](); return; }
  if (!$("#trace-suggest").classList.contains("hidden")) { hideSuggest(); return; }
  if (!$("#q-menu").classList.contains("hidden")) { toggleMenu(false); return; }
  if (drawer) closeDrawer();
});

// ── 탭·해시·프로젝트 ─────────────────────────────────────

const TAB_NAMES = { overview: "한눈에 보기", questions: "인수인계 질문", trace: "경로 추적", ask: "사수에게 묻기", cleanup: "고도화 준비" };
const TAB_ORDER = ["overview", "questions", "trace", "ask", "cleanup"];
const views = {};
const loaded = new Set();
let currentView = null;
let projectInfo = null;
const cache = {};

// "#trace?url=/cop/…" → { view: "trace", params: { url: "/cop/…" } }
function parseHash() {
  const raw = location.hash.slice(1);
  const i = raw.indexOf("?");
  const view = i < 0 ? raw : raw.slice(0, i);
  const params = i < 0 ? null : Object.fromEntries(new URLSearchParams(raw.slice(i + 1)));
  return { view: views[view] ? view : "overview", params };
}

function navigate(view, params) {
  const hash = `#${view}${params ? "?" + new URLSearchParams(params) : ""}`;
  if (location.hash !== hash) history.pushState(null, "", hash);
  show(view, params);
}

function show(view, params) {
  if (!views[view]) view = "overview";
  document.querySelectorAll("#tabs a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("on", s.id === `view-${view}`));
  document.title = `${TAB_NAMES[view]} · 사수`;
  if (currentView !== view) window.scrollTo(0, 0);
  currentView = view;
  const wasLoaded = loaded.has(view);
  if (!wasLoaded) {
    loaded.add(view);
    views[view](params);
  } else if (params && paramHandlers[view]) paramHandlers[view](params);
  // 인수인계 질문: 탭을 열 때마다 다른 사람이 저장한 답을 다시 불러온다
  if (view === "questions" && wasLoaded) pollQuestions();
  layoutOffsets();
}

const paramHandlers = {
  questions: (p) => { if (p.q) openQuestionById(Number(p.q)); },
  trace: (p) => { if (p.url && p.url !== traceState.last) traceUrl(p.url, { fromHash: true }); },
  ask: (p) => {
    if (!p.q) return;
    $("#ask-q").value = p.q;
    $("#ask-target").value = p.target || "";
    $("#ask-form").requestSubmit();
  },
};

window.addEventListener("hashchange", () => { const { view, params } = parseHash(); show(view, params); });
window.addEventListener("popstate", () => { const { view, params } = parseHash(); show(view, params); });

async function loadProjects() {
  const select = $("#project");
  try {
    const data = await api("/api/projects");
    setKids(select, ...(data.projects.length ? data.projects : ["(없음)"]).map((name) =>
      h("option", { value: name, selected: name === data.current }, name)));
    select.disabled = !data.projects.length;
    projectInfo = data.info ? { name: data.current, ...data.info } : null;
    const head = projectInfo && projectInfo.head && projectInfo.head !== "-" ? projectInfo.head.slice(0, 7) : "";
    const label = projectInfo ? `분석 기준 ${stamp(projectInfo.indexed_at)}${head ? ` · ${head}` : ""}` : "";
    $("#indexed").textContent = label;
    select.title = projectInfo ? `${projectInfo.repo_path}\n${label}` : "";
  } catch {
    setKids(select, h("option", {}, "(불러오기 실패)"));
  }
}

$("#project").addEventListener("change", async (e) => {
  try {
    await api("/api/projects/current", { json: { name: e.target.value } });
    toast(`${e.target.value} 프로젝트로 바꿨어요`);
    loaded.clear();
    for (const k of Object.keys(cache)) delete cache[k];
    Q.data = null;
    Q.drafts.clear();
    Q.byDrafts.clear();
    Q.justSaved.clear();
    Q.open = null;
    traceState.last = null;
    setKids($("#ask-result"), );
    setKids($("#trace-result"), );
    await loadProjects();
    loadQuestionCount();
    const { view } = parseHash();
    show(view);
  } catch (err) {
    toast(err.message);
  }
});

// 밝게/어둡게: 기본은 OS 설정을 따른다. 저장값은 <head>의 스크립트가 먼저 적용한다
(function theme() {
  const btn = $("#theme");
  const isDark = () => (document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches);
  const paint = () => {
    const dark = isDark();
    const label = dark ? "밝게 보기" : "어둡게 보기";
    btn.setAttribute("aria-label", label);
    btn.title = label;
    setKids(btn, icon(dark ? "sun" : "moon", "i20"));
  };
  btn.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store("sasu-theme", next);
    paint();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paint);
  paint();
})();

// 글자 크게 (회의실·빔프로젝터용)
(function bigText() {
  const btn = $("#bigtext");
  const paint = () => btn.setAttribute("aria-pressed", String(document.documentElement.classList.contains("big")));
  btn.addEventListener("click", () => {
    const on = document.documentElement.classList.toggle("big");
    store("sasu-big", on ? "1" : "0");
    paint();
  });
  paint();
})();

// ── 한눈에 보기 (7.1) ────────────────────────────────────

function getOverview() {
  if (!cache.overview) cache.overview = api("/api/overview").catch((err) => { delete cache.overview; throw err; });
  return cache.overview;
}

// 모듈 이름 옆 한글 업무명 (없으면 이름만)
function moduleOptionText(m) {
  return m.label ? `${m.name} · ${m.label}` : m.name;
}

function getUnused() {
  if (!cache.unused) cache.unused = api("/api/unused").catch((err) => { delete cache.unused; throw err; });
  return cache.unused;
}

const overviewState = { sub: "modules", sortKey: "urls", sortDir: -1, filter: "", showAll: false, openModule: null };

views.overview = async function () {
  const root = $("#view-overview");
  setKids(root, loading());
  let data;
  try {
    data = await getOverview();
  } catch (err) {
    setKids(root, errorBox(err, () => views.overview()));
    return;
  }
  const [qd, unused] = await Promise.all([getQuestions().catch(() => null), getUnused().catch(() => null)]);
  const b = data.build;
  const name = (projectInfo && projectInfo.name) || $("#project").value;
  const sub = [
    b ? `${b.project}${b.version ? " " + b.version : ""}` : null,
    projectInfo ? `분석 기준 ${stamp(projectInfo.indexed_at)}` : null,
    projectInfo && projectInfo.head && projectInfo.head !== "-" ? `커밋 ${projectInfo.head.slice(0, 7)}` : null,
  ].filter(Boolean).join(" · ");

  const parts = [h("h2", { id: "overview-title" }, name), h("p", { class: "lead" }, sub)];

  // 할 일 카드 4개
  const cards = [];
  if (qd) {
    const p = questionProgress();
    cards.push(h("a", { class: "card todo-card", href: "#questions" },
      h("div", { class: "k" }, icon("message-circle-question-mark"), "질문 답변"),
      h("div", { class: "n" }, `${p.topAnswered}`, h("small", {}, `/ ${p.topTotal} 먼저 물을 질문`)),
      h("div", { class: "progress", role: "progressbar", "aria-label": "먼저 물을 질문 답변 진행률", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(p.topPct) },
        h("i", { style: `width:${p.topPct}%` })),
      h("div", { class: "s", style: "margin-top:6px" }, `먼저 물을 ${p.topTotal}개 중 ${p.topAnswered} · 전체 ${fmt(p.total)}개 중 ${fmt(p.answered)}`)));
  }
  const cov = data.coverage;
  if (cov.total) {
    const segs = [
      ["DB까지 연결", cov.to_sql, "var(--ord-3)"],
      ["코드에서 끝남", cov.code_only, "var(--ord-2)"],
      ["컨트롤러에서 끝남", cov.handler_only, "var(--ord-1)"],
    ];
    const pct = (n) => (n / cov.total) * 100;
    cards.push(h("a", { class: "card todo-card", href: "#trace", title: "동적 ID로 추정한 연결 포함" },
      h("div", { class: "k" }, icon("route"), "URL → SQL 연결"),
      h("div", { class: "n" }, `${Math.round(pct(cov.to_sql))}%`, h("small", {}, `URL ${fmt(cov.total)}개 중 ${fmt(cov.to_sql)}`)),
      h("div", { class: "cov", "aria-hidden": "true" }, segs.map(([, n, c]) => h("i", { style: `width:${pct(n)}%;background:${c}` }))),
      h("div", { class: "s" }, segs.map(([label, n, c], i) => h("div", {},
        h("span", { style: `display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;background:${c}` }),
        `${label} ${fmt(n)}${i === 0 ? "" : ` (${pct(n).toFixed(1)}%)`}`))),
      h("div", { class: "s explain" }, "URL에서 코드를 따라가 SQL까지 닿는지 셉니다. 고도화 준비의 '부르는 곳 못 찾은 URL'(어느 화면도 부르지 않는 URL)과는 다른 숫자입니다.")));
  }
  const bs = data.broken_sql;
  if (bs && typeof bs === "object") {
    // 없는 SQL 호출: URL에서 이어지는 곳(실제 실행 오류 위험)과 이어지지 않는 곳을 나눠 보인다
    const reach = bs.reachable || 0;
    const unreach = bs.unreachable || 0;
    cards.push(h("a", { class: `card todo-card${reach ? " danger" : ""}`, href: "#cleanup" },
      h("div", { class: "k" }, icon(reach ? "triangle-alert" : "unlink"), "없는 SQL 호출"),
      h("div", { class: "n" }, fmt(reach), h("small", {}, "곳 · URL에서 이어짐")),
      reach
        ? h("div", { class: "s risk" }, icon("triangle-alert", "i12"), "실행 오류 위험 — 화면에서 부르면 Mapper에 없는 SQL을 불러요.")
        : h("div", { class: "s" }, "URL에서 이어지는 코드는 없는 SQL을 부르지 않아요."),
      unreach ? h("div", { class: "s" }, `이어지지 않는 ${fmt(unreach)}곳은 고도화 준비에서 확인하세요 →`) : null));
  } else if (unused) {
    const broken = unused.sections.find((s) => s.kind === "broken_sql");
    const n = broken ? broken.items.length : 0;
    cards.push(h("a", { class: `card todo-card${n ? " danger" : ""}`, href: "#cleanup" },
      h("div", { class: "k" }, icon(n ? "triangle-alert" : "unlink"), "없는 SQL 호출"),
      h("div", { class: "n" }, fmt(n), h("small", {}, "곳")),
      h("div", { class: "s" }, n ? "코드가 부르는데 Mapper에 없어요. URL에서 이어지는 곳이면 실행 오류가 나요." : "코드가 부르는 SQL이 모두 Mapper에 있어요.")));
  }
  cards.push(h("a", {
    class: "card todo-card", href: "#overview",
    onclick: (e) => { e.preventDefault(); overviewState.sub = "tables"; renderOverviewSub(data); $("#ov-sub").scrollIntoView({ block: "start" }); },
  },
  h("div", { class: "k" }, icon("table-2"), "여러 모듈이 함께 쓰는 테이블"),
  h("div", { class: "n" }, fmt(data.tables.length), h("small", {}, "개")),
  h("div", { class: "s" }, "바꾸면 여러 기능에 영향이 가요.")));
  parts.push(h("div", { class: "todo" }, cards));

  parts.push(gradeLegendLine());

  // 규모 흐름 띠
  const c = data.counts;
  const chain = [["URL", "URL"], ["컨트롤러", "컨트롤러"], ["서비스", "서비스"], ["DAO", "DAO"], ["SQL", "SQL"], ["테이블", "테이블"]]
    .filter(([k]) => c[k] !== undefined);
  const flow = [];
  chain.forEach(([k, label], i) => {
    if (i) flow.push(h("span", { class: "arrow", "aria-hidden": "true" }, "→"));
    flow.push(h("span", {}, `${label} `, h("b", {}, fmt(c[k]))));
  });
  if (c.JSP !== undefined) flow.push(h("span", { class: "sep", "aria-hidden": "true" }, "·"), h("span", {}, "화면(JSP) ", h("b", {}, fmt(c.JSP))));
  if (c["모듈"] !== undefined) flow.push(h("span", { class: "sep", "aria-hidden": "true" }, "·"), h("span", {}, "모듈 ", h("b", {}, fmt(c["모듈"]))));
  parts.push(h("div", { class: "card flow", "aria-label": "규모" }, flow));

  // 하위 탭
  const seg = h("div", { class: "seg subtabs", role: "group", "aria-label": "자세히 보기", id: "ov-sub" },
    [["modules", "모듈"], ["tables", "함께 쓰는 테이블"], ["stack", "기술 스택"]].map(([k, label]) =>
      h("button", { type: "button", "data-sub": k, "aria-pressed": String(overviewState.sub === k),
        onclick: () => { overviewState.sub = k; renderOverviewSub(data); } }, label)));
  parts.push(seg, h("div", { id: "ov-panel" }));
  setKids(root, ...parts);
  renderOverviewSub(data);
};

function renderOverviewSub(data) {
  document.querySelectorAll("#ov-sub button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.sub === overviewState.sub)));
  const panel = $("#ov-panel");
  if (!panel) return;
  if (overviewState.sub === "tables") setKids(panel, sharedTables(data));
  else if (overviewState.sub === "stack") setKids(panel, stackPanel(data.build));
  else setKids(panel, moduleTable(data));
}

const MOD_COLS = [
  ["name", "모듈"], ["urls", "URL"], ["controllers", "컨트롤러"], ["services", "서비스"], ["daos", "DAO"], ["statements", "SQL"],
];

function moduleTable(data) {
  const st = overviewState;
  const shared = new Map(data.tables.map((t) => [t.name, t.modules.length]));
  const wrap = h("div");
  const render = () => {
    const needle = st.filter.trim().toLowerCase();
    // 대표 업무명뿐 아니라 모듈 안 모든 업무명(labels)으로도 찾는다: uss.ion = "당직관리 외 27"에 숨은 "휴가관리"
    const hit = (m) => (m.labels || []).find((l) => l.toLowerCase().includes(needle) && l !== m.label);
    let rows = data.modules.filter((m) => !needle || m.name.toLowerCase().includes(needle)
      || (m.label || "").toLowerCase().includes(needle) || hit(m) || m.tables.some((t) => t.toLowerCase().includes(needle)));
    rows = rows.slice().sort((a, b) => {
      const x = a[st.sortKey];
      const y = b[st.sortKey];
      const r = typeof x === "string" ? x.localeCompare(y) : x - y;
      return r * st.sortDir || a.name.localeCompare(b.name);
    });
    const limit = st.showAll || needle ? rows.length : 20;
    const max = Math.max(1, ...data.modules.map((m) => (st.sortKey === "name" ? 0 : m[st.sortKey])));
    const tbody = h("tbody");
    for (const m of rows.slice(0, limit)) {
      const open = st.openModule === m.name;
      const tr = h("tr", {
        class: `click${open ? " open" : ""}`,
        onclick: () => { st.openModule = open ? null : m.name; render(); },
      },
      h("td", {}, h("button", { class: "rowbtn mono", type: "button", "aria-expanded": String(open), title: "URL마다 읽고 쓰는 테이블 보기" }, m.name),
        m.label ? h("span", { class: "modlabel" }, m.label) : null,
        needle && hit(m) ? h("span", { class: "modlabel" }, `찾은 업무: ${hit(m)}`) : null),
      ...MOD_COLS.slice(1).map(([k]) => {
        const v = m[k];
        if (!v) return h("td", { class: "num zero" }, "–");
        if (k === st.sortKey) {
          return h("td", { class: "num" }, h("span", { class: "cellbar" }, h("i", { style: `width:${Math.max(2, (v / max) * 56)}px` }), fmt(v)));
        }
        return h("td", { class: "num" }, fmt(v));
      }),
      h("td", {}, h("span", { class: "tabchips" }, m.tables.map((t) => h("span", {
        class: "chip mono", title: shared.has(t) ? `${shared.get(t)}개 모듈이 씀` : null,
      }, shared.has(t) ? icon("network", "i12") : null, t)))),
      h("td", { class: "go", "aria-hidden": "true" }, icon(open ? "chevron-down" : "chevron-right", "i14")));
      tbody.append(tr);
      if (open) {
        const cell = h("td", { colspan: "8" }, loading());
        tbody.append(h("tr", { class: "detail" }, cell));
        showModule(m.name, cell);
      }
    }
    const thead = h("thead", {}, h("tr", {}, MOD_COLS.map(([k, label], i) => {
      const on = st.sortKey === k;
      return h("th", { class: i ? "num" : null, "aria-sort": on ? (st.sortDir > 0 ? "ascending" : "descending") : "none" },
        h("button", { class: "sort", type: "button", onclick: () => {
          if (st.sortKey === k) st.sortDir = -st.sortDir;
          else { st.sortKey = k; st.sortDir = k === "name" ? 1 : -1; }
          render();
        } }, label, on ? (st.sortDir > 0 ? " ▲" : " ▼") : ""));
    }), h("th", {}, "주요 테이블"), h("th", {}, h("span", { class: "visually-hidden" }, "펼치기"))));
    if (!rows.length) {
      tbody.append(h("tr", {}, h("td", { colspan: "8", class: "nomatch" },
        icon("search", "i14"), ` ${noMatch(st.filter.trim(), "모듈이")}. 모듈 이름·업무명·테이블로 찾아요.`)));
    }
    const excluded = (data.counts["모듈"] || data.modules.length) - data.modules.length;
    const shown = Math.min(rows.length, limit);
    const table = h("table", { class: "grid" },
      h("caption", {}, needle
        ? `${fmt(shown)}개 표시 · 전체 ${fmt(data.modules.length)}개 중 '${st.filter.trim()}'에 맞는 것`
        : `${fmt(shown)}개 표시${rows.length > shown ? ` (전체 ${fmt(rows.length)}개)` : ""}${excluded > 0 ? ` · ${excluded}개 제외 (URL·SQL이 없는 모듈)` : ""}`),
      thead, tbody);
    if (needle && st.lastAnnounced !== `${needle}:${rows.length}`) {
      st.lastAnnounced = `${needle}:${rows.length}`;
      announce(rows.length ? `모듈 ${rows.length}개` : noMatch(st.filter.trim(), "모듈이"));
    }
    setKids(wrap, 
      h("div", { class: "row modfind" },
        h("input", { type: "search", placeholder: "모듈·업무명·테이블 찾기", "aria-label": "모듈 찾기", value: st.filter,
          oninput: (e) => { st.filter = e.target.value; clearTimeout(st.timer); st.timer = setTimeout(() => { render(); const i = wrap.querySelector("input"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }, 150); } }),
        h("span", { class: "muted xs" }, "모듈을 누르면 URL마다 읽고 쓰는 테이블이 아래에 열려요")),
      h("div", { class: "card table-wrap" }, table),
      rows.length > limit
        ? h("button", { class: "btn", type: "button", style: "margin-top:8px", onclick: () => { st.showAll = true; render(); } }, `나머지 ${fmt(rows.length - limit)}개 더 보기`)
        : null);
    if (st.scrollToOpen) {
      st.scrollToOpen = false;
      setTimeout(() => { const r = wrap.querySelector("tr.open"); if (r) { r.scrollIntoView({ block: "start" }); r.querySelector("button").focus({ preventScroll: true }); } }, 0);
    }
  };
  render();
  return wrap;
}

async function showModule(name, target) {
  try {
    const data = await api(`/api/modules/${encodeURIComponent(name)}`);
    for (const r of data.urls) if (r.label) urlLabels.set(r.url, r.label);
    setKids(target, 
      h("div", { class: "row", style: "margin-bottom:8px" },
        h("b", { class: "grow" }, `CRUD (URL × 테이블) · ${name} · URL ${fmt(data.urls.length)}개`),
        h("span", { class: "muted xs" }, "채운 칩 = 쓰기, 외곽선 = 읽기"),
        h("a", { class: "btn sm", href: `/api/behaviors/export?module=${encodeURIComponent(name)}`, download: "", onclick: (e) => e.stopPropagation() },
          icon("file-spreadsheet", "i14"), "이 모듈 기존 동작 목록(AS-IS) 내려받기")),
      h("div", { class: "card table-wrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["URL", "처리 메서드", "테이블"].map((c) => h("th", {}, c)))),
        h("tbody", {}, data.urls.map((r) => h("tr", {},
          h("td", {}, h("button", { class: "urlbtn", type: "button", title: "경로 추적", onclick: () => traceUrl(r.url) }, wbr(r.url)),
            r.label ? h("div", { class: "ko" }, r.label) : null),
          h("td", { class: "mono xs" }, r.handler),
          h("td", {}, h("span", { class: "tabchips" },
            r.writes.map((t) => h("span", { class: "chip mono solid", title: "쓰기" }, t)),
            r.tables.filter((t) => !r.writes.includes(t)).map((t) => h("span", { class: "chip mono ctl", title: "읽기" }, t)),
            r.tables.length ? null : h("span", { class: "zero" }, "–")))))))));
  } catch (err) {
    setKids(target, errorBox(err));
  }
}

function sharedTables(data) {
  if (!data.tables.length) return emptyState("table-2", "여러 모듈이 함께 쓰는 테이블이 없어요", null);
  const max = Math.max(1, ...data.tables.map((t) => t.modules.length));
  const LETTERS = [["insert", "C"], ["update", "U"], ["delete", "D"], ["merge", "M"]];
  return h("div", { class: "card table-wrap" }, h("table", { class: "grid" },
    h("caption", {}, "쓰기는 이 소스의 SQL만 셉니다. 배치·프로시저·다른 시스템 연계는 보지 못합니다."),
    h("thead", {}, h("tr", {}, h("th", {}, "테이블"), h("th", {}, "쓰는 모듈 수"), h("th", { class: "num" }, "SQL"),
      h("th", { class: "num" }, "읽기"), h("th", {}, "쓰기"), h("th", {}, "모듈"))),
    h("tbody", {}, data.tables.map((t) => {
      const writes = LETTERS.filter(([k]) => t.kinds[k]);
      return h("tr", {},
        h("td", {}, h("b", { class: "mono" }, t.name)),
        h("td", { class: "num", style: "width:160px" }, h("span", { class: "cellbar", style: "justify-content:flex-start" },
          h("i", { style: `width:${(t.modules.length / max) * 100}px` }), fmt(t.modules.length))),
        h("td", { class: "num" }, fmt(t.statements)),
        h("td", { class: "num" }, t.kinds.select ? fmt(t.kinds.select) : h("span", { class: "zero" }, "–")),
        h("td", {}, writes.length
          ? h("span", { class: "tabchips" }, writes.map(([k, l]) => h("span", { class: "chip solid mono", title: k.toUpperCase() }, `${l} ${t.kinds[k]}`)))
          : h("span", { class: "muted xs" }, "이 소스에서는 조회만")),
        h("td", {}, h("span", { class: "tabchips" },
          t.modules.slice(0, 8).map((m) => h("button", { class: "chip mono", type: "button", title: `${m} 모듈 펼치기`,
            onclick: () => { Object.assign(overviewState, { sub: "modules", openModule: m, filter: "", showAll: true, scrollToOpen: true }); renderOverviewSub(data); } }, m)),
          t.modules.length > 8 ? h("span", { class: "muted xs" }, `외 ${t.modules.length - 8}`) : null)));
    }))));
}

function stackPanel(b) {
  if (!b) return emptyState("package", "빌드 파일을 찾지 못했어요", "pom.xml·build.gradle·pyproject.toml이 없거나 읽지 못했어요.");
  const chips = (items) => items.map((x) => h("span", { class: "chip mono ctl" }, x));
  const core = h("dl", { class: "stack" });
  const rest = h("dl", { class: "stack" });
  core.append(h("dt", {}, "빌드"), h("dd", {}, chips([`${b.tool}${b.packaging ? " · " + b.packaging : ""}`])));
  const stack = b.stack || {};
  const CORE = ["프레임워크", "데이터 접근", "DB 드라이버", "화면"];
  for (const k of CORE) if (stack[k]) core.append(h("dt", {}, k), h("dd", {}, chips(stack[k])));
  let more = 0;
  const extra = [];
  if (b.java_version) extra.push(["Java", [b.java_version]]);
  if (b.python_version) extra.push(["Python", [b.python_version]]);
  if (b.parent) extra.push(["부모", [b.parent]]);
  if (b.servlet_api) extra.push(["Servlet API", [b.servlet_api]]);
  for (const [k, items] of Object.entries(stack)) if (!CORE.includes(k)) { extra.push([k, items]); more += items.length; }
  for (const [k, items] of extra) rest.append(h("dt", {}, k), h("dd", {}, chips(items)));
  return h("div", { class: "card pad" }, core,
    extra.length ? h("details", { class: "more" }, h("summary", {}, `라이브러리 ${more}개 더`), h("div", { style: "margin-top:8px" }, rest)) : null);
}

// ── 인수인계 질문 (7.2) ──────────────────────────────────

const KIND_ICON = {
  broken_sql: "unlink", dynamic_sql: "braces", unused_sql: "database", external_bean: "package", magic_value: "hash",
  note: "sticky-note", complex: "git-branch", copied_code: "copy", empty_catch: "shield-off",
};
const STATUS_LABEL = { open: "미답변", answered: "답변 완료", skipped: "건너뜀" };
const TOP = 20;
const NEXT = 50;

const Q = {
  data: null, rank: new Map(), ties: new Set(),
  status: "open", kind: "", search: "",
  drafts: new Map(),     // 쓰던 답 (검색·필터·접기에도 남는다)
  byDrafts: new Map(),   // 고쳐 쓴 '답한 사람' (펼친 단위 key별)
  polling: false,
  justSaved: new Set(),  // 방금 저장한 질문: 미답변 목록에서도 잠시 남겨 [이 코드로 묻기]를 보인다
  open: null,            // 펼친 단위 key ("q:4" 또는 "t:대상")
};
let qPromise = null;

function getQuestions(force) {
  if (Q.data && !force) return Promise.resolve(Q.data);
  if (!qPromise || force) {
    qPromise = api("/api/questions").then((d) => { setQuestions(d); qPromise = null; return d; },
      (err) => { qPromise = null; throw err; });
  }
  return qPromise;
}

function setQuestions(d) {
  Q.data = d;
  Q.rank = new Map(d.items.map((q, i) => [q.id, i]));
  // 같은 점수로 경계(20위·50위)가 갈린 질문
  Q.ties = new Set();
  for (const b of [TOP, NEXT]) {
    if (d.items.length > b && d.items[b - 1].priority === d.items[b].priority) Q.ties.add(d.items[b].priority);
  }
  recount();
}

function recount() {
  const d = Q.data;
  d.total = d.items.length;
  d.answered = d.items.filter((x) => x.status === "answered").length;
  d.skipped = d.items.filter((x) => x.status === "skipped").length;
  d.open = d.total - d.answered - d.skipped;
  updateQuestionCount();
}

function loadQuestionCount() {
  getQuestions().catch(() => { /* 프로젝트 없음 */ });
}

function updateQuestionCount() {
  const d = Q.data;
  const badge = $("#q-count");
  if (!d) { badge.classList.add("hidden"); return; }
  badge.textContent = `미답변 ${fmt(d.open)}`;
  badge.classList.toggle("hidden", !d.open);
}

// 다른 사람이 저장한 답을 반영한다. 펼친 행·작성 중 답·입력 위치는 그대로 둔다
const qSig = (d) => d.items.map((q) => [q.id, q.status, q.answer, q.answered_by, q.answered_at, q.label].join("\u0001")).join("\u0002");
async function pollQuestions() {
  if (!Q.data || Q.polling) return;
  Q.polling = true;
  try {
    const d = await api("/api/questions");
    if (!Q.data || qSig(d) === qSig(Q.data)) return;
    const before = new Map(Q.data.items.map((q) => [q.id, `${q.status}\u0001${q.answer}`]));
    const changed = d.items.filter((q) => before.has(q.id) && before.get(q.id) !== `${q.status}\u0001${q.answer}`).length;
    const list = $("#q-list");
    const a = document.activeElement;
    const keep = a && a.id && list.contains(a)
      ? { id: a.id, s: a.selectionStart, e: a.selectionEnd } : null;
    // 화면 위치 기준: 입력 중인 칸, 없으면 펼친 단위
    const anchorEl = keep ? a : (Q.open ? list.querySelector(`.qunit[data-key="${CSS.escape(Q.open)}"]`) : null);
    const anchorTop = anchorEl ? anchorEl.getBoundingClientRect().top : null;
    setQuestions(d);
    if (currentView !== "questions" || !loaded.has("questions")) return;
    fillKindSelect();
    renderQuestions();
    const again = keep ? document.getElementById(keep.id)
      : (Q.open ? list.querySelector(`.qunit[data-key="${CSS.escape(Q.open)}"]`) : null);
    if (again && anchorTop !== null) window.scrollBy(0, again.getBoundingClientRect().top - anchorTop);
    if (keep && again) {
      again.focus({ preventScroll: true });
      try { if (keep.s !== null && keep.s !== undefined) again.setSelectionRange(keep.s, keep.e); } catch { /* 선택 범위가 없는 칸 */ }
    }
    if (changed) toast(`다른 사람이 저장한 답 ${changed}개를 반영했어요`);
  } catch { /* 다음에 다시 */ } finally { Q.polling = false; }
}
setInterval(() => {
  if (currentView === "questions" && document.visibilityState === "visible") pollQuestions();
}, 60000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && currentView === "questions") pollQuestions();
});

function questionProgress() {
  const items = Q.data.items;
  const top = items.slice(0, TOP);
  const topAnswered = top.filter((q) => q.status === "answered").length;
  return {
    total: items.length, answered: Q.data.answered, topTotal: top.length, topAnswered,
    topPct: top.length ? Math.round((topAnswered / top.length) * 100) : 0,
  };
}

const tierOf = (rank) => (rank < TOP ? 0 : rank < NEXT ? 1 : 2);
const TIER_NAME = ["먼저 물을 질문", "다음", "나중"];
const TIER_SHORT = ["먼저", "다음", "나중"];

views.questions = async function (params) {
  const list = $("#q-list");
  setKids(list, loading("질문을 불러오는 중…"));
  try {
    await getQuestions();
  } catch (err) {
    setKids(list, errorBox(err, () => { loaded.delete("questions"); views.questions(); }));
    return;
  }
  fillKindSelect();
  renderQuestions();
  if (params && params.q) openQuestionById(Number(params.q));
};

function fillKindSelect() {
  const present = new Set(Q.data.items.map((q) => q.kind));
  setKids($("#q-kind"), h("option", { value: "" }, "모든 종류"),
    ...Object.entries(Q.data.kinds).filter(([k]) => present.has(k)).map(([k, label]) =>
      h("option", { value: k, selected: k === Q.kind }, label)));
}

function inOpenUnit(q) {
  return Q.open !== null && (Q.open === `q:${q.id}` || (q.target && Q.open === `t:${q.target}`));
}

function filteredItems() {
  const needle = Q.search.trim().toLowerCase();
  // 방금 저장한 질문·펼친 질문·작성 중인 질문은 상태가 바뀌어도 그 자리에 남긴다
  const passStatus = (q) => Q.status === "all" || q.status === Q.status
    || (Q.status === "open" && (Q.justSaved.has(q.id) || inOpenUnit(q) || hasDraft(q)));
  return Q.data.items.filter((q) => passStatus(q)
    && (!Q.kind || q.kind === Q.kind)
    && (!needle || [q.question, q.target, q.file_path, q.why, q.answer, q.label, q.module_label, q.module]
      .some((x) => (x || "").toLowerCase().includes(needle))));
}

// 같은 target 질문이 2개 이상이면 한 묶음으로. 묶음 순위는 묶음 안 최고 순위
function buildUnits(items) {
  const byTarget = new Map();
  for (const q of items) if (q.target) byTarget.set(q.target, [...(byTarget.get(q.target) || []), q]);
  const used = new Set();
  const units = [];
  for (const q of items) {
    if (used.has(q.id)) continue;
    const same = q.target ? byTarget.get(q.target) : null;
    if (same && same.length > 1) {
      units.push({ key: `t:${q.target}`, target: q.target, items: same, rank: Q.rank.get(same[0].id) });
      same.forEach((x) => used.add(x.id));
    } else units.push({ key: `q:${q.id}`, items: [q], rank: Q.rank.get(q.id) });
  }
  return units;
}

function renderQuestions(opts = {}) {
  const d = Q.data;
  if (!d) return;
  const p = questionProgress();
  $("#q-progress-text").textContent = `먼저 물을 ${p.topTotal}개 중 ${p.topAnswered}개 · 전체 ${fmt(p.total)}개 중 ${fmt(p.answered)}개 답변 완료`;
  $("#q-progress").style.width = `${p.topPct}%`;
  $("#q-progressbar").setAttribute("aria-valuenow", String(p.topPct));
  const counts = { open: d.open, answered: d.answered, skipped: d.skipped, all: d.total };
  document.querySelectorAll("#q-status button").forEach((b) => {
    b.textContent = `${{ open: "미답변", answered: "답변 완료", skipped: "건너뜀", all: "전체" }[b.dataset.status]} ${fmt(counts[b.dataset.status])}`;
    b.setAttribute("aria-pressed", String(b.dataset.status === Q.status));
  });
  $("#q-kind").value = Q.kind;
  renderKinds();
  updateQuestionCount();

  const list = $("#q-list");
  const items = filteredItems();
  // 보이는 질문만 내려받기 (서버가 ids를 모르면 전체가 내려온다)
  const exp = $("#q-export-visible");
  exp.href = items.length ? `/api/questions/export?ids=${items.map((q) => q.id).join(",")}` : "/api/questions/export";
  exp.setAttribute("aria-disabled", String(!items.length));
  $("#q-export-visible-text").textContent = `보이는 ${fmt(items.length)}개 질문지 내려받기`;
  if (Q.lastCount !== items.length) {
    Q.lastCount = items.length;
    announce(items.length ? `질문 ${items.length}개` : Q.search.trim() ? noMatch(Q.search.trim(), "질문이") : "질문 0개");
  }
  const STATUS_NAME = { open: "미답변", answered: "답변 완료", skipped: "건너뜀", all: "전체" };
  const conds = [STATUS_NAME[Q.status], Q.kind ? d.kinds[Q.kind] : null, Q.search.trim() ? `‘${Q.search.trim()}’` : null].filter(Boolean).join(" · ");
  const head = h("div", { class: "qcount", id: "q-result-head", tabindex: "-1" },
    h("b", {}, `${fmt(items.length)}개 표시`), h("span", { class: "muted" }, ` · ${conds}`));
  if (!items.length) {
    let msg;
    if (d.total === 0) {
      msg = emptyState("message-circle-question-mark", "물어볼 곳을 찾지 못했어요",
        "업무 코드 분기, 주의 주석, 없는 SQL 호출, 복사한 코드가 보이지 않아요. 작은 코드이거나 아직 지원하지 않는 방식일 수 있어요.",
        h("a", { class: "btn", href: "#trace" }, icon("route"), "경로 추적으로 둘러보기"));
    } else if (Q.status === "open" && !Q.search.trim() && !Q.kind) {
      msg = emptyState("circle-check", "남은 미답변 질문이 없어요",
        "받은 답변은 '답변 완료'에서 보고, 사수에게 물을 때 근거로 쓰여요.",
        h("button", { class: "btn", type: "button", onclick: () => setStatus("answered") }, "답변 완료 보기"));
    } else if (Q.status === "skipped" && !Q.search.trim() && !Q.kind) {
      msg = emptyState("circle-check", "건너뛴 질문이 없어요",
        "질문을 펼쳐 [건너뜀]을 누르면 이유와 함께 여기에 모여요. 되돌리면 다시 미답변이 돼요.",
        h("button", { class: "btn", type: "button", onclick: () => setStatus("open") }, "미답변 보기"));
    } else if (Q.search.trim()) {
      msg = emptyState("search", noMatch(Q.search.trim(), "질문이"),
        `질문 문장·업무명·클래스·파일 이름으로 찾아요. 지금 조건(${conds})을 넓혀 보세요.`,
        Q.status !== "all" ? h("button", { class: "btn", type: "button", onclick: () => setStatus("all") }, "전체에서 찾기") : null,
        h("button", { class: "btn", type: "button", onclick: clearFilters }, icon("x"), "조건 지우기"));
    } else {
      msg = emptyState("search", "조건에 맞는 질문이 없어요", "상태나 종류를 바꿔 보세요.",
        h("button", { class: "btn", type: "button", onclick: clearFilters }, icon("x"), "조건 지우기"));
    }
    setKids(list, head, msg);
    return;
  }
  const units = buildUnits(items);
  const out = [head];
  let tier = -1;
  for (const u of units) {
    const t = tierOf(u.rank);
    if (t !== tier) {
      tier = t;
      const n = units.filter((x) => tierOf(x.rank) === t).reduce((s, x) => s + x.items.length, 0);
      const range = t === 0 ? `우선 1~${TOP}위` : t === 1 ? `우선 ${TOP + 1}~${NEXT}위` : `우선 ${NEXT + 1}위부터`;
      const tie = units.some((x) => tierOf(x.rank) === t && x.items.some((q) => Q.ties.has(q.priority)));
      out.push(h("div", { class: "qsec" }, TIER_NAME[t], h("span", {}, `${range} · 이 목록에 ${n}개`),
        tie ? h("span", { class: "tie-note" }, "동점 = 같은 점수라 순위 경계(20·50위)에서 갈린 질문") : null));
    }
    out.push(renderUnit(u, t));
  }
  setKids(list, ...out);
  if (opts.focus) {
    const el = list.querySelector(opts.focus);
    if (el) el.focus();
  }
}

function renderKinds() {
  const d = Q.data;
  const stats = Object.keys(d.kinds).map((k) => {
    const its = d.items.filter((q) => q.kind === k);
    return { k, label: d.kinds[k], n: its.length, a: its.filter((q) => q.status === "answered").length };
  }).sort((x, y) => y.n - x.n);
  const max = Math.max(1, ...stats.map((s) => s.n));
  setKids($("#q-kinds"), ...stats.map((s) => h("button", {
    type: "button", class: s.n ? null : "none", "aria-pressed": String(Q.kind === s.k), disabled: !s.n,
    title: `${s.label}: 질문 ${s.n}개 중 ${s.a}개 답변 완료`,
    onclick: () => { Q.kind = Q.kind === s.k ? "" : s.k; Q.open = null; renderQuestions(); },
  },
  icon(KIND_ICON[s.k] || "info", "i14"), h("span", {}, s.label),
  h("span", { class: "track" }, s.n ? h("i", { style: `width:${(s.n / max) * 100}%` }, h("b", { style: `width:${s.n ? (s.a / s.n) * 100 : 0}%` })) : null),
  h("span", { class: "cnt" }, `${s.a}/${s.n}`))));
}

function statusChip(q) {
  if (q.status === "answered") return h("span", { class: "chip ok" }, icon("check", "i12"), STATUS_LABEL.answered);
  if (q.status === "skipped") return h("span", { class: "chip" }, STATUS_LABEL.skipped);
  return h("span", { class: "chip ctl" }, STATUS_LABEL.open);
}

function hasDraft(q) {
  const v = Q.drafts.get(q.id);
  return v !== undefined && v.trim() !== "" && v !== (q.answer || "");
}

function qLocText(q) {
  return `${q.target || fileName(q.file_path) || "-"}:${q.line || 1}`;
}

function renderUnit(u, sectionTier) {
  const open = Q.open === u.key;
  const box = h("div", { class: `qunit${open ? " open" : ""}`, "data-key": u.key });
  // 더블클릭의 두 번째 클릭은 무시한다 (펼쳤다가 바로 닫히던 문제)
  const toggle = (e) => { if (e.detail > 1) return; toggleUnit(u.key, e.currentTarget); };
  if (u.items.length > 1) {
    const short = u.target.split(".").pop();
    box.append(h("button", { class: "qgroup-head", type: "button", "aria-expanded": String(open), "data-key": u.key, onclick: toggle,
      title: u.target },
    icon(open ? "chevron-down" : "chevron-right", "i14"), h("span", { class: "name" }, short), h("span", { class: "muted" }, `· ${u.items.length}`),
    h("span", { class: "muted xs grow" }, u.target)));
    for (const q of u.items) box.append(qRow(q, u, sectionTier, open, toggle, true));
  } else box.append(qRow(u.items[0], u, sectionTier, open, toggle, false));
  if (open) box.append(unitBody(u));
  return box;
}

function qRow(q, u, sectionTier, open, toggle, inGroup) {
  const rank = Q.rank.get(q.id);
  const tags = [statusChip(q)];
  if (hasDraft(q)) tags.push(h("span", { class: "chip dashed" }, "작성 중"));
  if (Q.ties.has(q.priority)) tags.push(h("span", { class: "chip", title: "같은 점수로 순위 경계가 갈렸어요" }, "동점"));
  if (inGroup && tierOf(rank) !== sectionTier) tags.push(h("span", { class: "chip", title: `우선 ${rank + 1}위` }, TIER_SHORT[tierOf(rank)]));
  const row = h("div", { class: "row", style: "gap:0;flex-wrap:nowrap" },
    h("button", { class: "qrow", type: "button", "aria-expanded": String(open), "data-key": u.key, "data-qid": q.id, onclick: toggle,
      style: inGroup ? "padding-left:28px" : null },
    h("span", { class: "qid" }, `Q${q.id}`),
    h("span", { class: "chip kindc" }, icon(KIND_ICON[q.kind] || "info", "i12"), q.kind_label),
    h("span", { class: "qt", title: q.question }, q.question),
    h("span", { class: "qloc", title: [q.label, q.file_path ? `${q.file_path}:${q.line}` : null].filter(Boolean).join("\n") || null },
      q.label ? h("span", { class: "qko" }, q.label) : null, h("span", { class: "qcode" }, qLocText(q))),
    h("span", { class: "tags" }, tags),
    h("span", { class: "qans-print" }, q.answer || "")),
    Q.justSaved.has(q.id) && q.status === "answered"
      ? h("button", { class: "btn sm", type: "button", style: "margin-right:8px", onclick: () => askAbout(q) }, icon("message-square-quote", "i14"), "이 코드로 묻기")
      : null);
  return row;
}

function toggleUnit(key, el) {
  const before = el.getBoundingClientRect().top;
  Q.open = Q.open === key ? null : key;
  const qid = el.dataset.qid;
  renderQuestions();
  // 누른 행의 화면 위치를 고정한다
  const sel = qid ? `.qrow[data-qid="${qid}"]` : `.qgroup-head[data-key="${CSS.escape(key)}"]`;
  const now = $("#q-list").querySelector(sel);
  if (now) {
    window.scrollBy(0, now.getBoundingClientRect().top - before);
    now.focus({ preventScroll: true });
    if (Q.open) keepEditorVisible(now.closest(".qunit"));
  }
}

// 펼친 단위의 답변 칸·저장 버튼이 화면 밖이면, 행이 머리글에 가리지 않는 만큼만 올린다
function keepEditorVisible(unit) {
  if (!unit) return;
  const ed = unit.querySelector(".editor");
  if (!ed) return;
  const top = topOffset();
  const r = ed.getBoundingClientRect();
  const u = unit.getBoundingClientRect();
  if (r.bottom <= window.innerHeight - 8) return;
  const need = r.bottom - window.innerHeight + 16;
  window.scrollBy(0, Math.max(0, Math.min(need, u.top - top)));
}

function unitBody(u) {
  const body = h("div", { class: "qbody" });
  const byId = `by-${u.key.replace(/[^\w]/g, "_")}`;
  // 답한 사람: 마지막에 쓴 이름을 미리 채우되, 어떤 이름으로 저장되는지 늘 보여 준다
  const saved = u.items.find((q) => q.status === "answered" && q.answered_by);
  const initial = Q.byDrafts.has(u.key) ? Q.byDrafts.get(u.key) : (saved ? saved.answered_by : store("sasu-answered-by") || "");
  const by = h("input", { type: "text", id: byId, placeholder: "이름 (선택)", value: initial, autocomplete: "off" });
  const hint = h("span", { class: "by-hint" });
  const syncs = [];
  const paintHint = () => {
    const v = by.value.trim();
    hint.textContent = v ? `‘${v}’ 이름으로 저장돼요` : "이름 없이 저장돼요";
  };
  by.addEventListener("input", () => { Q.byDrafts.set(u.key, by.value); paintHint(); syncs.forEach((f) => f()); });
  by.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); const s = body.querySelector(".editor .btn.primary"); if (s && !s.disabled) s.click(); }
  });
  paintHint();
  const byRow = h("div", { class: "row by" },
    h("label", { for: byId, class: "xs" }, icon("user-round", "i14"), " 답한 사람"), by, hint,
    u.items.length > 1 ? h("span", { class: "muted xs" }, "· 이 묶음의 답변에 함께 저장해요") : null);
  const ctx = { by, syncs, byRow: u.items.length > 1 ? null : byRow };
  if (u.items.length > 1) body.append(byRow);
  for (const q of u.items) body.append(memberBlock(q, u, ctx));
  return body;
}

// 저장된 답변 기준값 (건너뜀의 이유는 답변이 아니므로 빈 값)
const answerBase = (q) => (q.status === "answered" ? q.answer || "" : "");
const byBase = (q) => (q.status === "answered" ? q.answered_by || "" : "");

// 모듈 업무명 "당직관리 외 27"의 대표 이름만 ("당직관리")
const moduleHead = (ml) => (ml ? ml.split(" 외 ")[0].trim() : "");

function memberBlock(q, u, ctx) {
  const { by } = ctx;
  const taId = `ans-${q.id}`;
  const ta = h("textarea", { id: taId, placeholder: "받은 답변을 적어 주세요. 짧아도 괜찮습니다. (Ctrl+Enter로 저장하고 다음 질문으로)" });
  ta.value = Q.drafts.has(q.id) ? Q.drafts.get(q.id) : answerBase(q);
  const save = h("button", { class: "btn primary sm", type: "button", title: "Ctrl+Enter" }, icon("save", "i14"), "답변 저장 (Ctrl+Enter)");
  // 답변을 고쳤거나, 저장된 답변의 '답한 사람'만 바꿨을 때 저장 버튼이 켜진다
  const sync = () => {
    const t = ta.value.trim();
    save.disabled = !t || (t === answerBase(q) && by.value.trim() === byBase(q));
  };
  ctx.syncs.push(sync);
  ta.addEventListener("input", () => { Q.drafts.set(q.id, ta.value); sync(); });
  sync();
  const doSave = async (fromKey) => {
    const text = ta.value.trim();
    if (!text) {
      // 빈 칸에서는 저장하지 않는다 (받은 답변이 지워지던 문제 방지). 상태는 그대로 두고 다음 질문으로
      if (fromKey) { announce("다음 질문으로"); openNext(q); }
      return;
    }
    const name = by.value.trim();
    if (text === answerBase(q) && name === byBase(q)) { if (fromKey) openNext(q); return; }
    save.disabled = true;
    try {
      const r = await api(`/api/questions/${q.id}/answer`, { json: { answer: text, by: name || null } });
      if (name) store("sasu-answered-by", name);
      Object.assign(q, r);
      Q.drafts.delete(q.id);
      Q.byDrafts.delete(u.key);
      Q.justSaved.add(q.id);
      recount();
      toast(`Q${q.id} 답변을 저장했어요${name ? ` (답한 사람 ${name})` : ""}. 이제 사수에게 물을 때 근거로 쓰여요.`);
      openNext(q);
    } catch (err) {
      toast(err.message);
      sync();
    }
  };
  save.addEventListener("click", () => doSave(false));
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSave(true); } });

  // 건너뜀 + 이유 한 줄
  const skipId = `skip-${q.id}`;
  const reason = h("input", { type: "text", id: skipId, placeholder: "건너뛰는 이유 (선택, 예: 전임자도 모름 · 쓰지 않는 기능)", class: "grow" });
  const skipRow = h("div", { class: "row skip-row hidden" },
    h("label", { for: skipId, class: "xs" }, "건너뛰는 이유"), reason,
    h("button", { class: "btn sm primary", type: "button", onclick: () => doSkip() }, "건너뜀으로 저장"),
    h("button", { class: "btn sm", type: "button", onclick: () => { skipRow.classList.add("hidden"); skipBtn.focus(); } }, "취소"));
  const doSkip = async () => {
    const name = by.value.trim();
    try {
      const r = await api(`/api/questions/${q.id}/skip`, { json: { reason: reason.value.trim(), by: name || null } });
      if (name) store("sasu-answered-by", name);
      Object.assign(q, r);
      Q.drafts.delete(q.id);
      Q.justSaved.add(q.id);
      recount();
      toast(`Q${q.id}를 건너뜀으로 표시했어요`);
      openNext(q);
    } catch (err) {
      toast(err.status === 405 || (err.status === 404 && err.message === "Not Found")
        ? "이 사수 서버는 아직 건너뜀을 저장하지 못해요. 새 버전으로 다시 띄워 주세요."
        : err.message);
    }
  };
  reason.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSkip(); } });
  const skipBtn = q.status === "skipped"
    ? h("button", { class: "btn sm", type: "button", onclick: () => clearAnswer(q) }, icon("x", "i14"), "건너뜀 되돌리기")
    : h("button", { class: "btn sm", type: "button", "aria-expanded": "false", onclick: (e) => {
      const hidden = skipRow.classList.toggle("hidden");
      e.currentTarget.setAttribute("aria-expanded", String(!hidden));
      if (!hidden) reason.focus();
    } }, "건너뜀");

  const rank = Q.rank.get(q.id);
  const actions = h("div", { class: "row" }, save,
    q.status === "answered" && q.answer ? h("button", { class: "btn sm", type: "button", onclick: () => clearAnswer(q) }, icon("x", "i14"), "답변 지우기") : null,
    skipBtn,
    h("button", { class: "btn sm", type: "button", onclick: () => showDraft(q) }, icon("mail", "i14"), "메시지 만들기"),
    q.target || q.file_path ? h("button", { class: "btn sm", type: "button", onclick: () => askAbout(q) }, icon("message-square-quote", "i14"), "사수에게 묻기") : null);
  const savedInfo = [q.answered_by, localDate(q.answered_at)].filter(Boolean).join(" · ");
  return h("div", { class: "member" },
    h("div", { class: "qtext" }, u.items.length > 1 ? h("span", { class: "qid mono muted xs" }, `Q${q.id} `) : null, q.question),
    h("div", { class: "meta" },
      h("span", {}, `우선 ${rank + 1}위`),
      Q.ties.has(q.priority) ? h("span", { class: "chip", title: "같은 점수라 순위 경계(20·50위)에서 갈린 질문이에요" }, "동점") : null,
      h("span", { class: "chip" }, icon(KIND_ICON[q.kind] || "info", "i12"), q.kind_label),
      statusChip(q),
      q.label ? h("span", { class: "qko-meta", title: q.module ? `모듈 ${q.module}${q.module_label ? ` (${q.module_label})` : ""}` : null },
        moduleHead(q.module_label) && !q.label.startsWith(moduleHead(q.module_label)) ? `${moduleHead(q.module_label)} · ${q.label}` : q.label) : null,
      q.file_path ? locLink(`${q.file_path}:${q.line || 1}`, `${fileName(q.file_path)}:${q.line || 1}`) : null),
    h("div", { class: "why" }, h("b", {}, "묻는 이유"), q.why),
    q.code ? h("pre", { class: "line" }, q.code) : null,
    q.status === "skipped"
      ? h("div", { class: "skipped-note" }, h("b", {}, "건너뜀"), savedInfo ? ` · ${savedInfo}` : "", q.answer ? ` · 이유: ${q.answer}` : " · 이유 없음")
      : null,
    h("div", { class: "editor" },
      ctx.byRow,
      h("label", { for: taId }, `답변${q.status === "answered" && savedInfo ? ` · 지금 저장된 답변: ${savedInfo}` : ""}`),
      ta, actions, skipRow));
}

// 다음 미답변 질문을 펼친다 (지금 보이는 순서대로)
function openNext(q) {
  const items = filteredItems();
  const order = buildUnits(items).flatMap((u) => u.items);
  const i = order.findIndex((x) => x.id === q.id);
  const next = order.slice(i + 1).find((x) => x.status === "open");
  if (!next) {
    Q.open = null;
    renderQuestions({ focus: `.qrow[data-qid="${q.id}"]` });
    return;
  }
  Q.open = buildUnits(items).find((u) => u.items.some((x) => x.id === next.id)).key;
  renderQuestions();
  const ta = document.getElementById(`ans-${next.id}`);
  if (ta) {
    const row = $("#q-list").querySelector(`.qrow[data-qid="${next.id}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
    keepEditorVisible(ta.closest(".qunit"));
    ta.focus({ preventScroll: true });
  }
}

async function clearAnswer(q) {
  const skipped = q.status === "skipped";
  if (!window.confirm(skipped
    ? `Q${q.id}의 건너뜀을 되돌릴까요? 상태가 미답변으로 돌아가요.`
    : `Q${q.id}에 저장된 답변을 지울까요? 상태가 미답변으로 돌아가요.`)) return;
  try {
    const r = await api(`/api/questions/${q.id}/answer`, { json: { answer: "", by: null } });
    Object.assign(q, r);
    Q.drafts.delete(q.id);
    Q.justSaved.delete(q.id);
    recount();
    toast(skipped ? `Q${q.id}를 미답변으로 되돌렸어요` : `Q${q.id} 답변을 지웠어요`);
    renderQuestions();
  } catch (err) {
    toast(err.message);
  }
}

function openQuestionById(id) {
  if (!Q.data) return;
  const q = Q.data.items.find((x) => x.id === id);
  if (!q) { toast(`Q${id} 질문이 없어요`); return; }
  if (!filteredItems().some((x) => x.id === id)) { Q.status = "all"; Q.kind = ""; Q.search = ""; $("#q-search").value = ""; }
  Q.open = buildUnits(filteredItems()).find((u) => u.items.some((x) => x.id === id)).key;
  renderQuestions();
  const row = $("#q-list").querySelector(`.qrow[data-qid="${id}"]`);
  if (row) { row.scrollIntoView({ block: "center" }); row.focus({ preventScroll: true }); }
}

function setStatus(status) {
  Q.status = status;
  Q.justSaved.clear();
  Q.open = null;
  renderQuestions();
}

function clearFilters() {
  Q.kind = "";
  Q.search = "";
  $("#q-search").value = "";
  renderQuestions();
}

// 메시지 만들기: 보내는 사람([이름])·받는 사람을 채우고 본문은 고쳐 쓸 수 있다. 복사는 고친 글
function draftModal(title, message) {
  const lines = message.split("\n");
  const codeIdx = lines.findIndex((l) => l.startsWith("■ 코드:"));
  const codeLine = codeIdx >= 0 ? lines[codeIdx] : null;
  const st = {
    name: store("sasu-sender-name") || "",
    recipient: store("sasu-recipient") || "전임자",
    withCode: false,
  };
  const nameIn = h("input", { type: "text", id: "draft-name", value: st.name, placeholder: "예: 홍길동", autocomplete: "off" });
  const recIn = h("input", { type: "text", id: "draft-rec", value: st.recipient, placeholder: "예: 김선배", autocomplete: "off" });
  const body = h("textarea", { id: "draft-body", class: "draft-body", rows: "12", spellcheck: "false" });
  const nameOf = (v) => v.trim() || "[이름]";
  // "박과장님"처럼 이미 '님'을 붙여 적으면 다시 붙이지 않는다
  const greet = (r) => (r.trim() ? `${r.trim().replace(/님$/, "")}님, 안녕하세요. ` : "안녕하세요, ");
  // 처음 글: 서버 초안에서 [이름]·인사·코드 줄을 지금 값으로 바꾼다
  const compose = () => {
    let out = lines.filter((l, i) => i !== codeIdx || st.withCode).join("\n");
    out = out.replace("[이름]", nameOf(st.name));
    out = out.replace(/^안녕하세요, /, greet(st.recipient));
    return out;
  };
  body.value = compose();
  // 본문을 고친 뒤에도 이름·받는 사람·코드 줄만 바꿔 넣는다 (고친 글은 그대로)
  const swap = (from, to) => { if (from !== to && body.value.includes(from)) body.value = body.value.replace(from, to); };
  nameIn.addEventListener("input", () => {
    const before = nameOf(st.name);
    st.name = nameIn.value;
    store("sasu-sender-name", st.name.trim());
    swap(`업무를 이어받은 ${before}입니다`, `업무를 이어받은 ${nameOf(st.name)}입니다`);
  });
  recIn.addEventListener("input", () => {
    const before = greet(st.recipient);
    st.recipient = recIn.value;
    store("sasu-recipient", st.recipient.trim());
    if (body.value.startsWith(before)) body.value = greet(st.recipient) + body.value.slice(before.length);
  });
  const toggleCode = (on) => {
    st.withCode = on;
    const cur = body.value.split("\n");
    if (!on) { body.value = cur.filter((l) => !l.startsWith("■ 코드:")).join("\n"); return; }
    if (cur.some((l) => l.startsWith("■ 코드:"))) return;
    const at = cur.findIndex((l) => l.startsWith("■ 위치:"));
    cur.splice(at >= 0 ? at + 1 : cur.length, 0, codeLine);
    body.value = cur.join("\n");
  };
  modal(title, [
    h("p", { class: "muted small", style: "margin:0" }, "메신저나 메일로 보내세요. 본문은 고쳐 써도 됩니다. 답변을 받으면 이 질문에 적어 두면 됩니다."),
    h("div", { class: "draft-fields" },
      h("label", { for: "draft-name" }, "보내는 사람"), nameIn,
      h("label", { for: "draft-rec" }, "받는 사람"), recIn),
    codeLine ? h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => toggleCode(e.target.checked) }), "코드 한 줄 넣기 (반출 규정 확인 후)") : null,
    h("label", { for: "draft-body", class: "visually-hidden" }, "메시지 본문"),
    body,
  ].filter(Boolean), (close) => [
    h("button", { class: "btn", type: "button", onclick: close }, "닫기"),
    h("button", { class: "btn primary", type: "button", onclick: () => copy(body.value,
      body.value.includes("[이름]") ? "메시지를 복사했어요. [이름]은 아직 그대로예요" : "메시지를 복사했어요") }, icon("copy"), "메시지 복사"),
  ]);
  if (!st.name.trim()) nameIn.focus();
}

async function showDraft(q) {
  try {
    const { message } = await api(`/api/questions/${q.id}/draft`);
    draftModal(`Q${q.id} · 전임자에게 보낼 메시지`, message);
  } catch (err) {
    toast(err.message);
  }
}

function askAbout(q) {
  const target = q.target && /^[\w.]+\.\w+$/.test(q.target) ? q.target : (q.file_path ? `${q.file_path}:${q.line}` : "");
  navigate("ask");
  $("#ask-q").value = q.question;
  $("#ask-target").value = target;
  $("#ask-form").requestSubmit();
}

function askWithTarget(target) {
  closeDrawer(false);
  navigate("ask");
  $("#ask-target").value = target;
  $("#ask-q").focus();
  toast("대상 코드를 채웠어요. 질문을 적고 물어보세요.");
}

// 노드·서랍의 질문 배지: 누르면 그 질문으로
function questionBadge(q) {
  return h("button", { class: "qbadge", type: "button", title: `${q.question} (${STATUS_LABEL[q.status] || q.status})`,
    "aria-label": `Q${q.id} 질문 보기, ${STATUS_LABEL[q.status] || q.status}`,
    onclick: (e) => { e.stopPropagation(); closeDrawer(false); navigate("questions", { q: q.id }); } },
  icon(q.status === "answered" ? "check" : "message-circle-question-mark", "i12"), `Q${q.id}`);
}

document.querySelectorAll("#q-status button").forEach((b) => b.addEventListener("click", () => setStatus(b.dataset.status)));
$("#q-kind").addEventListener("change", (e) => { Q.kind = e.target.value; Q.open = null; renderQuestions(); });
let searchTimer = null;
$("#q-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { Q.search = e.target.value; renderQuestions(); }, 150);
});
// Enter: 결과 머리로 포커스를 옮기고 그 자리로 스크롤한다
$("#q-search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  clearTimeout(searchTimer);
  Q.search = e.target.value;
  renderQuestions();
  focusResultHead($("#q-result-head"));
});
$("#q-export-visible").addEventListener("click", (e) => {
  if (e.currentTarget.getAttribute("aria-disabled") === "true") { e.preventDefault(); toast("보이는 질문이 없어요"); }
});
$("#q-import").addEventListener("click", () => $("#q-import-file").click());
$("#q-import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const r = await api("/api/questions/import", { method: "POST", body: form });
    // saved = 실제로 바뀐 행, unchanged = 이미 같은 행, missing = 질문을 못 찾은 행
    const parts = [`새로 반영 ${fmt(r.saved)}`];
    if (r.unchanged !== undefined && r.unchanged !== null) parts.push(`이미 같음 ${fmt(r.unchanged)}`);
    parts.push(`못 찾음 ${fmt(r.missing || 0)}`);
    toast(`답변 엑셀을 올렸어요: ${parts.join(" · ")}`);
    await getQuestions(true);
    renderQuestions();
  } catch (err) {
    toast(/읽지 못했어요/.test(err.message)
      ? "파일을 열 수 없어요. 문서 보안(DRM)이 걸린 파일일 수 있으니, 사내 보안 담당자에게 처리 방법을 문의해 주세요."
      : err.message);
  }
  e.target.value = "";
});

function toggleMenu(open) {
  const menu = $("#q-menu");
  menu.classList.toggle("hidden", !open);
  $("#q-more").setAttribute("aria-expanded", String(open));
  if (open) {
    // 좁은 화면에서 버튼이 왼쪽으로 줄바꿈되면 메뉴가 화면 밖으로 잘리지 않게 붙는 쪽을 바꾼다
    menu.classList.remove("to-left");
    if (menu.getBoundingClientRect().left < 8) menu.classList.add("to-left");
    $("#q-refresh").focus();
  }
}
$("#q-more").addEventListener("click", () => toggleMenu($("#q-menu").classList.contains("hidden")));
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) toggleMenu(false); });
$("#q-refresh").addEventListener("click", () => {
  toggleMenu(false);
  const when = projectInfo ? stamp(projectInfo.indexed_at) : "마지막 분석";
  modal("질문 다시 만들기", [
    h("p", { style: "margin:0" }, withCode(`분석 기준(${when})으로 질문을 다시 만들어요. 받은 답변은 그대로예요. 코드가 바뀌었으면 먼저 터미널에서 \`sasu index\`를 실행하세요.`)),
  ], (close) => [
    h("button", { class: "btn", type: "button", onclick: close }, "닫기"),
    h("button", { class: "btn primary", type: "button", onclick: async (e) => {
      e.currentTarget.disabled = true;
      try {
        const r = await api("/api/questions/refresh", { method: "POST" });
        close();
        toast(`질문을 다시 만들었어요: 새 질문 ${r.new}개, 사라진 질문 ${r.stale}개. 받은 답변은 그대로예요`);
        await getQuestions(true);
        fillKindSelect();
        renderQuestions();
      } catch (err) {
        close();
        toast(err.message);
      }
    } }, icon("refresh-cw"), "질문 다시 만들기"),
  ]);
});

// 인쇄: 질문 화면을 표로. 인쇄하는 동안만 필터를 "전체"로
function buildPrintSheet() {
  const d = Q.data;
  const p = questionProgress();
  const today = new Date();
  const meta = [
    projectInfo ? projectInfo.name : $("#project").value,
    projectInfo ? `분석 기준 ${stamp(projectInfo.indexed_at)}` : null,
    projectInfo && projectInfo.head ? `커밋 ${projectInfo.head.slice(0, 7)}` : null,
    `출력일 ${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`,
    `먼저 물을 ${p.topTotal}개 중 ${p.topAnswered}`,
    `전체 ${p.total}개 중 ${p.answered} 답변 완료`,
  ].filter(Boolean).join(" · ");
  setKids($("#q-print"), 
    h("h1", {}, "인수인계 질문·답변 목록"),
    h("div", { class: "meta" }, meta),
    h("table", {},
      h("colgroup", {}, ["6%", "8%", "9%", "17%", "29%", "19%", "6%", "6%"].map((w) => h("col", { style: `width:${w}` }))),
      h("thead", {}, h("tr", {}, ["번호", "상태", "종류", "위치", "질문", "답변", "답한 사람", "일자"].map((c) => h("th", {}, c)))),
      h("tbody", {}, d.items.map((q) => h("tr", {},
        h("td", {}, `Q${q.id}`), h("td", {}, STATUS_LABEL[q.status] || q.status), h("td", {}, q.kind_label),
        h("td", { class: "ploc" }, q.target ? wbr(q.target) : null, q.file_path ? h("div", {}, `${fileName(q.file_path)}:${q.line}`) : null),
        h("td", {}, q.question), h("td", {}, q.answer || ""), h("td", {}, q.answered_by || ""),
        h("td", {}, localDate(q.answered_at)))))));
}
let printSaved = null;
window.addEventListener("beforeprint", () => {
  if (currentView !== "questions" || !Q.data) return;
  printSaved = { status: Q.status, kind: Q.kind, search: Q.search };
  Q.status = "all"; Q.kind = ""; Q.search = "";
  renderQuestions();
  buildPrintSheet();
  document.body.classList.add("printing-questions");
});
window.addEventListener("afterprint", () => {
  document.body.classList.remove("printing-questions");
  if (!printSaved) return;
  Object.assign(Q, printSaved);
  printSaved = null;
  renderQuestions();
});

// ── 경로 추적 (7.3) ──────────────────────────────────────

const traceState = { last: null };
// URL → 한글 설명 (서버 /api/urls의 labels, 모듈 상세의 urls[].label에서 모은다)
const urlLabels = new Map();
function rememberUrlLabels(labels) {
  if (labels && typeof labels === "object") for (const [u, l] of Object.entries(labels)) if (l) urlLabels.set(u, l);
}
let suggestTimer = null;
let suggestIndex = -1;
let suggestToken = 0;  // 추적을 시작하면 늦게 온 추천 목록은 버린다

views.trace = async function (params) {
  renderRecentTraces();
  if (params && params.url) { traceUrl(params.url, { fromHash: true }); return; }
  $("#trace-q").focus();
  if ($("#trace-result").childElementCount) return;
  // 빈 상태: 누를 수 있는 실제 예시
  try {
    const data = await api("/api/urls?limit=3");
    if ($("#trace-result").childElementCount) return;
    setKids($("#trace-result"), emptyState("route", "URL을 넣으면 거치는 코드·SQL·테이블을 따라가요", null,
      ...data.urls.map((u) => h("button", { class: "chip mono", type: "button", onclick: () => traceUrl(u) }, u))));
  } catch (err) {
    setKids($("#trace-result"), errorBox(err));
  }
};

function renderRecentTraces() {
  const list = recentList("sasu-recent-trace");
  const box = $("#trace-recent");
  box.classList.toggle("hidden", !list.length);
  setKids(box, "최근 추적:", ...list.map((u) => h("button", { class: "chip mono", type: "button", onclick: () => traceUrl(u) }, u)));
}

function hideSuggest() {
  $("#trace-suggest").classList.add("hidden");
  $("#trace-q").setAttribute("aria-expanded", "false");
  $("#trace-q").removeAttribute("aria-activedescendant");
  suggestIndex = -1;
}

$("#trace-q").addEventListener("input", (e) => {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(() => suggest(e.target.value), 180);
});
$("#trace-q").addEventListener("keydown", (e) => {
  const items = [...document.querySelectorAll("#trace-suggest li[data-url]")];
  const openList = !$("#trace-suggest").classList.contains("hidden");
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && openList && items.length) {
    e.preventDefault();
    suggestIndex = Math.max(0, Math.min(items.length - 1, suggestIndex + (e.key === "ArrowDown" ? 1 : -1)));
    items.forEach((el, i) => el.setAttribute("aria-selected", String(i === suggestIndex)));
    $("#trace-q").setAttribute("aria-activedescendant", items[suggestIndex].id);
    items[suggestIndex].scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter") {
    e.preventDefault();
    const pick = openList ? items[suggestIndex] : null;
    traceUrl(pick ? pick.dataset.url : e.target.value);
  }
});
document.addEventListener("click", (e) => { if (!e.target.closest(".search-wrap")) hideSuggest(); });
$("#trace-full").addEventListener("change", () => { if (traceState.last) traceUrl(traceState.last); });

async function suggest(q) {
  const box = $("#trace-suggest");
  const token = ++suggestToken;
  if (!q.trim()) { hideSuggest(); return; }
  try {
    const data = await api(`/api/urls?q=${encodeURIComponent(cleanUrl(q).path)}&limit=30`);
    if (token !== suggestToken) return;
    suggestIndex = -1;
    rememberUrlLabels(data.labels);
    if (!data.urls.length) {
      setKids(box, h("li", { class: "more", role: "presentation" },
        `${noMatch(q.trim(), "URL이")}. URL 일부${HANGUL.test(q) ? "" : "나 업무명(예: 휴가)"}으로 찾아 보세요.`));
      box.classList.remove("hidden");
      $("#trace-q").setAttribute("aria-expanded", "true");
      announce(noMatch(q.trim(), "URL이"));
      return;
    }
    setKids(box, ...data.urls.map((u, i) => h("li", { id: `sug-${i}`, role: "option", "aria-selected": "false", "data-url": u, onclick: () => traceUrl(u) },
      h("span", { class: "sug-url" }, u), urlLabels.get(u) ? h("span", { class: "sug-ko" }, urlLabels.get(u)) : null)),
      data.total > data.urls.length ? h("li", { class: "more", role: "presentation" }, `… ${fmt(data.total - data.urls.length)}개 더 (더 입력해 좁히세요)`) : null);
    box.classList.remove("hidden");
    $("#trace-q").setAttribute("aria-expanded", "true");
    $("#trace-q").removeAttribute("aria-activedescendant");
  } catch { hideSuggest(); }
}

// 브라우저 주소를 붙여 넣으면 경로만 남긴다 (화면에서만, API는 그대로)
function cleanUrl(raw) {
  const v = (raw || "").trim();
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(v) && !v.startsWith("/")) return { path: v, query: "", changed: false };
  let u;
  try { u = new URL(v, location.origin); } catch { return { path: v, query: "", changed: false }; }
  let path = u.pathname.replace(/;jsessionid=[^/?#]*/i, "");
  try { path = decodeURI(path); } catch { /* 그대로 */ }
  return { path, query: u.search, changed: path !== v };
}

async function traceUrl(input, opts = {}) {
  if (!input) return;
  if (currentView !== "trace" && !opts.fromHash) navigate("trace");
  suggestToken++;
  clearTimeout(suggestTimer);
  hideSuggest();
  $("#trace-q").value = input;
  const out = $("#trace-result");
  out.setAttribute("aria-busy", "true");
  setKids(out, loading("경로를 따라가는 중…"));
  const c = cleanUrl(input);
  const full = $("#trace-full").checked;
  try {
    let data = await api(`/api/trace?url=${encodeURIComponent(c.path)}&full=${full}`);
    let note = c.changed ? { from: input, to: c.path, query: c.query } : null;
    if (!data.url && !data.candidates.length) {
      // 컨텍스트 경로(첫 조각)를 떼고 다시 찾는다. 정확히 같은 URL일 때만 바로 추적
      const segs = c.path.split("/").filter(Boolean);
      if (c.path.startsWith("/") && segs.length >= 2) {
        const stripped = `/${segs.slice(1).join("/")}`;
        const d2 = await api(`/api/trace?url=${encodeURIComponent(stripped)}&full=${full}`);
        if (d2.url === stripped) { data = d2; note = { from: input, to: stripped, query: c.query }; }
        else if (d2.url || d2.candidates.length) {
          data = { url: null, candidates: d2.url ? [d2.url] : d2.candidates };
          note = { from: input, to: stripped, query: c.query };
        }
      }
    }
    if (!data.url && !data.candidates.length) {
      // 업무어(한글)·처리 메서드 설명으로도 찾아 본다 (/api/urls가 한글 설명을 함께 찾는다)
      const d3 = await api(`/api/urls?q=${encodeURIComponent(c.path)}&limit=30`).catch(() => null);
      if (d3 && d3.urls.length) {
        rememberUrlLabels(d3.labels);
        data = { url: null, candidates: d3.urls };
      }
    }
    out.removeAttribute("aria-busy");
    const noteEl = note ? h("div", { class: "notice trace-note" }, icon("info"),
      h("div", {}, "붙여 넣은 주소를 ", h("code", { class: "cmd" }, note.to), "로 바꿔 찾았어요.",
        h("div", { class: "muted xs" }, `원래 입력: ${note.from}`, note.query ? ` · 쿼리스트링 ${note.query}은 추적에 쓰지 않아요` : ""))) : null;
    if (!data.url) {
      if (!data.candidates.length) {
        setKids(out, ...[noteEl, emptyState("search", noMatch(input, "URL이"),
          "URL 일부(예: selectArticleList)나 업무명(예: 휴가)으로 찾아 보세요.")].filter(Boolean));
        announce(noMatch(input, "URL이"));
        focusResultHead(out.querySelector(".empty b"));
      } else {
        setKids(out, ...[noteEl, h("div", { class: "card pad" },
          h("b", { class: "result-head", tabindex: "-1" }, `‘${note ? note.to : input}’에 맞는 URL이 ${data.candidates.length}개 있어요. 하나를 고르세요.`),
          h("ul", { class: "cand-urls" }, data.candidates.map((u) => h("li", {},
            h("button", { class: "urlbtn", type: "button", onclick: () => traceUrl(u) }, wbr(u)),
            urlLabels.get(u) ? h("span", { class: "ko" }, urlLabels.get(u)) : null))))].filter(Boolean));
        announce("추적할 URL을 고르세요");
        focusResultHead(out.querySelector(".result-head"));
      }
      return;
    }
    traceState.last = data.url;
    $("#trace-q").value = data.url;
    pushRecent("sasu-recent-trace", data.url);
    renderRecentTraces();
    history.replaceState(null, "", `#trace?${new URLSearchParams({ url: data.url })}`);
    await getQuestions().catch(() => null);
    setKids(out, ...[noteEl, ...renderTrace(data)].filter(Boolean));
    announce(`${data.url} 경로를 보여 줍니다`);
    if (!opts.fromHash || currentView === "trace") focusResultHead(out.querySelector(".trace-sum"));
  } catch (err) {
    out.removeAttribute("aria-busy");
    setKids(out, errorBox(err, () => traceUrl(input)));
  }
}

const LAYER = { Controller: "C", RestController: "C", Service: "S", Repository: "D" };
const LAYER_NAME = { Controller: "Controller (컨트롤러)", RestController: "RestController (컨트롤러)", Service: "Service (서비스)", Repository: "Repository (DAO)" };
const WRITE_VERBS = new Set(["INSERT", "UPDATE", "DELETE", "MERGE"]);
// 낯선 용어 한 줄 설명 (title과 범례에 같이 쓴다)
const TERM = {
  estimated: "동적 SQL ID를 문자열 앞부분으로 짐작해 이은 SQL이에요. 실제로 부르는 SQL과 다를 수 있어요.",
  external: "이 저장소에 소스가 없는 빈(라이브러리 jar·설정 XML에만 정의)이에요. 안쪽은 따라가지 못해요.",
  broken: "이 메서드가 부르는 SQL ID가 Mapper에 없어요. 이 경로를 실행하면 오류가 나요.",
};
// 추정 연결: 서버가 estimated를 주면 그것을, 없으면 예전처럼 note로 판단
const isEstimated = (s) => (s.estimated === undefined || s.estimated === null ? !!s.note : !!s.estimated);

function splitStatement(label) {
  const i = label.indexOf(" ");
  return i < 0 ? { verb: "", id: label } : { verb: label.slice(0, i), id: label.slice(i + 1) };
}
const tablesOf = (n) => (n.detail || "").split(",").map((t) => t.trim()).filter(Boolean);

function renderTrace(data) {
  const stmts = [];
  const broken = new Set();
  let methods = 0;
  let external = 0;
  const walk = (n) => {
    if (n.kind === "external") external++;
    if (n.kind === "method") {
      methods++;
      for (const id of n.broken || []) broken.add(id);
    }
    if (n.kind === "statement") stmts.push(n);
    n.children.forEach(walk);
  };
  walk(data.tree);
  const reads = stmts.filter((s) => !WRITE_VERBS.has(splitStatement(s.label).verb)).length;
  const writes = stmts.length - reads;
  const dyn = stmts.filter(isEstimated).length;
  // 테이블별 CRUD. 추정 연결로만 닿은 칸은 "U?"처럼 따로 센다
  const tinfo = new Map();
  for (const s of stmts) {
    const verb = splitStatement(s.label).verb;
    const est = isEstimated(s);
    for (const t of tablesOf(s)) {
      const x = tinfo.get(t) || { C: 0, R: 0, U: 0, D: 0, staticRef: false };
      const mark = (k) => { x[k] = est ? Math.max(x[k], 1) : 2; };  // 2 = 확정, 1 = 추정만
      if (verb === "SELECT") mark("R");
      if (verb === "INSERT" || verb === "MERGE") mark("C");
      if (verb === "UPDATE" || verb === "MERGE") mark("U");
      if (verb === "DELETE") mark("D");
      if (!est) x.staticRef = true;
      tinfo.set(t, x);
    }
  }
  const anyGuess = [...tinfo.values()].some((x) => ["C", "R", "U", "D"].some((k) => x[k] === 1));
  const ctx = { first: new Map(), qmap: new Map(), treeEl: null };
  if (Q.data) for (const q of Q.data.items) if (q.target) ctx.qmap.set(q.target, [...(ctx.qmap.get(q.target) || []), q]);

  const highlight = (table) => {
    ctx.treeEl.querySelectorAll(".node").forEach((el) => {
      const ts = (el.dataset.tables || "").split(",");
      el.classList.toggle("dim", table !== null && !ts.includes(table));
    });
  };
  const tableChip = (t, x) => h("button", {
    class: `chip mono ${x.C || x.U || x.D ? "solid" : x.staticRef ? "ctl" : "dashed"}`, type: "button",
    title: `${x.C || x.U || x.D ? "쓰기" : "읽기"}${x.staticRef ? "" : " · 추정 연결로만 닿음"} — 이 테이블을 쓰는 SQL만 강조`,
    onmouseenter: () => highlight(t), onmouseleave: () => highlight(null), onfocus: () => highlight(t), onblur: () => highlight(null),
  }, t);

  const summary = h("div", { class: "card trace-sum", tabindex: "-1" },
    h("div", { class: "row", style: "gap:8px" },
      h("span", {}, [`메서드 ${methods}`, `SQL ${stmts.length}`, `읽기 ${reads}`, writes ? `쓰기 ${writes}` : "이 소스에서 쓰기 없음"].join(" · "),
        " · ", h("span", { class: "term", title: TERM.estimated }, `추정 연결 ${dyn}`)),
      broken.size ? h("span", { class: "chip bad", title: `Mapper에 없는 SQL: ${[...broken].join(", ")}` }, icon("triangle-alert", "i12"), `없는 SQL ${broken.size}`) : null),
    tinfo.size ? h("div", { class: "tabchips" }, [...tinfo].map(([t, x]) => tableChip(t, x))) : h("div", { class: "muted xs" }, "닿는 테이블이 없어요"),
    h("div", { class: "trace-legend" },
      h("span", {}, h("span", { class: "dashed-tag" }, "추정 연결"), " ", TERM.estimated),
      external ? h("span", {}, h("span", { class: "dashed-tag" }, "저장소 밖 · 빈"), " ", TERM.external) : null,
      broken.size ? h("span", {}, h("span", { class: "chip bad risk" }, icon("triangle-alert", "i12"), "없는 SQL 호출"), " ", TERM.broken) : null));

  const tree = h("ul", { class: "tree" }, traceNode(data.tree, 0, ctx));
  ctx.treeEl = tree;
  const crud = h("div", { class: "card crud-col" },
    h("div", { class: "pad", style: "padding-bottom:8px" }, h("b", { class: "small" }, "이 URL이 쓰는 테이블"), h("div", { class: "muted xs" }, "CRUD (URL × 테이블)")),
    tinfo.size ? h("div", { class: "table-wrap" }, h("table", { class: "grid crud-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "테이블"), ...["C", "R", "U", "D"].map((k) => h("th", { class: "c", style: "text-align:center" }, k)))),
      h("tbody", {}, [...tinfo].map(([t, x]) => h("tr", {}, h("td", { class: "mono xs" }, t, x.staticRef ? null : h("span", { class: "muted" }, " (추정)")),
        ...["C", "R", "U", "D"].map((k) => h("td", { class: "c" }, x[k] === 2 ? k
          : x[k] === 1 ? h("span", { class: "guess-cell", title: `추정 연결로만 닿음 — ${TERM.estimated}` }, `${k}?`)
          : h("span", { class: "zero" }, "–")))))))) : null,
    tinfo.size && anyGuess ? h("div", { class: "pad muted xs", style: "padding-top:8px" }, `U?처럼 물음표 = 추정 연결로만 닿은 칸이에요. ${TERM.estimated}`) : null,
    tinfo.size ? null : h("div", { class: "pad muted xs" }, "SQL에 닿지 않는 URL이에요."));
  return [summary, h("div", { class: "trace-grid" }, h("div", { class: "card pad" }, tree), crud)];
}

function isUtil(n) {
  return n.kind === "method" && !n.detail && !n.repeated;
}

function renderChildren(children, depth, ctx) {
  const out = [];
  for (let i = 0; i < children.length;) {
    if (isUtil(children[i])) {
      let j = i;
      while (j < children.length && isUtil(children[j])) j++;
      if (j - i >= 2) {
        const group = children.slice(i, j);
        const li = h("li", {});
        const btn = h("button", { class: "utilfold", type: "button", "aria-expanded": "false",
          title: group.map((n) => n.label).join("\n") },
        icon("chevron-right", "i14"), h("span", { class: "lyr util" }), `유틸 ${group.length}개`,
        h("span", { class: "muted" }, ` (${group.map((n) => n.label.split(".")[0]).filter((v, k, a) => a.indexOf(v) === k).join(", ")})`));
        btn.addEventListener("click", () => {
          const lis = group.map((n) => traceNode(n, depth, ctx));
          li.replaceWith(...lis);
          const f = lis[0].querySelector("button");
          if (f) f.focus();
        });
        li.append(btn);
        out.push(li);
        i = j;
        continue;
      }
    }
    out.push(traceNode(children[i], depth, ctx));
    i++;
  }
  return out;
}

function traceNode(n, depth, ctx) {
  const lead = [];
  const body = [];
  let koEl = null;
  let cls = `node ${n.kind}`;
  if (n.kind === "url" && depth === 0) {
    lead.push(h("span", { class: "pill" }, "URL"));
    body.push(h("span", { class: "name" }, wbr(n.label)));
  } else if (n.kind === "method") {
    const letter = n.detail ? (LAYER[n.detail] || n.detail[0].toUpperCase()) : null;
    if (letter) lead.push(h("span", { class: "lyr", title: LAYER_NAME[n.detail] || n.detail }, letter));
    else { lead.push(h("span", { class: "lyr util", title: "유틸 (스테레오타입 없음)" })); cls += " util"; }
    if (n.repeated) {
      cls += " repeated";
      body.push(icon("repeat", "i14"), h("span", { class: "name" }, n.label),
        h("button", { class: "btn-reset muted xs", type: "button", style: "text-decoration:underline", onclick: () => jumpToFirst(n.label, ctx) }, "(위에서 펼침 · 처음 위치로)"));
    } else body.push(h("span", { class: "name" }, n.label));
    for (const q of ctx.qmap.get(n.label) || []) body.push(questionBadge(q));
    // 없는 SQL 호출: 위험 칩(아이콘 + 글자)
    for (const id of n.broken || []) {
      body.push(h("span", { class: "chip bad risk", title: TERM.broken }, icon("triangle-alert", "i12"), `없는 SQL 호출: ${id}`));
    }
    const ko = koOf(n);
    if (ko && !n.repeated) koEl = h("span", { class: "node-ko", title: ko }, ko);
  } else if (n.kind === "statement") {
    const { verb, id } = splitStatement(n.label);
    const est = isEstimated(n);
    lead.push(icon("database", "i14"));
    body.push(h("span", { class: `verb${WRITE_VERBS.has(verb) ? " w" : ""}` }, verb), h("span", { class: "name" }, wbr(id)));
    if (est) body.push(h("span", { class: "dashed-tag", title: TERM.estimated }, `추정 연결${n.note ? ` · ${n.note}` : ""}`));
    const ts = tablesOf(n);
    if (ts.length) {
      body.push(h("span", { class: "tables" }, ts.map((t) => h("span", {
        class: `chip mono ${WRITE_VERBS.has(verb) ? "solid" : est ? "dashed" : "ctl"}`,
      }, t))));
    }
  } else if (n.kind === "view") {
    lead.push(icon("app-window", "i14"));
    body.push(h("span", { class: "name" }, wbr(n.label)));
    if (n.detail) body.push(h("span", { class: "chip bad" }, icon("triangle-alert", "i12"), n.detail));
  } else if (n.kind === "url") {
    lead.push(icon("corner-down-right", "i14"));
    body.push(h("button", { class: "urlbtn", type: "button", title: "이 URL 추적", onclick: () => traceUrl(n.label) }, wbr(n.label)),
      h("span", { class: "muted xs" }, n.detail || ""));
  } else {
    lead.push(h("span", { class: "lyr ext", title: `저장소 밖 — ${TERM.external}` }));
    body.push(h("span", { class: "name" }, n.label));
    const bean = (n.note || "").replace(/\s*\(저장소 밖\)\s*$/, "");
    body.push(h("span", { class: "dashed-tag", title: TERM.external }, `저장소 밖${bean ? ` · ${bean}` : ""}`));
  }
  if (n.count > 1) body.push(h("span", { class: "muted xs" }, `×${n.count}`));
  if (koEl) body.push(koEl);  // 한글 설명은 맨 끝 한 줄 (×n이 그 아래로 밀리지 않게)

  const li = h("li", { class: n.kind === "statement" && isEstimated(n) ? "dyn" : null });
  const kids = n.children.length ? h("ul", { class: depth >= 5 ? "hidden" : null }, renderChildren(n.children, depth + 1, ctx)) : null;
  const toggle = kids ? h("button", { class: "toggle-kids", type: "button" }) : h("span", { style: "display:inline-block;width:24px" });
  if (kids) {
    const shortName = n.label.split(".")[0].replace(/^(SELECT|INSERT|UPDATE|DELETE|MERGE) /, "");
    const paint = () => {
      const open = !kids.classList.contains("hidden");
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", `${shortName} 아래 ${n.children.length}개 ${open ? "접기" : "펼치기"}`);
      setKids(toggle, icon(open ? "chevron-down" : "chevron-right", "i14"), open ? "" : `${n.children.length}개 더`);
    };
    toggle.addEventListener("click", () => { kids.classList.toggle("hidden"); paint(); });
    kids._paint = paint;
    paint();
  }
  const tables = n.kind === "statement" ? tablesOf(n).join(",") : "";
  const row = h("div", { class: cls, "data-tables": tables },
    h("span", { class: "lead-ic" }, toggle, lead),
    h("span", { class: "body" }, body),
    n.location ? locLink(n.location, shortLoc(n.location), n.location) : h("span"));
  if (n.kind === "method" && !n.repeated && !ctx.first.has(n.label)) ctx.first.set(n.label, row);
  li.append(row);
  if (kids) li.append(kids);
  return li;
}

function jumpToFirst(label, ctx) {
  const el = ctx.first.get(label);
  if (!el) return;
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (p.tagName === "UL" && p.classList.contains("hidden")) { p.classList.remove("hidden"); if (p._paint) p._paint(); }
  }
  el.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1500);
  const f = el.querySelector("button");
  if (f) f.focus({ preventScroll: true });
}

// ── 사수에게 묻기 (7.4) ──────────────────────────────────

let askRun = 0;

views.ask = async function (params) {
  const box = $("#ask-examples");
  if (params && params.q) paramHandlers.ask(params);
  renderRecentAsks();
  try {
    const data = await getQuestions();
    const picks = data.items.filter((q) => q.target && /^[\w.]+\.\w+$/.test(q.target)).slice(0, 3);
    setKids(box, ...(picks.length ? [h("span", { class: "muted" }, "인수인계 질문에서 골라 묻기:")] : []),
      ...picks.map((q) => h("button", {
        type: "button", title: `대상: ${q.target}`,
        onclick: () => { $("#ask-q").value = q.question; $("#ask-target").value = q.target; $("#ask-form").requestSubmit(); },
      }, `${q.question.slice(0, 30)}${q.question.length > 30 ? "…" : ""}`)));
  } catch { setKids(box, ); }
  if (!params || !params.q) $("#ask-q").focus();
};

function renderRecentAsks() {
  const list = recentList("sasu-recent-ask");
  const box = $("#ask-recent");
  box.classList.toggle("hidden", !list.length);
  setKids(box, "최근:", ...list.map((r) => h("button", { class: "chip", type: "button", title: r.target ? `대상: ${r.target}` : null,
    onclick: () => { $("#ask-q").value = r.question; $("#ask-target").value = r.target || ""; $("#ask-q").focus(); } },
  `${r.question.slice(0, 16)}${r.question.length > 16 ? "…" : ""}`)));
}

// 한 번 물은 뒤 질문을 새로 쓰면, 앞 질문에 쓰인 '대상 코드'를 비운다.
// 남아 있으면 새 질문이 다른 코드 기준으로 답을 받는다(재시험 N01). 대상 칸을 직접 고치면 그대로 둔다.
$("#ask-q").addEventListener("input", () => {
  const t = $("#ask-target");
  if (t.dataset.used === "1" && t.value) {
    t.value = "";
    t.dataset.used = "";
    toast("새 질문이라 앞 질문의 대상 코드를 비웠어요.");
  }
});
$("#ask-target").addEventListener("input", () => { $("#ask-target").dataset.used = ""; });
$("#ask-form").addEventListener("submit", () => { $("#ask-target").dataset.used = "1"; });
$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runAsk({ question: $("#ask-q").value.trim(), target: $("#ask-target").value.trim() || null });
});
$("#ask-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#ask-form").requestSubmit(); }
});

function steps(on) {
  const names = ["① 코드 찾기", "② 근거 모으기", "③ 답 쓰기"];
  const el = h("div", { class: "steps" });
  names.forEach((n, i) => {
    if (i) el.append(h("span", { "aria-hidden": "true" }, "→"));
    el.append(h("span", { class: i < on ? "done" : i === on ? "on" : null }, i === on ? icon("loader-circle", "i14 spin") : i < on ? icon("check", "i14") : null, ` ${n}`));
  });
  announce(`${names[on]} 중`);
  return el;
}

// 묻기는 두 단계: ① 근거(대상)만 받는다 → 사수가 검색으로 고른 대상이면 확인받는다 → ② 확인 뒤에만 답을 만든다
async function runAsk(req, candidates) {
  if (!req.question) return;
  const run = ++askRun;
  const body = { question: req.question, target: req.target || null, method_id: req.method_id || null, use_git: $("#ask-git").checked };
  pushRecent("sasu-recent-ask", { question: req.question, target: req.target || null });
  renderRecentAsks();
  const out = $("#ask-result");
  out.setAttribute("aria-busy", "true");
  setKids(out, steps(0));
  $("#ask-go").disabled = true;
  const done = () => { if (run === askRun) { $("#ask-go").disabled = false; out.removeAttribute("aria-busy"); } };
  let ev;
  try {
    ev = await api("/api/ask/evidence", { json: body });
  } catch (err) {
    setKids(out, errorBox(err, () => runAsk(req, candidates)));
    done();
    return;
  }
  if (run !== askRun) return;
  if (!ev.target) {
    setKids(out, emptyState("search", "관련 코드를 찾지 못했어요", "질문에 URL이나 메서드 이름을 넣거나 '대상 코드' 칸을 채워 보세요."));
    done();
    focusResultHead(out.querySelector(".empty b"));
    return;
  }
  const cands = (candidates && candidates.length ? candidates : ev.candidates) || [];
  // 서버가 auto를 주지 않으면: 대상·후보 선택 없이 검색으로 골랐을 때(후보가 있을 때)를 자동으로 본다
  const auto = typeof ev.auto === "boolean" ? ev.auto : (!body.target && !body.method_id && cands.length > 0);
  if (auto) {
    setKids(out, confirmCard(req, ev, cands, () => { const r = ++askRun; answerPhase(r, req, body, ev, cands); }));
    done();
    announce("이 코드에 대해 물을지 확인해 주세요");
    focusResultHead(out.querySelector(".confirm-head"));
    return;
  }
  answerPhase(run, req, body, ev, cands);
}

// 후보 칩 글자: 한글 설명 + Class.method
function candLabel(c) {
  const ko = koOf(c);
  return ko ? [h("span", { class: "cko" }, ko), " ", h("span", { class: "mono" }, c.label)] : h("span", { class: "mono" }, c.label);
}

function confirmCard(req, ev, cands, go) {
  const t = ev.target;
  const ko = koOf(t);
  const others = cands.filter((c) => c.method_id !== t.method_id);
  return h("div", { class: "card confirm-card" },
    h("h3", { class: "confirm-head", tabindex: "-1" }, icon("search", "i20"), "이 코드에 대해 물을까요?"),
    h("p", { class: "muted small" }, "질문 낱말로 사수가 고른 코드예요. 답은 질문 전체가 아니라 이 코드를 기준으로 만들어요."),
    h("div", { class: "confirm-target" },
      ko ? h("div", { class: "ko-big" }, ko) : h("div", { class: "muted xs" }, "한글 설명 없음"),
      h("div", { class: "mono" }, t.label),
      locLink(`${t.path}:${t.line_start}-${t.line_end}`, `${t.path}:${t.line_start}`, `코드 보기: ${t.path}:${t.line_start}`)),
    h("div", { class: "row" },
      h("button", { class: "btn primary lg", type: "button", onclick: go }, icon("message-square-quote"), "이 코드로 묻기")),
    others.length ? h("div", { class: "candidates" }, h("span", { class: "muted" }, "다른 후보:"),
      others.map((c) => h("button", { type: "button", title: `${c.path}:${c.line}`,
        onclick: () => runAsk({ question: req.question, method_id: c.method_id }, cands) }, candLabel(c)))) : null,
    urlPicker(req),
    h("p", { class: "muted xs", style: "margin:4px 0 0" }, "다른 코드면 후보에서 고르거나, 화면(URL)으로 찾거나, ",
      h("button", { class: "btn-reset linkish", type: "button", onclick: () => $("#ask-target").focus() }, "'대상 코드' 칸"),
      "에 넣으세요."));
}

// 확인 카드의 "화면(URL)으로 찾기": URL 일부나 한글 업무명으로 화면을 고르면 그 URL을 대상으로 바로 묻는다
let urlPickSeq = 0;
function urlPicker(req) {
  const n = ++urlPickSeq;
  const listId = `ask-url-sug-${n}`;
  const inId = `ask-url-q-${n}`;
  let idx = -1;
  let timer = null;
  let token = 0;
  let urls = [];
  const box = h("ul", { class: "suggest card hidden", id: listId, role: "listbox", "aria-label": "화면(URL) 추천" });
  const input = h("input", {
    type: "search", id: inId, autocomplete: "off", style: "width:100%",
    placeholder: "URL 일부나 업무명 (예: 로그인, 휴가, selectArticleList)",
    role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false", "aria-controls": listId,
  });
  const hide = () => { box.classList.add("hidden"); input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); idx = -1; };
  const pick = (u) => { hide(); runAsk({ question: req.question, target: u }); };
  const mark = () => {
    box.querySelectorAll("li[role=option]").forEach((li, i) => li.setAttribute("aria-selected", String(i === idx)));
    const cur = box.querySelector(`#${listId}-${idx}`);
    if (cur) { input.setAttribute("aria-activedescendant", cur.id); cur.scrollIntoView({ block: "nearest" }); }
  };
  const load = async (q) => {
    const my = ++token;
    try {
      const data = await api(`/api/urls?q=${encodeURIComponent(q)}&limit=20`);
      if (my !== token) return;
      rememberUrlLabels(data.labels);
      urls = data.urls;
      idx = -1;
      if (!urls.length) {
        setKids(box, h("li", { class: "more", role: "presentation" }, `${noMatch(q, "화면(URL)이")}. 다른 낱말로 찾아 보세요.`));
        announce(noMatch(q, "화면(URL)이"));
      } else {
        setKids(box, ...urls.map((u, i) => h("li", { id: `${listId}-${i}`, role: "option", "aria-selected": "false", onclick: () => pick(u) },
          h("span", { class: "sug-url" }, u), urlLabels.get(u) ? h("span", { class: "sug-ko" }, urlLabels.get(u)) : null)),
          data.total > urls.length ? h("li", { class: "more", role: "presentation" }, `… ${fmt(data.total - urls.length)}개 더 (더 입력해 좁히세요)`) : null);
      }
      box.classList.remove("hidden");
      input.setAttribute("aria-expanded", "true");
      // 카드가 화면 아래쪽이면 추천 목록이 잘려 보이지 않으니 목록까지 보이게 올린다
      const r = box.getBoundingClientRect();
      if (r.bottom > window.innerHeight - 8) window.scrollBy(0, Math.min(r.bottom - window.innerHeight + 16, input.getBoundingClientRect().top - topOffset() - 8));
    } catch { hide(); }
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { token++; hide(); return; }
    timer = setTimeout(() => load(q), 200);
  });
  input.addEventListener("keydown", (e) => {
    const open = !box.classList.contains("hidden") && urls.length;
    if (e.key === "ArrowDown" && open) { e.preventDefault(); idx = Math.min(idx + 1, urls.length - 1); mark(); }
    else if (e.key === "ArrowUp" && open) { e.preventDefault(); idx = Math.max(idx - 1, 0); mark(); }
    else if (e.key === "Escape") { if (!box.classList.contains("hidden")) { e.preventDefault(); e.stopPropagation(); hide(); } }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (open && idx >= 0) pick(urls[idx]);
      else if (open && urls.length === 1) pick(urls[0]);
      else if (input.value.trim().startsWith("/")) pick(input.value.trim());
      else if (input.value.trim()) load(input.value.trim());
    }
  });
  input.addEventListener("blur", () => setTimeout(() => { if (!box.contains(document.activeElement)) hide(); }, 150));
  return h("div", { class: "url-pick" },
    h("label", { for: inId, class: "small" }, icon("app-window", "i14"), " ", h("b", {}, "화면(URL)으로 찾기"),
      h("span", { class: "muted xs" }, " — 고르면 그 화면을 처리하는 코드로 바로 물어요")),
    h("div", { class: "search-wrap" }, input, box));
}

async function answerPhase(run, req, body, ev, cands) {
  const out = $("#ask-result");
  out.setAttribute("aria-busy", "true");
  $("#ask-go").disabled = true;
  const done = () => { if (run === askRun) { $("#ask-go").disabled = false; out.removeAttribute("aria-busy"); } };
  const t = ev.target;
  const ko = koOf(t);
  const progress = steps(2);
  const answerBox = h("div", { class: "card answer-card" }, progress);
  const evidenceCol = h("div", { class: "evidence-col" },
    h("h3", { style: "margin-top:0" }, `근거 ${ev.evidence.length}개`),
    h("div", { class: "evidence" }, ev.evidence.map(evidenceCard)));
  setKids(out,
    cands && cands.length ? h("div", { class: "candidates" }, h("span", { class: "muted" }, "찾은 코드 (다른 코드면 고르세요):"),
      cands.map((c) => h("button", {
        type: "button", "aria-pressed": String(c.method_id === t.method_id), title: `${c.path}:${c.line}`,
        onclick: () => runAsk({ question: req.question, method_id: c.method_id }, cands),
      }, candLabel(c)))) : null,
    h("div", { class: "target-line" }, icon("info", "i14"),
      h("span", {}, "대상: ", h("b", {}, ko || t.label), ko ? h("span", { class: "mono" }, ` (${t.label})`) : null,
        "에 대한 답이에요 — 질문 전체가 아니라 이 코드 기준"),
      locLink(`${t.path}:${t.line_start}-${t.line_end}`, `${fileName(t.path)}:${t.line_start}`)),
    h("div", { class: "ask-grid" }, answerBox, evidenceCol),
  );
  try {
    const ans = await api("/api/ask/answer", { json: { ...body, method_id: t.method_id } });
    if (run !== askRun) return;
    setKids(answerBox, ...renderAnswer(ans, ev));
    announce(ans.error ? "근거만 보여 줍니다" : "사수의 답을 보여 줍니다");
    focusResultHead(out.querySelector(".target-line"));
  } catch (err) {
    if (run === askRun) setKids(answerBox, errorBox(err, () => runAsk(req, cands)));
  }
  done();
}

function evidenceCard(e) {
  const isCode = e.title.endsWith(" 코드");
  const lv = LEVEL[e.level] || { name: e.level, cls: "record", icon: "info" };
  return h("details", { class: "card ev", id: `ev-${e.id}`, open: !isCode },
    h("summary", {}, h("span", { class: "eid" }, e.id), h("span", { class: "muted" }, "·"),
      h("span", {}, icon(lv.icon, `i14 ${lv.cls}`), ` ${lv.name}`), h("span", { class: "muted" }, "·"),
      h("span", { class: "grow" }, e.title),
      e.location ? locLink(e.location, locLabel(e.location)) : null),
    h("div", { class: "back hidden" }),
    h("pre", {}, e.text));
}

function downgradeInfo(text) {
  const m = /^\[(사실|답변)\]인데 '([^']+)' 근거가 없음$/.exec(text || "");
  if (m) return { from: m[1], reason: `${(LEVEL[m[2]] || { name: m[2] }).name} 근거 없음` };
  if (/표시가 없어/.test(text)) return { from: null, reason: "등급 표시 없음" };
  if (/근거 번호가 없음/.test(text)) return { from: null, reason: "근거 번호 없음" };
  return { from: null, reason: text };
}

function renderAnswer(ans, ev) {
  if (ans.error) {
    if (/LLM이 설정되지/.test(ans.error)) {
      return [h("div", { class: "notice" }, icon("info"), h("div", {},
        h("b", {}, "사수의 답을 쓰려면 LLM 연결이 필요해요"),
        h("div", {}, withCode("근거는 아래에서 그대로 볼 수 있어요. 연결은 터미널에서 `sasu setup`으로 해요.")),
        h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "btn sm", type: "button", onclick: () => copy("sasu setup", "명령을 복사했어요") }, icon("copy", "i14"), "명령 복사"))))];
    }
    return [h("div", { class: "notice bad" }, icon("circle-alert"), h("div", {},
      h("b", {}, "LLM 서버에 연결하지 못했어요"),
      h("div", {}, withCode("잠시 뒤 다시 묻거나 `sasu doctor`로 점검하세요. 근거는 아래에 있어요.")),
      h("details", {}, h("summary", {}, "자세히"), ans.error)))];
  }
  const levels = new Map(ev.evidence.map((e) => [e.id, e.level]));
  // 비용은 화면에 보이지 않는다. 모델 이름만 머리 title로
  const parts = [
    h("div", { class: "row" }, h("span", { class: "answer-head grow", title: ans.model ? `모델: ${ans.model}` : null }, "사수의 답")),
    h("div", { class: "muted" }, "근거로 만든 답이에요. 추정·모름은 전임자에게 확인하세요."),
  ];
  const asks = ans.sentences.filter((s) => s.label === "질문");
  const used = new Map();
  let num = 0;
  for (const s of ans.sentences) {
    if (s.label === "질문") continue;
    num++;
    s.citations.forEach((c) => used.set(c, [...(used.get(c) || []), num]));
    const g = GRADE[s.label] || GRADE.추정;
    const down = s.downgraded ? downgradeInfo(s.downgraded) : null;
    const grades = h("div", { class: "grades" },
      down && down.from ? gradeChip(down.from, "struck") : null, gradeChip(s.label));
    const cites = s.citations.map((c) => {
      const lv = LEVEL[levels.get(c)] || { name: "근거", cls: "record", icon: "info" };
      return h("button", { class: "cite", type: "button", "aria-label": `${c} ${lv.name} 근거 보기`, onclick: () => flashEvidence(c) },
        icon(lv.icon, `i12 ${lv.cls}`), c);
    });
    const text = h("div", { class: "grow" }, s.text, " ", cites,
      down ? h("div", { class: "why-down" }, `낮춘 이유: ${down.reason}`) : null);
    if (s.label === "모름") {
      const next = asks.length ? asks[0].text : s.text;
      text.append(h("div", { class: "ask-next" },
        h("div", { class: "small" }, gradeChip("질문"), " ", next),
        h("div", { class: "row" },
          h("button", { class: "btn sm", type: "button", onclick: () => copy(next, "질문을 복사했어요") }, icon("copy", "i14"), "이 질문 복사"),
          h("button", { class: "btn sm", type: "button", onclick: () => askDraft(next, ev.target) }, icon("mail", "i14"), "메시지 만들기"))));
    }
    parts.push(h("div", { class: `sentence ${g.cls}` }, grades, text));
  }
  if (asks.length) {
    parts.push(h("div", { class: "ask-box-q" }, h("b", { class: "small" }, "전임자에게 물을 것"),
      asks.map((s) => h("div", { class: "row" }, h("span", { class: "grow" }, s.text),
        h("button", { class: "btn sm", type: "button", onclick: () => copy(s.text, "질문을 복사했어요") }, icon("copy", "i14"), "이 질문 복사")))));
  }
  const issues = (ans.issues || []).filter((x) => !x.startsWith("추정으로 낮춤"));
  if (issues.length) parts.push(h("div", { class: "issues" }, issues.map((x) => h("div", {}, icon("triangle-alert", "i12"), ` ${x}`))));
  parts.push(h("div", { class: "legend-levels" }, ["사실", "답변", "추정", "모름", "질문"].map((k) => h("div", {}, gradeChip(k), GRADE[k].desc))));
  // 근거 카드에 역링크 "문장 1·3에서 씀"
  for (const e of ev.evidence) {
    const back = document.querySelector(`#ev-${e.id} .back`);
    if (!back) continue;
    const ns = used.get(e.id);
    back.textContent = ns ? `문장 ${ns.join("·")}에서 씀` : "";
    back.classList.toggle("hidden", !ns);
  }
  return parts;
}

// 사수의 답에서 '모름' 다음 행동: 전임자에게 보낼 메시지 (서버 초안과 같은 모양)
function askDraft(question, target) {
  const project = (projectInfo && projectInfo.name) || $("#project").value;
  const dir = target.path.split("/").slice(-3, -1).join("/");
  const message = [
    `안녕하세요, ${project} 업무를 이어받은 [이름]입니다.`,
    "코드 한 곳만 여쭤봐도 될까요?",
    "",
    `■ 질문: ${question}`,
    `■ 위치: ${target.label} (${dir ? dir + "/" : ""}${fileName(target.path)}:${target.line_start})`,
    "",
    "한두 줄로 답해 주셔도 충분합니다. 기억나지 않으시면 \"모름\"이라고만 주셔도 됩니다.",
    "주신 답변은 코드 위치에 기록해 다음 담당자도 볼 수 있게 하겠습니다. 감사합니다.",
  ].join("\n");
  draftModal("전임자에게 보낼 메시지", message);
}

function flashEvidence(id) {
  const el = document.getElementById(`ev-${id}`);
  if (!el) return;
  el.open = true;
  const col = el.closest(".evidence-col");
  if (col && getComputedStyle(col).position === "sticky") {
    // 두 열일 때는 근거 열 안에서만 움직인다 (페이지 스크롤 없음)
    col.scrollTop = el.offsetTop - 8;
  } else el.scrollIntoView({ block: "nearest" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1500);
}

// ── 고도화 준비 (7.5) ────────────────────────────────────

function nameCell(name) {
  const cut = Math.max(name.lastIndexOf("."), name.startsWith("/") ? name.lastIndexOf("/") : -1);
  if (cut <= 0) return h("span", { class: "fname" }, h("b", {}, wbr(name)));
  return h("span", { class: "fname" }, h("span", { class: "pre" }, wbr(name.slice(0, cut + 1))), h("wbr"), h("b", {}, name.slice(cut + 1)));
}

function commonDir(locations) {
  const dirs = locations.map((l) => (parseLocation(l) ? parseLocation(l).path : l).split("/").slice(0, -1));
  if (!dirs.length) return 0;
  let n = 0;
  while (dirs.every((d) => d.length > n && d[n] === dirs[0][n])) n++;
  return n;
}

function whereCell(location, skip) {
  const loc = parseLocation(location);
  const path = loc ? loc.path : location;
  const segs = path.split("/");
  const dir = segs.slice(skip, -1).join("/");
  return h("span", { class: "fwhere" }, locLink(location, `${segs[segs.length - 1]}${loc ? ":" + loc.start : ""}`),
    dir ? h("span", { class: "dir" }, wbr(dir)) : null);
}

// 모듈(기능)별 기존 동작 목록 받기
async function fillAsisModules() {
  const sel = $("#asis-module");
  try {
    const ov = await getOverview();
    const mods = ov.modules.slice().sort((a, b) => a.name.localeCompare(b.name));
    setKids(sel, h("option", { value: "" }, `모듈을 고르세요 (${fmt(mods.length)}개)`),
      ...mods.map((m) => h("option", { value: m.name }, `${moduleOptionText(m)} · URL ${fmt(m.urls)}`)));
  } catch {
    setKids(sel, h("option", { value: "" }, "모듈 목록을 불러오지 못했어요"));
  }
  paintAsis();
}
function paintAsis() {
  const m = $("#asis-module").value;
  const go = $("#asis-module-go");
  go.href = m ? `/api/behaviors/export?module=${encodeURIComponent(m)}` : "/api/behaviors/export";
  go.setAttribute("aria-disabled", String(!m));
}
$("#asis-module").addEventListener("change", paintAsis);
$("#asis-module-go").addEventListener("click", (e) => {
  if (e.currentTarget.getAttribute("aria-disabled") === "true") {
    e.preventDefault();
    toast("모듈을 먼저 고르세요");
    $("#asis-module").focus();
  }
});

views.cleanup = async function () {
  fillAsisModules();
  const unused = $("#cleanup-unused");
  const common = $("#cleanup-common");
  const tiles = $("#cleanup-tiles");
  setKids(unused, loading("부르는 곳을 못 찾은 코드를 찾는 중…"));
  setKids(common, );
  setKids(tiles, );
  let u;
  let c;
  try {
    [u, c] = await Promise.all([getUnused(), api("/api/common")]);
  } catch (err) {
    setKids(unused, errorBox(err, () => { loaded.delete("cleanup"); views.cleanup(); }));
    return;
  }
  const openSection = (id) => {
    const d = document.getElementById(id);
    if (!d) return;
    d.open = true;
    d.scrollIntoView({ block: "start" });
    d.querySelector("summary").focus({ preventScroll: true });
  };
  const tile = (id, label, n, unit, risk) => h("button", { class: "card tile", type: "button", onclick: () => openSection(id) },
    h("span", { class: "n" }, fmt(n), h("small", {}, unit)), h("span", {}, label),
    risk && n ? h("span", { class: "risk" }, icon("triangle-alert", "i12"), "실행 오류 위험") : null);
  const T = [];
  for (const s of u.sections) T.push(tile(`sec-${s.kind}`, s.title, s.items.length, "건", s.kind === "broken_sql"));
  T.push(tile("sec-clones", "복사한 코드", c.clone_stats.groups, "묶음"));
  T.push(tile("sec-dupsql", "같은 SQL", c.duplicate_sql.length, "묶음"));
  setKids(tiles, ...T);

  setKids(unused, ...u.sections.map((s) => {
    const broken = s.kind === "broken_sql";
    const confs = new Set(s.items.map((f) => f.confidence));
    const mixed = confs.size > 1;
    const count = h("span", { class: `chip${broken && s.items.length ? " bad" : ""}` },
      broken && s.items.length ? icon("triangle-alert", "i12") : null, `${fmt(s.items.length)}건`);
    return h("details", { class: "card section", id: `sec-${s.kind}`, open: broken && s.items.length > 0 },
      h("summary", {}, icon("chevron-right", "i14 chev"), count, h("span", {}, s.title),
        h("span", { class: "judge" }, broken ? "● Mapper에 없음 확인" : "○ 후보 — 동적 ID·외부 호출은 못 봄"),
        h("span", { class: "help" }, s.help)),
      h("div", { class: "body" }, s.items.length ? findingsTable(s.items, mixed) : h("div", { class: "muted small" }, "없어요")));
  }));

  const parts = [];
  if (c.shared.length) {
    parts.push(h("details", { class: "card section", id: "sec-shared", open: true },
      h("summary", {}, icon("chevron-right", "i14 chev"), h("span", { class: "chip" }, `${c.shared.length}개`), h("span", {}, "공통 코드"),
        h("span", { class: "help" }, "여러 모듈이 함께 쓰는 클래스입니다. 고치면 여러 기능에 영향이 갑니다.")),
      h("div", { class: "body table-wrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["클래스", "모듈", "종류", "쓰는 모듈 수", "호출 수"].map((col, i) => h("th", { class: i >= 3 ? "num" : null }, col)))),
        h("tbody", {}, c.shared.map((s) => h("tr", {}, h("td", { class: "mono" }, s.class_name), h("td", { class: "mono xs" }, s.module),
          h("td", {}, s.stereotype || "유틸"), h("td", { class: "num" }, s.used_by_modules.length), h("td", { class: "num" }, fmt(s.call_sites)))))))));
  }
  parts.push(h("details", { class: "card section", id: "sec-clones", open: false },
    h("summary", {}, icon("chevron-right", "i14 chev"), h("span", { class: "chip" }, `${fmt(c.clone_stats.groups)}묶음`), h("span", {}, "복사한 코드"),
      h("span", { class: "help" }, `상속하지 않고 복사한 곳 ${fmt(c.clone_stats.methods)}. 변수 이름만 다르고 구조가 같습니다. 한 곳을 고치면 나머지도 확인하세요.`)),
    h("div", { class: "body" }, c.clones.length ? c.clones.map((g, i) => h("div", { class: "clone" },
      h("div", { class: "row" }, h("b", {}, `${i + 1}. ${g.methods.length}곳`), h("span", { class: "muted xs" }, `토큰 ${g.tokens}개`),
        g.note ? h("span", { class: "chip" }, g.note) : null),
      h("div", {}, g.methods.slice(0, 6).map(([name, loc]) => h("div", { class: "row", style: "gap:6px" }, h("span", { class: "mono xs" }, name), locLink(loc, shortLoc(loc), loc))),
        g.methods.length > 6 ? h("div", { class: "muted xs" }, `… 외 ${g.methods.length - 6}곳`) : null))) : h("div", { class: "muted small" }, "없어요"))));
  parts.push(h("details", { class: "card section", id: "sec-dupsql", open: false },
    h("summary", {}, icon("chevron-right", "i14 chev"), h("span", { class: "chip" }, `${c.duplicate_sql.length}묶음`), h("span", {}, "같은 SQL"),
      h("span", { class: "help" }, "같은 SQL이 다른 ID로 있습니다. 한쪽만 고치면 결과가 달라집니다.")),
    h("div", { class: "body" }, c.duplicate_sql.length ? c.duplicate_sql.map((d) => h("div", { class: "clone" },
      h("div", {}, d.statements.map(([id, loc]) => h("div", { class: "row", style: "gap:6px" }, h("span", { class: "mono xs" }, id), locLink(loc, shortLoc(loc), loc)))),
      h("pre", { class: "mono xs muted", style: "white-space:pre-wrap;margin-top:4px" }, d.sql_preview))) : h("div", { class: "muted small" }, "없어요"))));
  setKids(common, ...parts);
};

function findingsTable(items, mixed) {
  const LIMIT = 50;
  const skip = commonDir(items.map((f) => f.location));
  const tbody = h("tbody");
  const reason = (r) => (r || "").replace("(죽은 코드)", "(URL에서 이어지는 경로를 못 찾음)");
  const render = (n) => setKids(tbody, ...items.slice(0, n).map((f) => h("tr", {},
    h("td", {}, nameCell(f.name)), h("td", {}, whereCell(f.location, skip)),
    mixed ? h("td", { class: "xs" }, f.confidence === "높음" ? "● 확실" : "○ 후보") : null,
    h("td", { class: "small" }, reason(f.reason), f.evidence ? h("div", { class: "muted xs" }, f.evidence) : null))));
  render(LIMIT);
  const more = items.length > LIMIT
    ? h("button", { class: "btn sm", type: "button", style: "margin-top:8px", onclick: (e) => { render(items.length); e.currentTarget.remove(); } }, `${fmt(items.length - LIMIT)}건 더 보기`)
    : null;
  return h("div", { class: "table-wrap" }, h("table", { class: "grid findings" },
    h("colgroup", {}, h("col", { class: "name" }), h("col", { class: "where" }), mixed ? h("col", { class: "conf" }) : null, h("col", {})),
    h("thead", {}, h("tr", {}, ["이름", "위치", mixed ? "판단" : null, "설명"].filter(Boolean).map((col) => h("th", {}, col)))), tbody), more);
}

// ── 단축키 (좋음): / 검색, Alt+1~5 탭, 질문 목록에서 J/K·↑↓ ─────

document.addEventListener("keydown", (e) => {
  const typing = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
  if (typing || modals.length) return;
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-5]$/.test(e.key)) {
    e.preventDefault();
    navigate(TAB_ORDER[Number(e.key) - 1]);
    return;
  }
  if (e.key === "/" && !e.ctrlKey && !e.altKey && !e.metaKey) {
    const target = { questions: "#q-search", trace: "#trace-q", ask: "#ask-q" }[currentView];
    if (target) { e.preventDefault(); $(target).focus(); }
    return;
  }
  if (currentView === "questions" && e.target.closest && e.target.closest("#q-list") && ["j", "k", "ArrowDown", "ArrowUp"].includes(e.key)) {
    const rows = [...document.querySelectorAll("#q-list .qrow, #q-list .qgroup-head")];
    const i = rows.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "j" || e.key === "ArrowDown" ? 1 : -1)))];
    next.focus();
    next.scrollIntoView({ block: "nearest" });
  }
});

// ── 좁은 화면·키보드 (고정 머리글 높이·건너뛰기·결과로 포커스) ─────

// 고정된 머리글(+ 질문 화면의 고정 도구 막대) 실제 높이
function topOffset() {
  const head = document.querySelector("header.top");
  let n = head ? head.getBoundingClientRect().height : 52;
  if (currentView === "questions") {
    const bar = $("#q-toolbar");
    if (bar && getComputedStyle(bar).position === "sticky") n += bar.getBoundingClientRect().height;
  }
  return Math.ceil(n);
}

// 머리글 높이를 재서 도구 막대 위치와 scroll-padding-top에 넣는다 (포커스가 고정 막대에 가리지 않게)
function layoutOffsets() {
  const head = document.querySelector("header.top");
  if (!head) return;
  const hh = Math.ceil(head.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--head-real", `${hh}px`);
  document.documentElement.style.scrollPaddingTop = `${topOffset() + 8}px`;
}
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => layoutOffsets());
  ro.observe(document.querySelector("header.top"));
  ro.observe($("#q-toolbar"));
}
window.addEventListener("resize", layoutOffsets);

// 결과 머리로 포커스를 옮기고 고정 막대에 가리지 않게 보인다
function focusResultHead(el) {
  if (!el) return;
  if (!el.hasAttribute("tabindex") && !/^(A|BUTTON|INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) el.setAttribute("tabindex", "-1");
  el.focus({ preventScroll: true });
  const r = el.getBoundingClientRect();
  const top = topOffset() + 8;
  if (r.top < top || r.top > window.innerHeight * 0.6) window.scrollBy(0, r.top - top);
}

// 본문으로 건너뛰기: 해시를 바꾸지 않고(화면 id가 바뀌지 않게) 지금 화면 제목으로
$("#skip-link").addEventListener("click", (e) => {
  e.preventDefault();
  const sec = document.querySelector("section.view.on");
  const target = (sec && sec.querySelector("h2")) || $("#main");
  focusResultHead(target);
});

// ── 시작 ────────────────────────────────────────────────

(async function start() {
  await loadProjects();
  loadQuestionCount();
  const { view, params } = parseHash();
  show(view, params);
})();
