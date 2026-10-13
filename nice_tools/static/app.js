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
const linesPerPage = 200;

function clearDiff() {
  diffController?.abort();
  diffController = null;
  selectedPath = null;
  diffData = null;
  $("diff-content").hidden = true;
  $("diff-path").hidden = true;
  $("diff-note").hidden = true;
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
    const root = files[0]?.webkitRelativePath.split("/")[0];
    $(`${side}-name`).textContent = root || "폴더를 선택하세요";
    $(`${side}-detail`).textContent = files.length ? `${files.length.toLocaleString("ko-KR")}개 파일 · ${(files.reduce((sum, file) => sum + file.size, 0) / (1024 * 1024)).toFixed(2)} MiB` : "내 PC에서 비교할 폴더 선택";
    input.nextElementSibling.classList.toggle("selected", files.length > 0);
  }
  const ready = fileInputs.left.files.length > 0 && fileInputs.right.files.length > 0;
  $("compare-button").disabled = busy || !ready;
  $("selection-status").textContent = ready ? "준비되었습니다. 내 PC에서 비교합니다." : "폴더 두 개를 선택하면 비교할 수 있습니다.";
}

function setBusy(value) {
  busy = value;
  for (const input of [...Object.values(fileInputs), ...optionInputs]) input.disabled = value;
  $("reset-options").disabled = value;
  $("results-panel").setAttribute("aria-busy", String(value));
  $("compare-label").textContent = value ? "비교 중…" : "비교 시작";
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
  const start = diffPage * linesPerPage;
  for (const line of diffData.lines.slice(start, start + linesPerPage)) {
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
  const pages = Math.ceil(diffData.lines.length / linesPerPage);
  $("diff-pagination").hidden = pages <= 1;
  $("diff-page-label").textContent = `${diffPage + 1} / ${pages} · ${diffData.lines.length.toLocaleString("ko-KR")}줄`;
  $("diff-prev").disabled = diffPage === 0;
  $("diff-next").disabled = diffPage >= pages - 1;
  document.querySelector(".diff-scroll").scrollTop = 0;
}

async function showDiff(path) {
  diffController?.abort();
  const controller = new AbortController();
  diffController = controller;
  selectedPath = path;
  diffData = null;
  diffPage = 0;
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
  $("diff-note").hidden = false;
  const original = $("diff-mode").value === "original";
  $("diff-note").textContent = original ? "원본 줄 기준 · 줄바꿈 형식과 마지막 줄바꿈 유무는 무시합니다." : "옵션 적용 후 줄 기준 · 정렬 시 줄 번호도 정렬된 순서입니다.";
  $("diff-message").hidden = false;
  $("diff-message").textContent = "변경 내용을 읽고 있습니다…";
  $("diff-panel").setAttribute("aria-busy", "true");
  try {
    const form = new FormData();
    for (const [side, input] of Object.entries(fileInputs)) {
      const file = Array.from(input.files).find((item) => item.webkitRelativePath.split("/").slice(1).join("/") === path);
      form.append(`${side}_paths`, JSON.stringify(file ? [path] : []));
      if (file) form.append(`${side}_files`, file, file.name);
    }
    appendOptions(form, original);
    const response = await fetch("/api/diff", { method: "POST", body: form, signal: controller.signal });
    const data = await response.json().catch(() => null);
    if (diffController !== controller) return;
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "변경 내용을 읽지 못했습니다.");
    if (!data || !Array.isArray(data.lines)) throw new Error("변경 내용을 읽지 못했습니다.");
    diffData = data;
    $("diff-content").hidden = false;
    $("diff-added").textContent = `+${data.additions.toLocaleString("ko-KR")} 추가`;
    $("diff-deleted").textContent = `−${data.deletions.toLocaleString("ko-KR")} 삭제`;
    $("diff-headers").textContent = `diff --git a/${path} b/${path}\n--- ${data.old_exists ? `a/${path}` : "/dev/null"}\n+++ ${data.new_exists ? `b/${path}` : "/dev/null"}`;
    $("diff-message").hidden = data.lines.length > 0;
    $("diff-message").textContent = data.identical ? "표시할 줄 차이가 없습니다." : "한쪽 폴더에만 있는 빈 파일입니다.";
    renderDiffPage();
  } catch (error) {
    if (diffController !== controller || error.name === "AbortError") return;
    $("diff-message").textContent = error instanceof TypeError ? "앱에 연결할 수 없습니다. 실행창을 확인한 뒤 다시 시도하세요." : error.message;
    $("diff-retry").hidden = false;
  } finally {
    if (diffController === controller) $("diff-panel").setAttribute("aria-busy", "false");
  }
}

$("compare-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  clearResults();
  try {
    const form = buildForm();
    setBusy(true);
    feedback("파일을 보내고 비교하고 있습니다. 잠시 기다려 주세요.");
    const response = await fetch("/api/compare", { method: "POST", body: form });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "비교 요청을 처리하지 못했습니다. 다시 시도해 주세요.");
    if (!data || !Array.isArray(data.results) || !data.summary) throw new Error("비교 결과를 읽을 수 없습니다. 다시 시도해 주세요.");
    results = data.results;
    for (const element of document.querySelectorAll("[data-stat]")) element.textContent = String(data.summary[element.dataset.stat] ?? 0);
    $("empty-state").hidden = true;
    $("result-content").hidden = false;
    $("status-filter").disabled = false;
    renderRows();
    feedback(`${data.summary.total.toLocaleString("ko-KR")}개 파일의 비교가 완료되었습니다.`, data.summary.error ? "error" : "info");
  } catch (error) {
    feedback(error instanceof TypeError ? "서버에 연결할 수 없습니다. 연결 상태를 확인하고 다시 시도해 주세요." : error.message, "error");
  } finally {
    setBusy(false);
  }
});

for (const input of Object.values(fileInputs)) input.addEventListener("change", () => { clearResults(); updateSelection(); });
for (const input of optionInputs) input.addEventListener("input", clearResults);
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
$("diff-retry").addEventListener("click", () => { if (selectedPath) showDiff(selectedPath); });
$("diff-prev").addEventListener("click", () => { diffPage -= 1; renderDiffPage(); });
$("diff-next").addEventListener("click", () => { diffPage += 1; renderDiffPage(); });
$("reset-options").addEventListener("click", () => {
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
updateSelection();
