"use strict";

const $ = (id) => document.getElementById(id);
const fileInputs = { left: $("left-folder"), right: $("right-folder") };
const optionInputs = [$("sort-lines"), $("ignore-whitespace"), $("ignore-start"), $("ignore-end")];
const maxFiles = 1000;
const maxFileBytes = 10 * 1024 * 1024;
const maxPayloadBytes = 50 * 1024 * 1024;
const labels = { same: "동일", different: "차이", left_only: "왼쪽만", right_only: "오른쪽만", error: "읽기 오류" };
const descriptions = { same: "옵션 적용 후 내용이 같습니다", different: "옵션 적용 후 내용이 다릅니다", left_only: "왼쪽 폴더에만 있습니다", right_only: "오른쪽 폴더에만 있습니다" };
let results = [];
let busy = false;

function feedback(message = "", tone = "info") {
  $("feedback").textContent = message;
  $("feedback").dataset.tone = tone;
  $("feedback").hidden = !message;
}

function clearResults() {
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
  $("selection-status").textContent = ready ? "준비되었습니다. 선택한 폴더를 서버로 보내 비교합니다." : "폴더 두 개를 선택하면 비교할 수 있습니다.";
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
  const large = all.find((file) => file.size > maxFileBytes);
  if (large) throw new Error(`개별 파일은 10 MiB 이내여야 합니다: ${large.name}`);
  const form = new FormData();
  let overhead = 0;
  const encoder = new TextEncoder();
  for (const [side, files] of [["left", left], ["right", right]]) {
    const paths = files.map((file) => {
      const parts = file.webkitRelativePath.split("/");
      if (parts.length < 2) throw new Error("폴더 선택을 지원하는 브라우저에서 폴더를 다시 선택해 주세요.");
      return parts.slice(1).join("/");
    });
    const encodedPaths = JSON.stringify(paths);
    overhead += encoder.encode(encodedPaths).length;
    form.append(`${side}_paths`, encodedPaths);
    for (const file of files) {
      form.append(`${side}_files`, file, file.name);
      overhead += encoder.encode(file.name).length + 512;
    }
  }
  if (all.reduce((sum, file) => sum + file.size, 0) + overhead + 4096 > maxPayloadBytes) {
    throw new Error("업로드 요청은 두 폴더 합계 50 MiB 이내여야 합니다. 파일 크기를 줄여 주세요.");
  }
  for (const [name, id] of [["ignore_start", "ignore-start"], ["ignore_end", "ignore-end"]]) {
    const input = $(id);
    if (!/^\d+$/.test(input.value)) throw new Error("무시할 글자 수는 0 이상의 정수여야 합니다.");
    form.append(name, input.value);
  }
  form.append("sort_lines", String($("sort-lines").checked));
  form.append("ignore_whitespace", String($("ignore-whitespace").checked));
  return form;
}

function renderRows() {
  const filter = $("status-filter").value;
  const rows = results.filter((entry) => filter === "all" || entry.status === filter);
  const fragment = document.createDocumentFragment();
  for (const entry of rows) {
    const row = document.createElement("tr");
    const path = document.createElement("td");
    path.textContent = entry.path;
    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `status-badge ${entry.status}`;
    badge.textContent = labels[entry.status] || entry.status;
    status.append(badge);
    const description = document.createElement("td");
    description.textContent = entry.message || descriptions[entry.status] || "";
    row.append(path, status, description);
    fragment.append(row);
  }
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = "이 상태의 파일이 없습니다.";
    row.append(cell);
    fragment.append(row);
  }
  $("result-body").replaceChildren(fragment);
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
$("status-filter").addEventListener("change", renderRows);
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
