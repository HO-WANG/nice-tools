"use strict";

(() => {
  const get = (name) => document.getElementById(`iq-${name}`);
  const connectionFields = Array.from(get("connection-form").querySelectorAll("input, select"));
  const exportFields = Array.from(get("export-form").querySelectorAll("input, select, textarea"));
  const phases = { queued: "대기 중", connecting: "IQ 연결 중", querying: "SELECT 실행 중", writing: "파일 기록 중", cancelling: "취소 중", done: "처리 종료" };
  const states = { running: "실행 중", succeeded: "완료", failed: "실패", cancelled: "취소됨" };
  let runtime = null;
  let session = "";
  let generation = 0;
  let connecting = false;
  let picking = false;
  let starting = false;
  let cancelling = false;
  let job = "";
  let timer = null;
  let pollController = null;
  let pickerController = null;
  const current = (epoch, token) => epoch === generation && token === session;
  const locked = () => connecting || picking || starting || !!job;
  const numeric = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
  const number = (value, digits = 0) => numeric(value).toLocaleString("ko-KR", { maximumFractionDigits: digits });

  function feedback(message = "", error = false) {
    get("feedback").textContent = message;
    get("feedback").hidden = !message;
    get("feedback").dataset.tone = error ? "error" : "info";
  }

  async function request(endpoint, payload, signal) {
    const response = await fetch(`/api/iq/${endpoint}`, {
      method: payload === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", signal,
      headers: { "Cache-Control": "no-store", ...(payload === undefined ? {} : { "Content-Type": "application/json", "X-Nice-Tools": "iq" }) },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "IQ 요청을 처리하지 못했습니다.");
    return data;
  }

  function refresh() {
    for (const field of connectionFields) field.disabled = connecting || !!session || !runtime;
    get("driver").disabled = connecting || !!session || !runtime?.available;
    get("connect").disabled = !runtime?.available || connecting || !!session;
    get("connect").textContent = connecting ? "검증 중…" : "연결 검증";
    get("disconnect").hidden = !session && !connecting;
    for (const field of exportFields) field.disabled = !session || locked();
    get("pick-directory").disabled = !session || locked();
    get("start").disabled = !session || locked() || !get("sql").value.trim() || !get("filename").value.trim();
    get("cancel").hidden = !job;
    get("cancel").disabled = cancelling;
    get("cancel").textContent = cancelling ? "취소 중…" : "실행 취소";
    get("job").setAttribute("aria-busy", String(!!job || starting));
    get("ready-note").textContent = !session ? "먼저 IQ 연결을 검증하세요." : picking ? "폴더 선택 창을 닫으면 계속할 수 있습니다." : starting ? "내보내기 요청 중…" : job ? "전체 결과를 저장 중입니다. 실행을 취소할 수 있습니다." : "SELECT 한 문장을 입력하고 내보내기를 시작하세요.";
  }

  function clearResult() {
    get("job").hidden = true;
    get("job-status").textContent = "";
    get("result-path").textContent = "";
    get("result-columns").textContent = "";
    get("result-details").hidden = true;
    get("result-details").open = false;
    get("stat-rows").textContent = "0";
    get("stat-bytes").textContent = "0 바이트";
    get("stat-elapsed").textContent = "0초";
    get("stat-rate").textContent = "—";
  }

  async function release(token) {
    if (!token) return;
    try { await request("disconnect", { session: token }); } catch { /* Already expired or disconnected. */ }
  }

  function disconnect() {
    const oldSession = session;
    generation += 1;
    session = "";
    connecting = picking = starting = cancelling = false;
    job = "";
    clearTimeout(timer);
    pollController?.abort();
    pickerController?.abort();
    get("password").value = "";
    get("sql").value = "";
    get("path").textContent = runtime?.export_directory || "";
    get("connection-status").textContent = "연결 검증 정보가 해제되었습니다.";
    clearResult();
    feedback();
    refresh();
    void release(oldSession);
  }

  get("connection-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (connecting || session || !runtime?.available) return;
    const epoch = ++generation;
    connecting = true;
    feedback();
    refresh();
    const payload = { host: get("host").value, port: Number(get("port").value), database: get("database").value,
      server_name: get("server-name").value, user: get("user").value, password: get("password").value, driver: get("driver").value };
    // Keep this request observable after disconnect so an eventual session can be released.
    try {
      const data = await request("connect", payload);
      if (epoch !== generation) { void release(data.session); return; }
      session = data.session;
      get("path").textContent = data.export_directory;
      get("connection-status").textContent = `연결 검증 완료 · ${data.driver} · ${data.version || "SAP IQ"}. 검증 연결은 닫혔으며 내보낼 때 다시 연결합니다.`;
    } catch (error) {
      if (epoch === generation) feedback(error.message, true);
    } finally {
      payload.password = "";
      if (epoch === generation) { connecting = false; get("password").value = ""; refresh(); }
    }
  });

  get("disconnect").addEventListener("click", disconnect);
  get("pick-directory").addEventListener("click", async () => {
    if (!session || locked()) return;
    const epoch = generation;
    const token = session;
    picking = true;
    pickerController = new AbortController();
    refresh();
    feedback();
    try {
      const data = await request("pick-directory", { session: token }, pickerController.signal);
      if (!current(epoch, token)) return;
      if (!data.cancelled && typeof data.path === "string") get("path").textContent = data.path;
    } catch (error) {
      if (current(epoch, token) && error.name !== "AbortError") feedback(error.message, true);
    } finally {
      if (current(epoch, token)) { picking = false; pickerController = null; refresh(); }
    }
  });

  function render(data) {
    const result = data.result || data;
    if (data.state !== "failed") feedback();
    get("job").hidden = false;
    get("job-status").textContent = `${states[data.state] || data.state} · ${phases[data.phase] || data.phase}${data.message ? `\n${data.message}` : ""}`;
    get("stat-rows").textContent = number(result.rows);
    get("stat-bytes").textContent = `${number(result.bytes_written)} 바이트`;
    get("stat-elapsed").textContent = `${number(result.elapsed_seconds, 1)}초`;
    get("stat-rate").textContent = result.rows_per_second == null ? "—" : `${number(result.rows_per_second, 1)} 행/초`;
    get("result-path").textContent = data.state === "succeeded" ? result.path || data.path || "" : "";
    get("result-details").hidden = data.state !== "succeeded" || !Array.isArray(result.columns);
    get("result-columns").textContent = Array.isArray(result.columns) ? `열 ${result.columns.length}개: ${result.columns.join(", ")}` : "";
    if (data.state !== "running") {
      job = "";
      cancelling = false;
      if (data.state === "failed") feedback(data.message || "내보내기에 실패했습니다. 설정을 확인하고 다시 시도하세요.", true);
    }
    refresh();
  }

  function schedulePoll(epoch, token, id) {
    clearTimeout(timer);
    if (current(epoch, token) && job === id) timer = setTimeout(() => void poll(epoch, token, id), 600);
  }

  async function poll(epoch, token, id) {
    if (!current(epoch, token) || job !== id || cancelling) return;
    const controller = new AbortController();
    pollController = controller;
    try {
      const data = await request("job", { session: token, job: id }, controller.signal);
      if (!current(epoch, token) || job !== id || cancelling || controller.signal.aborted) return;
      render(data);
    } catch (error) {
      if (current(epoch, token) && job === id && !controller.signal.aborted) feedback(`상태 확인 실패. 자동으로 다시 확인합니다. ${error.message}`, true);
    } finally {
      if (pollController === controller) pollController = null;
      if (!cancelling) schedulePoll(epoch, token, id);
    }
  }

  get("export-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!session || locked() || !get("sql").value.trim()) return;
    const filename = get("filename").value;
    if (!filename.trim() || /[\\/]/.test(filename) || filename === "." || filename === "..") {
      feedback("폴더 경로 없이 파일 이름만 입력하세요.", true);
      return;
    }
    const epoch = generation;
    const token = session;
    const payload = { session: token, sql: get("sql").value, filename, options: {
      encoding: get("encoding").value, field_terminator: get("field-terminator").value,
      row_terminator: get("row-terminator").value, include_header: get("header").checked,
    } };
    starting = true;
    feedback();
    clearResult();
    refresh();
    try {
      const data = await request("jobs", payload);
      if (!current(epoch, token)) return;
      job = data.job;
      get("job").hidden = false;
      get("job-status").textContent = "대기 중";
      void poll(epoch, token, job);
    } catch (error) {
      if (current(epoch, token)) feedback(error.message, true);
    } finally {
      payload.sql = "";
      if (current(epoch, token)) { starting = false; refresh(); }
    }
  });

  get("cancel").addEventListener("click", async () => {
    if (!session || !job || cancelling) return;
    const epoch = generation;
    const token = session;
    const id = job;
    cancelling = true;
    clearTimeout(timer);
    pollController?.abort();
    refresh();
    try {
      const data = await request("cancel", { session: token, job: id });
      if (current(epoch, token) && job === id) render(data);
    } catch (error) {
      if (current(epoch, token) && job === id) feedback(error.message, true);
    } finally {
      if (current(epoch, token) && job === id) { cancelling = false; refresh(); schedulePoll(epoch, token, id); }
    }
  });

  get("sql").addEventListener("input", refresh);
  get("filename").addEventListener("input", refresh);
  window.addEventListener("pagehide", () => {
    if (session) void fetch("/api/iq/disconnect", { method: "POST", credentials: "same-origin", cache: "no-store", keepalive: true,
      headers: { "Cache-Control": "no-store", "Content-Type": "application/json", "X-Nice-Tools": "iq" }, body: JSON.stringify({ session }) }).catch(() => {});
  });
  refresh();
  void (async () => {
    try {
      runtime = await request("status");
      get("driver").replaceChildren();
      for (const driver of runtime.drivers || []) {
        const option = document.createElement("option");
        option.value = driver;
        option.textContent = driver + (driver === runtime.driver ? " (권장)" : "");
        get("driver").append(option);
      }
      if (runtime.driver) get("driver").value = runtime.driver;
      if (!get("driver").options.length) {
        const option = document.createElement("option");
        option.value = ""; option.textContent = "호환 드라이버 없음"; get("driver").append(option);
      }
      get("path").textContent = runtime.export_directory || "";
      get("runtime").textContent = runtime.available ? "설치된 64비트 IQ / SQL Anywhere ODBC 드라이버를 사용합니다. 연결 정보는 저장하지 않습니다." :
        `64비트 SAP IQ / SQL Anywhere ODBC 드라이버를 PC에 설치한 후 앱을 다시 실행하세요. ASE 드라이버만으로는 IQ에 연결할 수 없습니다.${runtime.detail ? `\n${runtime.detail}` : ""}`;
      get("runtime").dataset.tone = runtime.available ? "info" : "error";
    } catch (error) {
      get("runtime").textContent = `IQ 드라이버 확인 실패. 앱을 다시 실행하세요. ${error.message}`;
      get("runtime").dataset.tone = "error";
    }
    refresh();
  })();
})();
