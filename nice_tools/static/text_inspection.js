"use strict";

(() => {
  const get = (id) => document.getElementById(`text-inspection-${id}`);
  const kinds = {
    space: "특수 공백", unusualspace: "특수 공백", zero_width: "폭 없는 문자", control: "제어 문자",
    bidi: "글자 방향 제어", format: "서식 제어", filler: "채움 문자", variation: "모양 선택자",
    replacement: "대체 문자", noncharacter: "비문자 코드", common: "일반 공백·줄바꿈", strict: "엄격 모드 허용 외 문자",
  };
  const explanations = {
    space: "보통 스페이스와 다른 공백입니다.", unusualspace: "보통 스페이스와 다른 공백입니다.",
    zero_width: "폭 없이 글자를 연결하거나 분리할 수 있습니다.", control: "표시나 장치 동작을 제어하는 문자입니다.",
    bidi: "글자를 표시하는 방향을 바꿀 수 있습니다.", format: "눈에 보이지 않는 서식 제어 문자입니다.",
    filler: "빈 글자처럼 표시되는 채움 문자입니다.", variation: "앞 글자의 모양이나 이모지 표현을 선택합니다.",
    replacement: "앞선 변환에서 읽지 못한 글자를 대신했을 수 있습니다.", noncharacter: "일반 텍스트 교환에 쓰지 않는 코드입니다.",
    common: "일반 스페이스, 탭 또는 줄바꿈입니다.", strict: "엄격 모드의 허용 범위 밖입니다. 문맥에 맞는 사용인지 확인하세요.",
  };
  let mode = "pending", initialized = false, closed = false, busy = false, browserPicker = false;
  let session = null, selection = null, activeJob = null, completedJob = null;
  let lifetime = 0, jobRequest = 0, pageRequest = 0, pollTimer = null, currentPage = 0, totalPages = 1;

  async function api(endpoint, data, extra = {}) {
    const multipart = data instanceof FormData;
    let response;
    try {
      response = await fetch(`/api/text-inspection/${endpoint}`, {
        method: data === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
        headers: { "X-Nice-Tools": "text-inspection", ...(data === undefined || multipart ? {} : { "Content-Type": "application/json" }) },
        ...(data === undefined ? {} : { body: multipart ? data : JSON.stringify(data) }), ...extra,
      });
    } catch (error) { error.uncertain = true; throw error; }
    const result = await response.json().catch(() => null);
    if (!response.ok || !result) {
      const error = new Error(typeof result?.detail === "string" ? result.detail : "앱의 응답을 읽지 못했습니다. 다시 시도하세요.");
      error.sessionExpired = result?.code === "session_expired";
      error.uncertain = response.status >= 500 || !result;
      error.knownFailure = response.status >= 400 && response.status < 500 && Boolean(result);
      throw error;
    }
    return result;
  }
  const message = (error) => error instanceof TypeError ? "앱에 연결할 수 없습니다. 실행창을 확인하고 다시 시도하세요." : error.message;
  function feedback(text = "", error = false) {
    get("feedback").textContent = text;
    get("feedback").dataset.tone = error ? "error" : "info";
    get("feedback").hidden = !text;
  }
  function closeSession(token) {
    if (token) api("close", { session: token }, { keepalive: true }).catch(() => {});
  }
  function clearResults() {
    pageRequest += 1;
    completedJob = null;
    get("results").hidden = true;
    get("rows").replaceChildren();
    get("summary").replaceChildren();
    get("kinds").replaceChildren();
    get("page-retry").hidden = true;
    get("results").setAttribute("aria-busy", "false");
  }
  function update() {
    const ready = ["native", "upload"].includes(mode) && !closed;
    get("pick").disabled = busy || !ready;
    get("file").disabled = mode !== "upload" || closed || (busy && !browserPicker);
    get("auto").disabled = busy || closed;
    get("encoding").disabled = busy || closed || get("auto").checked;
    get("strict").disabled = busy || closed;
    get("common").disabled = busy || closed;
    get("scan").disabled = busy || !ready || !selection;
    get("scan").textContent = busy ? "처리 중…" : "검사 시작";
    get("name").textContent = selection?.name || "파일을 선택하세요";
    get("detail").textContent = selection ? `${selection.path || selection.name} · ${Number(selection.size || 0).toLocaleString("ko-KR")} bytes` : "내 PC의 텍스트 파일 한 개 선택";
    get("pick").classList.toggle("selected", Boolean(selection));
    get("form").setAttribute("aria-busy", String(busy));
  }
  function setBusy(value) { busy = value; update(); }
  function retire() {
    lifetime += 1;
    jobRequest += 1;
    clearTimeout(pollTimer);
    const old = session;
    session = selection = activeJob = null;
    browserPicker = false;
    get("file").value = "";
    clearResults();
    get("progress").hidden = true;
    get("cancel").hidden = true;
    get("job-retry").hidden = true;
    setBusy(false);
    closeSession(old);
  }
  function handleError(error) {
    if (error.sessionExpired) retire();
    feedback(message(error), true);
  }
  async function initialize() {
    if (closed || busy) return;
    initialized = true;
    mode = "pending";
    const generation = lifetime;
    get("runtime-retry").hidden = true;
    get("runtime").textContent = "파일 선택 기능을 확인하고 있습니다…";
    update();
    try {
      const result = await api("status");
      if (closed || generation !== lifetime) return;
      if (typeof result.native_available !== "boolean") throw new Error("파일 선택 기능을 확인하지 못했습니다.");
      mode = result.native_available ? "native" : "upload";
      get("runtime").textContent = result.native_available ? "Windows 파일 창으로 선택합니다. 파일 내용을 전송하지 않고 내 PC에서 읽습니다." : "이 개발 환경에서는 선택한 파일을 이 PC의 로컬 앱에 전달합니다.";
      get("runtime").dataset.tone = "info";
    } catch (error) {
      if (closed || generation !== lifetime) return;
      mode = "error";
      get("runtime").textContent = message(error);
      get("runtime").dataset.tone = "error";
      get("runtime-retry").hidden = false;
    }
    update();
  }
  async function ensureSession(generation) {
    if (session) return session;
    const result = await api("session", {});
    if (closed || generation !== lifetime) { closeSession(result.session); return null; }
    if (typeof result.session !== "string" || !result.session) throw new Error("파일 선택 세션을 만들지 못했습니다.");
    session = result.session;
    return session;
  }
  function acceptSelection(result) {
    if (result.cancelled) { feedback("파일 선택을 취소했습니다. 기존 선택을 유지합니다."); return; }
    if (typeof result.selection?.token !== "string") {
      const error = new Error("선택한 파일 정보를 읽지 못했습니다. 파일을 다시 선택하세요.");
      error.uncertain = true;
      throw error;
    }
    clearResults();
    get("progress").hidden = true;
    selection = result.selection;
    feedback("파일을 선택했습니다. 인코딩을 확인한 뒤 검사하세요.");
  }
  async function pick() {
    if (busy || closed) return;
    if (mode === "upload") {
      browserPicker = true;
      setBusy(true);
      get("file").value = "";
      get("file").click();
      return;
    }
    if (mode !== "native") return;
    const generation = lifetime;
    setBusy(true);
    feedback("Windows 파일 창에서 텍스트 파일을 선택하세요.");
    try {
      const token = await ensureSession(generation);
      if (!token) return;
      const result = await api("pick-file", { session: token });
      if (closed || generation !== lifetime || session !== token) { closeSession(token); return; }
      acceptSelection(result);
    } catch (error) {
      if (closed || generation !== lifetime) return;
      if (error.uncertain) retire();
      handleError(error);
    } finally { if (!closed && generation === lifetime) setBusy(false); }
  }
  async function upload() {
    if (closed || mode !== "upload" || !browserPicker) return;
    const file = get("file").files[0];
    browserPicker = false;
    if (!file) { setBusy(false); return; }
    const generation = lifetime;
    setBusy(true);
    feedback("선택한 파일을 로컬 앱에 전달하고 있습니다…");
    try {
      const token = await ensureSession(generation);
      if (!token) return;
      const form = new FormData();
      form.append("file", file);
      form.append("session", token);
      const result = await api("upload", form);
      if (closed || generation !== lifetime || session !== token) { closeSession(token); return; }
      acceptSelection(result);
    } catch (error) {
      if (closed || generation !== lifetime) return;
      if (error.uncertain) retire();
      handleError(error);
    } finally { if (!closed && generation === lifetime) { get("file").value = ""; setBusy(false); } }
  }
  function addMetric(label, value) {
    const pair = document.createElement("div"), term = document.createElement("dt"), detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = typeof value === "number" ? value.toLocaleString("ko-KR") : String(value ?? "없음");
    pair.append(term, detail);
    get("summary").append(pair);
  }
  function renderSummary(summary) {
    if (!summary || typeof summary.findings !== "number") throw new Error("검사 요약을 읽지 못했습니다. 결과를 다시 불러오세요.");
    get("summary").replaceChildren();
    get("kinds").replaceChildren();
    for (const [label, value] of [["확인 대상 문자", summary.findings], ["인코딩", summary.encoding], ["선두 BOM", summary.bom], ["파일 크기 (bytes)", summary.file_size], ["문자 수", summary.characters], ["줄 수", summary.lines]]) addMetric(label, value);
    const common = summary.common_whitespace || {};
    get("whitespace").textContent = `일반 공백 집계 · 스페이스 ${common.space ?? 0} · 탭 ${common.tab ?? 0} · CRLF ${common.crlf ?? 0} · LF ${common.lf ?? 0} · CR ${common.cr ?? 0}`;
    for (const [kind, count] of Object.entries(summary.kinds || {})) {
      const item = document.createElement("span");
      item.textContent = `${kinds[kind] || kind} ${Number(count).toLocaleString("ko-KR")}`;
      get("kinds").append(item);
    }
  }
  // Escape directional controls inside excerpts as visible labels so they cannot reorder the marker.
  function safeContext(value) {
    return String(value ?? "").replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/gu, (char) => {
      if (char === "\n") return "[LF]";
      if (char === "\r") return "[CR]";
      if (char === "\t") return "[TAB]";
      return `[U+${char.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")}]`;
    });
  }
  function renderRows(results) {
    const fragment = document.createDocumentFragment();
    for (const row of results) {
      const tr = document.createElement("tr");
      for (const value of [row.line, row.column, row.codepoint, row.name]) {
        const td = document.createElement("td");
        td.textContent = safeContext(value);
        tr.append(td);
      }
      const kind = document.createElement("td"), explanation = document.createElement("small");
      kind.append(document.createTextNode(kinds[row.kind] || "확인 대상"));
      explanation.textContent = explanations[row.kind] || "사용 목적을 확인하세요.";
      kind.append(explanation);
      const context = document.createElement("td");
      context.className = "text-inspection-context";
      const before = document.createElement("bdi"), marker = document.createElement("mark"), after = document.createElement("bdi");
      before.textContent = safeContext(row.before);
      marker.textContent = safeContext(row.display || `[${row.label || row.codepoint}]`);
      marker.title = safeContext(`${row.codepoint} ${row.name}`);
      after.textContent = safeContext(row.after);
      context.append(before, marker, after);
      tr.append(kind, context);
      fragment.append(tr);
    }
    get("rows").replaceChildren(fragment);
  }
  async function loadPage(number = 0) {
    if (!completedJob || closed) return;
    const token = session, job = completedJob, generation = lifetime, request = ++pageRequest;
    currentPage = number;
    get("results").hidden = false;
    get("results").setAttribute("aria-busy", "true");
    get("page-retry").hidden = true;
    get("prev").disabled = get("next").disabled = true;
    get("rows").replaceChildren();
    get("empty").hidden = false;
    get("empty").textContent = "결과를 불러오고 있습니다…";
    try {
      const result = await api("page", { session: token, job, page: number });
      if (closed || generation !== lifetime || completedJob !== job || request !== pageRequest) return;
      if (!Array.isArray(result.results) || result.page !== number || !Number.isInteger(result.pages) || result.pages < 1) throw new Error("검사 결과를 읽지 못했습니다. 다시 불러오세요.");
      renderSummary(result.summary);
      renderRows(result.results);
      totalPages = result.pages;
      get("page-label").textContent = `${number + 1} / ${totalPages} 페이지 · 총 ${Number(result.total).toLocaleString("ko-KR")}개 · 페이지당 200개`;
      get("empty").hidden = result.results.length !== 0;
      get("empty").textContent = "현재 옵션에서 확인 대상 문자가 없습니다.";
      get("prev").disabled = number === 0;
      get("next").disabled = number + 1 >= totalPages;
    } catch (error) {
      if (closed || generation !== lifetime || completedJob !== job || request !== pageRequest) return;
      handleError(error);
      if (!error.sessionExpired) {
        get("empty").textContent = "결과를 불러오지 못했습니다. 다시 시도하세요.";
        get("page-retry").hidden = false;
      }
    } finally {
      if (generation === lifetime && request === pageRequest) get("results").setAttribute("aria-busy", "false");
    }
  }
  function applyStatus(result, job) {
    if (result.job !== job || !["queued", "running", "succeeded", "failed", "cancelled"].includes(result.state)) throw new Error("작업 상태를 읽지 못했습니다. 상태를 다시 확인하세요.");
    get("job-status").textContent = `${result.state === "queued" ? "검사 대기" : result.phase === "done" ? "검사 완료" : "파일 검사"} · ${Number(result.completed || 0).toLocaleString("ko-KR")} / ${Number(result.total || 0).toLocaleString("ko-KR")} bytes · ${Number(result.elapsed_seconds || 0).toFixed(1)}초`;
    if (result.total > 0) { get("bar").max = result.total; get("bar").value = Math.min(result.completed || 0, result.total); }
    else get("bar").removeAttribute("value");
    if (["queued", "running"].includes(result.state)) return false;
    activeJob = null;
    jobRequest += 1;
    clearTimeout(pollTimer);
    get("cancel").hidden = true;
    get("job-retry").hidden = true;
    setBusy(false);
    if (result.state === "succeeded") {
      completedJob = job;
      feedback("검사가 완료되었습니다. 확인 대상 문자의 용도를 문맥과 함께 확인하세요.");
      loadPage(0);
    } else {
      feedback(result.state === "cancelled" ? "검사를 취소했습니다." : result.message || "검사에 실패했습니다. 파일과 인코딩을 확인하고 다시 시도하세요.", result.state === "failed");
      get("job-status").textContent += result.state === "cancelled" ? " · 취소됨" : " · 실패";
    }
    return true;
  }
  async function requestJob(cancel = false) {
    if (!activeJob || closed) return;
    clearTimeout(pollTimer);
    const token = session, job = activeJob, generation = lifetime, request = ++jobRequest;
    get("job-retry").hidden = true;
    get("cancel").disabled = cancel;
    try {
      const result = await api(cancel ? "cancel" : "job", { session: token, job });
      if (closed || generation !== lifetime || activeJob !== job || request !== jobRequest) return;
      if (!applyStatus(result, job)) pollTimer = setTimeout(() => requestJob(), 400);
    } catch (error) {
      if (closed || generation !== lifetime || activeJob !== job || request !== jobRequest) return;
      handleError(error);
      if (!error.sessionExpired) get("job-retry").hidden = false;
    } finally { if (generation === lifetime && request === jobRequest) get("cancel").disabled = false; }
  }
  async function start(event) {
    event.preventDefault();
    if (busy || closed || !session || !selection) return;
    const token = session, file = selection.token, generation = lifetime;
    const encoding = get("auto").checked ? "auto" : get("encoding").value.trim();
    if (!encoding || encoding.length > 128 || /[\u0000-\u001f\u007f]/u.test(encoding)) {
      feedback("인코딩 이름을 확인하세요. 자동 감지를 사용하거나 올바른 인코딩 이름을 입력하세요.", true);
      get("encoding").focus();
      return;
    }
    const options = { encoding, include_common_whitespace: get("common").checked, strict_mode: get("strict").checked };
    clearResults();
    feedback();
    setBusy(true);
    get("progress").hidden = false;
    get("job-status").textContent = "검사를 준비하고 있습니다…";
    get("bar").removeAttribute("value");
    get("cancel").hidden = true;
    get("job-retry").hidden = true;
    try {
      const result = await api("start", { session: token, file, options });
      if (closed || generation !== lifetime || session !== token) {
        if (result.job) api("cancel", { session: token, job: result.job }, { keepalive: true }).catch(() => {});
        closeSession(token);
        return;
      }
      if (typeof result.job !== "string") { const error = new Error("검사 작업을 시작하지 못했습니다."); error.uncertain = true; throw error; }
      activeJob = result.job;
      get("cancel").hidden = false;
      requestJob();
    } catch (error) {
      if (closed || generation !== lifetime) return;
      if (error.knownFailure && !error.sessionExpired) {
        get("progress").hidden = true;
        setBusy(false);
        feedback(message(error), true);
      } else {
        retire();
        feedback(`${message(error)} 파일을 다시 선택하세요.`, true);
      }
    }
  }
  get("pick").addEventListener("click", pick);
  get("file").addEventListener("change", upload);
  get("file").addEventListener("cancel", () => { if (browserPicker) { browserPicker = false; setBusy(false); feedback("파일 선택을 취소했습니다. 기존 선택을 유지합니다."); } });
  get("form").addEventListener("submit", start);
  function optionsChanged() { clearResults(); get("progress").hidden = true; feedback(); update(); }
  for (const id of ["auto", "common", "strict"]) get(id).addEventListener("change", optionsChanged);
  get("encoding").addEventListener("input", optionsChanged);
  get("runtime-retry").addEventListener("click", initialize);
  get("cancel").addEventListener("click", () => requestJob(true));
  get("job-retry").addEventListener("click", () => requestJob());
  get("page-retry").addEventListener("click", () => loadPage(currentPage));
  get("prev").addEventListener("click", () => { if (currentPage > 0) loadPage(currentPage - 1); });
  get("next").addEventListener("click", () => { if (currentPage + 1 < totalPages) loadPage(currentPage + 1); });
  function activate() { if (!initialized && location.hash === "#text-inspection-tool") initialize(); }
  window.addEventListener("hashchange", activate);
  window.addEventListener("pagehide", () => { closed = true; retire(); });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    closed = false;
    initialized = false;
    mode = "pending";
    activate();
    update();
  });
  update();
  activate();
})();
