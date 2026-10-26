"use strict";

const $ = (id) => document.getElementById(id);
const fileInputs = { left: $("left-folder"), right: $("right-folder") };
const optionInputs = [$("sort-lines"), $("ignore-whitespace"), $("ignore-start"), $("ignore-end")];
const maxFiles = 1000;
const labels = { same: "동일", different: "차이", left_only: "왼쪽만", right_only: "오른쪽만", error: "읽기 오류" };
const descriptions = { same: "옵션 적용 후 내용이 같습니다", different: "옵션 적용 후 내용이 다릅니다", left_only: "왼쪽 폴더에만 있습니다", right_only: "오른쪽 폴더에만 있습니다" };
let results = [];
let busy = false;
let selectedPath = null;
let diffController = null;
let diffData = null;
let diffPage = 0;
let comparisonMode = "pending";
let comparisonSession = null;
const nativeSelections = { left: null, right: null };
let activeJob = null;
let completedJob = null;
let lifetime = 0;
let jobRequest = 0;
let pollTimer = null;
let pageClosed = false;
let diffRequestedPage = 0;

async function comparisonAPI(endpoint, data, extra = {}) {
  const response = await fetch(`/api/comparison/${endpoint}`, {
    method: data === undefined ? "GET" : "POST",
    credentials: "same-origin", cache: "no-store",
    headers: { "X-Nice-Tools": "comparison", ...(data === undefined ? {} : { "Content-Type": "application/json" }) },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }), ...extra,
  });
  const result = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(typeof result?.detail === "string" ? result.detail : "요청을 처리하지 못했습니다. 다시 시도하세요.");
    error.code = typeof result?.code === "string" && /^[a-z_]{1,40}$/.test(result.code) ? result.code : null;
    error.sessionExpired = response.status === 404 || response.status === 410 || result?.code === "session_expired";
    throw error;
  }
  if (!result) throw new Error("앱의 응답을 읽지 못했습니다. 다시 시도하세요.");
  return result;
}

function connectionMessage(error) {
  return error instanceof TypeError ? "앱에 연결할 수 없습니다. 실행창을 확인한 뒤 다시 시도하세요." : error.message;
}

function closeComparisonSession(session) {
  if (session) comparisonAPI("close", { session }, { keepalive: true }).catch(() => {});
}

function retireComparisonSession() {
  lifetime += 1;
  jobRequest += 1;
  clearTimeout(pollTimer);
  pollTimer = null;
  const previous = comparisonSession;
  comparisonSession = null;
  nativeSelections.left = nativeSelections.right = null;
  activeJob = completedJob = null;
  clearResults();
  $("comparison-progress").hidden = true;
  $("comparison-cancel").hidden = true;
  setBusy(false);
  closeComparisonSession(previous);
}

function handleNativeError(error) {
  if (error.sessionExpired) retireComparisonSession();
  feedback(connectionMessage(error), "error");
}

async function initializeComparison() {
  if (pageClosed || busy) return;
  comparisonMode = "pending";
  $("comparison-runtime-retry").hidden = true;
  $("comparison-runtime").hidden = false;
  $("comparison-runtime").textContent = "폴더 선택 기능을 확인하고 있습니다…";
  updateSelection();
  const generation = lifetime;
  try {
    const data = await comparisonAPI("status");
    if (pageClosed || generation !== lifetime) return;
    if (typeof data.native_available !== "boolean") throw new Error("폴더 선택 기능을 확인하지 못했습니다.");
    comparisonMode = data.native_available ? "native" : "upload";
    $("comparison-runtime").textContent = data.native_available ? "Windows 폴더 창에서 선택한 파일을 전송 없이 내 PC에서 비교합니다." : "이 개발 환경에서는 브라우저 폴더 선택으로 파일을 로컬 앱에 전달합니다.";
    $("comparison-runtime").dataset.tone = "info";
  } catch (error) {
    if (pageClosed || generation !== lifetime) return;
    comparisonMode = "error";
    $("comparison-runtime").textContent = `${connectionMessage(error)} 폴더 선택 기능을 다시 확인하세요.`;
    $("comparison-runtime").dataset.tone = "error";
    $("comparison-runtime-retry").hidden = false;
  }
  updateSelection();
}

async function pickDirectory(side) {
  if (busy || pageClosed) return;
  if (comparisonMode === "upload") { fileInputs[side].click(); return; }
  if (comparisonMode !== "native") return;
  const generation = lifetime;
  setBusy(true);
  feedback("Windows 폴더 창에서 폴더를 선택하세요.");
  try {
    if (!comparisonSession) {
      const data = await comparisonAPI("session", {});
      if (pageClosed || generation !== lifetime) { closeComparisonSession(data.session); return; }
      if (typeof data.session !== "string") throw new Error("폴더 선택 세션을 만들지 못했습니다.");
      comparisonSession = data.session;
    }
    const session = comparisonSession;
    const data = await comparisonAPI("pick-directory", { session, side });
    if (pageClosed || generation !== lifetime || comparisonSession !== session) { closeComparisonSession(session); return; }
    if (!data.cancelled) {
      if (!data.selection || typeof data.selection.token !== "string") throw new Error("선택한 폴더 정보를 읽지 못했습니다. 다시 선택하세요.");
      clearResults();
      completedJob = null;
      nativeSelections[side] = data.selection;
    }
    feedback(data.cancelled ? "폴더 선택을 취소했습니다. 기존 선택을 유지합니다." : "폴더를 선택했습니다.");
  } catch (error) {
    if (!pageClosed && generation === lifetime) {
      // A disconnected picker can still be open; close its session before retrying.
      if (error instanceof TypeError) retireComparisonSession();
      handleNativeError(error);
    }
  } finally {
    if (!pageClosed && generation === lifetime) setBusy(false);
  }
}

function nativeOptions() {
  const options = { sort_lines: $("sort-lines").checked, ignore_whitespace: $("ignore-whitespace").checked };
  for (const [key, id] of [["ignore_start", "ignore-start"], ["ignore_end", "ignore-end"]]) {
    if (!/^\d+$/.test($(id).value) || !Number.isSafeInteger(Number($(id).value))) throw new Error("무시할 글자 수는 0 이상의 정수여야 합니다.");
    options[key] = Number($(id).value);
  }
  return options;
}

function showResults(data) {
  if (!Array.isArray(data.results) || !data.summary) throw new Error("비교 결과를 읽을 수 없습니다. 다시 시도해 주세요.");
  results = data.results;
  for (const element of document.querySelectorAll("[data-stat]")) element.textContent = String(data.summary[element.dataset.stat] ?? 0);
  $("empty-state").hidden = true;
  $("result-content").hidden = false;
  $("status-filter").disabled = false;
  renderRows();
  feedback(`${data.summary.total.toLocaleString("ko-KR")}개 파일의 비교가 완료되었습니다.`, data.summary.error ? "error" : "info");
}

function applyJobStatus(data, job) {
  if (data.job !== job) throw new Error("작업 상태가 일치하지 않습니다. 상태를 다시 확인하세요.");
  const phase = data.phase === "scanning" ? "폴더 확인" : data.phase === "preparing-reports" ? "상세 내용 준비" : data.phase === "done" ? "완료" : "파일 비교";
  const elapsed = Number(data.elapsed_seconds || 0).toFixed(1);
  $("comparison-job-status").textContent = `${phase} · ${data.completed ?? 0} / ${data.total ?? 0}개 · ${elapsed}초${data.path ? ` · ${data.path}` : ""}`;
  const progress = $("comparison-progress-bar");
  if (data.total > 0) { progress.max = data.total; progress.value = Math.min(data.completed || 0, data.total); }
  else progress.removeAttribute("value");
  if (["queued", "running"].includes(data.state)) { feedback(); return false; }
  if (!["succeeded", "failed", "cancelled"].includes(data.state)) throw new Error("작업 상태를 읽지 못했습니다. 상태를 다시 확인하세요.");
  if (data.state === "succeeded") {
    showResults(data);
    completedJob = job;
  } else {
    feedback(data.state === "cancelled" ? "비교를 취소했습니다." : (data.message || "비교에 실패했습니다. 폴더와 옵션을 확인하고 다시 시도하세요."), data.state === "failed" ? "error" : "info");
    $("comparison-job-status").textContent += data.state === "cancelled" ? " · 취소됨" : " · 실패";
  }
  activeJob = null;
  jobRequest += 1;
  clearTimeout(pollTimer);
  $("comparison-cancel").hidden = true;
  $("comparison-job-retry").hidden = true;
  setBusy(false);
  return true;
}

async function pollComparisonJob() {
  if (!activeJob || pageClosed) return;
  clearTimeout(pollTimer);
  const job = activeJob, session = comparisonSession, generation = lifetime, request = ++jobRequest;
  $("comparison-job-retry").hidden = true;
  try {
    const data = await comparisonAPI("job", { session, job });
    if (pageClosed || generation !== lifetime || activeJob !== job || request !== jobRequest) return;
    if (!applyJobStatus(data, job)) pollTimer = setTimeout(pollComparisonJob, 400);
  } catch (error) {
    if (pageClosed || generation !== lifetime || activeJob !== job || request !== jobRequest) return;
    handleNativeError(error);
    if (!error.sessionExpired) $("comparison-job-retry").hidden = false;
  }
}

async function startNativeComparison() {
  if (!comparisonSession || !nativeSelections.left || !nativeSelections.right) return;
  let options;
  try { options = nativeOptions(); } catch (error) { feedback(error.message, "error"); return; }
  const session = comparisonSession, generation = lifetime;
  clearResults();
  completedJob = null;
  setBusy(true);
  $("comparison-progress").hidden = false;
  $("comparison-job-status").textContent = "비교를 준비하고 있습니다…";
  $("comparison-progress-bar").removeAttribute("value");
  $("comparison-cancel").hidden = true;
  $("comparison-job-retry").hidden = true;
  try {
    const data = await comparisonAPI("start", { session, left: nativeSelections.left.token, right: nativeSelections.right.token, options });
    if (pageClosed || generation !== lifetime || session !== comparisonSession) {
      if (data.job) comparisonAPI("cancel", { session, job: data.job }, { keepalive: true }).catch(() => {});
      closeComparisonSession(session);
      return;
    }
    if (typeof data.job !== "string") throw new Error("비교 작업을 시작하지 못했습니다.");
    activeJob = data.job;
    $("comparison-cancel").hidden = false;
    $("comparison-cancel").disabled = false;
    pollComparisonJob();
  } catch (error) {
    if (pageClosed || generation !== lifetime) return;
    // A lost start response may still have created a server job. Closing owns its cleanup.
    retireComparisonSession();
    feedback(`${connectionMessage(error)} 폴더를 다시 선택하세요.`, "error");
  }
}

async function startUploadComparison() {
  const generation = lifetime;
  clearResults();
  completedJob = null;
  let form;
  try { form = buildForm(); } catch (error) { feedback(error.message, "error"); return; }
  setBusy(true);
  $("comparison-progress").hidden = false;
  $("comparison-job-status").textContent = "파일을 보내고 비교를 준비하고 있습니다…";
  $("comparison-progress-bar").removeAttribute("value");
  $("comparison-cancel").hidden = true;
  $("comparison-job-retry").hidden = true;
  try {
    if (!comparisonSession) {
      const data = await comparisonAPI("session", {});
      if (pageClosed || generation !== lifetime) { closeComparisonSession(data.session); return; }
      if (typeof data.session !== "string") throw new Error("비교 세션을 만들지 못했습니다.");
      comparisonSession = data.session;
    }
    const session = comparisonSession;
    const response = await fetch(`/api/comparison/upload?session=${encodeURIComponent(session)}`, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "X-Nice-Tools": "comparison" }, body: form,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "비교 요청을 처리하지 못했습니다. 다시 시도하세요.");
    if (pageClosed || generation !== lifetime || session !== comparisonSession) {
      if (data?.job) comparisonAPI("cancel", { session, job: data.job }, { keepalive: true }).catch(() => {});
      closeComparisonSession(session);
      return;
    }
    if (typeof data?.job !== "string") throw new Error("비교 작업을 시작하지 못했습니다.");
    activeJob = data.job;
    $("comparison-cancel").hidden = false;
    $("comparison-cancel").disabled = false;
    pollComparisonJob();
  } catch (error) {
    if (pageClosed || generation !== lifetime) return;
    // A disconnected upload can still own staged files or a job on the server.
    retireComparisonSession();
    feedback(connectionMessage(error), "error");
  }
}

async function cancelNativeComparison() {
  if (!activeJob || pageClosed) return;
  const job = activeJob, session = comparisonSession, generation = lifetime, request = ++jobRequest;
  clearTimeout(pollTimer);
  $("comparison-cancel").disabled = true;
  $("comparison-job-retry").hidden = true;
  $("comparison-job-status").textContent = "비교를 취소하고 있습니다…";
  try {
    const data = await comparisonAPI("cancel", { session, job });
    if (pageClosed || generation !== lifetime || activeJob !== job || request !== jobRequest) return;
    if (!applyJobStatus(data, job)) pollTimer = setTimeout(pollComparisonJob, 400);
  } catch (error) {
    if (pageClosed || generation !== lifetime || activeJob !== job || request !== jobRequest) return;
    handleNativeError(error);
    if (!error.sessionExpired) $("comparison-job-retry").hidden = false;
  } finally {
    if (!pageClosed && generation === lifetime) $("comparison-cancel").disabled = false;
  }
}

function clearDiff() {
  diffController?.abort();
  diffController = null;
  selectedPath = null;
  diffData = null;
  $("diff-content").hidden = true;
  $("diff-path").hidden = true;
  $("diff-note").hidden = true;
  $("diff-limit-note").hidden = true;
  $("diff-message").hidden = false;
  $("diff-message").textContent = "목록에서 파일을 선택하세요.";
  $("diff-retry").hidden = true;
  $("diff-mode").value = "compared";
  $("diff-mode").disabled = true;
  $("diff-code").replaceChildren();
  $("diff-panel").setAttribute("aria-busy", "false");
}

function feedback(message = "", tone = "info") {
  $("feedback").textContent = message;
  $("feedback").dataset.tone = tone;
  $("feedback").hidden = !message;
}

function clearResults() {
  clearDiff();
  results = [];
  $("result-content").hidden = true;
  $("empty-state").hidden = false;
  $("result-body").replaceChildren();
  $("status-filter").value = "all";
  $("status-filter").disabled = true;
  feedback();
}

function updateSelection() {
  for (const [side, input] of Object.entries(fileInputs)) {
    const files = Array.from(input.files);
    const selection = comparisonMode === "native" ? nativeSelections[side] : null;
    const root = comparisonMode === "upload" ? files[0]?.webkitRelativePath.split("/")[0] : null;
    $(`${side}-name`).textContent = selection?.name || root || "폴더를 선택하세요";
    const count = selection ? selection.file_count : files.length;
    const bytes = selection ? selection.total_bytes : files.reduce((sum, file) => sum + file.size, 0);
    $(`${side}-detail`).textContent = selection || root ? `${selection ? `${selection.path} · ` : ""}${count.toLocaleString("ko-KR")}개 파일 · ${(bytes / (1024 * 1024)).toFixed(2)} MiB` : "내 PC에서 비교할 폴더 선택";
    const button = $(`${side}-pick-directory`);
    button.classList.toggle("selected", Boolean(selection || root));
    button.disabled = busy || !["native", "upload"].includes(comparisonMode) || pageClosed;
    input.disabled = comparisonMode !== "upload" || busy || pageClosed;
  }
  const ready = comparisonMode === "native" ? Boolean(nativeSelections.left && nativeSelections.right) : comparisonMode === "upload" && fileInputs.left.files.length > 0 && fileInputs.right.files.length > 0;
  $("compare-button").disabled = busy || !ready || pageClosed;
  $("selection-status").textContent = ready ? "준비되었습니다. 내 PC에서 비교합니다." : "폴더 두 개를 선택하면 비교할 수 있습니다.";
}

function setBusy(value) {
  busy = value;
  for (const input of optionInputs) input.disabled = value || pageClosed;
  $("reset-options").disabled = value || pageClosed;
  $("results-panel").setAttribute("aria-busy", String(value));
  $("compare-label").textContent = value ? "처리 중…" : "비교 시작";
  updateSelection();
}

function buildForm() {
  const left = Array.from(fileInputs.left.files);
  const right = Array.from(fileInputs.right.files);
  if (!left.length || !right.length) throw new Error("파일이 있는 폴더 두 개를 선택해 주세요.");
  const all = [...left, ...right];
  if (all.length > maxFiles) throw new Error("파일은 두 폴더 합계 1,000개까지 비교할 수 있습니다.");
  const form = new FormData();
  for (const [side, files] of [["left", left], ["right", right]]) {
    const paths = files.map((file) => {
      const parts = file.webkitRelativePath.split("/");
      if (parts.length < 2) throw new Error("폴더 선택을 지원하는 브라우저에서 폴더를 다시 선택해 주세요.");
      return parts.slice(1).join("/");
    });
    const encodedPaths = JSON.stringify(paths);
    form.append(`${side}_paths`, encodedPaths);
    for (const file of files) {
      form.append(`${side}_files`, file, file.name);
    }
  }
  appendOptions(form);
  return form;
}

function appendOptions(form, original = false) {
  for (const [name, id] of [["ignore_start", "ignore-start"], ["ignore_end", "ignore-end"]]) {
    const input = $(id);
    if (!original && !/^\d+$/.test(input.value)) throw new Error("무시할 글자 수는 0 이상의 정수여야 합니다.");
    form.append(name, original ? "0" : input.value);
  }
  form.append("sort_lines", String(!original && $("sort-lines").checked));
  form.append("ignore_whitespace", String(!original && $("ignore-whitespace").checked));
}

function renderRows() {
  const filter = $("status-filter").value;
  const rows = results.filter((entry) => filter === "all" || entry.status === filter);
  const fragment = document.createDocumentFragment();
  for (const entry of rows) {
    const row = document.createElement("tr");
    row.classList.toggle("selected-row", entry.path === selectedPath);
    const path = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-button";
    button.textContent = entry.path;
    button.dataset.path = entry.path;
    button.title = entry.message || descriptions[entry.status] || "";
    button.setAttribute("aria-pressed", String(entry.path === selectedPath));
    path.append(button);
    if (entry.message) {
      const message = document.createElement("small");
      message.className = "file-error";
      message.textContent = entry.message;
      path.append(message);
    }
    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `status-badge ${entry.status}`;
    badge.textContent = labels[entry.status] || entry.status;
    status.append(badge);
    row.append(path, status);
    fragment.append(row);
  }
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 2;
    cell.textContent = "이 상태의 파일이 없습니다.";
    row.append(cell);
    fragment.append(row);
  }
  $("result-body").replaceChildren(fragment);
}

function renderDiffPage() {
  const fragment = document.createDocumentFragment();
  for (const line of diffData.lines) {
    const row = document.createElement("tr");
    row.className = line.kind;
    if (line.kind === "hunk") {
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.textContent = line.text;
      row.append(cell);
    } else {
      for (const value of [line.old_line ?? "", line.new_line ?? "", line.kind === "add" ? "+" : line.kind === "delete" ? "-" : " ", line.text]) {
        const cell = document.createElement("td");
        cell.textContent = String(value);
        row.append(cell);
      }
    }
    fragment.append(row);
  }
  $("diff-code").replaceChildren(fragment);
  const totalLines = diffData.total_lines;
  const pages = diffData.pages;
  $("diff-pagination").hidden = pages <= 1;
  $("diff-page-label").textContent = `${diffPage + 1} / ${pages} · ${totalLines.toLocaleString("ko-KR")}줄`;
  $("diff-prev").disabled = diffPage === 0;
  $("diff-next").disabled = diffPage >= pages - 1;
  document.querySelector(".diff-scroll").scrollTop = 0;
}

async function showDiff(path, page = 0) {
  diffController?.abort();
  const controller = new AbortController();
  diffController = controller;
  selectedPath = path;
  diffData = null;
  diffPage = page;
  diffRequestedPage = page;
  for (const button of $("result-body").querySelectorAll("button[data-path]")) {
    const selected = button.dataset.path === path;
    button.setAttribute("aria-pressed", String(selected));
    button.closest("tr").classList.toggle("selected-row", selected);
  }
  $("diff-path").textContent = path;
  $("diff-path").hidden = false;
  $("diff-mode").disabled = false;
  $("diff-content").hidden = true;
  $("diff-code").replaceChildren();
  $("diff-retry").hidden = true;
  $("diff-limit-note").hidden = true;
  $("diff-note").hidden = false;
  const original = $("diff-mode").value === "original";
  $("diff-note").textContent = original ? "원본 줄 기준 · 줄바꿈 형식과 마지막 줄바꿈 유무는 무시합니다." : "옵션 적용 후 줄 기준 · 정렬 시 줄 번호도 정렬된 순서입니다.";
  $("diff-message").hidden = false;
  $("diff-message").textContent = "변경 내용을 읽고 있습니다…";
  $("diff-panel").setAttribute("aria-busy", "true");
  const entry = results.find((item) => item.path === path);
  if (entry?.status === "error") {
    $("diff-mode").disabled = true;
    $("diff-note").hidden = true;
    $("diff-message").textContent = entry.message || "이 파일은 읽기 오류로 상세 보고서를 만들지 못했습니다. 파일 상태·인코딩·권한을 확인하고 다시 비교하세요.";
    $("diff-panel").setAttribute("aria-busy", "false");
    return;
  }
  try {
    const session = comparisonSession, job = completedJob;
    if (!session || !job) throw new Error("파일 비교를 다시 실행하세요.");
    const data = await comparisonAPI("diff", { session, job, path, mode: original ? "original" : "compared", page }, { signal: controller.signal });
    if (diffController !== controller) return;
    if (!data || !Array.isArray(data.lines)) throw new Error("변경 내용을 읽지 못했습니다.");
    diffData = data;
    $("diff-content").hidden = false;
    $("diff-limit-note").hidden = !data.truncated;
    $("diff-limit-note").textContent = `전체 상세 보고서 기록 완료 · 화면은 앞 ${Number(data.change_limit || 1000).toLocaleString("ko-KR")}개 변경 표시 (문맥 별도). 추가·삭제 수는 전체 내용을 기준으로 합니다.`;
    $("diff-added").textContent = `+${data.additions.toLocaleString("ko-KR")} 추가`;
    $("diff-deleted").textContent = `−${data.deletions.toLocaleString("ko-KR")} 삭제`;
    $("diff-headers").textContent = `diff --git a/${path} b/${path}\n--- ${data.old_exists ? `a/${path}` : "/dev/null"}\n+++ ${data.new_exists ? `b/${path}` : "/dev/null"}`;
    $("diff-message").hidden = data.lines.length > 0;
    $("diff-message").textContent = data.identical ? "표시할 줄 차이가 없습니다." : "한쪽 폴더에만 있는 빈 파일입니다.";
    renderDiffPage();
  } catch (error) {
    if (diffController !== controller || error.name === "AbortError") return;
    if (error.sessionExpired) { handleNativeError(error); return; }
    $("diff-message").textContent = error instanceof TypeError ? "앱에 연결할 수 없습니다. 실행창을 확인한 뒤 다시 시도하세요." : `${error.code ? `[${error.code}] ` : ""}${error.message}`;
    $("diff-retry").hidden = false;
  } finally {
    if (diffController === controller) $("diff-panel").setAttribute("aria-busy", "false");
  }
}

$("compare-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  if (comparisonMode === "native") { await startNativeComparison(); return; }
  if (comparisonMode === "upload") await startUploadComparison();
});

for (const input of Object.values(fileInputs)) input.addEventListener("change", () => { clearResults(); completedJob = null; updateSelection(); });
for (const input of optionInputs) input.addEventListener("input", () => { if (!busy) { clearResults(); completedJob = null; } });
$("status-filter").addEventListener("change", () => {
  const filter = $("status-filter").value;
  if (selectedPath && !results.some((entry) => entry.path === selectedPath && (filter === "all" || entry.status === filter))) clearDiff();
  renderRows();
});
$("result-body").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-path]");
  if (button) showDiff(button.dataset.path);
});
$("diff-mode").addEventListener("change", () => { if (selectedPath) showDiff(selectedPath); });
$("diff-retry").addEventListener("click", () => { if (selectedPath) showDiff(selectedPath, diffRequestedPage); });
$("diff-prev").addEventListener("click", () => showDiff(selectedPath, diffPage - 1));
$("diff-next").addEventListener("click", () => showDiff(selectedPath, diffPage + 1));
$("reset-options").addEventListener("click", () => {
  if (busy) return;
  completedJob = null;
  $("sort-lines").checked = false;
  $("ignore-whitespace").checked = false;
  $("ignore-start").value = "0";
  $("ignore-end").value = "0";
  clearResults();
});
$("sidebar-toggle").addEventListener("click", () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  $("sidebar-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("sidebar-toggle").setAttribute("aria-label", collapsed ? "메뉴 펼치기" : "메뉴 접기");
});
for (const side of ["left", "right"]) $(`${side}-pick-directory`).addEventListener("click", () => pickDirectory(side));
$("comparison-runtime-retry").addEventListener("click", initializeComparison);
$("comparison-job-retry").addEventListener("click", pollComparisonJob);
$("comparison-cancel").addEventListener("click", cancelNativeComparison);
window.addEventListener("pagehide", () => {
  pageClosed = true;
  retireComparisonSession();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) { pageClosed = false; setBusy(false); initializeComparison(); }
});
updateSelection();
initializeComparison();
