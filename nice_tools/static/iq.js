"use strict";

(() => {
  const get = (name) => document.getElementById(`iq-${name}`);
  const publicNames = ["host", "port", "database", "server-name", "user", "driver"];
  const connectionFields = publicNames.map(get).concat(get("password"));
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
  let profiles = [];
  let profileId = "";
  let profileLoading = false;
  let profileGeneration = 0;
  let profileController = null;
  let activeEndpoint = null;
  const selectedProfile = () => profiles.find((profile) => profile.id === profileId);
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
    get("profile").disabled = locked() || profileLoading;
    get("refresh-profiles").disabled = locked() || profileLoading;
    get("refresh-profiles").textContent = profileLoading ? "읽는 중…" : "설정 파일 다시 읽기";
    get("password").disabled = connecting || !!session || !runtime || !!selectedProfile();
    get("password").placeholder = selectedProfile() ? "저장된 비밀번호 사용" : "";
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

  function endpoint() {
    return { host: get("host").value.trim(), port: Number(get("port").value), user: get("user").value,
      database: get("database").value, server_name: get("server-name").value, driver: get("driver").value };
  }

  function matches(profile, target) {
    return target && hostIdentity(profile.host) === hostIdentity(target.host) &&
      ["port", "user", "database", "server_name"].every((key) => profile[key] === target[key]) &&
      (profile.driver || runtime?.driver || "") === target.driver;
  }

  function hostIdentity(host) {
    const value = host.trim();
    const address = value.startsWith("[") && value.endsWith("]") ? value.slice(1, -1) : value;
    if (address.includes(":")) {
      try { return new URL(`http://[${address}]/`).hostname; } catch { /* Keep invalid/native-specific input intact. */ }
    }
    return value;
  }

  function renderDrivers(value = get("driver").value) {
    const drivers = runtime?.drivers || [];
    get("driver").replaceChildren();
    for (const driver of drivers) get("driver").add(new Option(driver + (driver === runtime.driver ? " (권장)" : ""), driver));
    if (value && !drivers.includes(value)) get("driver").add(new Option(`${value} (설치 확인 필요)`, value));
    if (!get("driver").options.length) get("driver").add(new Option("호환 드라이버 없음", ""));
    get("driver").value = value || runtime?.driver || "";
    const missing = !!value && !drivers.includes(value) && !!runtime;
    get("driver-note").textContent = missing ? `저장된 드라이버 ${value}이(가) 설치 목록에 없습니다. 해당 64비트 IQ 드라이버를 설치하거나 직접 입력에서 드라이버를 변경하세요.` : "";
    get("driver-note").hidden = !missing;
    get("driver-note").dataset.tone = "error";
  }

  function invalidateProfiles() {
    profileGeneration += 1;
    profileController?.abort();
    profileController = null;
    if (profileLoading) get("profile-note").textContent = "연결 정보가 변경되었습니다. 설정 파일을 다시 읽어 목록을 갱신할 수 있습니다.";
    profileLoading = false;
  }

  function applyProfile(id, clearManual = false) {
    profileId = profiles.some((profile) => profile.id === id) ? id : "";
    get("profile").value = profileId;
    get("password").value = "";
    const profile = selectedProfile();
    if (profile || clearManual) {
      for (const name of publicNames) {
        if (name === "driver") continue;
        get(name).value = profile ? String(profile[name.replace("-", "_")]) : name === "port" ? "2638" : "";
      }
      renderDrivers(profile?.driver || runtime?.driver || "");
    }
    refresh();
  }

  async function loadProfiles({ initial = false, preferredId = "" } = {}) {
    invalidateProfiles();
    const revision = profileGeneration;
    const epoch = generation;
    const controller = new AbortController();
    profileController = controller;
    const valid = () => revision === profileGeneration && epoch === generation && !controller.signal.aborted;
    profileLoading = true;
    get("profile-note").textContent = "IQ 연결 설정 파일 확인 중…";
    refresh();
    try {
      const data = await request("profiles", undefined, controller.signal);
      if (!valid()) return;
      if (!Array.isArray(data.profiles) || !data.profiles.every((profile) => profile &&
        ["id", "name", "host", "user"].every((key) => typeof profile[key] === "string" && profile[key]) &&
        ["database", "server_name", "driver"].every((key) => typeof profile[key] === "string") &&
        Number.isInteger(profile.port) && profile.port > 0 && profile.port <= 65535) ||
        new Set(data.profiles.map((profile) => profile.id)).size !== data.profiles.length) throw new Error("IQ 서버 목록을 확인할 수 없습니다. 직접 입력으로 연결하세요.");
      // Keep only public metadata. Passwords are resolved by the server from the opaque ID.
      profiles = data.profiles.map(({ id, name, host, port, user, database, server_name, driver }) => ({ id, name, host, port, user, database, server_name, driver }));
      get("profile").replaceChildren(new Option("직접 입력 / 새 연결", ""));
      for (const profile of profiles) get("profile").add(new Option(profile.name, profile.id));
      get("profile-path").textContent = typeof data.path === "string" ? data.path : "";
      let id = "";
      if (session || profileId) {
        const candidates = profiles.filter((profile) => matches(profile, activeEndpoint || endpoint()));
        id = [preferredId, profileId].find((value) => candidates.some((profile) => profile.id === value)) || (candidates.length === 1 ? candidates[0].id : "");
      } else if (initial) {
        id = profiles.find((profile) => profile.id === data.last_profile_id)?.id || profiles[0]?.id || "";
      }
      if (session) { profileId = id; get("profile").value = id; }
      else applyProfile(id);
      get("profile-note").textContent = session && !id ? "현재 연결과 일치하는 저장된 서버가 없습니다. 연결은 유지됩니다." :
        `성공한 연결을 별도 IQ 설정 파일에 자동 저장합니다. 비밀번호는 Jasypt로 암호화하며 앱의 고정 키로 자동 복호화합니다. ${profiles.length ? `서버 ${profiles.length.toLocaleString()}개 · 다음 실행에는 마지막으로 성공한 서버를 선택합니다.` : "처음에는 직접 입력으로 연결하세요."}`;
      get("profile-note").dataset.tone = "info";
    } catch (error) {
      if (valid() && error.name !== "AbortError") {
        get("profile-note").textContent = `IQ 설정 파일을 읽지 못했습니다. 직접 입력으로 연결할 수 있습니다. ${error.message}`;
        get("profile-note").dataset.tone = "error";
      }
    } finally {
      if (valid()) { profileLoading = false; profileController = null; refresh(); }
    }
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
    invalidateProfiles();
    session = "";
    activeEndpoint = null;
    connecting = picking = starting = cancelling = false;
    job = "";
    clearTimeout(timer);
    pollController?.abort();
    pickerController?.abort();
    get("password").value = "";
    get("sql").value = "";
    get("path").textContent = runtime?.export_directory || "";
    get("connection-status").textContent = "연결 검증 정보가 해제되었습니다.";
    get("save-warning").hidden = true;
    get("save-warning").textContent = "";
    clearResult();
    feedback();
    refresh();
    void release(oldSession);
  }

  get("connection-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (connecting || session || !runtime?.available) return;
    const profile = selectedProfile();
    const target = endpoint();
    const payload = profile ? { profile_id: profile.id } : { ...target, password: get("password").value };
    invalidateProfiles();
    const epoch = ++generation;
    connecting = true;
    feedback();
    refresh();
    get("save-warning").hidden = true;
    get("password").value = "";
    // Keep this request observable after disconnect so an eventual session can be released.
    try {
      const data = await request("connect", payload);
      if (epoch !== generation) { void release(data.session); return; }
      session = data.session;
      activeEndpoint = target;
      get("path").textContent = data.export_directory;
      const label = typeof data.profile_name === "string" ? data.profile_name : profile?.name || `${target.host} · ${target.user}`;
      get("connection-status").textContent = `연결 검증 완료 · ${label} · ${data.driver} · ${data.version || "SAP IQ"}. 검증 연결은 닫혔으며 내보낼 때 다시 연결합니다.`;
      if (data.saved === false) {
        get("save-warning").textContent = typeof data.save_warning === "string" ? data.save_warning : "연결 검증은 성공했지만 설정 파일에 저장하지 못했습니다. 쓰기 권한을 확인하세요.";
        get("save-warning").hidden = false;
      }
      if (data.saved === true || data.profile_id) await loadProfiles({ preferredId: data.profile_id });
    } catch (error) {
      if (epoch === generation) feedback(error.message, true);
    } finally {
      if ("password" in payload) payload.password = "";
      if (epoch === generation) { connecting = false; get("password").value = ""; refresh(); }
    }
  });

  get("disconnect").addEventListener("click", disconnect);
  get("profile").addEventListener("change", () => {
    if (locked()) { get("profile").value = profileId; return; }
    const id = get("profile").value;
    disconnect();
    applyProfile(id, !id);
  });
  get("refresh-profiles").addEventListener("click", () => { if (!locked()) void loadProfiles(); });
  for (const name of publicNames) get(name).addEventListener("input", () => {
    if (locked() || session) return;
    invalidateProfiles();
    profileId = "";
    get("profile").value = "";
    get("password").value = "";
    renderDrivers();
    refresh();
  });
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
  void loadProfiles({ initial: true });
  void (async () => {
    try {
      runtime = await request("status");
      renderDrivers(selectedProfile()?.driver || get("driver").value || runtime.driver || "");
      get("path").textContent = runtime.export_directory || "";
      get("runtime").textContent = runtime.available ? "설치된 64비트 IQ / SQL Anywhere ODBC 드라이버를 사용합니다. ASE 연결 설정과 별도의 IQ 설정 파일을 사용합니다." :
        `64비트 SAP IQ / SQL Anywhere ODBC 드라이버를 PC에 설치한 후 앱을 다시 실행하세요. ASE 드라이버만으로는 IQ에 연결할 수 없습니다.${runtime.detail ? `\n${runtime.detail}` : ""}`;
      get("runtime").dataset.tone = runtime.available ? "info" : "error";
    } catch (error) {
      get("runtime").textContent = `IQ 드라이버 확인 실패. 앱을 다시 실행하세요. ${error.message}`;
      get("runtime").dataset.tone = "error";
    }
    refresh();
  })();
})();
