"use strict";

(() => {
  const get = (id) => document.getElementById(id);
  const pages = { "directory-tool": "디렉토리 비교", "jasypt-tool": "Jasypt 암호화", "ase-tool": "ASE / BCP" };
  function showPage() {
    const selected = Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : "directory-tool";
    for (const id of Object.keys(pages)) get(id).hidden = id !== selected;
    for (const link of document.querySelectorAll("[data-tool]")) {
      const active = link.dataset.tool === selected;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    get("current-tool").textContent = pages[selected];
    document.title = `${pages[selected]} · nice-tools`;
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", showPage);
  showPage();

  let busy = false;
  let controller = null;
  let hasResult = false;
  const form = get("jasypt-form");
  const inputs = Array.from(form.querySelectorAll("input, select, textarea"));
  const operation = () => get("jasypt-decrypt").checked ? "decrypt" : "encrypt";

  function feedback(message = "", error = false) {
    get("jasypt-feedback").textContent = message;
    get("jasypt-feedback").dataset.tone = error ? "error" : "info";
    get("jasypt-feedback").hidden = !message;
  }
  function updateMode() {
    const decrypt = operation() === "decrypt";
    get("jasypt-input-label").textContent = decrypt ? "암호문" : "원문";
    get("jasypt-input").placeholder = decrypt ? "복호화할 암호문 또는 ENC(...) 값을 입력하세요." : "암호화할 문자열을 입력하세요. 공백과 줄바꿈도 그대로 처리합니다.";
    get("jasypt-wrap-label").hidden = decrypt;
    get("jasypt-decrypt-note").hidden = !decrypt;
    get("jasypt-submit-label").textContent = busy ? (decrypt ? "복호화 중…" : "암호화 중…") : (decrypt ? "Decrypt · 복호화" : "Encrypt · 암호화");
  }
  function setBusy(value) {
    busy = value;
    for (const input of inputs) input.disabled = value;
    get("jasypt-submit").disabled = value;
    get("jasypt-show-password").disabled = value;
    form.setAttribute("aria-busy", String(value));
    updateMode();
  }
  function clearResult() {
    controller?.abort();
    controller = null;
    hasResult = false;
    get("jasypt-output").value = "";
    get("jasypt-result").hidden = true;
    feedback();
    setBusy(false);
  }
  for (const input of inputs) input.addEventListener("input", clearResult);
  for (const id of ["jasypt-encrypt", "jasypt-decrypt"]) get(id).addEventListener("change", updateMode);
  get("jasypt-algorithm").addEventListener("change", () => {
    const aes = get("jasypt-algorithm").value === "PBEWITHHMACSHA512ANDAES_256";
    get("jasypt-no-iv").disabled = aes;
    get("jasypt-iv").value = aes ? "random" : "none";
  });
  get("jasypt-show-password").addEventListener("click", () => {
    const visible = get("jasypt-password").type === "password";
    get("jasypt-password").type = visible ? "text" : "password";
    get("jasypt-show-password").textContent = visible ? "숨기기" : "보기";
    get("jasypt-show-password").setAttribute("aria-pressed", String(visible));
  });
  get("jasypt-clear").addEventListener("click", () => {
    clearResult();
    form.reset();
    get("jasypt-password").value = "";
    get("jasypt-input").value = "";
    get("jasypt-password").type = "password";
    get("jasypt-show-password").textContent = "보기";
    get("jasypt-show-password").setAttribute("aria-pressed", "false");
    get("jasypt-no-iv").disabled = true;
    updateMode();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    clearResult();
    const current = new AbortController();
    controller = current;
    const task = operation();
    const payload = {
      operation: task, text: get("jasypt-input").value, password: get("jasypt-password").value,
      algorithm: get("jasypt-algorithm").value, iterations: Number(get("jasypt-iterations").value),
      salt_generator: get("jasypt-salt").value, iv_generator: get("jasypt-iv").value,
      output_type: get("jasypt-output-type").value, wrap_enc: get("jasypt-wrap").checked,
    };
    setBusy(true);
    feedback(task === "encrypt" ? "암호화하고 있습니다…" : "복호화하고 있습니다…");
    try {
      const response = await fetch("/api/jasypt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: current.signal, cache: "no-store" });
      const data = await response.json().catch(() => null);
      if (controller !== current) return;
      if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "요청을 처리하지 못했습니다. 설정을 확인하고 다시 시도하세요.");
      if (typeof data?.result !== "string") throw new Error("결과를 읽지 못했습니다. 다시 시도하세요.");
      get("jasypt-output").value = data.result;
      get("jasypt-output-label").textContent = task === "encrypt" ? "암호문" : "복호화된 원문";
      get("jasypt-result").hidden = false;
      hasResult = true;
      feedback(task === "encrypt" ? "암호화가 완료되었습니다." : "복호화가 완료되었습니다.");
    } catch (error) {
      if (controller !== current || error.name === "AbortError") return;
      feedback(error instanceof TypeError ? "앱에 연결할 수 없습니다. 실행창을 확인하고 다시 시도하세요." : error.message, true);
    } finally {
      if (controller === current) setBusy(false);
    }
  });
  get("jasypt-use-result").addEventListener("click", () => {
    if (!hasResult) return;
    const result = get("jasypt-output").value;
    const decrypt = operation() === "encrypt";
    clearResult();
    get("jasypt-input").value = result;
    get("jasypt-decrypt").checked = decrypt;
    get("jasypt-encrypt").checked = !decrypt;
    updateMode();
    get("jasypt-input").focus();
  });
  get("jasypt-copy").addEventListener("click", async () => {
    if (!hasResult) return;
    try {
      await navigator.clipboard.writeText(get("jasypt-output").value);
      feedback("결과를 클립보드에 복사했습니다.");
    } catch {
      get("jasypt-output").focus();
      get("jasypt-output").select();
      feedback("자동 복사가 지원되지 않습니다. 선택된 결과를 Ctrl+C로 복사하세요.");
    }
  });
  updateMode();
})();
