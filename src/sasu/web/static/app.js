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
const fmt = (n) => (n ?? 0).toLocaleString("ko-KR");

class ApiError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(init.json);
    delete init.json;
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* 본문 없음 */ }
  if (!res.ok) throw new ApiError((data && data.detail) || `요청 실패 (${res.status})`, data && data.code);
  return data;
}

function toast(message) {
  const el = h("div", { class: "toast" }, message);
  document.body.append(el);
  setTimeout(() => el.remove(), 2600);
}

function loading(text = "불러오는 중…") {
  return h("div", { class: "empty" }, h("span", { class: "spinner" }), " ", text);
}

function errorBox(err) {
  if (err.code === "no_project") return noProject();
  return h("div", { class: "error-box" }, err.message || String(err));
}

function noProject() {
  return h("div", { class: "card empty" },
    h("b", {}, "분석한 프로젝트가 없어요"),
    "터미널에서 프로젝트 폴더를 먼저 분석해 주세요.",
    h("pre", { class: "mono", style: "margin-top:12px" }, "uv run sasu index <프로젝트 폴더>"),
  );
}

function shortPath(p) {
  if (!p) return "";
  return p.replace("src/main/java/", "").replace("src/main/resources/", "").replace("src/main/webapp/", "");
}

// "경로:줄" 또는 "경로:시작-끝"을 누르면 코드 서랍을 연다
function locLink(location, label) {
  if (!location) return null;
  return h("span", { class: "loc", title: "코드 보기", onclick: (e) => { e.stopPropagation(); openCode(location); } },
    label || shortPath(location));
}

function parseLocation(location) {
  const m = /^(.*?):(\d+)(?:-(\d+))?$/.exec(location || "");
  if (!m) return null;
  return { path: m[1], start: Number(m[2]), end: Number(m[3] || m[2]) };
}

function copy(text) {
  const done = () => toast("복사했어요");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = h("textarea", { style: "position:fixed;opacity:0" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch { toast("복사하지 못했어요"); }
  ta.remove();
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* 저장소를 못 쓰는 환경 */ }
  return null;
}

// ── 코드 서랍 ────────────────────────────────────────────

async function openCode(location) {
  const loc = parseLocation(location);
  if (!loc) return;
  closeDrawer();
  const body = h("div", { class: "code" }, loading("코드를 읽는 중…"));
  const back = h("div", { class: "drawer-back", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-label": "코드 보기" },
    h("header", {},
      h("span", { class: "path grow" }, `${loc.path}:${loc.start}`),
      h("button", { class: "btn sm", onclick: () => copy(`${loc.path}:${loc.start}`) }, "위치 복사"),
      h("button", { class: "btn sm", onclick: closeDrawer }, "닫기 ✕"),
    ),
    body,
  );
  document.body.append(back, drawer);
  try {
    const end = Math.max(loc.end, loc.start + 40);
    const data = await api(`/api/code?path=${encodeURIComponent(loc.path)}&start=${loc.start}&end=${end}`);
    body.replaceChildren(...data.lines.map((line, i) => {
      const n = data.start + i;
      const focus = n >= loc.start && n <= loc.end;
      return h("div", { class: focus ? "focus" : null, id: n === loc.start ? "code-focus" : null },
        h("span", { class: "ln" }, n), line || " ");
    }));
    const focus = document.getElementById("code-focus");
    if (focus) focus.scrollIntoView({ block: "center" });
  } catch (err) {
    body.replaceChildren(h("div", { class: "empty" }, err.message));
  }
}

function closeDrawer() {
  document.querySelectorAll(".drawer, .drawer-back").forEach((el) => el.remove());
}

function modal(title, ...content) {
  const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) back.remove(); } });
  back.append(h("div", { class: "card modal", role: "dialog" },
    h("div", { class: "row" }, h("b", { class: "grow" }, title),
      h("button", { class: "btn sm ghost", onclick: () => back.remove() }, "닫기 ✕")),
    ...content));
  document.body.append(back);
  return back;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeDrawer();
    document.querySelectorAll(".modal-back").forEach((el) => el.remove());
  }
});

// ── 탭과 프로젝트 ────────────────────────────────────────

const views = {};
const loaded = new Set();

function show(view) {
  if (!views[view]) view = "overview";
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("on", s.id === `view-${view}`));
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  if (!loaded.has(view)) {
    loaded.add(view);
    views[view]();
  }
}

document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));

async function loadProjects() {
  const select = $("#project");
  try {
    const data = await api("/api/projects");
    select.replaceChildren(...(data.projects.length ? data.projects : ["(없음)"]).map((name) =>
      h("option", { value: name, selected: name === data.current }, name)));
    select.disabled = !data.projects.length;
    select.title = data.info ? `${data.info.repo_path}\n분석: ${data.info.indexed_at}` : "";
  } catch (err) {
    select.replaceChildren(h("option", {}, "(불러오기 실패)"));
  }
}

$("#project").addEventListener("change", async (e) => {
  try {
    await api("/api/projects/current", { json: { name: e.target.value } });
    toast(`${e.target.value} 프로젝트로 바꿨어요`);
    loaded.clear();
    questionsState.data = null;
    $("#q-count").textContent = "";
    $("#ask-result").replaceChildren();
    $("#trace-result").replaceChildren();
    show(location.hash.slice(1) || "overview");
    loadQuestionCount();
  } catch (err) {
    toast(err.message);
  }
});

// 밝게/어둡게: 기본은 OS 설정을 따른다
(function theme() {
  const saved = store("sasu-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store("sasu-theme", next);
  });
})();

// ── 한눈에 보기 ──────────────────────────────────────────

views.overview = async function () {
  const root = $("#view-overview");
  root.replaceChildren(loading());
  let data;
  try {
    data = await api("/api/overview");
  } catch (err) {
    root.replaceChildren(errorBox(err));
    return;
  }
  const b = data.build;
  const title = b ? `${b.project}${b.version ? " " + b.version : ""}` : $("#project").value;
  const parts = [
    h("div", { class: "row" }, h("h2", { class: "grow" }, title),
      h("a", { class: "btn", href: "/api/behaviors/export", title: "URL마다 지금 하는 일(화면·읽고 쓰는 테이블·분기 값). 고도화 뒤 같은 목록으로 확인" }, "기존 동작 목록 엑셀")),
    h("p", { class: "lead" }, "처음 투입되면 여기서 시작하세요. 기술 스택, 모듈, 여러 곳이 함께 쓰는 테이블입니다."),
  ];

  parts.push(h("div", { class: "stats" }, Object.entries(data.counts).map(([k, v]) =>
    h("div", { class: "card stat" }, h("div", { class: "n" }, fmt(v)), h("div", { class: "k" }, k)))));

  const left = [];
  if (b) {
    const rows = [["빌드", `${b.tool}${b.packaging ? " · " + b.packaging : ""}`]];
    if (b.java_version) rows.push(["Java", b.java_version]);
    if (b.python_version) rows.push(["Python", b.python_version]);
    if (b.parent) rows.push(["부모", b.parent]);
    if (b.servlet_api) rows.push(["Servlet API", b.servlet_api]);
    const dl = h("dl", { class: "stack" });
    for (const [k, v] of rows) dl.append(h("dt", {}, k), h("dd", {}, v));
    for (const [k, items] of Object.entries(b.stack || {})) {
      dl.append(h("dt", {}, k), h("dd", {}, items.map((x) => h("span", { class: "chip blue" }, x))));
    }
    left.push(h("div", { class: "card pad" }, h("b", {}, "기술 스택"), h("div", { style: "margin-top:10px" }, dl)));
  }
  const cov = data.coverage;
  if (cov.total) {
    const pct = (n) => (n / cov.total) * 100;
    const segs = [
      ["SQL까지 이어짐", cov.to_sql, "var(--ok)"],
      ["코드만 거침", cov.code_only, "var(--accent)"],
      ["컨트롤러에서 끝남", cov.handler_only, "var(--line)"],
    ];
    left.push(h("div", { class: "card pad" },
      h("b", {}, `URL ${fmt(cov.total)}개가 어디까지 이어지나`),
      h("div", { class: "bar" }, segs.map(([, n, color]) => h("i", { style: `width:${pct(n)}%;background:${color}` }))),
      h("div", { class: "legend" }, segs.map(([label, n, color]) =>
        h("span", {}, h("i", { style: `background:${color}` }), `${label} ${fmt(n)} (${pct(n).toFixed(1)}%)`))),
    ));
  }
  if (left.length) parts.push(h("div", { class: "two" }, ...left));

  const moduleDetail = h("div");
  const modTable = h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["모듈", "URL", "컨트롤러", "서비스", "DAO", "SQL", "주요 테이블"].map((c, i) =>
      h("th", { class: i && i < 6 ? "num" : null }, c)))),
    h("tbody", {}, data.modules.map((m) => h("tr", {
      class: m.urls ? "click" : null,
      title: m.urls ? "눌러서 URL마다 읽고 쓰는 테이블 보기" : null,
      onclick: m.urls ? () => showModule(m.name, moduleDetail) : null,
    },
    h("td", {}, h("b", {}, m.name)), h("td", { class: "num" }, fmt(m.urls)), h("td", { class: "num" }, fmt(m.controllers)),
    h("td", { class: "num" }, fmt(m.services)), h("td", { class: "num" }, fmt(m.daos)), h("td", { class: "num" }, fmt(m.statements)),
    h("td", { class: "small" }, m.tables.join(", "))))),
  );
  parts.push(h("h3", {}, "모듈 (URL 많은 순)"), h("div", { class: "card table-wrap" }, modTable), moduleDetail);

  if (data.tables.length) {
    parts.push(h("h3", {}, "여러 모듈이 함께 쓰는 테이블", h("span", { class: "muted small" }, " — 바꾸면 영향이 큽니다")));
    parts.push(h("div", { class: "card table-wrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["테이블", "모듈 수", "SQL 수", "조회", "등록", "수정", "삭제", "저장", "쓰는 모듈"].map((c, i) =>
        h("th", { class: i && i < 8 ? "num" : null }, c)))),
      h("tbody", {}, data.tables.map((t) => h("tr", {},
        h("td", {}, h("b", { class: "mono" }, t.name)), h("td", { class: "num" }, t.modules.length), h("td", { class: "num" }, t.statements),
        ...["select", "insert", "update", "delete", "merge"].map((k) => h("td", { class: "num" }, t.kinds[k] || 0)),
        h("td", { class: "small muted" }, t.modules.slice(0, 6).join(", ") + (t.modules.length > 6 ? ` 외 ${t.modules.length - 6}` : ""))))),
    )));
  }
  root.replaceChildren(...parts);
};

async function showModule(name, target) {
  target.replaceChildren(loading());
  try {
    const data = await api(`/api/modules/${encodeURIComponent(name)}`);
    target.replaceChildren(
      h("h3", {}, `${name} 모듈: URL ${data.urls.length}개`, h("span", { class: "muted small" }, " — URL을 누르면 경로 추적")),
      h("div", { class: "card table-wrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["URL", "처리 메서드", "읽는 테이블", "쓰는 테이블"].map((c) => h("th", {}, c)))),
        h("tbody", {}, data.urls.map((r) => h("tr", { class: "click", onclick: () => traceUrl(r.url) },
          h("td", { class: "mono" }, r.url), h("td", { class: "mono small" }, r.handler),
          h("td", { class: "small" }, r.tables.filter((t) => !r.writes.includes(t)).join(", ") || "-"),
          h("td", { class: "small" }, r.writes.length ? h("span", { class: "chip orange" }, r.writes.join(", ")) : "-")))),
      )),
    );
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    target.replaceChildren(errorBox(err));
  }
}

// ── 인수인계 질문 ────────────────────────────────────────

const questionsState = { data: null, status: "open", kind: "", search: "", editing: new Set() };
const KIND_COLOR = {
  broken_sql: "red", dynamic_sql: "orange", magic_value: "purple", note: "orange", external_bean: "blue",
  empty_catch: "red", complex: "", copied_code: "blue", unused_sql: "",
};

async function loadQuestionCount() {
  try {
    const data = await api("/api/questions");
    questionsState.data = data;
    updateQuestionCount();
  } catch { /* 프로젝트 없음 */ }
}

function updateQuestionCount() {
  const d = questionsState.data;
  if (!d) return;
  const open = d.total - d.answered;
  $("#q-count").textContent = open ? String(open) : "";
}

views.questions = async function () {
  const list = $("#q-list");
  list.replaceChildren(loading("질문을 만드는 중…"));
  try {
    questionsState.data = await api("/api/questions");
  } catch (err) {
    list.replaceChildren(errorBox(err));
    return;
  }
  const kinds = $("#q-kind");
  const present = new Set(questionsState.data.items.map((q) => q.kind));
  kinds.replaceChildren(h("option", { value: "" }, "모든 종류"),
    ...Object.entries(questionsState.data.kinds).filter(([k]) => present.has(k)).map(([k, label]) =>
      h("option", { value: k, selected: k === questionsState.kind }, label)));
  renderQuestions();
};

function renderQuestions() {
  const d = questionsState.data;
  const pct = d.total ? Math.round((d.answered / d.total) * 100) : 0;
  $("#q-progress-text").textContent = `질문 ${fmt(d.total)}개 중 ${fmt(d.answered)}개 답변`;
  $("#q-progress-pct").textContent = `${pct}%`;
  $("#q-progress").style.width = `${pct}%`;
  updateQuestionCount();

  const s = questionsState;
  const needle = s.search.trim().toLowerCase();
  const items = d.items.filter((q) =>
    (s.status === "all" || q.status === s.status || (s.status === "open" && q.status !== "answered"))
    && (!s.kind || q.kind === s.kind)
    && (!needle || [q.question, q.target, q.file_path, q.why, q.answer].some((x) => (x || "").toLowerCase().includes(needle))));

  const list = $("#q-list");
  if (!items.length) {
    const msg = s.status === "open" && d.total && !needle && !s.kind
      ? h("div", { class: "card empty" }, h("b", {}, "모든 질문에 답했어요 🎉"), "\"답변함\"에서 받은 답을 다시 볼 수 있습니다.")
      : d.total === 0
        ? h("div", { class: "card empty" }, h("b", {}, "물어볼 만한 곳을 찾지 못했어요"),
          "드문 업무 코드 분기, TODO·주의 주석, 끊어진 쿼리, 복잡한 분기, 복사한 코드가 없습니다. 작고 깔끔한 코드이거나 아직 지원하지 않는 방식일 수 있어요.")
        : h("div", { class: "card empty" }, "조건에 맞는 질문이 없어요.");
    list.replaceChildren(msg);
    return;
  }
  list.replaceChildren(...items.map(questionCard));
}

function questionCard(q) {
  const card = h("article", { class: `card q${q.status === "answered" ? " answered" : ""}`, id: `q-${q.id}` });
  const editing = questionsState.editing.has(q.id);
  const head = h("div", { class: "head" },
    h("span", { class: "qid" }, `Q${q.id}`),
    h("span", { class: `chip ${KIND_COLOR[q.kind] || ""}` }, q.kind_label),
    q.status === "answered" ? h("span", { class: "chip green" }, "답변함") : null,
    h("span", { class: "grow" }),
    q.target ? h("span", { class: "mono small muted" }, q.target) : null,
    q.file_path ? locLink(`${q.file_path}:${q.line || 1}`) : null,
  );
  card.append(head, h("div", { class: "text" }, q.question), h("div", { class: "why" }, q.why));
  if (q.code) card.append(h("pre", { class: "line" }, q.code));

  if (q.answer && !editing) {
    card.append(h("div", { class: "answer" }, h("div", {}, q.answer),
      h("div", { class: "by" }, [q.answered_by, q.answered_at && q.answered_at.slice(0, 10)].filter(Boolean).join(" · "))));
  }
  const actions = h("div", { class: "actions" });
  if (editing || !q.answer) {
    const ta = h("textarea", { placeholder: "받은 답을 적어 주세요. 짧아도 괜찮습니다. (Ctrl+Enter로 저장)" });
    ta.value = q.answer || "";
    const by = h("input", { type: "text", placeholder: "답한 사람 (선택)" });
    by.value = q.answered_by || store("sasu-answered-by") || "";
    const save = h("button", { class: "btn primary sm" }, "답 저장");
    const doSave = async () => {
      save.disabled = true;
      try {
        const r = await api(`/api/questions/${q.id}/answer`, { json: { answer: ta.value, by: by.value } });
        if (by.value) store("sasu-answered-by", by.value);
        Object.assign(q, r);
        questionsState.data.answered = questionsState.data.items.filter((x) => x.status === "answered").length;
        questionsState.editing.delete(q.id);
        toast(r.status === "answered" ? `Q${q.id} 답을 저장했어요` : `Q${q.id} 답을 지웠어요`);
        if (questionsState.status === "open" && r.status === "answered") {
          card.style.transition = "opacity .3s"; card.style.opacity = "0";
          setTimeout(renderQuestions, 300);
        } else card.replaceWith(questionCard(q));
        updateProgressOnly();
      } catch (err) {
        toast(err.message);
        save.disabled = false;
      }
    };
    save.addEventListener("click", doSave);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) doSave(); });
    const editor = h("div", { class: "editor" }, ta, h("div", { class: "row" }, by, save,
      editing ? h("button", { class: "btn sm ghost", onclick: () => { questionsState.editing.delete(q.id); card.replaceWith(questionCard(q)); } }, "취소") : null));
    card.append(editor);
  } else {
    actions.append(h("button", { class: "btn sm", onclick: () => { questionsState.editing.add(q.id); card.replaceWith(questionCard(q)); } }, "답 고치기"));
  }
  actions.append(
    h("button", { class: "btn sm ghost", onclick: () => showDraft(q) }, "✉ 떠난 개발자에게 물을 메시지"),
    q.target ? h("button", { class: "btn sm ghost", onclick: () => askAbout(q) }, "물어보기에서 보기") : null,
  );
  card.append(actions);
  return card;
}

function updateProgressOnly() {
  const d = questionsState.data;
  const pct = d.total ? Math.round((d.answered / d.total) * 100) : 0;
  $("#q-progress-text").textContent = `질문 ${fmt(d.total)}개 중 ${fmt(d.answered)}개 답변`;
  $("#q-progress-pct").textContent = `${pct}%`;
  $("#q-progress").style.width = `${pct}%`;
  updateQuestionCount();
}

async function showDraft(q) {
  try {
    const { message } = await api(`/api/questions/${q.id}/draft`);
    modal(`Q${q.id} 질문 메시지`,
      h("p", { class: "muted small" }, "메신저나 메일로 보내세요. 답을 받으면 이 질문에 적어 두면 됩니다."),
      h("pre", {}, message),
      h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn primary", onclick: () => copy(message) }, "복사")));
  } catch (err) {
    toast(err.message);
  }
}

function askAbout(q) {
  $("#ask-q").value = q.question;
  $("#ask-target").value = q.target && /^[\w.]+\.\w+$/.test(q.target) ? q.target : (q.file_path ? `${q.file_path}:${q.line}` : "");
  show("ask");
  $("#ask-form").requestSubmit();
}

document.querySelectorAll("#q-status button").forEach((b) => b.addEventListener("click", () => {
  questionsState.status = b.dataset.status;
  document.querySelectorAll("#q-status button").forEach((x) => x.classList.toggle("on", x === b));
  if (questionsState.data) renderQuestions();
}));
$("#q-kind").addEventListener("change", (e) => { questionsState.kind = e.target.value; renderQuestions(); });
$("#q-search").addEventListener("input", (e) => { questionsState.search = e.target.value; renderQuestions(); });
$("#q-export").addEventListener("click", () => { window.location.href = "/api/questions/export"; });
$("#q-import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const r = await api("/api/questions/import", { method: "POST", body: form });
    toast(`답변 ${r.saved}개를 가져왔어요${r.missing ? ` (못 찾은 행 ${r.missing}개)` : ""}`);
    loaded.delete("questions");
    views.questions();
  } catch (err) {
    toast(err.message);
  }
  e.target.value = "";
});
$("#q-refresh").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/questions/refresh", { method: "POST" });
    toast(`질문 ${r.total}개 (새 질문 ${r.new}, 사라진 질문 ${r.stale})`);
    views.questions();
  } catch (err) {
    toast(err.message);
  }
  e.target.disabled = false;
});

// ── 물어보기 ─────────────────────────────────────────────

const LABEL_COLOR = { 사실: "green", 답변: "blue", 추정: "orange", 모름: "red", 질문: "purple" };
const LEVEL_COLOR = { "확인된 사실": "green", "인수인계 답변": "blue", 기록: "" };
let askRun = 0;

views.ask = async function () {
  const box = $("#ask-examples");
  try {
    const data = questionsState.data || await api("/api/questions");
    questionsState.data = data;
    const picks = data.items.filter((q) => q.target && /^[\w.]+\.\w+$/.test(q.target)).slice(0, 3);
    box.replaceChildren(...picks.map((q) => h("button", {
      type: "button", title: q.target,
      onclick: () => { $("#ask-q").value = q.question; $("#ask-target").value = q.target; $("#ask-form").requestSubmit(); },
    }, `${q.target.split(".").slice(-2).join(".")}: ${q.question.slice(0, 34)}${q.question.length > 34 ? "…" : ""}`)));
  } catch { box.replaceChildren(); }
  $("#ask-q").focus();
};

$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runAsk({ question: $("#ask-q").value.trim(), target: $("#ask-target").value.trim() || null });
});
$("#ask-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#ask-form").requestSubmit();
});

async function runAsk(req, candidates) {
  if (!req.question) return;
  const run = ++askRun;
  const body = { ...req, use_git: $("#ask-git").checked };
  const out = $("#ask-result");
  out.replaceChildren(loading("관련 코드와 근거를 모으는 중…"));
  $("#ask-go").disabled = true;
  let ev;
  try {
    ev = await api("/api/ask/evidence", { json: body });
  } catch (err) {
    out.replaceChildren(errorBox(err));
    $("#ask-go").disabled = false;
    return;
  }
  if (run !== askRun) return;
  if (!ev.target) {
    out.replaceChildren(h("div", { class: "card empty" }, h("b", {}, "관련 코드를 찾지 못했어요"),
      "대상 칸에 Class.method나 URL을 적어 보세요."));
    $("#ask-go").disabled = false;
    return;
  }
  const cands = candidates || ev.candidates;
  const answerBox = h("div", { class: "card answer-card" }, loading("근거로 답을 만드는 중… (보통 10~30초)"));
  const evidenceBox = h("div", { class: "evidence" }, ev.evidence.map(evidenceCard));
  out.replaceChildren(
    cands && cands.length ? h("div", { class: "candidates" }, h("span", { class: "muted small" }, "질문으로 찾은 코드:"),
      cands.map((c) => h("button", {
        class: c.method_id === ev.target.method_id ? "on" : null, title: `${c.path}:${c.line}`,
        onclick: () => runAsk({ question: req.question, method_id: c.method_id }, cands),
      }, c.label))) : null,
    h("div", { class: "row", style: "margin-top:10px" }, h("b", {}, "대상 "), h("span", { class: "mono" }, ev.target.label),
      locLink(`${ev.target.path}:${ev.target.line_start}-${ev.target.line_end}`, shortPath(`${ev.target.path}:${ev.target.line_start}`))),
    answerBox,
    h("h3", {}, `근거 ${ev.evidence.length}개`),
    evidenceBox,
  );

  try {
    const ans = await api("/api/ask/answer", { json: { ...body, method_id: ev.target.method_id } });
    if (run !== askRun) return;
    answerBox.replaceChildren(...renderAnswer(ans));
  } catch (err) {
    if (run === askRun) answerBox.replaceChildren(errorBox(err));
  }
  if (run === askRun) $("#ask-go").disabled = false;
}

function evidenceCard(e) {
  const isCode = e.title.endsWith(" 코드");
  return h("details", { class: "card ev", id: `ev-${e.id}`, open: !isCode },
    h("summary", {}, h("span", { class: "eid" }, e.id), h("span", { class: `chip ${LEVEL_COLOR[e.level] || ""}` }, e.level),
      h("span", { class: "grow" }, e.title), e.location ? locLink(e.location) : null),
    h("pre", {}, e.text));
}

function renderAnswer(ans) {
  if (ans.error) return [h("div", { class: "error-box" }, ans.error)];
  const meta = [ans.model, ans.cost_usd ? `$${ans.cost_usd.toFixed(4)}` : null].filter(Boolean).join(" · ");
  const parts = [h("div", { class: "row" }, h("b", { class: "grow" }, "답"), meta ? h("span", { class: "muted small" }, meta) : null)];
  for (const s of ans.sentences) {
    parts.push(h("div", { class: "sentence" },
      h("span", { class: `chip lab ${LABEL_COLOR[s.label] || ""}` }, s.label),
      h("div", {}, s.text, " ", ...s.citations.map((c) => h("span", { class: "cite", onclick: () => flashEvidence(c) }, `[${c}]`)),
        s.downgraded ? h("span", { class: "down" }, `↓ ${s.downgraded}`) : null)));
  }
  for (const issue of ans.issues || []) parts.push(h("div", { class: "muted small" }, `※ ${issue}`));
  parts.push(h("div", { class: "legend-levels" },
    h("span", {}, h("span", { class: "chip green" }, "사실"), " 코드·SQL로 확인"),
    h("span", {}, h("span", { class: "chip blue" }, "답변"), " 인수인계 때 받은 답"),
    h("span", {}, h("span", { class: "chip orange" }, "추정"), " 근거가 약함 — 떠난 개발자에게 확인하세요")));
  return parts;
}

function flashEvidence(id) {
  const el = document.getElementById(`ev-${id}`);
  if (!el) return;
  el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1500);
}

// ── 경로 추적 ────────────────────────────────────────────

let suggestTimer = null;
let suggestIndex = -1;
let suggestToken = 0;  // 추적을 시작하면 늦게 온 추천 목록은 버린다

views.trace = function () { $("#trace-q").focus(); };

$("#trace-q").addEventListener("input", (e) => {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(() => suggest(e.target.value), 180);
});
$("#trace-q").addEventListener("focus", (e) => { if (e.target.value && !$("#trace-result").childElementCount) suggest(e.target.value); });
$("#trace-q").addEventListener("keydown", (e) => {
  const items = [...document.querySelectorAll("#trace-suggest div[data-url]")];
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    suggestIndex = Math.max(0, Math.min(items.length - 1, suggestIndex + (e.key === "ArrowDown" ? 1 : -1)));
    items.forEach((el, i) => el.classList.toggle("on", i === suggestIndex));
  } else if (e.key === "Enter") {
    e.preventDefault();
    const pick = items[suggestIndex];
    traceUrl(pick ? pick.dataset.url : e.target.value);
  }
});
document.addEventListener("click", (e) => { if (!e.target.closest(".search-wrap")) $("#trace-suggest").classList.add("hidden"); });
$("#trace-full").addEventListener("change", () => { if ($("#trace-q").value) traceUrl($("#trace-q").value); });

async function suggest(q) {
  const box = $("#trace-suggest");
  const token = ++suggestToken;
  try {
    const data = await api(`/api/urls?q=${encodeURIComponent(q)}&limit=30`);
    if (token !== suggestToken) return;
    suggestIndex = -1;
    if (!data.urls.length) { box.classList.add("hidden"); return; }
    box.replaceChildren(...data.urls.map((u) => h("div", { "data-url": u, onclick: () => traceUrl(u) }, u)),
      data.total > data.urls.length ? h("div", { class: "muted" }, `… ${fmt(data.total - data.urls.length)}개 더 (더 입력해 좁히세요)`) : null);
    box.classList.remove("hidden");
  } catch { box.classList.add("hidden"); }
}

async function traceUrl(url) {
  if (!url) return;
  show("trace");
  suggestToken++;
  clearTimeout(suggestTimer);
  $("#trace-q").value = url;
  $("#trace-suggest").classList.add("hidden");
  const out = $("#trace-result");
  out.replaceChildren(loading("경로를 따라가는 중…"));
  try {
    const data = await api(`/api/trace?url=${encodeURIComponent(url)}&full=${$("#trace-full").checked}`);
    if (!data.url) {
      out.replaceChildren(h("div", { class: "card pad" },
        data.candidates.length ? [h("b", {}, `'${url}'와 비슷한 URL이 ${data.candidates.length}개 있어요. 하나를 고르세요.`),
          h("ul", {}, data.candidates.map((u) => h("li", {}, h("a", { href: "#trace", onclick: (e) => { e.preventDefault(); traceUrl(u); } }, u))))]
          : `'${url}'와 일치하는 URL이 없어요.`));
      return;
    }
    const tree = h("ul", { class: "tree" }, h("li", {}, h("span", { class: "node" }, h("span", { class: "chip blue" }, "URL"), h("span", { class: "name" }, data.url)),
      data.tree.children.length ? h("ul", {}, data.tree.children.map((c) => traceNode(c, 1))) : null));
    const summary = summarizeTrace(data.tree);
    out.replaceChildren(h("div", { class: "card pad" },
      h("div", { class: "row small muted", style: "margin-bottom:10px" }, summary), tree));
  } catch (err) {
    out.replaceChildren(errorBox(err));
  }
}

function summarizeTrace(tree) {
  const tables = new Set();
  let methods = 0;
  let sql = 0;
  const walk = (n) => {
    if (n.kind === "method") methods++;
    if (n.kind === "statement") { sql++; (n.detail || "").split(",").map((t) => t.trim()).filter(Boolean).forEach((t) => tables.add(t)); }
    n.children.forEach(walk);
  };
  walk(tree);
  return [`메서드 ${methods}개`, `SQL ${sql}개`, tables.size ? `테이블 ${[...tables].join(", ")}` : "테이블 없음"].join(" · ");
}

function traceNode(n, depth) {
  const cls = ["node", n.kind];
  if (n.kind === "method" && n.detail) cls.push(n.detail);
  if (n.repeated) cls.push("repeated");
  const parts = [];
  if (n.kind === "statement") parts.push(h("span", { class: "chip purple" }, "SQL"));
  if (n.kind === "view") parts.push(h("span", { class: "chip blue" }, "화면"));
  if (n.kind === "url") parts.push(h("span", { class: "chip" }, "→"));
  parts.push(h("span", { class: "name" }, n.label));
  if (n.kind === "method" && n.detail) parts.push(h("span", { class: "chip" }, n.detail));
  if (n.count > 1) parts.push(h("span", { class: "muted small" }, `×${n.count}`));
  if (n.location) parts.push(locLink(n.location));
  if (n.repeated) parts.push(h("span", { class: "muted small" }, "(위에서 펼침)"));
  if (n.kind === "statement") parts.push(h("span", { class: "tables" }, "테이블 ", h("b", { class: "mono" }, n.detail || "-")));
  if (n.kind === "view" && n.detail) parts.push(h("span", { class: "chip red" }, n.detail));
  if (n.kind === "url" && n.detail) parts.push(h("span", { class: "muted small" }, `(${n.detail})`));
  if (n.note) parts.push(h("span", { class: "note" }, n.note));

  const li = h("li", {});
  const kids = n.children.length ? h("ul", { class: depth >= 5 ? "hidden" : null }, n.children.map((c) => traceNode(c, depth + 1))) : null;
  if (kids) {
    const btn = h("button", { class: "toggle-kids", title: "펼치기/접기" }, depth >= 5 ? "▸" : "▾");
    btn.addEventListener("click", () => { kids.classList.toggle("hidden"); btn.textContent = kids.classList.contains("hidden") ? "▸" : "▾"; });
    parts.unshift(btn);
  }
  li.append(h("span", { class: cls.join(" ") }, ...parts));
  if (kids) li.append(kids);
  return li;
}

// ── 정리하기 ─────────────────────────────────────────────

views.cleanup = async function () {
  const unused = $("#cleanup-unused");
  const common = $("#cleanup-common");
  unused.replaceChildren(loading("부르는 곳이 없는 코드를 찾는 중…"));
  common.replaceChildren(loading());
  api("/api/unused").then((data) => {
    unused.replaceChildren(...data.sections.map((s) => h("details", { class: "card section", open: s.kind === "broken_sql" && s.items.length > 0 },
      h("summary", {}, h("span", { class: `chip ${s.items.length ? (s.kind === "broken_sql" ? "red" : "orange") : "green"}` }, `${fmt(s.items.length)}건`),
        h("span", {}, s.title), h("span", { class: "help" }, s.help)),
      h("div", { class: "body" }, s.items.length ? findingsTable(s.items) : h("div", { class: "muted small" }, "없음")))));
  }).catch((err) => unused.replaceChildren(errorBox(err)));

  api("/api/common").then((data) => {
    const parts = [];
    if (data.shared.length) {
      parts.push(h("details", { class: "card section", open: true },
        h("summary", {}, h("span", { class: "chip blue" }, `${data.shared.length}개`), "여러 모듈이 함께 쓰는 공통 코드",
          h("span", { class: "help" }, "고치면 여러 기능에 영향이 갑니다")),
        h("div", { class: "body table-wrap" }, h("table", { class: "grid" },
          h("thead", {}, h("tr", {}, ["클래스", "모듈", "종류", "쓰는 모듈 수", "호출 수"].map((c, i) => h("th", { class: i >= 3 ? "num" : null }, c)))),
          h("tbody", {}, data.shared.map((s) => h("tr", {}, h("td", { class: "mono" }, s.class_name), h("td", {}, s.module),
            h("td", {}, s.stereotype || "유틸"), h("td", { class: "num" }, s.used_by_modules.length), h("td", { class: "num" }, fmt(s.call_sites)))))))));
    }
    parts.push(h("details", { class: "card section", open: data.clones.length > 0 },
      h("summary", {}, h("span", { class: `chip ${data.clone_stats.groups ? "orange" : "green"}` }, `${fmt(data.clone_stats.groups)}묶음`),
        "복사해 붙인 메서드", h("span", { class: "help" }, `변수 이름만 다르고 구조가 같은 코드 ${fmt(data.clone_stats.methods)}개. 한 곳을 고치면 나머지도 확인하세요`)),
      h("div", { class: "body" }, data.clones.length ? data.clones.map((g, i) => h("div", { class: "clone" },
        h("div", { class: "row" }, h("b", {}, `${i + 1}. ${g.methods.length}곳`), h("span", { class: "muted small" }, `토큰 ${g.tokens}개`),
          g.note ? h("span", { class: "chip orange" }, g.note) : null),
        h("div", { class: "small" }, g.methods.slice(0, 6).map(([name, loc]) => h("div", {}, h("span", { class: "mono" }, name), " ", locLink(loc))),
          g.methods.length > 6 ? h("div", { class: "muted" }, `… 외 ${g.methods.length - 6}곳`) : null))) : h("div", { class: "muted small" }, "없음"))));
    parts.push(h("details", { class: "card section", open: false },
      h("summary", {}, h("span", { class: `chip ${data.duplicate_sql.length ? "orange" : "green"}` }, `${data.duplicate_sql.length}묶음`),
        "같은 SQL이 다른 ID로", h("span", { class: "help" }, "한쪽만 고치면 결과가 달라집니다")),
      h("div", { class: "body" }, data.duplicate_sql.length ? data.duplicate_sql.map((d) => h("div", { class: "clone" },
        h("div", { class: "small" }, d.statements.map(([id, loc]) => h("div", {}, h("span", { class: "mono" }, id), " ", locLink(loc)))),
        h("pre", { class: "mono small muted", style: "white-space:pre-wrap" }, d.sql_preview))) : h("div", { class: "muted small" }, "없음"))));
    common.replaceChildren(...parts);
  }).catch((err) => common.replaceChildren(errorBox(err)));
};

function findingsTable(items) {
  const LIMIT = 50;
  const tbody = h("tbody");
  const render = (n) => tbody.replaceChildren(...items.slice(0, n).map((f) => h("tr", {},
    h("td", { class: "mono small" }, f.name), h("td", {}, locLink(f.location)),
    h("td", {}, h("span", { class: `chip ${f.confidence === "높음" ? "red" : ""}` }, f.confidence)),
    h("td", { class: "small" }, f.reason, f.evidence ? h("div", { class: "muted" }, f.evidence) : null))));
  render(LIMIT);
  const more = items.length > LIMIT
    ? h("button", { class: "btn sm", style: "margin-top:8px", onclick: (e) => { render(items.length); e.target.remove(); } }, `${fmt(items.length - LIMIT)}건 더 보기`)
    : null;
  return h("div", { class: "table-wrap" }, h("table", { class: "grid findings" },
    h("colgroup", {}, h("col", { class: "name" }), h("col", { class: "where" }), h("col", { class: "conf" }), h("col", {})),
    h("thead", {}, h("tr", {}, ["이름", "위치", "확신", "설명"].map((c) => h("th", {}, c)))), tbody), more);
}

// ── 시작 ────────────────────────────────────────────────

loadProjects();
loadQuestionCount();
show(location.hash.slice(1) || "overview");
