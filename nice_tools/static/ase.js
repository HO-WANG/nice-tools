"use strict";

(() => {
  const get = (id) => document.getElementById(id);
  if (!get("ase-tool")) return;
  const state = {
    available: false, bcpAvailable: false, session: null, databases: [], owners: [], ownersLoaded: false, objects: [], searched: false, object: null,
    columns: [], columnsLoaded: false, file: null, path: "", exports: new Set(), exportDirectory: "", defaultDirectory: "",
    job: null, jobStatus: null, busy: "", generation: 0, timer: null, controllers: new Set(),
    profiles: [], profileId: "", profileLoading: false, profileGeneration: 0, profileController: null, clientPaths: {}, configuredPaths: {}, odbcDriver: "", configuredDriver: "",
  };
  let renderedObjects = null;
  let objectRows = [];
  const connectionIds = ["ase-host", "ase-port", "ase-user", "ase-password"];
  const optionIds = ["ase-encoding", "ase-field-terminator", "ase-row-terminator", "ase-batch-size", "ase-keep-identity", "ase-concurrency"];
  const kindNames = { U: "테이블", V: "뷰", P: "프로시저" };
  const direction = () => get("ase-in").checked ? "in" : "out";
  const active = () => Boolean(state.job);
  const locked = () => active() || ["connect", "start", "disconnect", "cancel", "directory", "picker"].includes(state.busy);
  const profileLocked = () => active() || ["start", "disconnect", "cancel", "directory", "picker"].includes(state.busy);
  const selectedProfile = () => state.profiles.find((profile) => profile.id === state.profileId);
  const objectKey = (object) => JSON.stringify([object.database, object.owner_id, object.id]);
  const exportObjects = () => state.objects.filter((object) => ["U", "V"].includes(object.kind) && state.exports.has(objectKey(object)));
  const transferable = () => state.bcpAvailable && (direction() === "out" ? exportObjects().length > 0 : state.columnsLoaded && state.object?.kind === "U");
  const stale = () => new DOMException("요청이 변경되었습니다.", "AbortError");

  function feedback(message = "", error = false) {
    get("ase-feedback").textContent = message;
    get("ase-feedback").dataset.tone = error ? "error" : "info";
    get("ase-feedback").hidden = !message;
  }

  function clearSecrets() {
    get("ase-password").value = "";
  }

  function showOdbcDriver(odbc, configured = false) {
    const driver = odbc && typeof odbc.driver === "string" ? odbc.driver : "";
    if (configured) state.configuredDriver = driver;
    else state.odbcDriver = driver;
    const selected = state.odbcDriver || state.configuredDriver;
    get("ase-odbc-driver").textContent = selected ? `ODBC 드라이버${state.odbcDriver ? "" : " (설정)"}: ${selected}` : "";
    get("ase-odbc-driver").hidden = !selected;
  }

  function showClientPaths(paths, configured = false) {
    const values = {};
    if (paths && typeof paths === "object") {
      if (typeof paths.bcp_path === "string" && paths.bcp_path) values.bcp_path = paths.bcp_path;
    }
    if (configured) state.configuredPaths = values;
    else state.clientPaths = values;
    get("ase-client-paths").textContent = [["bcp", "bcp_path"]]
      .filter(([, key]) => state.clientPaths[key] || state.configuredPaths[key])
      .map(([name, key]) => `${name}${state.clientPaths[key] ? "" : " (설정)"}: ${state.clientPaths[key] || state.configuredPaths[key]}`).join("\n");
    get("ase-client-paths").hidden = !get("ase-client-paths").textContent;
  }

  async function api(endpoint, payload, generation = state.generation) {
    const controller = new AbortController();
    // A completed connect may create a server session even after the UI changes.
    // Keep its response available so the abandoned session can be released.
    if (endpoint !== "connect") state.controllers.add(controller);
    try {
      const response = await fetch(`/api/ase/${endpoint}`, {
        method: payload === undefined ? "GET" : "POST",
        headers: { "Content-Type": "application/json", "X-Nice-Tools": "ase", "Cache-Control": "no-store" },
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
        ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
      });
      const data = await response.json();
      if (generation !== state.generation || controller.signal.aborted) {
        if (endpoint === "connect" && response.ok && typeof data?.session === "string") await releaseSession(data.session);
        throw stale();
      }
      if (!data || typeof data !== "object") throw new Error("서버 응답을 확인할 수 없습니다.");
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "요청을 처리할 수 없습니다.");
      return data;
    } catch (error) {
      if (generation !== state.generation || controller.signal.aborted) throw stale();
      if (error.name === "AbortError") throw error;
      if (error instanceof SyntaxError || error instanceof TypeError) throw new Error("서버 응답을 받지 못했습니다. 연결 상태를 확인하세요.");
      throw error;
    } finally {
      state.controllers.delete(controller);
    }
  }

  async function releaseSession(session) {
    try {
      await fetch("/api/ase/disconnect", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Nice-Tools": "ase", "Cache-Control": "no-store" },
        cache: "no-store", credentials: "same-origin", body: JSON.stringify({ session }),
      });
    } catch {
      // Cleanup must never restore stale UI state or expose connection details.
    }
  }

  function nextGeneration() {
    state.generation += 1;
    for (const controller of state.controllers) controller.abort();
    state.controllers.clear();
    clearTimeout(state.timer);
    state.timer = null;
    state.busy = "";
    return state.generation;
  }

  function clearStatistics() {
    get("ase-out-statistics").hidden = true;
    get("ase-bcp-statistics").hidden = true;
    get("ase-bcp-statistics").textContent = "";
    for (const id of ["ase-stat-rows", "ase-stat-elapsed", "ase-stat-rate", "ase-stat-bytes", "ase-stat-bytes-exact"]) get(id).textContent = "";
  }

  function clearTimings() {
    get("ase-catalog-timings").textContent = "";
    get("ase-catalog-timings").hidden = true;
  }

  function showTimings(label, data) {
    clearTimings();
    const timings = data.timings;
    if (!timings || !Number.isFinite(timings.total_seconds) || timings.total_seconds < 0) return;
    const phases = ["login_seconds", "query_seconds"];
    if (phases.some((key) => key in timings && (!Number.isFinite(timings[key]) || timings[key] < 0))) return;
    const measured = phases.reduce((sum, key) => sum + (timings[key] || 0), 0);
    if (!Number.isFinite(measured)) return;
    const seconds = (value) => `${value.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}초`;
    const parts = [`${label} · 총 ${seconds(timings.total_seconds)}`];
    if (Number.isFinite(timings.login_seconds)) parts.push(timings.login_seconds === 0 ? "연결 재사용" : `로그인 ${seconds(timings.login_seconds)}`);
    if (Number.isFinite(timings.query_seconds)) parts.push(`조회 ${seconds(timings.query_seconds)}`);
    if (phases.every((key) => Number.isFinite(timings[key]))) parts.push(`앱 준비 ${seconds(Math.max(0, timings.total_seconds - measured))}`);
    get("ase-catalog-timings").textContent = parts.join(" · ");
    get("ase-catalog-timings").hidden = false;
  }

  function reset(level) {
    nextGeneration();
    state.job = null;
    state.jobStatus = null;
    get("ase-job").hidden = true;
    get("ase-job-status").textContent = "";
    get("ase-job-events").replaceChildren();
    get("ase-job-items").replaceChildren();
    clearStatistics();
    clearTimings();
    feedback();
    if (level === "connection") {
      state.session = null;
      state.exportDirectory = state.defaultDirectory;
      state.databases = [];
      setChoices("ase-database", [], "DB 선택", "name");
    }
    if (["connection", "database"].includes(level)) {
      state.owners = [];
      state.ownersLoaded = false;
      setChoices("ase-owner", [], "전체 소유자", "id");
    }
    if (["connection", "database", "search"].includes(level)) {
      state.objects = [];
      state.searched = false;
      state.exports.clear();
    }
    if (["connection", "database", "search", "object"].includes(level)) {
      state.object = null;
      state.columns = [];
      state.columnsLoaded = false;
      get("ase-column-body").replaceChildren();
      get("ase-keep-identity").checked = false;
    }
    if (level !== "options") {
      state.file = null;
      state.path = "";
    }
    render();
  }

  function setChoices(id, choices, placeholder, key) {
    const select = get(id);
    const first = document.createElement("option");
    first.value = "";
    first.textContent = placeholder;
    select.replaceChildren(first);
    for (const choice of choices) {
      const option = document.createElement("option");
      option.value = String(choice[key]);
      option.textContent = String(choice.name);
      select.append(option);
    }
  }

  function invalidateProfileRead() {
    state.profileGeneration += 1;
    state.profileController?.abort();
    state.profileController = null;
    if (state.profileLoading) get("ase-profile-help").textContent = "연결 정보가 변경되었습니다. 설정 파일 다시 읽기로 서버 목록을 갱신할 수 있습니다.";
    state.profileLoading = false;
  }

  function applyProfile(id, { preserveConnection = false } = {}) {
    const wasProfile = Boolean(state.profileId);
    state.profileId = state.profiles.some((profile) => profile.id === id) ? id : "";
    const profile = selectedProfile();
    get("ase-profile").value = state.profileId;
    clearSecrets();
    if (profile) {
      get("ase-host").value = profile.host;
      get("ase-port").value = String(profile.port);
      get("ase-user").value = profile.user;
    } else if (wasProfile && !preserveConnection) {
      get("ase-host").value = "";
      get("ase-port").value = "5000";
      get("ase-user").value = "";
    }
    render();
  }

  async function clearConnection() {
    const session = state.session;
    reset("connection");
    clearSecrets();
    get("ase-connection-note").textContent = "성공한 연결 정보는 암호화된 비밀번호와 함께 설정 파일에 저장합니다. 저장한 서버는 비밀번호를 다시 입력하지 않고 연결할 수 있습니다.";
    if (session) await perform("disconnect", (generation) => api("disconnect", { session }, generation));
  }

  async function loadProfiles({ preferredId = "", preserveSession = false } = {}) {
    state.profileController?.abort();
    const controller = new AbortController();
    state.profileController = controller;
    const profileGeneration = ++state.profileGeneration;
    const generation = state.generation;
    const current = () => profileGeneration === state.profileGeneration && generation === state.generation && !controller.signal.aborted;
    state.profileLoading = true;
    get("ase-profile-help").textContent = "설정 파일을 확인하는 중입니다.";
    get("ase-profile-help").dataset.tone = "info";
    render();
    try {
      const response = await fetch("/api/ase/profiles", {
        method: "GET", headers: { "Content-Type": "application/json", "X-Nice-Tools": "ase", "Cache-Control": "no-store" },
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
      });
      const data = await response.json();
      if (!current()) throw stale();
      if (!data || typeof data !== "object") throw new Error("설정 파일 응답을 확인할 수 없습니다.");
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "설정 파일을 읽을 수 없습니다. 직접 입력으로 연결할 수 있습니다.");
      if (!Array.isArray(data.profiles) || !data.profiles.every((profile) => profile &&
        ["id", "name", "host", "user"].every((key) => typeof profile[key] === "string" && profile[key]) &&
        Number.isInteger(profile.port) && profile.port >= 1 && profile.port <= 65535)) throw new Error("설정 파일의 서버 목록을 확인할 수 없습니다. 직접 입력으로 연결할 수 있습니다.");
      state.profiles = data.profiles.map(({ id, name, host, port, user, database }) => ({ id, name, host, port, user, ...(typeof database === "string" ? { database } : {}) }));
      setChoices("ase-profile", state.profiles, "직접 입력", "id");
      let profileId;
      if (preserveSession) {
        const matches = state.profiles.filter((profile) => profile.host === get("ase-host").value.trim() && profile.port === Number(get("ase-port").value) && profile.user === get("ase-user").value.trim());
        const preferred = [preferredId, state.profileId].find((id) => matches.some((profile) => profile.id === id));
        profileId = preferred || (matches.length === 1 ? matches[0].id : "");
      } else {
        const preferred = [preferredId, state.profileId, data.last_profile_id].find((id) => state.profiles.some((profile) => profile.id === id));
        profileId = preferred || state.profiles[0]?.id || "";
      }
      applyProfile(profileId, { preserveConnection: preserveSession });
      showClientPaths(data.clients, true);
      showOdbcDriver(data.odbc, true);
      get("ase-profile-path").textContent = typeof data.path === "string" ? `설정 파일: ${data.path}` : "";
      get("ase-profile-path").hidden = !get("ase-profile-path").textContent;
      get("ase-profile-help").textContent = !data.exists ? "EXE와 같은 폴더의 ase-connections.txt에 성공한 연결을 자동 저장합니다. 처음에는 직접 입력으로 연결하세요." : state.profiles.length ? `설정 파일에서 ${state.profiles.length.toLocaleString()}개 서버를 읽었습니다. 다음 실행에는 마지막으로 성공한 서버가 선택됩니다.` : "설정 파일에 서버가 없습니다. 직접 입력으로 연결하면 성공한 정보를 저장합니다.";
      if (preserveSession && !profileId) get("ase-profile-help").textContent = "연결 중인 서버와 일치하는 설정을 확인할 수 없어 직접 입력으로 표시합니다. 현재 연결은 유지됩니다.";
      const settings = data.jasypt;
      get("ase-profile-jasypt").textContent = settings && typeof settings.algorithm === "string" && Number.isInteger(settings.iterations) ? `ASE 연결 파일: ${settings.algorithm} · 반복 ${settings.iterations.toLocaleString()}회 · 앱의 고정 키로 자동 복호화합니다. Jasypt 도구의 키 입력은 별도입니다.` : "";
      get("ase-profile-jasypt").hidden = !get("ase-profile-jasypt").textContent;
      if (!preserveSession) await checkRuntime();
      return true;
    } catch (error) {
      if (current() && error.name !== "AbortError") {
        const message = error instanceof SyntaxError || error instanceof TypeError ? "설정 파일 응답을 받지 못했습니다. 직접 입력으로 연결할 수 있습니다." : error.message;
        get("ase-profile-help").textContent = message;
        get("ase-profile-help").dataset.tone = "error";
        feedback(message, true);
      }
      return false;
    } finally {
      if (profileGeneration === state.profileGeneration) {
        state.profileLoading = false;
        state.profileController = null;
        render();
      }
    }
  }

  function cell(row, value) {
    const td = document.createElement("td");
    td.textContent = value === null || value === undefined ? "NULL" : String(value);
    row.append(td);
    return td;
  }

  function renderObjects() {
    const body = get("ase-object-body");
    const objects = state.objects;
    if (renderedObjects !== objects) {
      const fragment = document.createDocumentFragment();
      objectRows = objects.map((object) => {
        const row = document.createElement("tr");
        const selection = cell(row, "");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.className = "ase-export-checkbox";
        checkbox.setAttribute("aria-label", `${object.owner}.${object.name} OUT 선택`);
        checkbox.addEventListener("change", () => {
          if (locked()) return;
          if (checkbox.checked) state.exports.add(objectKey(object));
          else state.exports.delete(objectKey(object));
          reset("options");
        });
        selection.append(checkbox);
        const name = cell(row, "");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ase-object-button";
        button.textContent = String(object.name);
        button.addEventListener("click", () => selectObject(object));
        name.append(button);
        cell(row, object.owner);
        cell(row, kindNames[object.kind] || object.kind);
        fragment.append(row);
        return { row, checkbox, button };
      });
      // Rebuild only for new search results. Removing a focused checkbox on
      // every state update loses focus and can reset its scroll container.
      body.replaceChildren(fragment);
      renderedObjects = objects;
    }
    objectRows.forEach(({ row, checkbox, button }, index) => {
      const object = objects[index];
      const selected = state.object?.id === object.id && state.object?.owner_id === object.owner_id;
      row.classList.toggle("selected-row", selected);
      checkbox.checked = state.exports.has(objectKey(object));
      checkbox.disabled = locked() || direction() !== "out" || !["U", "V"].includes(object.kind) || !state.bcpAvailable;
      button.setAttribute("aria-pressed", String(selected));
      button.disabled = locked();
    });
    const eligible = objects.filter((object) => ["U", "V"].includes(object.kind));
    const count = exportObjects().length;
    get("ase-select-all").checked = eligible.length > 0 && count === eligible.length;
    get("ase-select-all").indeterminate = count > 0 && count < eligible.length;
    get("ase-select-all").disabled = locked() || direction() !== "out" || !state.bcpAvailable || !eligible.length;
    get("ase-selection-count").textContent = `OUT ${count.toLocaleString()}개 선택`;
    get("ase-clear-selection").disabled = locked() || !count;
    get("ase-object-empty").hidden = objects.length > 0;
    get("ase-object-empty").textContent = !state.session ? "서버에 연결한 뒤 DB를 선택하세요." : state.busy === "objects" ? "객체를 검색하는 중입니다." : !get("ase-database").value ? "DB를 선택하세요. 소유자는 선택하지 않아도 됩니다." : !state.searched ? "검색을 누르면 객체를 조회합니다. 소유자는 선택하지 않아도 됩니다." : "조건에 맞는 객체가 없습니다.";
    get("ase-object-count").textContent = !state.session ? "연결 후 선택할 수 있습니다." : !state.searched ? "검색 후 선택할 수 있습니다." : `${objects.length.toLocaleString()}개 객체`;
  }

  function renderColumns() {
    get("ase-object-name").textContent = state.object ? `${state.object.database}.${state.object.owner}.${state.object.name}` : "";
    get("ase-column-scroll").hidden = !state.columns.length;
    get("ase-column-empty").hidden = state.columns.length > 0;
    get("ase-column-empty").textContent = state.busy === "columns" ? "컬럼을 불러오는 중입니다." : state.object?.kind === "P" ? "프로시저는 조회만 가능합니다. BCP를 실행할 수 없습니다." : state.object ? "컬럼 정보가 없습니다." : "목록에서 객체를 선택하세요.";
  }

  function render() {
    const isLocked = locked();
    const canTransfer = transferable();
    for (const id of connectionIds) get(id).disabled = Boolean(state.session) || Boolean(state.busy);
    const profile = selectedProfile();
    for (const id of ["ase-host", "ase-port", "ase-user"]) get(id).readOnly = Boolean(profile);
    get("ase-password").required = false;
    get("ase-password").disabled = Boolean(profile) || Boolean(state.session) || Boolean(state.busy);
    get("ase-password-field").hidden = Boolean(profile);
    get("ase-password-field").parentElement.classList.toggle("has-saved-profile", Boolean(profile));
    get("ase-profile").disabled = profileLocked();
    get("ase-profile-reload").disabled = profileLocked();
    get("ase-profile-reload").textContent = state.profileLoading ? "설정 파일 읽는 중…" : "설정 파일 다시 읽기";
    get("ase-connect").disabled = !state.available || Boolean(state.session) || Boolean(state.busy);
    get("ase-connect").textContent = state.busy === "connect" ? "연결 중…" : state.session ? "연결됨" : "연결";
    get("ase-disconnect").disabled = !state.session || state.busy === "disconnect";
    get("ase-database").disabled = !state.session || isLocked;
    const databaseSelected = Boolean(state.session && get("ase-database").value);
    get("ase-owner").disabled = !databaseSelected || isLocked;
    get("ase-kind").disabled = !databaseSelected || isLocked;
    get("ase-search").disabled = !databaseSelected || isLocked;
    get("ase-search-button").disabled = !databaseSelected || isLocked || Boolean(state.busy);
    get("ase-search-button").textContent = state.busy === "objects" ? "검색 중…" : "검색";
    get("ase-out").disabled = !state.bcpAvailable || !state.session || Boolean(state.busy) || isLocked;
    get("ase-in").disabled = !state.bcpAvailable || !state.session || Boolean(state.busy) || isLocked;
    for (const id of optionIds) get(id).disabled = !canTransfer || isLocked || (id === "ase-keep-identity" && direction() !== "in");
    get("ase-crlf").disabled = !canTransfer || isLocked;
    get("ase-batch-size").disabled = !canTransfer || isLocked || direction() !== "in";
    get("ase-concurrency-field").hidden = direction() !== "out";
    get("ase-concurrency").disabled = !state.bcpAvailable || !state.session || Boolean(state.busy) || isLocked || direction() !== "out";
    get("ase-pick-file").hidden = direction() !== "in";
    get("ase-pick-directory").hidden = direction() !== "out";
    get("ase-pick-file").disabled = !canTransfer || Boolean(state.busy) || isLocked;
    get("ase-pick-file").textContent = state.busy === "picker" ? "선택 창 열림…" : "파일 선택";
    get("ase-pick-directory").disabled = !state.session || !state.bcpAvailable || Boolean(state.busy) || isLocked;
    get("ase-pick-directory").textContent = state.busy === "directory" ? "선택 창 열림…" : "폴더 선택";
    get("ase-start").disabled = !canTransfer || (direction() === "in" && !state.file) || Boolean(state.busy) || isLocked;
    get("ase-start").textContent = state.busy === "start" ? "시작 중…" : direction() === "in" ? "BCP IN · 행 추가 시작" : `BCP OUT · ${exportObjects().length.toLocaleString()}개 시작`;
    get("ase-identity-options").hidden = direction() !== "in";
    get("ase-append-warning").hidden = direction() !== "in";
    get("ase-path").textContent = direction() === "out" ? state.exportDirectory || "EXE가 있는 폴더" : state.path || "파일을 선택하세요.";
    get("ase-file-label").textContent = direction() === "in" ? "읽을 파일" : "저장할 폴더";
    get("ase-file-note").textContent = direction() === "in" ? "Windows 파일 선택 창에서 내 PC의 파일을 지정합니다. 컬럼 순서와 구분자가 일치해야 합니다." : "기본값은 EXE가 있는 폴더입니다. 출력 폴더에 DB명.오너명.테이블명.txt로 바로 저장하고 같은 이름이 있으면 DB명.오너명.테이블명_1.txt처럼 번호를 붙입니다. 동시 작업 수는 1~5개 중 선택할 수 있습니다.";
    get("ase-transfer-note").textContent = !state.bcpAvailable ? "DB 객체는 조회할 수 있습니다. 파일 전송을 사용하려면 SAP bcp.exe를 설치하거나 ase-connections.txt의 [sap_client]에 bcp 경로를 지정하세요." : state.object?.kind === "P" ? "프로시저는 조회만 가능합니다. BCP를 실행할 수 없습니다." : state.object?.kind === "V" ? "뷰는 BCP OUT만 사용할 수 있습니다." : "테이블은 OUT / IN, 뷰는 OUT을 사용할 수 있습니다. 프로시저는 조회만 가능합니다.";
    let readyNote;
    if (active()) readyNote = "실행 중에는 객체, 파일과 설정을 바꿀 수 없습니다.";
    else if (!state.session) readyNote = "서버에 연결한 뒤 IN 또는 OUT을 선택하세요.";
    else if (direction() === "out") readyNote = "내보낼 객체를 체크하고 시작하세요. 실행 시 스키마를 자동 검증합니다.";
    else if (state.object?.kind !== "U") readyNote = "목록에서 가져올 테이블 이름을 클릭하세요. 체크박스는 OUT 전용입니다.";
    else if (!state.columnsLoaded) readyNote = state.busy === "columns" ? "테이블 구조를 조회하는 중입니다." : "테이블 구조를 확인하지 못했습니다. 테이블 이름을 다시 클릭하세요.";
    else if (!state.file) readyNote = "파일 선택을 눌러 가져올 텍스트 파일을 선택하세요.";
    else readyNote = "선택한 테이블에 파일 데이터를 추가합니다. 시작 시 스키마와 전체 파일을 자동 검증합니다.";
    get("ase-ready-note").textContent = readyNote;
    get("ase-cancel").hidden = !active();
    get("ase-cancel").disabled = ["cancel", "disconnect"].includes(state.busy);
    get("ase-cancel").textContent = state.busy === "cancel" ? "취소 요청 중…" : "실행 취소";
    get("ase-browser").setAttribute("aria-busy", String(["owners", "objects", "columns"].includes(state.busy)));
    get("ase-job").setAttribute("aria-busy", String(active()));
    get("ase-connect-form").setAttribute("aria-busy", String(state.busy === "connect"));
    renderObjects();
    renderColumns();
  }

  async function perform(kind, work) {
    const generation = state.generation;
    state.busy = kind;
    render();
    try {
      await work(generation);
    } catch (error) {
      if (generation === state.generation && error.name !== "AbortError") feedback(error.message, true);
    } finally {
      if (generation === state.generation) {
        state.busy = "";
        render();
      }
    }
  }

  get("ase-connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.session || state.busy || !state.available || !event.currentTarget.reportValidity()) return;
    const profile = selectedProfile();
    const payload = profile ? { profile_id: profile.id } : {
      host: get("ase-host").value.trim(), port: Number(get("ase-port").value),
      user: get("ase-user").value.trim(), password: get("ase-password").value,
    };
    invalidateProfileRead();
    reset("connection");
    clearSecrets();
    const generation = state.generation;
    let defaultDatabase = "";
    await perform("connect", async (generation) => {
      const data = await api("connect", payload, generation);
      if (!data.session || !Array.isArray(data.databases)) {
        if (typeof data.session === "string") await releaseSession(data.session);
        throw new Error("연결 응답을 확인할 수 없습니다.");
      }
      state.session = data.session;
      state.databases = data.databases;
      clearSecrets();
      setChoices("ase-database", state.databases, "DB 선택", "name");
      defaultDatabase = typeof data.database === "string" ? data.database : profile?.database || "";
      showTimings("서버 연결", data);
      get("ase-connection-note").textContent = `${profile ? `${typeof data.profile_name === "string" ? data.profile_name : profile.name}에 ` : ""}연결되었습니다. DB를 선택하고 검색을 누르세요. 비밀번호는 입력창에서 지웠습니다.`;
      const refreshed = typeof data.profile_id === "string" ? await loadProfiles({ preferredId: data.profile_id, preserveSession: true }) : true;
      if (generation !== state.generation) throw stale();
      if (data.saved === false) feedback(`서버에 연결되었지만 연결 정보를 저장하지 못했습니다. ${typeof data.save_warning === "string" ? data.save_warning : "설정 파일의 쓰기 권한을 확인하세요."}`, true);
      else if (refreshed) feedback(data.saved === true ? "서버에 연결되었습니다. 다음 연결을 위해 암호화된 접속 정보를 저장했습니다." : "서버에 연결되었습니다.");
    });
    if (generation === state.generation && state.session && state.databases.some((database) => database.name === defaultDatabase)) {
      get("ase-database").value = defaultDatabase;
      render();
    }
  });

  for (const id of connectionIds) get(id).addEventListener("input", () => {
    if (state.session || state.busy || profileLocked()) return;
    invalidateProfileRead();
    if (id !== "ase-password") clearSecrets();
    reset("connection");
  });

  get("ase-profile").addEventListener("change", async () => {
    if (profileLocked()) {
      get("ase-profile").value = state.profileId;
      return;
    }
    const id = get("ase-profile").value;
    invalidateProfileRead();
    await clearConnection();
    applyProfile(id);
  });

  get("ase-profile-reload").addEventListener("click", async () => {
    if (profileLocked()) return;
    invalidateProfileRead();
    await clearConnection();
    await loadProfiles();
  });

  async function loadOwners() {
    if (locked() || state.busy || state.ownersLoaded) return;
    const database = get("ase-database").value;
    if (!database || !state.session) return;
    await perform("owners", async (generation) => {
      const data = await api("owners", { session: state.session, database }, generation);
      const selected = get("ase-owner").value;
      state.owners = Array.isArray(data.owners) ? data.owners : [];
      state.ownersLoaded = true;
      setChoices("ase-owner", state.owners, "전체 소유자", "id");
      get("ase-owner").value = state.owners.some((owner) => String(owner.id) === selected) ? selected : "";
      if (get("ase-owner").value !== selected) reset("search");
      showTimings("소유자 조회", data);
    });
  }

  get("ase-database").addEventListener("change", () => { if (!locked()) reset("database"); });
  for (const event of ["focus", "pointerdown"]) get("ase-owner").addEventListener(event, loadOwners);

  async function searchObjects() {
    if (locked() || state.busy || !state.session || !get("ase-database").value) return;
    if (!get("ase-search").reportValidity()) return;
    reset("search");
    const owner = state.owners.find((item) => String(item.id) === get("ase-owner").value);
    await perform("objects", async (generation) => {
      const data = await api("objects", { session: state.session, database: get("ase-database").value, owner_id: owner?.id ?? null, search: get("ase-search").value.trim(), kind: get("ase-kind").value }, generation);
      state.objects = Array.isArray(data.objects) ? data.objects : [];
      state.searched = true;
      if (!state.ownersLoaded) {
        const selected = get("ase-owner").value;
        const owners = new Map(state.owners.map((item) => [item.id, item]));
        for (const object of state.objects) owners.set(object.owner_id, { id: object.owner_id, name: object.owner });
        state.owners = [...owners.values()];
        setChoices("ase-owner", state.owners, "전체 소유자", "id");
        get("ase-owner").value = selected;
      }
      showTimings("객체 검색", data);
    });
  }

  get("ase-search-button").addEventListener("click", searchObjects);
  get("ase-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      searchObjects();
    }
  });
  get("ase-owner").addEventListener("change", () => { if (!locked()) reset("search"); });

  async function selectObject(object) {
    if (locked()) return;
    reset("object");
    state.object = object;
    await perform("columns", async (generation) => {
      const data = await api("columns", { session: state.session, object }, generation);
      state.columns = Array.isArray(data.columns) ? data.columns : [];
      state.columnsLoaded = true;
      showTimings("컬럼 조회", data);
      const body = get("ase-column-body");
      body.replaceChildren();
      for (const column of state.columns) {
        const row = document.createElement("tr");
        cell(row, column.position);
        cell(row, column.name);
        let type = String(column.type_name ?? column.type_id ?? "");
        if (["char", "varchar", "nchar", "nvarchar", "unichar", "univarchar", "binary", "varbinary"].includes(type.toLowerCase())) type += `(${column.length})`;
        if (["numeric", "decimal"].includes(type.toLowerCase())) type += `(${column.precision},${column.scale})`;
        cell(row, type);
        cell(row, column.nullable ? "허용" : "불가");
        cell(row, column.identity ? "예" : "—");
        body.append(row);
      }
    });
  }

  get("ase-kind").addEventListener("change", () => { if (!locked()) reset("search"); });
  get("ase-search").addEventListener("input", () => { if (!locked()) reset("search"); });
  for (const id of ["ase-out", "ase-in"]) get(id).addEventListener("change", () => {
    if (locked()) return;
    get("ase-keep-identity").checked = false;
    reset("mode");
  });
  for (const id of optionIds) get(id).addEventListener("input", () => { if (!locked()) reset("options"); });
  get("ase-crlf").addEventListener("click", () => {
    if (locked()) return;
    get("ase-row-terminator").value = "\\r\\n";
    reset("options");
  });

  get("ase-select-all").addEventListener("change", (event) => {
    if (locked() || direction() !== "out" || !state.bcpAvailable) return;
    const checked = event.target.checked;
    reset("options");
    for (const object of state.objects.filter((item) => ["U", "V"].includes(item.kind))) {
      if (checked) state.exports.add(objectKey(object));
      else state.exports.delete(objectKey(object));
    }
    render();
  });
  get("ase-clear-selection").addEventListener("click", () => {
    if (locked()) return;
    reset("options");
    state.exports.clear();
    render();
  });
  get("ase-pick-directory").addEventListener("click", async () => {
    if (!state.session || !state.bcpAvailable || locked() || state.busy) return;
    reset("options");
    await perform("directory", async (generation) => {
      const data = await api("pick-directory", { session: state.session }, generation);
      if (data.cancelled) { feedback("폴더 선택을 취소했습니다."); return; }
      if (typeof data.path !== "string" || !data.path) throw new Error("폴더 선택 결과를 확인할 수 없습니다.");
      state.exportDirectory = data.path;
    });
  });

  get("ase-pick-file").addEventListener("click", async () => {
    if (direction() !== "in" || !transferable() || locked() || state.busy) return;
    reset("file");
    await perform("picker", async (generation) => {
      const data = await api("pick-file", { session: state.session, direction: direction(), suggested_name: `${state.object.name}.txt` }, generation);
      if (data.cancelled) {
        feedback("파일 선택을 취소했습니다.");
        return;
      }
      if (!data.file || typeof data.path !== "string") throw new Error("파일 선택 결과를 확인할 수 없습니다.");
      state.file = data.file;
      state.path = data.path;
    });
  });

  function options() {
    return {
      direction: direction(), encoding: get("ase-encoding").value,
      field_terminator: get("ase-field-terminator").value, row_terminator: get("ase-row-terminator").value,
      batch_size: Number(get("ase-batch-size").value), keep_identity: direction() === "in" && get("ase-keep-identity").checked,
    };
  }

  function showStatistics(data) {
    clearStatistics();
    const result = data.result;
    const statistics = result?.statistics;
    if (data.state !== "succeeded" || result?.direction !== "out" || !statistics) return;
    const { elapsed_seconds: elapsed, rows_per_second: rate, bytes_written: bytes } = statistics;
    if (!Number.isSafeInteger(result.rows) || result.rows < 0 || !Number.isFinite(elapsed) || elapsed < 0 || !Number.isSafeInteger(bytes) || bytes < 0 || (rate !== null && (!Number.isFinite(rate) || rate < 0))) return;
    const number = (value, digits = 2) => value.toLocaleString("ko-KR", { maximumFractionDigits: digits });
    get("ase-stat-rows").textContent = `${number(result.rows, 0)}행`;
    get("ase-stat-elapsed").textContent = elapsed > 0 && elapsed < 0.01 ? "0.01초 미만" : `${elapsed.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}초`;
    get("ase-stat-rate").textContent = rate === null ? "측정 불가" : rate > 0 && rate < 0.01 ? "0.01행/초 미만" : `${number(rate)}행/초`;
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    const unit = bytes === 0 ? 0 : Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    get("ase-stat-bytes").textContent = `${number(bytes / (1024 ** unit))} ${units[unit]}`;
    get("ase-stat-bytes-exact").textContent = `${number(bytes, 0)} 바이트`;
    const bcpSeconds = statistics.bcp_seconds ?? result.bcp_seconds;
    const bcpRate = statistics.bcp_rows_per_second ?? result.bcp_rows_per_second;
    if (Number.isFinite(bcpSeconds) && bcpSeconds >= 0 && Number.isFinite(bcpRate) && bcpRate >= 0) {
      const duration = bcpSeconds > 0 && bcpSeconds < 0.01 ? "0.01초 미만" : `${bcpSeconds.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}초`;
      const speed = bcpRate > 0 && bcpRate < 0.01 ? "0.01행/초 미만" : `${number(bcpRate)}행/초`;
      get("ase-bcp-statistics").textContent = `bcp.exe 실행 시간 ${duration} · bcp.exe 실행 속도 ${speed} (프로세스 시작·로그인 포함)`;
      get("ase-bcp-statistics").hidden = false;
    }
    get("ase-out-statistics").hidden = false;
  }

  function showJob(data) {
    state.jobStatus = data;
    showStatistics(data);
    get("ase-job").hidden = false;
    const names = { queued: "대기 중", validating: "검증 중", running: "실행 중", succeeded: "완료", failed: "실패", cancelled: "취소됨" };
    const rows = Number.isFinite(data.rows) ? ` · ${data.rows.toLocaleString()}행` : "";
    get("ase-job-status").textContent = `${names[data.state] || "실행 중"}${rows}${data.message ? ` · ${data.message}` : ""}`;
    get("ase-job-status").dataset.state = data.state;
    const events = get("ase-job-events");
    events.replaceChildren();
    if (Array.isArray(data.events)) for (const event of data.events.slice(-100)) {
      const item = document.createElement("li");
      item.textContent = String(event.message ?? "");
      events.append(item);
    }
    const entries = Array.isArray(data.items) ? data.items : direction() === "in" && state.object ? [{
      object: state.object, path: data.result?.path || state.path,
      state: data.state, phase: data.phase, message: data.message,
      rows: data.result?.rows ?? data.rows,
    }] : [];
    if (entries.length) {
      const count = (status) => entries.filter((entry) => status.includes(entry.state)).length;
      get("ase-job-status").textContent += ` · 전체 ${entries.length}개 · 완료 ${count(["succeeded"])} · 실행 ${count(["running"])} · 대기 ${count(["queued"])} · 실패 ${count(["failed"])}`;
    }
    const items = get("ase-job-items");
    items.replaceChildren();
    for (const entry of entries) {
      const row = document.createElement("tr");
      const object = entry.object || {};
      cell(row, [object.database, object.owner, object.name].filter(Boolean).join("."));
      cell(row, names[entry.state] || entry.state || "대기 중");
      cell(row, typeof entry.path === "string" ? entry.path : "—");
      cell(row, Number.isFinite(entry.rows) ? `${entry.rows.toLocaleString()}행` : "—");
      const detail = cell(row, entry.message || "");
      const statistics = entry.statistics || entry.result?.statistics;
      if (entry.state === "succeeded" && statistics) {
        const parts = [];
        const measured = (value, unit) => value > 0 && value < 0.01 ? `0.01${unit} 미만` : `${value.toLocaleString("ko-KR", { maximumFractionDigits: 2 })}${unit}`;
        if (Number.isFinite(statistics.elapsed_seconds) && statistics.elapsed_seconds >= 0) parts.push(measured(statistics.elapsed_seconds, "초"));
        if (statistics.rows_per_second === null) parts.push("속도 측정 불가");
        else if (Number.isFinite(statistics.rows_per_second) && statistics.rows_per_second >= 0) parts.push(measured(statistics.rows_per_second, "행/초"));
        if (Number.isSafeInteger(statistics.bytes_written) && statistics.bytes_written >= 0) parts.push(`${statistics.bytes_written.toLocaleString()} 바이트`);
        if (Number.isFinite(statistics.bcp_seconds) && statistics.bcp_seconds >= 0) parts.push(`bcp.exe 실행 ${measured(statistics.bcp_seconds, "초")} (시작·로그인 포함)`);
        if (statistics.bcp_rows_per_second === null) parts.push("bcp.exe 속도 측정 불가");
        else if (Number.isFinite(statistics.bcp_rows_per_second) && statistics.bcp_rows_per_second >= 0) parts.push(`bcp.exe ${measured(statistics.bcp_rows_per_second, "행/초")}`);
        if (parts.length) {
          const metrics = document.createElement("p");
          metrics.className = "ase-item-statistics";
          metrics.textContent = parts.join(" · ");
          detail.append(metrics);
        }
      }
      items.append(row);
    }
    get("ase-job-items-scroll").hidden = !entries.length;
    if (data.phase === "validating" && !["succeeded", "failed", "cancelled"].includes(data.state)) get("ase-job-status").textContent += " · 자동 검증 중";
    const terminal = ["succeeded", "failed", "cancelled"].includes(data.state);
    const partial = direction() === "in" && ["failed", "cancelled"].includes(data.state);
    get("ase-job-warning").hidden = !partial;
    get("ase-job-warning").textContent = "완료된 배치는 테이블에 남습니다. 같은 파일을 재실행하면 중복 행이 생길 수 있으니 DB를 확인하세요.";
    if (terminal) {
      state.job = null;
      clearTimeout(state.timer);
      state.timer = null;
    }
    render();
    return terminal;
  }

  function schedulePoll(generation = state.generation) {
    clearTimeout(state.timer);
    if (state.job && generation === state.generation) state.timer = setTimeout(() => poll(generation), 1000);
  }

  async function poll(generation) {
    if (!state.job || generation !== state.generation) return;
    try {
      const data = await api("job", { session: state.session, job: state.job }, generation);
      if (!showJob(data)) schedulePoll(generation);
    } catch (error) {
      if (generation !== state.generation || error.name === "AbortError") return;
      feedback(`${error.message} 실행 상태를 다시 확인하는 중입니다.`, true);
      schedulePoll(generation);
    }
  }

  get("ase-start").addEventListener("click", async () => {
    if (!transferable() || (direction() === "in" && !state.file) || locked() || state.busy) return;
    for (const id of ["ase-field-terminator", "ase-row-terminator"]) {
      if (!get(id).value) {
        feedback("컬럼 구분자와 행 구분자를 입력하세요.", true);
        get(id).focus();
        return;
      }
    }
    if (direction() === "in" && (!get("ase-batch-size").checkValidity() || !Number.isInteger(Number(get("ase-batch-size").value)))) {
      feedback("배치 행 수를 1~1,000,000 사이의 정수로 입력하세요.", true);
      get("ase-batch-size").focus();
      return;
    }
    const payload = { session: state.session, options: options(), ...(direction() === "out" ? { objects: exportObjects(), concurrency: Number(get("ase-concurrency").value) } : { object: state.object, file: state.file }) };
    clearStatistics();
    await perform("start", async (generation) => {
      const data = await api("jobs", payload, generation);
      if (!data.job) throw new Error("실행 번호를 받지 못했습니다.");
      state.job = data.job;
      showJob({ state: "running", phase: "validating", message: "실행 조건을 자동 검증하는 중입니다.", events: [] });
      schedulePoll(generation);
    });
  });

  get("ase-cancel").addEventListener("click", async () => {
    if (!state.job || ["cancel", "disconnect"].includes(state.busy)) return;
    nextGeneration();
    await perform("cancel", async (generation) => {
      const data = await api("cancel", { session: state.session, job: state.job }, generation);
      showJob(data);
    });
    schedulePoll();
  });

  get("ase-disconnect").addEventListener("click", async () => {
    if (!state.session || state.busy === "disconnect") return;
    const session = state.session;
    const job = state.job;
    const wasIn = direction() === "in";
    reset("connection");
    clearSecrets();
    get("ase-connection-note").textContent = "저장된 서버를 선택하면 비밀번호를 다시 입력하지 않고 연결할 수 있습니다.";
    await perform("disconnect", async (generation) => {
      if (job) {
        try { await api("cancel", { session, job }, generation); }
        catch (error) { if (error.name === "AbortError") throw error; }
      }
      await api("disconnect", { session }, generation);
      feedback(job && wasIn ? "실행을 취소하고 연결을 해제했습니다. 완료된 배치는 남을 수 있으며 재실행하면 중복 행이 생길 수 있습니다." : "연결을 해제했습니다.");
    });
  });

  async function checkRuntime() {
    try {
      const response = await fetch("/api/ase/status", { method: "GET", headers: { "Content-Type": "application/json", "X-Nice-Tools": "ase", "Cache-Control": "no-store" }, cache: "no-store", credentials: "same-origin" });
      const data = await response.json();
      state.available = response.ok && data.available === true;
      state.bcpAvailable = response.ok && data.bcp_available === true;
      if (typeof data.export_directory === "string") {
        state.defaultDirectory = data.export_directory;
        if (!state.session) state.exportDirectory = data.export_directory;
      }
      get("ase-runtime").textContent = state.available ? "SAP ASE ODBC 사용 가능 · 연결을 유지하며 DB 객체를 조회합니다." : typeof data.detail === "string" ? data.detail : "SAP ASE 64비트 ODBC 드라이버를 찾을 수 없습니다. 드라이버를 설치하거나 ase-connections.txt의 [odbc]에 드라이버 이름을 지정하세요.";
      showOdbcDriver(data.odbc);
      showClientPaths(data);
      get("ase-runtime").dataset.tone = state.available ? "info" : "error";
      get("ase-bcp-runtime").hidden = state.bcpAvailable;
      get("ase-bcp-runtime").dataset.tone = "error";
      get("ase-bcp-runtime").textContent = typeof data.bcp_detail === "string" && data.bcp_detail !== "ready" ? data.bcp_detail : "SAP bcp.exe를 찾을 수 없습니다. DB 객체 조회는 사용할 수 있으며, 파일 전송에는 SAP bcp 설치 또는 경로 설정이 필요합니다.";
    } catch {
      state.available = false;
      state.bcpAvailable = false;
      get("ase-runtime").textContent = "SAP ASE ODBC 실행 환경을 확인하지 못했습니다. 프로그램을 다시 실행하세요.";
      get("ase-runtime").dataset.tone = "error";
    }
    render();
  }

  render();
  checkRuntime();
  loadProfiles();
})();
