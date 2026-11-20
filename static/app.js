const FIXED_LOCATIONS = [
  "Mestrado",
  "Doutorado",
  "Doc. P. e D.",
  "Sala do Servidor",
  "Sala de Reuniões",
];

const LEGACY_LOCATION_MAP = {
  "Sala de reunião": "Sala de Reuniões",
  "Sala do servidor e equipamentos": "Sala do Servidor",
};

function renderLocationSelect(name, currentValue) {
  const raw = (currentValue || "").trim();
  const normalized = LEGACY_LOCATION_MAP[raw] || raw;
  const extraOption =
    normalized && !FIXED_LOCATIONS.includes(normalized)
      ? `<option value="${esc(normalized)}" selected>${esc(normalized)} (atual)</option>`
      : "";
  return `
    <select class="select" name="${esc(name)}">
      <option value="">-- Selecione --</option>
      ${FIXED_LOCATIONS.map(
        (loc) => `<option value="${esc(loc)}" ${normalized === loc ? "selected" : ""}>${esc(loc)}</option>`
      ).join("")}
      ${extraOption}
    </select>
  `;
}

const ADMIN_TAB_LABELS = {
  dashboard: "Dashboard",
  computadores: "Computadores",
  mapa: "Mapa do LSEE",
  simulador: "Simulador",
  criterios: "Critérios",
  pessoas: "Pessoas",
  alocacoes: "Alocações",
  monitores: "Monitores",
  patrimonios: "Patrimônios",
  etiquetas: "Etiquetas",
  rede: "Rede",
  solicitacoes: "Solicitações",
  parametros: "Parâmetros & Log",
};

const state = {
  isAdmin: false,
  needsBootstrap: false,
  currentUser: null,
  publicResponsibles: [],
  activeTab: "dashboard",
  collab: {
    online_admins: [],
    other_online_count: 0,
    messages: [],
    latest_message_id: 0,
    lastSeenMessageId: 0,
    initialized: false,
    unread: 0,
    chatOpen: false,
    timer: null,
    pollingBusy: false,
    lastPollAt: 0,
    pending_dismissals: [],
    shownDismissalIds: [],
  },
  simPc: "",
  compFilters: { search: "", status: "", tipo: "", pendencia: "" },
  peopleFilters: { search: "", situacao: "" },
  alocOnlyActive: true,
  patrimonyFilters: { search: "", responsavel: "", origem: "", local: "" },
  redeFilters: { search: "", categoria: "" },
  tableSorts: {},
  mapFilters: { search: "", room: "", mode: "" },
  mapShowPcs: true,
  mapPrintMode: false,
  mapZoom: "100",
  mapSelectedStation: "D01",
  mapSwapSource: "",
  mapData: null,
  labels: {
    mode: "pc",
    selectedPcId: "",
    selectedMonitorId: "",
    draft: null,
    quantity: 1,
    skipFilled: true,
    autoIncrement: false,
    activePage: 0,
    selectedSlot: 0,
    pages: [Array(18).fill(null)],
  },
  requests: {
    activeId: null,
    activeCreatedAt: "",
    mode: "auto",
    dirty: false,
    hydrated: false,
    orientador: "",
    laboratorio: "LSEE",
    data_solicitacao: new Date().toISOString().slice(0, 10),
    blankDate: false,
    items: [
      { material: "", quantidade: "", valor_unitario: "", patrimonio: "", valor: "", adicionado_em: "" },
      { material: "", quantidade: "", valor_unitario: "", patrimonio: "", valor: "", adicionado_em: "" },
      { material: "", quantidade: "", valor_unitario: "", patrimonio: "", valor: "", adicionado_em: "" },
    ],
    justificativa: "",
    equipmentOptions: [],
  },
  caches: {
    computers: [],
    processors: [],
    people: [],
    allocations: { items: [], people: [], pcs: [] },
    monitors: { items: [], pcs: [] },
    patrimonies: [],
    network: [],
  },
};

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtNum(val, suffix = "") {
  if (val === null || val === undefined || val === "") return "—";
  return `${esc(val)}${suffix}`;
}

const collatorNat = new Intl.Collator("pt-BR", { numeric: true, sensitivity: "base" });

const PC_STATUS_SORT_ORDER = {
  "Em uso": 0,
  Livre: 1,
  "Manutenção": 2,
  "Indisponível": 3,
  Despatrimoniado: 4,
};

function isEmptySortVal(val) {
  if (val === null || val === undefined) return true;
  if (typeof val === "string") {
    const trimmed = val.trim();
    return trimmed === "" || trimmed === "—";
  }
  return false;
}

function toSortableNumber(val) {
  if (isEmptySortVal(val)) return null;
  if (typeof val === "number") return Number.isFinite(val) ? val : null;
  const normalized = String(val).trim().replace(",", ".");
  if (!normalized) return null;
  const num = Number(normalized);
  return Number.isFinite(num) ? num : null;
}

function parseEntradaCode(val) {
  if (isEmptySortVal(val)) return null;
  const m = /^(\d{4})\s*\/\s*(\d+)$/.exec(String(val).trim());
  if (!m) return null;
  return Number(m[1]) * 10 + Number(m[2]);
}

function compareIpv4(a, b) {
  const pa = String(a).trim().split(".");
  const pb = String(b).trim().split(".");
  if (pa.length === 4 && pb.length === 4 && [...pa, ...pb].every((p) => /^\d+$/.test(p))) {
    for (let i = 0; i < 4; i += 1) {
      const diff = Number(pa[i]) - Number(pb[i]);
      if (diff !== 0) return diff;
    }
    return 0;
  }
  return collatorNat.compare(String(a), String(b));
}

const SORT_COMPARATORS = {
  text: (a, b) => collatorNat.compare(String(a), String(b)),
  natural: (a, b) => collatorNat.compare(String(a), String(b)),
  number: (a, b) => toSortableNumber(a) - toSortableNumber(b),
  bool: (a, b) => Number(Boolean(a)) - Number(Boolean(b)),
  entrada: (a, b) => {
    const na = parseEntradaCode(a);
    const nb = parseEntradaCode(b);
    if (na !== null && nb !== null) return na - nb;
    return collatorNat.compare(String(a), String(b));
  },
  ip: compareIpv4,
  pcStatus: (a, b) => {
    const ra = PC_STATUS_SORT_ORDER[String(a).trim()];
    const rb = PC_STATUS_SORT_ORDER[String(b).trim()];
    if (ra !== undefined && rb !== undefined && ra !== rb) return ra - rb;
    if (ra !== undefined && rb === undefined) return -1;
    if (ra === undefined && rb !== undefined) return 1;
    return collatorNat.compare(String(a), String(b));
  },
};

function isEmptyForType(val, type) {
  if (type === "bool") return val === null || val === undefined;
  if (type === "number") return toSortableNumber(val) === null;
  if (type === "entrada") return parseEntradaCode(val) === null && isEmptySortVal(val);
  return isEmptySortVal(val);
}

function applyTableSort(tableKey, rows, columns) {
  const list = Array.isArray(rows) ? [...rows] : [];
  const sort = state.tableSorts[tableKey];
  if (!sort || !sort.key) return list;
  const col = columns[sort.key];
  if (!col) return list;
  const type = col.type || "text";
  const cmp = SORT_COMPARATORS[type] || SORT_COMPARATORS.text;
  const getter = col.get || ((row) => row?.[sort.key]);
  const dir = sort.dir === "desc" ? -1 : 1;

  return list
    .map((row, idx) => ({ row, idx, val: getter(row) }))
    .sort((a, b) => {
      const aEmpty = isEmptyForType(a.val, type);
      const bEmpty = isEmptyForType(b.val, type);
      if (aEmpty && bEmpty) return a.idx - b.idx;
      if (aEmpty) return 1;
      if (bEmpty) return -1;
      const res = cmp(a.val, b.val);
      if (res !== 0) return res * dir;
      return a.idx - b.idx;
    })
    .map((entry) => entry.row);
}

function sortableTh(tableKey, colKey, label, extraHtml = "") {
  const current = state.tableSorts[tableKey];
  const isActive = Boolean(current && current.key === colKey);
  const arrow = isActive ? (current.dir === "asc" ? "▲" : "▼") : "⇅";
  const ariaSort = isActive ? (current.dir === "asc" ? "ascending" : "descending") : "none";
  const title = isActive
    ? current.dir === "asc"
      ? `Ordenar ${label} em ordem decrescente`
      : `Remover ordenação de ${label}`
    : `Ordenar ${label} em ordem crescente`;
  return `<th aria-sort="${ariaSort}"><span class="th-sort-wrap"><button class="sort-btn${isActive ? " is-active" : ""}" type="button" data-sort-table="${esc(tableKey)}" data-sort-col="${esc(colKey)}" title="${esc(title)}"><span>${esc(label)}</span><span class="sort-arrow" aria-hidden="true">${arrow}</span></button>${extraHtml}</span></th>`;
}

async function toggleTableSort(tableKey, colKey) {
  const prev = state.tableSorts[tableKey];
  if (!prev || prev.key !== colKey) {
    state.tableSorts[tableKey] = { key: colKey, dir: "asc" };
  } else if (prev.dir === "asc") {
    state.tableSorts[tableKey] = { key: colKey, dir: "desc" };
  } else {
    delete state.tableSorts[tableKey];
  }

  const refreshers = {
    dash_logs: renderDashboard,
    dash_top: renderDashboard,
    dash_sem_pc: renderDashboard,
    dash_fila: renderDashboard,
    computadores: renderComputers,
    sim_rows: renderSimulator,
    sim_decisions: renderSimulator,
    crit_cats: renderCriterios,
    crit_ram: renderCriterios,
    crit_arm: renderCriterios,
    crit_procs: renderCriterios,
    pessoas: renderPeople,
    alocacoes: renderAllocations,
    monitores: renderMonitors,
    patrimonios: () => renderPatrimonies(true),
    rede: renderNetwork,
    audit_logs: renderParamsAndAudit,
  };
  const refresh = refreshers[tableKey];
  if (!refresh) return;
  await refresh();
  const escSel = typeof CSS !== "undefined" && CSS.escape ? CSS.escape : (s) => s;
  const btn = document.querySelector(`.sort-btn[data-sort-table="${escSel(tableKey)}"][data-sort-col="${escSel(colKey)}"]`);
  if (btn) btn.focus({ preventScroll: true });
}

function statusBadge(status) {
  const s = (status || "").trim();
  if (!s) return '<span class="badge badge-muted">—</span>';
  if (s === "Livre") return '<span class="badge badge-livre">Livre</span>';
  if (s === "Em uso") return '<span class="badge badge-uso">Em uso</span>';
  if (s === "Manutenção") return '<span class="badge badge-warn">Manutenção</span>';
  if (s === "Indisponível" || s === "Despatrimoniado") {
    return `<span class="badge badge-danger">${esc(s)}</span>`;
  }
  return `<span class="badge badge-muted">${esc(s)}</span>`;
}

function simResultBadge(res) {
  if (res === "ELEGÍVEL") return '<span class="badge badge-elegivel">ELEGÍVEL</span>';
  if (res === "SEM PC - APÓS UPGRADES") return '<span class="badge badge-sempc">SEM PC - APÓS UPGRADES</span>';
  if (res === "PRIORIDADE INCOMPLETA") return '<span class="badge badge-warn">PRIORIDADE INCOMPLETA</span>';
  return `<span class="badge badge-muted">${esc(res)}</span>`;
}

function showToast(msg, isError = false) {
  const area = document.getElementById("toast-area");
  const el = document.createElement("div");
  el.className = `toast${isError ? " error" : ""}`;
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

const API_CACHE_TTL_MS = 60_000;
const API_CACHE_PREFIX = "lsee_api_v1:";
const apiMemCache = new Map();
const apiInflight = new Map();
let activeLoaderCount = 0;

function syncPageLoaderBar(forceLoading = false) {
  const bar = document.getElementById("page-loader-bar");
  if (!bar) return;
  bar.classList.toggle("is-loading", forceLoading || activeLoaderCount > 0);
}

function startPageLoader() {
  activeLoaderCount += 1;
  syncPageLoaderBar();
}

function stopPageLoader() {
  activeLoaderCount = Math.max(0, activeLoaderCount - 1);
  syncPageLoaderBar();
}

function buildTabLoadingHtml(tabName) {
  const label = ADMIN_TAB_LABELS[tabName] || "página";
  return `
    <div class="tab-loading-state" role="status" aria-live="polite">
      <div class="lsee-spinner" aria-hidden="true"></div>
      <p class="tab-loading-title">Carregando ${esc(label)}...</p>
      <p class="tab-loading-sub">Buscando dados atualizados do laboratório</p>
      <div class="tab-loading-skeleton" aria-hidden="true">
        <span class="skeleton-line w-80"></span>
        <span class="skeleton-line w-60"></span>
        <span class="skeleton-line w-70"></span>
      </div>
    </div>
  `;
}

function getApiCacheKey(url) {
  if (url === "/api/auth/status") {
    return `${API_CACHE_PREFIX}any:${url}`;
  }
  return `${API_CACHE_PREFIX}${state.isAdmin ? "adm" : "pub"}:${url}`;
}

function cloneJsonSafe(val) {
  if (typeof structuredClone === "function") {
    return structuredClone(val);
  }
  return JSON.parse(JSON.stringify(val));
}

function readApiCache(key) {
  const now = Date.now();
  const mem = apiMemCache.get(key);
  if (mem) {
    if (now - mem.ts < API_CACHE_TTL_MS) {
      return cloneJsonSafe(mem.data);
    }
    apiMemCache.delete(key);
  }
  try {
    if (typeof sessionStorage !== "undefined") {
      const raw = sessionStorage.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed.ts === "number" && now - parsed.ts < API_CACHE_TTL_MS) {
          apiMemCache.set(key, parsed);
          return cloneJsonSafe(parsed.data);
        }
        sessionStorage.removeItem(key);
      }
    }
  } catch {
    // ignora restrições de storage
  }
  return null;
}

function writeApiCache(key, data) {
  const entry = { ts: Date.now(), data };
  apiMemCache.set(key, entry);
  try {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.setItem(key, JSON.stringify(entry));
    }
  } catch {
    // ignora quota excedida
  }
}

function invalidateApiCache() {
  apiMemCache.clear();
  try {
    if (typeof sessionStorage !== "undefined") {
      const toRemove = [];
      for (let i = 0; i < sessionStorage.length; i += 1) {
        const k = sessionStorage.key(i);
        if (k && k.startsWith(API_CACHE_PREFIX)) {
          toRemove.push(k);
        }
      }
      toRemove.forEach((k) => sessionStorage.removeItem(k));
    }
  } catch {
    // ignora restrições de storage
  }
}

async function apiFetch(url, options = {}) {
  const { skipCache = false, headers: customHeaders, ...fetchOpts } = options;
  const method = String(fetchOpts.method || "GET").toUpperCase();
  const isCacheableGet = method === "GET" && !skipCache && !url.startsWith("/api/admin/collab");
  const cacheKey = isCacheableGet ? getApiCacheKey(url) : "";

  if (isCacheableGet) {
    const cached = readApiCache(cacheKey);
    if (cached !== null) {
      return cached;
    }
    if (apiInflight.has(cacheKey)) {
      const shared = await apiInflight.get(cacheKey);
      return cloneJsonSafe(shared);
    }
  }

  const opts = {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(customHeaders || {}) },
    ...fetchOpts,
  };
  if (method === "GET" && skipCache) {
    opts.cache = "no-cache";
  }

  const reqPromise = (async () => {
    const resp = await fetch(url, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(data.error || `Erro HTTP ${resp.status}`);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    if (isCacheableGet) {
      writeApiCache(cacheKey, data);
    } else if (method !== "GET" && !url.startsWith("/api/admin/collab/")) {
      invalidateApiCache();
    }
    return data;
  })();

  if (isCacheableGet) {
    apiInflight.set(cacheKey, reqPromise);
    try {
      const result = await reqPromise;
      return cloneJsonSafe(result);
    } finally {
      apiInflight.delete(cacheKey);
    }
  }
  return reqPromise;
}

async function apiDownload(url, payload = null, fallbackFilename = "download") {
  const opts = {
    method: payload === null ? "GET" : "POST",
    credentials: "same-origin",
  };
  if (payload !== null) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(payload);
  }
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `Erro HTTP ${resp.status}`);
  }
  const blob = await resp.blob();
  const disp = resp.headers.get("Content-Disposition") || "";
  const match = /filename="([^"]+)"/i.exec(disp);
  const filename = match ? match[1] : fallbackFilename;
  const link = document.createElement("a");
  const blobUrl = URL.createObjectURL(blob);
  link.href = blobUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 2500);
}

function openModal(title, htmlContent, wide = false) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = htmlContent;
  document.querySelector("#modal-backdrop .modal")?.classList.toggle("modal-wide", wide);
  document.getElementById("modal-backdrop").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal-backdrop").classList.add("hidden");
  document.querySelector("#modal-backdrop .modal")?.classList.remove("modal-wide");
  document.getElementById("modal-body").innerHTML = "";
}

function scoreInfoButton(title = "Explicação simplificada do cálculo do Score") {
  return `<button class="info-icon-btn js-score-info" type="button" title="${esc(title)}" aria-label="${esc(title)}">i</button>`;
}

async function openScoreInfoModal() {
  openModal("Como o Score dos PCs é calculado", '<p class="muted">Carregando detalhes do cálculo...</p>');
  try {
    const info = await apiFetch("/api/public/score-info");
    const pCpu = (info.pesos.cpu * 100).toFixed(1).replace(".0", "");
    const pRam = (info.pesos.ram * 100).toFixed(1).replace(".0", "");
    const pArm = (info.pesos.armazenamento * 100).toFixed(1).replace(".0", "");
    const pGpu = (info.pesos.gpu * 100).toFixed(1).replace(".0", "");
    const pesoHdPct = Math.round(info.armaz_peso_hd * 100);
    const refPcs = (info.arm_ref_pcs || []).length ? ` (${info.arm_ref_pcs.join(", ")})` : "";

    document.getElementById("modal-body").innerHTML = `
      <p class="help-intro" style="margin-bottom:12px;">
        Cada computador recebe uma nota final de <strong>0 a 100</strong>, formada pela média ponderada de 4 partes (cada parte vai de 0 a 10):
      </p>
      <ul class="score-summary-list">
        <li>
          <strong>Processador (${esc(pCpu)}%):</strong> compara a velocidade do processador com o melhor PC ativo do laboratório, incluindo a Sala do Servidor / RTDS (que recebe nota 10).
        </li>
        <li>
          <strong>Memória RAM (${esc(pRam)}%):</strong> usa faixas por capacidade instalada (ex.: 8 GB = 5, 16 GB = 8, 32 GB = 9, 64 GB = 10).
        </li>
        <li class="is-highlight">
          <strong>Armazenamento (${esc(pArm)}%):</strong>
          junta SSD e HD em uma <strong>Capacidade Equivalente</strong> onde o HD vale <strong>${esc(pesoHdPct)}%</strong> do SSD
          (<span class="mono">GB_eq = SSD + ${esc(info.armaz_peso_hd)} × HD</span>).
          Essa soma segue uma curva suave até a nota <strong>10</strong>, hoje definida pelo melhor armazenamento entre os PCs ativos (incluindo Sala do Servidor / RTDS):
          <strong class="mono">${esc(info.arm_ref)} GB eq.</strong>${esc(refPcs)}.
          <br /><span class="muted">Assim, ter só um SSD pequeno + HD comum não dá nota 10 fácil, e SSDs grandes (como 1 TB) não ficam atrás de combos menores.</span>
        </li>
        <li>
          <strong>Placa de Vídeo / GPU (${esc(pGpu)}%):</strong> recebe <strong>10</strong> se tiver GPU dedicada ou <strong>0</strong> caso contrário.
        </li>
      </ul>

      <div class="card" style="margin-bottom:12px;">
        <div class="card-header"><h3 class="card-title" style="font-size:0.9rem;">Exemplos práticos na nota de Armazenamento (0 a 10)</h3></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Configuração</th><th>Capacidade Equivalente</th><th>Nota Armazenamento</th></tr></thead>
            <tbody>
              ${(info.arm_examples || []).map((ex) => `
                <tr>
                  <td>${esc(ex.label)}</td>
                  <td class="mono">${esc(ex.armaz_eq)} GB eq.</td>
                  <td><strong>${esc(ex.score)}</strong></td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>

      <p class="muted" style="font-size:0.8rem;margin:0 0 12px;">
        Se algum componente essencial (CPU, RAM, disco ou GPU) ainda não estiver cadastrado, o Score Total não é estimado e o PC aparece pendente para o administrador.
      </p>
      <div class="form-actions" style="margin-top:0;">
        <button class="btn btn-primary" type="button" onclick="closeModal()">Fechar</button>
      </div>
    `;
  } catch (err) {
    document.getElementById("modal-body").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

async function openUsageRulesModal() {
  openModal("Regras Internas de Uso dos Computadores", '<p class="muted">Carregando regras...</p>', true);
  try {
    const data = await apiFetch("/api/public/usage-rules");
    let currentContent = data.content || "";

    const renderView = () => {
      document.getElementById("modal-body").innerHTML = `
        <div class="rules-text-view">${esc(currentContent)}</div>
        <div class="form-actions">
          ${state.isAdmin ? '<button id="btn-edit-usage-rules" class="btn btn-secondary" type="button">Editar texto</button>' : ""}
          <button class="btn btn-primary" type="button" onclick="closeModal()">Fechar</button>
        </div>
      `;
      document.getElementById("btn-edit-usage-rules")?.addEventListener("click", renderEditor);
    };

    const renderEditor = () => {
      document.getElementById("modal-body").innerHTML = `
        <form id="form-usage-rules">
          <div class="form-field">
            <label for="usage-rules-textarea">Texto exibido para todos os usuários (Visão Pública e Admin)</label>
            <textarea id="usage-rules-textarea" class="input" name="content" rows="12" style="width:100%;resize:vertical;" required>${esc(currentContent)}</textarea>
          </div>
          <div class="form-actions">
            <button id="btn-cancel-usage-rules" class="btn btn-secondary" type="button">Cancelar</button>
            <button class="btn btn-primary" type="submit">Salvar alterações</button>
          </div>
        </form>
      `;
      document.getElementById("btn-cancel-usage-rules").addEventListener("click", renderView);
      document.getElementById("form-usage-rules").addEventListener("submit", async (e) => {
        e.preventDefault();
        const content = new FormData(e.target).get("content") || "";
        try {
          const saved = await apiFetch("/api/admin/usage-rules", {
            method: "PUT",
            body: JSON.stringify({ content }),
          });
          currentContent = saved.content || String(content).trim();
          showToast("Regras de uso atualizadas!");
          renderView();
        } catch (err) {
          showToast(err.message, true);
        }
      });
    };

    renderView();
  } catch (err) {
    document.getElementById("modal-body").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

async function openReleaseTutorialModal() {
  openModal("Tutorial de Liberação de PC (Modo Admin)", '<p class="muted">Carregando tutorial...</p>', true);
  try {
    const data = await apiFetch("/api/admin/release-tutorial");
    const steps = data.steps || [];
    document.getElementById("modal-body").innerHTML = `
      <p class="help-intro">
        Checklist padrão para limpar e preparar um computador do LSEE antes de entregá-lo ao próximo usuário:
      </p>
      <div class="tutorial-steps-grid">
        ${steps.map((st) => `
          <div class="tutorial-step-card">
            <h3>${esc(st.title)}</h3>
            <ul>
              ${(st.details || []).map((d) => `<li>${esc(d)}</li>`).join("")}
            </ul>
            ${st.command ? `
              <div class="tutorial-cmd-row">
                <code>${esc(st.command)}</code>
                <button class="btn btn-sm btn-secondary js-copy-cmd" data-cmd="${esc(st.command)}" type="button">Copiar</button>
              </div>
            ` : ""}
          </div>
        `).join("")}
      </div>
      <div class="form-actions">
        <button class="btn btn-primary" type="button" onclick="closeModal()">Fechar</button>
      </div>
    `;
    document.querySelectorAll(".js-copy-cmd").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const cmd = btn.dataset.cmd || "";
        try {
          await navigator.clipboard.writeText(cmd);
          showToast("Comando copiado!");
        } catch {
          showToast(`Copie manualmente: ${cmd}`);
        }
      });
    });
  } catch (err) {
    document.getElementById("modal-body").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function fmtTimeLocalShort(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

function fmtDateTimeLocalShort(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
    return d.toLocaleString("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return String(iso).slice(0, 16);
  }
}

function fmtDateLocalBr(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
    return d.toLocaleDateString("pt-BR");
  } catch {
    return String(iso).slice(0, 10);
  }
}

function applyAuthDismissals(list) {
  state.collab.pending_dismissals = Array.isArray(list) ? list : [];
  renderCollabUI();
}

function renderCollabUI() {
  const pill = document.getElementById("admin-presence-pill");
  const dismissalBanner = document.getElementById("pending-dismissal-banner");
  const widget = document.getElementById("admin-chat-widget");
  const panel = document.getElementById("admin-chat-panel");
  const onlineCountEl = document.getElementById("admin-chat-online-count");
  const toggleMetaEl = document.getElementById("admin-chat-toggle-meta");
  const unreadEl = document.getElementById("admin-chat-unread");
  const onlineListEl = document.getElementById("admin-chat-online-list");
  const msgsEl = document.getElementById("admin-chat-messages");

  if (!state.isAdmin) {
    pill?.classList.add("hidden");
    dismissalBanner?.classList.add("hidden");
    if (dismissalBanner) dismissalBanner.innerHTML = "";
    widget?.classList.add("hidden");
    panel?.classList.add("hidden");
    return;
  }

  widget?.classList.remove("hidden");
  panel?.classList.toggle("hidden", !state.collab.chatOpen);

  const online = state.collab.online_admins || [];
  const others = online.filter((a) => !a.is_self);

  if (pill) {
    pill.classList.remove("hidden");
    if (others.length > 0) {
      const names = others
        .map((a) => `${a.nome} · ${ADMIN_TAB_LABELS[a.active_tab] || a.active_tab || "Dashboard"}`)
        .join(" | ");
      pill.innerHTML = `<span class="presence-dot"></span><span>${esc(names)}</span>`;
    } else {
      pill.innerHTML = `<span class="presence-dot"></span><span>Online</span>`;
    }
  }

  const pendingDismissals = state.collab.pending_dismissals || [];
  if (dismissalBanner) {
    if (pendingDismissals.length > 0) {
      const firstD = pendingDismissals[0];
      const rtype = firstD.request_type || "dismissal";
      let bannerHtml = "";
      let btnText = "Visualizar e responder";
      if (rtype === "resignation") {
        bannerHtml = `<strong>Pedido de demissão pendente:</strong> <strong>${esc(firstD.target_nome || firstD.requested_by_nome || "Administrador")}</strong> pediu desligamento e aguarda aprovação de outro ADM (prazo até ${esc(fmtDateTimeLocalShort(firstD.expires_at))}).`;
        btnText = "Analisar pedido";
      } else if (rtype === "reactivation") {
        bannerHtml = `<strong>Solicitação de retorno:</strong> <strong>${esc(firstD.target_nome || "Administrador")}</strong> (NUSP ${esc(firstD.target_numero_usp)}) solicitou reativação do acesso.`;
        btnText = "Analisar retorno";
      } else {
        bannerHtml = `<strong>Solicitação de demissão pendente</strong> enviada por <strong>${esc(firstD.requested_by_nome || "Administrador")}</strong> (prazo até ${esc(fmtDateTimeLocalShort(firstD.expires_at))}).`;
      }
      dismissalBanner.classList.remove("hidden");
      dismissalBanner.innerHTML = `
        <div>${bannerHtml}</div>
        <button class="btn btn-sm btn-danger js-respond-dismissal" type="button">${esc(btnText)}</button>
      `;
      dismissalBanner.querySelector(".js-respond-dismissal")?.addEventListener("click", () => {
        openRespondDismissalModal(firstD);
      });
      const modalBackdrop = document.getElementById("modal-backdrop");
      const isModalOpen = Boolean(modalBackdrop && !modalBackdrop.classList.contains("hidden"));
      const dId = Number(firstD.id);
      if (!isModalOpen && !state.collab.shownDismissalIds.includes(dId)) {
        state.collab.shownDismissalIds.push(dId);
        openRespondDismissalModal(firstD);
      }
    } else {
      dismissalBanner.classList.add("hidden");
      dismissalBanner.innerHTML = "";
    }
  }

  if (onlineCountEl) {
    onlineCountEl.textContent = `${online.length} ${online.length === 1 ? "online" : "online"}`;
  }
  if (toggleMetaEl) {
    toggleMetaEl.textContent = others.length > 0 ? `· ${others.length} outro(s)` : "";
  }
  if (unreadEl) {
    const showUnread = !state.collab.chatOpen && state.collab.unread > 0;
    unreadEl.classList.toggle("hidden", !showUnread);
    unreadEl.textContent = String(state.collab.unread);
  }
  if (onlineListEl) {
    onlineListEl.innerHTML = online.length
      ? online.map((a) => {
          const tabLbl = ADMIN_TAB_LABELS[a.active_tab] || a.active_tab || "Dashboard";
          return `<span class="admin-chat-online-chip"><span class="presence-dot"></span><strong>${esc(a.nome)}${a.is_self ? " (você)" : ""}</strong> · ${esc(tabLbl)}</span>`;
        }).join("")
      : '<span class="muted" style="font-size:0.75rem;">Sem sessões ativas registradas.</span>';
  }
  if (msgsEl) {
    const msgs = state.collab.messages || [];
    const wasAtBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 48;
    msgsEl.innerHTML = msgs.length
      ? msgs.map((m) => `
          <div class="chat-bubble ${m.is_self ? "is-self" : ""}">
            <div class="chat-bubble-meta">
              <strong>${esc(m.author_name || "ADM")}</strong>
              <span>${esc(fmtTimeLocalShort(m.created_at))}</span>
            </div>
            <div class="chat-bubble-text">${esc(m.message)}</div>
          </div>
        `).join("")
      : '<p class="muted" style="font-size:0.8rem;text-align:center;margin:20px 0;">Nenhuma mensagem ainda. Use este espaço para avisar quando for mexer no sistema.</p>';
    if (state.collab.chatOpen && (wasAtBottom || !state.collab.initialized)) {
      msgsEl.scrollTop = msgsEl.scrollHeight;
    }
  }
}

function applyCollabData(data) {
  const col = state.collab;
  const msgs = data.messages || [];
  const latestId = Number(data.latest_message_id || (msgs.length ? msgs[msgs.length - 1].id : 0));
  if (!col.initialized) {
    col.lastSeenMessageId = latestId;
    col.unread = 0;
    col.initialized = true;
  } else if (col.chatOpen) {
    col.lastSeenMessageId = latestId;
    col.unread = 0;
  } else {
    const newFromOthers = msgs.filter((m) => Number(m.id) > col.lastSeenMessageId && !m.is_self);
    if (newFromOthers.length > 0) {
      col.unread += newFromOthers.length;
    }
    col.lastSeenMessageId = Math.max(col.lastSeenMessageId, latestId);
  }
  col.online_admins = data.online_admins || [];
  col.other_online_count = Number(data.other_online_count || 0);
  col.messages = msgs;
  col.latest_message_id = latestId;
  if (Array.isArray(data.pending_dismissals)) {
    col.pending_dismissals = data.pending_dismissals;
  }
  renderCollabUI();
}

async function pollCollab(sendHeartbeat = true) {
  if (!state.isAdmin || state.collab.pollingBusy) return;
  state.collab.pollingBusy = true;
  try {
    const data = sendHeartbeat
      ? await apiFetch("/api/admin/collab/heartbeat", {
          method: "POST",
          body: JSON.stringify({ active_tab: state.activeTab }),
        })
      : await apiFetch(`/api/admin/collab?tab=${encodeURIComponent(state.activeTab)}`, { skipCache: true });
    state.collab.lastPollAt = Date.now();
    applyCollabData(data);
  } catch (err) {
    if (err && err.status === 401) {
      stopCollabPolling();
      invalidateApiCache();
      try {
        localStorage.removeItem("lsee_admin_hint");
      } catch {
        // ignora
      }
      state.isAdmin = false;
      state.currentUser = null;
      updateAuthUI();
      showToast(err.message || "Sessão administrativa encerrada.", true);
      loadActiveTab();
    }
  } finally {
    state.collab.pollingBusy = false;
  }
}

function scheduleNextCollabPoll() {
  if (state.collab.timer && typeof clearTimeout === "function") {
    clearTimeout(state.collab.timer);
    state.collab.timer = null;
  }
  if (!state.isAdmin || typeof setTimeout !== "function") return;
  if (typeof document !== "undefined" && document.hidden) return;
  const delay = state.collab.chatOpen ? 20000 : 60000;
  state.collab.timer = setTimeout(async () => {
    if (typeof document !== "undefined" && document.hidden) return;
    await pollCollab(true);
    scheduleNextCollabPoll();
  }, delay);
}

if (typeof document !== "undefined" && !document.__lseeCollabVisibilityBound) {
  document.__lseeCollabVisibilityBound = true;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.isAdmin) {
      if (Date.now() - (state.collab.lastPollAt || 0) >= 20000) {
        pollCollab(true).then(() => scheduleNextCollabPoll());
      } else {
        scheduleNextCollabPoll();
      }
    } else if (document.hidden && state.collab.timer) {
      clearTimeout(state.collab.timer);
      state.collab.timer = null;
    }
  });
}

function startCollabPolling(immediate = true) {
  stopCollabPolling();
  if (!state.isAdmin) return;
  if (immediate) {
    pollCollab(true).then(() => scheduleNextCollabPoll());
  } else {
    state.collab.lastPollAt = Date.now();
    scheduleNextCollabPoll();
  }
}

function stopCollabPolling() {
  if (state.collab.timer && typeof clearTimeout === "function") {
    clearTimeout(state.collab.timer);
    state.collab.timer = null;
  }
  state.collab.online_admins = [];
  state.collab.other_online_count = 0;
  state.collab.messages = [];
  state.collab.latest_message_id = 0;
  state.collab.lastSeenMessageId = 0;
  state.collab.initialized = false;
  state.collab.unread = 0;
  state.collab.chatOpen = false;
  state.collab.pending_dismissals = [];
  state.collab.shownDismissalIds = [];
  renderCollabUI();
}

function isMobileChatViewport() {
  if (typeof window === "undefined") return false;
  if (typeof window.matchMedia === "function") {
    return window.matchMedia("(max-width: 720px)").matches;
  }
  return Number(window.innerWidth || 0) <= 720;
}

function maybeCloseMobileChatOnOutsideTap(target) {
  if (!state.isAdmin || !state.collab.chatOpen || !isMobileChatViewport()) return;
  if (target && typeof target.closest === "function") {
    if (target.closest("#admin-chat-widget, #admin-presence-pill")) return;
  }
  toggleAdminChat(false);
}

function toggleAdminChat(forceOpen = null) {
  if (!state.isAdmin) return;
  const next = forceOpen === null ? !state.collab.chatOpen : Boolean(forceOpen);
  state.collab.chatOpen = next;
  if (next) {
    state.collab.unread = 0;
    state.collab.lastSeenMessageId = Math.max(state.collab.lastSeenMessageId, state.collab.latest_message_id);
  }
  renderCollabUI();
  scheduleNextCollabPoll();
  if (next) {
    pollCollab(true);
    setTimeout(() => document.getElementById("admin-chat-input")?.focus(), 40);
  }
}

async function sendAdminChatMessage(rawText) {
  const msg = String(rawText || "").trim();
  if (!msg || !state.isAdmin) return;
  const data = await apiFetch("/api/admin/collab/chat", {
    method: "POST",
    body: JSON.stringify({ message: msg, active_tab: state.activeTab }),
  });
  state.collab.unread = 0;
  applyCollabData(data);
  state.collab.lastSeenMessageId = state.collab.latest_message_id;
  const msgsEl = document.getElementById("admin-chat-messages");
  if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
}

function syncMobileTabSelect() {
  const sel = document.getElementById("mobile-tab-select");
  if (!sel) return;
  const visibleTabs = Array.from(document.querySelectorAll("#main-tabs .tab")).filter(
    (btn) => !btn.classList.contains("hidden")
  );
  sel.innerHTML = visibleTabs
    .map((btn) => `<option value="${esc(btn.dataset.tab)}">${esc(btn.textContent.trim())}</option>`)
    .join("");
  if (visibleTabs.some((btn) => btn.dataset.tab === state.activeTab)) {
    sel.value = state.activeTab;
  }
}

function renderPublicResponsibles() {
  const el = document.getElementById("public-responsibles");
  if (!el) return;
  const names = (state.publicResponsibles || []).map((n) => String(n || "").trim()).filter(Boolean);
  if (state.isAdmin || names.length === 0) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  const prefix = names.length === 1 ? "Responsável:" : "Responsáveis:";
  el.innerHTML = `<span class="muted">${prefix}</span> <strong>${esc(names.join(", "))}</strong>`;
}

function updateAuthUI() {
  const badge = document.getElementById("mode-badge");
  const btnLogin = document.getElementById("btn-admin-login");
  const btnLogout = document.getElementById("btn-admin-logout");
  const btnHelp = document.getElementById("btn-admin-help");
  document.querySelectorAll(".admin-tab").forEach((el) => {
    el.classList.toggle("hidden", !state.isAdmin);
  });

  if (state.isAdmin) {
    const label = state.currentUser?.nome ? `Admin · ${state.currentUser.nome}` : "Modo Administrador";
    badge.textContent = label;
    badge.title = state.currentUser?.numero_usp ? `NUSP ${state.currentUser.numero_usp}` : "";
    badge.className = "badge badge-admin";
    btnLogin.classList.add("hidden");
    btnLogout.classList.remove("hidden");
    btnHelp.classList.remove("hidden");
    renderCollabUI();
  } else {
    badge.textContent = "Visão Pública";
    badge.title = "";
    badge.className = "badge badge-public";
    btnLogin.classList.remove("hidden");
    btnLogout.classList.add("hidden");
    btnHelp.classList.add("hidden");
    state.mapSwapSource = "";
    state.compFilters.pendencia = "";
    if (!["", "Livre", "Em uso"].includes(state.compFilters.status)) {
      state.compFilters.status = "";
    }
    const adminTabs = ["criterios", "pessoas", "alocacoes", "monitores", "patrimonios", "etiquetas", "rede", "solicitacoes", "parametros"];
    if (adminTabs.includes(state.activeTab)) {
      switchTab("dashboard");
    }
    renderCollabUI();
  }
  renderPublicResponsibles();
  syncMobileTabSelect();
}

function switchTab(tabName) {
  document.body.classList.remove("printing-map");
  state.mapPrintMode = false;
  state.activeTab = tabName;
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  syncMobileTabSelect();
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabName}`);
  });
  if (state.isAdmin && Date.now() - (state.collab.lastPollAt || 0) >= 25000) {
    pollCollab(true);
  }
  loadActiveTab();
}

async function loadActiveTab() {
  const tab = state.activeTab;
  const panel = document.getElementById(`tab-${tab}`);
  const hasRealContent = Boolean(
    panel && panel.children.length > 0 && !panel.querySelector(".tab-loading-state")
  );
  let reloadingPill = null;

  startPageLoader();
  if (panel) {
    if (!hasRealContent) {
      panel.innerHTML = buildTabLoadingHtml(tab);
    } else {
      panel.classList.add("is-reloading");
      if (!document.querySelector(".tab-reloading-pill")) {
        reloadingPill = document.createElement("div");
        reloadingPill.className = "tab-reloading-pill";
        reloadingPill.setAttribute("role", "status");
        reloadingPill.innerHTML = '<span class="lsee-spinner-sm" aria-hidden="true"></span><span>Atualizando...</span>';
        document.body.appendChild(reloadingPill);
      }
    }
  }

  try {
    switch (tab) {
      case "dashboard":
        await renderDashboard();
        break;
      case "computadores":
        await renderComputers();
        break;
      case "mapa":
        await renderMap();
        break;
      case "simulador":
        await renderSimulator();
        break;
      case "criterios":
        await renderCriterios();
        break;
      case "pessoas":
        await renderPeople();
        break;
      case "alocacoes":
        await renderAllocations();
        break;
      case "monitores":
        await renderMonitors();
        break;
      case "patrimonios":
        await renderPatrimonies();
        break;
      case "etiquetas":
        await renderLabels();
        break;
      case "rede":
        await renderNetwork();
        break;
      case "solicitacoes":
        await renderRequests();
        break;
      case "parametros":
        await renderParamsAndAudit();
        break;
    }
  } catch (err) {
    showToast(err.message, true);
    if (panel && !hasRealContent) {
      panel.innerHTML = `
        <div class="tab-loading-state" role="alert">
          <p class="tab-loading-title">Não foi possível carregar esta seção</p>
          <p class="tab-loading-sub">${esc(err.message || "Falha de comunicação com o servidor.")}</p>
          <button class="btn btn-primary btn-sm js-retry-tab" type="button">Tentar novamente</button>
        </div>
      `;
      panel.querySelector(".js-retry-tab")?.addEventListener("click", () => loadActiveTab());
    }
  } finally {
    if (reloadingPill) {
      reloadingPill.remove();
    }
    if (panel) {
      panel.classList.remove("is-reloading", "tab-fade-in");
      void panel.offsetWidth;
      panel.classList.add("tab-fade-in");
    }
    stopPageLoader();
  }
}

/* ==================== MAPA DO LSEE ==================== */
const MAP_MODE_LABELS = {
  person: "Pessoa alocada",
  livre: "Livre",
  pc_ic: "PC IC",
  servidor: "Servidor / Bancada",
  vazio: "Vazia / Indisponível",
};

const MAP_STATIONS_META = {
  D01: {
    d: "m 2194.3345,2273.1594 v -172.9575 h -222.9634 v -222.9372 l -173.8843,-0.9296 v 396.8243 z",
    code: [1884, 1945], name: [1996, 2178], pc: [1996, 2228], maxChars: 16,
  },
  D02: {
    d: "m 2203.1841,2273.1594 v -172.9575 h 222.9634 v -222.9372 l 173.885,-0.9296 v 396.8243 z",
    code: [2513, 1945], name: [2401, 2178], pc: [2401, 2228], maxChars: 16,
  },
  D03: {
    d: "m 2426.1711,1877.2647 h 172.9351 v -317.4548 h -172.9351 z m 0,0",
    code: [2513, 1632], name: [2513, 1722], pc: [2513, 1788], maxChars: 9, compact: true,
  },
  D04: {
    d: "m 2203.1841,1172.7647 v 172.9575 h 222.9634 v 222.9372 l 173.885,0.9297 v -396.8244 z",
    code: [2513, 1505], name: [2401, 1252], pc: [2401, 1304], maxChars: 16,
  },
  D05: {
    d: "m 2194.3817,1172.7647 v 172.9575 h -222.9646 v 222.9372 l -173.8849,0.9297 v -396.8244 z",
    code: [1884, 1505], name: [1996, 1252], pc: [1996, 1304], maxChars: 16,
  },
  D06: {
    d: "m 1391.7888,1172.7647 v 172.9575 h 222.9634 v 222.9372 l 173.885,0.9297 v -396.8244 z",
    code: [1702, 1505], name: [1590, 1252], pc: [1590, 1304], maxChars: 16,
  },
  D07: {
    d: "m 443.05138,1164.5787 v 172.9576 h 222.9634 v 222.9372 l 173.8849,0.9296 v -396.8244 z",
    code: [753, 1496], name: [641, 1244], pc: [641, 1296], maxChars: 16,
  },
  D08: {
    d: "m 434.24898,1164.5787 v 172.9576 h -222.9634 v 222.9372 l -173.884967,0.9296 v -396.8244 z",
    code: [124, 1496], name: [236, 1244], pc: [236, 1296], maxChars: 16,
  },
  D09: {
    d: "M 210.31208,1876.9987 H 37.377013 V 1559.5439 H 210.31208 Z m 0,0",
    code: [124, 1632], name: [124, 1722], pc: [124, 1788], maxChars: 9, compact: true,
  },
  D10: {
    d: "m 434.24898,2273.8229 v -172.9574 h -222.9634 v -222.9372 l -173.884967,-0.9296 v 396.8242 z",
    code: [124, 1945], name: [236, 2178], pc: [236, 2228], maxChars: 16,
  },
  D11: {
    d: "m 443.09858,2273.8229 v -172.9574 h 222.9634 v -222.9372 l 173.8843,-0.9296 v 396.8242 z",
    code: [753, 1945], name: [641, 2178], pc: [641, 2228], maxChars: 16,
  },
  M00: {
    d: "m 1527.1947,4290.6408 h -241.0513 v -436.8105 h 241.0513 z m 0,0",
    code: [1407, 3922], name: [1407, 4075], pc: [1407, 4135], maxChars: 12, compact: true,
  },
  M01: {
    d: "m 1527.1947,3853.8303 h -241.0513 v -436.8104 h 241.0513 z m 0,0",
    code: [1407, 3485], name: [1407, 3638], pc: [1407, 3698], maxChars: 12, compact: true,
  },
  M02: {
    d: "m 1527.1947,3417.0199 h -241.0513 v -436.8103 h 241.0513 z m 0,0",
    code: [1407, 3048], name: [1407, 3200], pc: [1407, 3260], maxChars: 12, compact: true,
  },
  M03: {
    d: "m 1527.1947,2980.2096 h -241.0513 v -436.8104 h 241.0513 z m 0,0",
    code: [1407, 2612], name: [1407, 2765], pc: [1407, 2825], maxChars: 12, compact: true,
  },
  M04: {
    d: "m 941.03538,2329.4728 v 215.3854 h -283.4849 v -215.3854 z m 0,0",
    code: [799, 2382], name: [799, 2450], pc: [799, 2504], maxChars: 12, compact: true,
  },
  M05: {
    d: "m 648.70088,2329.4498 v 215.4084 h -283.4165 v -215.4084 z m 0,0",
    code: [507, 2382], name: [507, 2450], pc: [507, 2504], maxChars: 12, compact: true,
  },
  M06: {
    d: "m 356.43478,2329.4728 v 215.3854 H 72.949283 v -215.3854 z m 0,0",
    code: [215, 2382], name: [215, 2450], pc: [215, 2504], maxChars: 12, compact: true,
  },
  M07: {
    d: "m 463.12748,2989.0809 v 170.1005 H 66.257243 v -170.1005 z m 0,0",
    code: [122, 3038], name: [280, 3086], pc: [280, 3132], maxChars: 15, bench: true,
  },
  M08: {
    d: "m 868.80118,2989.15 v 170.0314 H 471.97698 V 2989.15 Z m 0,0",
    code: [812, 3038], name: [656, 3086], pc: [656, 3132], maxChars: 15, bench: true,
  },
  M09: {
    d: "m 868.70918,3159.1584 v 170.1005 h -396.8012 v -170.1005 z m 0,0",
    code: [812, 3208], name: [656, 3256], pc: [656, 3302], maxChars: 15, bench: true,
  },
  M10: {
    d: "m 463.05838,3159.1814 v 170.0775 H 66.257243 v -170.0775 z m 0,0",
    code: [122, 3208], name: [280, 3256], pc: [280, 3302], maxChars: 15, bench: true,
  },
  M11: {
    d: "m 463.05838,3782.6604 v 170.0775 H 66.257243 v -170.0775 z m 0,0",
    code: [122, 3832], name: [280, 3880], pc: [280, 3926], maxChars: 15, bench: true,
  },
  M12: {
    d: "m 868.70918,3782.6604 v 170.0774 h -396.8012 v -170.0774 z m 0,0",
    code: [812, 3832], name: [656, 3880], pc: [656, 3926], maxChars: 15, bench: true,
  },
  M13: {
    d: "m 868.77818,3952.807 v 170.0313 H 471.90798 V 3952.807 Z m 0,0",
    code: [812, 4002], name: [656, 4050], pc: [656, 4096], maxChars: 15, bench: true,
  },
  M14: {
    d: "m 463.05838,3952.7379 v 170.1004 H 66.257243 v -170.1004 z m 0,0",
    code: [122, 4002], name: [280, 4050], pc: [280, 4096], maxChars: 15, bench: true,
  },
  P01: {
    d: "m 1581.4673,2949.8176 v 172.9575 h 222.9634 v 222.9373 l 173.8844,0.9298 v -396.8246 z",
    code: [1892, 3282], name: [1780, 3030], pc: [1780, 3082], maxChars: 16,
  },
  P02: {
    d: "m 1978.3387,3625.632 -172.9575,0.024 v 222.9634 h -222.9369 l -0.9298,173.8848 h 396.8242 z",
    code: [1892, 3695], name: [1780, 3928], pc: [1780, 3978], maxChars: 16,
  },
  P03: {
    d: "m 1978.3387,4031.3533 v 172.9574 h -222.9634 v 222.9372 l -173.8844,0.9296 v -396.8242 z",
    code: [1668, 4364], name: [1780, 4110], pc: [1780, 4162], maxChars: 16,
  },
  P04: {
    d: "m 2192.0856,4031.3533 v 172.9574 h 222.9634 v 222.9372 l 173.8844,0.9296 v -396.8242 z",
    code: [2502, 4364], name: [2390, 4110], pc: [2390, 4162], maxChars: 16,
  },
  P05: {
    d: "m 2192.0856,3625.6321 172.9575,0.024 V 3848.619 H 2587.98 l 0.9298,173.8847 h -396.8242 z",
    code: [2278, 3695], name: [2390, 3928], pc: [2390, 3978], maxChars: 16,
  },
  P06: {
    d: "m 2574.2779,2949.8176 v 172.9575 h -222.9628 v 222.9374 l -173.885,0.9298 v -396.8247 z",
    code: [2264, 3282], name: [2376, 3030], pc: [2376, 3082], maxChars: 16,
  },
  P07: {
    d: "m 2587.8479,2305.874 v 172.9575 h -222.9628 v 222.9374 l -173.885,0.9298 v -396.8247 z",
    code: [2278, 2638], name: [2390, 2386], pc: [2390, 2438], maxChars: 16,
  },
  "PC 083": {
    d: "M 1103.3241,43.358956 H 1536.6673 V 286.03896 H 1103.3241 Z",
    code: [1320, 122], name: [1320, 182], pc: [1320, 236], singleY: 168, maxChars: 16,
  },
  "PC Hardware": {
    d: "M 1984.7083,43.358956 V 278.80146 h -448.041 V 43.358956 Z m 0,0",
    code: [1761, 120], name: [1761, 178], pc: [1761, 230], singleY: 165, maxChars: 16,
  },
  "PC 082": {
    d: "M 1103.3241,286.03896 H 1345.5667 L 1345.1292,620.08126 H 1103.3241 Z",
    code: [1224, 392], name: [1224, 464], pc: [1224, 526], singleY: 455, maxChars: 11, compact: true,
  },
  "PC 094": {
    d: "M 1747.278,982.17756 V 1147.3905 H 1383.6399 V 982.17756 Z m 0,0",
    code: [1565, 1034], name: [1565, 1084], pc: [1565, 1126], singleY: 1076, maxChars: 15, bench: true,
  },
  "PC 095": {
    d: "m 2110.916,982.1776 v 165.2128 H 1747.278 V 982.1776 Z m 0,0",
    code: [1929, 1034], name: [1929, 1084], pc: [1929, 1126], singleY: 1076, maxChars: 15, bench: true,
  },
};

function truncateMapLabel(text, maxChars) {
  const s = String(text || "").trim();
  if (!s || s.length <= maxChars) return s;
  return `${s.slice(0, Math.max(1, maxChars - 1))}…`;
}

function stationMatchesMapFilters(st) {
  const f = state.mapFilters;
  if (f.room && st.room !== f.room) return false;
  if (f.mode && st.occupant_mode !== f.mode) return false;
  const q = f.search.trim().toLowerCase();
  if (!q) return true;
  const hay = [
    st.station_code,
    st.room,
    st.display_label,
    st.person?.id_pessoa || "",
    st.person?.full_name || "",
    st.pc?.id_pc || "",
    st.raw_pc_id_pc || "",
  ]
    .join(" ")
    .toLowerCase();
  return hay.includes(q);
}

function refreshMapOverlay() {
  const overlay = document.getElementById("lsee-map-overlay");
  const data = state.mapData;
  if (!overlay || !data) return;

  overlay.innerHTML = data.stations
    .map((st) => {
      const meta = MAP_STATIONS_META[st.station_code];
      if (!meta) return "";
      const isSelected = state.mapSelectedStation === st.station_code;
      const isSwap = state.mapSwapSource === st.station_code;
      const isDimmed = !stationMatchesMapFilters(st);
      const cls = [
        "map-station",
        `mode-${st.occupant_mode}`,
        isSelected ? "is-selected" : "",
        isSwap ? "is-swap-source" : "",
        isDimmed ? "is-dimmed" : "",
      ]
        .filter(Boolean)
        .join(" ");

      const rawLabel = st.display_label || "";
      const sameAsCode = rawLabel.trim().toLowerCase() === st.station_code.trim().toLowerCase();
      const showName = Boolean(rawLabel) && !sameAsCode;
      const maxChars = meta.maxChars || 16;
      const shortName = truncateMapLabel(rawLabel, maxChars);
      const isPrinting = Boolean(state.mapPrintMode);
      const pcLabel = !isPrinting && state.mapShowPcs && st.pc?.id_pc ? st.pc.id_pc : "";

      let codeSize = meta.bench ? 33 : meta.compact ? 34 : 38;
      let nameSize = meta.compact
        ? (shortName.length > 9 ? 28 : 31)
        : meta.bench
          ? 34
          : (shortName.length > 13 ? 34 : 39);
      let nameY = meta.name[1];

      if (isPrinting) {
        codeSize = meta.bench ? 40 : meta.compact ? 42 : 46;
        if (sameAsCode && meta.singleY) {
          codeSize = meta.compact ? 42 : 48;
        }
        if (meta.compact) {
          // Mesas estreitas (D03, D09, M00..M06, PC 082)
          const isUltraNarrow = st.station_code === "D03" || st.station_code === "D09";
          if (isUltraNarrow) {
            nameSize = shortName.length > 6 ? 34 : 39;
          } else {
            nameSize = shortName.length > 9 ? 36 : 44;
          }
          nameY += 14;
        } else if (meta.bench) {
          nameSize = shortName.length > 11 ? 41 : 48;
          nameY += 20;
        } else {
          // Mesas em L (Doutorado / Doc. P. e D.)
          nameSize = shortName.length > 12 ? 44 : shortName.length > 9 ? 50 : 56;
          nameY += 22;
        }
      }

      const pcSize = meta.bench ? 26 : meta.compact ? 25 : 30;
      const codeY = sameAsCode && meta.singleY ? (pcLabel ? meta.singleY - 18 : meta.singleY) : meta.code[1];
      const pcY = sameAsCode && meta.singleY ? meta.singleY + 34 : meta.pc[1];

      const tipParts = [`${st.station_code} (${st.room})`, st.display_label || MAP_MODE_LABELS[st.occupant_mode] || ""];
      if (st.pc) tipParts.push(`${st.pc.id_pc} [${st.pc.status}]`);
      const tooltip = tipParts.filter(Boolean).join(" — ");

      return `
        <g class="${cls}" data-station="${esc(st.station_code)}" role="button" tabindex="0" aria-label="${esc(tooltip)}">
          <title>${esc(tooltip)}</title>
          <path class="station-shape" d="${meta.d}" />
          <text class="station-code" x="${meta.code[0]}" y="${codeY}" text-anchor="middle" style="font-size:${codeSize}px">${esc(st.station_code)}</text>
          ${showName ? `<text class="station-name" x="${meta.name[0]}" y="${nameY}" text-anchor="middle" style="font-size:${nameSize}px">${esc(shortName)}</text>` : ""}
          ${pcLabel ? `<text class="station-pc" x="${meta.pc[0]}" y="${pcY}" text-anchor="middle" style="font-size:${pcSize}px">${esc(pcLabel)}</text>` : ""}
        </g>
      `;
    })
    .join("");
}

function refreshMapSwapBanner() {
  const slot = document.getElementById("map-swap-banner-slot");
  if (!slot) return;
  if (!state.isAdmin || !state.mapSwapSource) {
    slot.innerHTML = "";
    return;
  }
  slot.innerHTML = `
    <div class="map-swap-banner">
      <span><strong>Modo de troca ativo:</strong> selecione outra estação no mapa para permutar com <strong>${esc(state.mapSwapSource)}</strong>.</span>
      <button class="btn btn-sm btn-secondary" id="btn-map-cancel-swap" type="button">Cancelar troca</button>
    </div>
  `;
  document.getElementById("btn-map-cancel-swap")?.addEventListener("click", () => {
    state.mapSwapSource = "";
    refreshMapView();
  });
}

function syncMapZoomClass() {
  const stage = document.getElementById("lsee-map-stage");
  if (!stage) return;
  stage.className = `map-stage zoom-${state.mapZoom}`;
  document.querySelectorAll(".map-zoom-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.zoom === state.mapZoom);
  });
}

function refreshMapInspector() {
  const slot = document.getElementById("map-inspector-slot");
  const data = state.mapData;
  if (!slot || !data) return;

  const st =
    data.stations.find((item) => item.station_code === state.mapSelectedStation) ||
    data.stations[0];
  if (!st) {
    slot.innerHTML = '<div class="map-inspector-card"><p class="empty-msg">Nenhuma estação cadastrada.</p></div>';
    return;
  }
  state.mapSelectedStation = st.station_code;

  const modeLabel = MAP_MODE_LABELS[st.occupant_mode] || st.occupant_mode;
  let occupantHtml = "";
  if (st.person) {
    occupantHtml = `
      <div class="map-kv-grid">
        <div class="map-kv">
          <span class="map-kv-label">Ocupante</span>
          <span class="map-kv-val">${esc(st.person.full_name)}</span>
        </div>
        <div class="map-kv">
          <span class="map-kv-label">Categoria / Entrada</span>
          <span class="map-kv-val">${esc(st.person.categoria || "—")}${st.person.entrada ? ` (${esc(st.person.entrada)})` : ""}</span>
        </div>
        <div class="map-kv">
          <span class="map-kv-label">ID Pessoa</span>
          <span class="map-kv-val">${esc(st.person.id_pessoa)}</span>
        </div>
        <div class="map-kv">
          <span class="map-kv-label">Situação</span>
          <span class="map-kv-val">${esc(st.person.situacao)}</span>
        </div>
      </div>
    `;
  } else {
    occupantHtml = `
      <div class="map-kv-grid">
        <div class="map-kv">
          <span class="map-kv-label">Identificação no mapa</span>
          <span class="map-kv-val">${esc(st.display_label || modeLabel)}</span>
        </div>
        <div class="map-kv">
          <span class="map-kv-label">Tipo de posto</span>
          <span class="map-kv-val">${esc(st.station_kind || "pesquisa")}</span>
        </div>
      </div>
    `;
  }

  let pcHtml = "";
  if (st.pc) {
    const srcText = st.pc.source === "direct" ? "Vinculado ao posto" : "Via alocação ativa da pessoa";
    pcHtml = `
      <div style="margin-bottom: 12px;">
        <div class="section-header" style="margin-bottom:6px;">
          <strong style="font-size:0.9rem;">Computador: <span class="mono">${esc(st.pc.id_pc)}</span></strong>
          <span>${statusBadge(st.pc.status)}</span>
        </div>
        <div class="map-kv-meta muted" style="font-size:0.78rem; margin-bottom:6px;">Vínculo: ${esc(srcText)}</div>
        <div class="map-kv-grid">
          <div class="map-kv">
            <span class="map-kv-label">Processador</span>
            <span class="map-kv-val">${esc(st.pc.processador || "—")}</span>
          </div>
          <div class="map-kv">
            <span class="map-kv-label">Score Total</span>
            <span class="map-kv-val">${esc(st.pc.score_total || "—")}</span>
          </div>
          <div class="map-kv">
            <span class="map-kv-label">RAM / GPU</span>
            <span class="map-kv-val">${fmtNum(st.pc.ram_gb, " GB")} / ${esc(st.pc.possui_gpu || "—")}</span>
          </div>
          <div class="map-kv">
            <span class="map-kv-label">Armazenamento</span>
            <span class="map-kv-val">SSD ${fmtNum(st.pc.ssd_gb, "GB")} · HD ${fmtNum(st.pc.hd_gb, "GB")}</span>
          </div>
        </div>
      </div>
    `;
  } else {
    pcHtml = `<p class="muted" style="font-size:0.84rem; margin: 0 0 12px;">Nenhum computador público vinculado a esta estação.</p>`;
  }

  let adminFormHtml = "";
  if (state.isAdmin) {
    const peopleOpts = (data.people_options || [])
      .map((p) => {
        const curLoc = p.assigned_station && p.assigned_station !== st.station_code
          ? ` [No mapa: ${p.assigned_station}]`
          : p.assigned_station === st.station_code
            ? " [Posto atual]"
            : " (Sem posto)";
        const sitTag = p.situacao !== "Ativo" ? ` [${p.situacao}]` : "";
        return `<option value="${esc(p.id_pessoa)}" ${p.id_pessoa === st.raw_id_pessoa ? "selected" : ""}>${esc(p.id_pessoa)} — ${esc(p.nome)}${esc(curLoc)}${esc(sitTag)}</option>`;
      })
      .join("");

    const pcOpts = (data.computer_options || [])
      .map((c) => {
        const userTag = c.usuario_atual ? ` — ${c.usuario_atual}` : "";
        return `<option value="${esc(c.id_pc)}" ${c.id_pc === st.raw_pc_id_pc ? "selected" : ""}>${esc(c.id_pc)} (${esc(c.status)} · ${esc(c.tipo_de_uso)})${esc(userTag)}</option>`;
      })
      .join("");

    const modeOpts = Object.entries(MAP_MODE_LABELS)
      .map(([k, label]) => `<option value="${esc(k)}" ${k === st.occupant_mode ? "selected" : ""}>${esc(label)}</option>`)
      .join("");

    adminFormHtml = `
      <div style="border-top: 1px solid var(--border); padding-top: 12px; margin-top: 8px;">
        <div class="section-header" style="margin-bottom:8px;">
          <strong style="font-size:0.9rem;">Editar Estação</strong>
          <button class="btn btn-sm btn-secondary" id="btn-map-start-swap" type="button">
            ${state.mapSwapSource === st.station_code ? "Cancelar troca" : "Trocar posto"}
          </button>
        </div>
        <form id="map-station-form">
          <div class="form-row">
            <label>Estado da estação
              <select id="map-edit-mode" name="occupant_mode">${modeOpts}</select>
            </label>
          </div>
          <div id="map-edit-person-wrap" class="form-row ${st.occupant_mode === "person" ? "" : "hidden"}">
            <label>Pessoa alocada
              <select id="map-edit-person" name="id_pessoa">
                <option value="">Selecione...</option>
                ${peopleOpts}
              </select>
            </label>
            <label style="flex-direction:row; align-items:center; gap:8px; font-weight:500;">
              <input id="map-edit-move" type="checkbox" name="move_from_previous" checked style="width:auto;" />
              Mover automaticamente caso a pessoa esteja em outro posto
            </label>
          </div>
          <div class="form-row">
            <label>Rótulo curto no mapa (opcional)
              <input id="map-edit-label" type="text" name="custom_label" value="${esc(st.raw_custom_label || "")}" placeholder="Vazio usa o nome cadastrado" />
            </label>
          </div>
          <div class="form-row">
            <label>PC fixo na estação (opcional)
              <select id="map-edit-pc" name="pc_id_pc">
                <option value="">Automático (PC principal da pessoa)</option>
                ${pcOpts}
              </select>
            </label>
          </div>
          <div class="form-row">
            <label>Observações internas da estação
              <input id="map-edit-obs" type="text" name="observacoes" value="${esc(st.observacoes || "")}" placeholder="Ex: monitor extra, bancada de hardware..." />
            </label>
          </div>
          <div class="form-actions" style="justify-content:space-between;">
            <button class="btn btn-sm btn-secondary" id="btn-map-mark-free" type="button" ${st.occupant_mode === "livre" ? "disabled" : ""}>Marcar Livre</button>
            <button class="btn btn-sm btn-primary" type="submit">Salvar alterações</button>
          </div>
        </form>
      </div>
    `;
  }

  slot.innerHTML = `
    <div class="map-inspector-card">
      <div class="map-inspector-head">
        <div>
          <h3 class="map-inspector-title">Estação ${esc(st.station_code)}</h3>
          <span class="muted" style="font-size:0.82rem;">${esc(st.room)}</span>
        </div>
        <span class="badge ${st.occupant_mode === "livre" ? "badge-livre" : st.occupant_mode === "person" ? "badge-uso" : "badge-muted"}">${esc(modeLabel)}</span>
      </div>
      ${occupantHtml}
      ${pcHtml}
      ${adminFormHtml}
    </div>
  `;

  if (state.isAdmin) {
    const modeSel = document.getElementById("map-edit-mode");
    const personWrap = document.getElementById("map-edit-person-wrap");
    const personSel = document.getElementById("map-edit-person");
    const labelInput = document.getElementById("map-edit-label");
    let previousPerson = st.raw_id_pessoa || "";

    modeSel?.addEventListener("change", () => {
      personWrap?.classList.toggle("hidden", modeSel.value !== "person");
    });
    personSel?.addEventListener("change", () => {
      if (personSel.value !== previousPerson) {
        if (labelInput && (labelInput.value === (st.raw_custom_label || ""))) {
          labelInput.value = "";
        }
        previousPerson = personSel.value;
      }
    });

    document.getElementById("btn-map-start-swap")?.addEventListener("click", () => {
      state.mapSwapSource = state.mapSwapSource === st.station_code ? "" : st.station_code;
      refreshMapView();
    });

    document.getElementById("btn-map-mark-free")?.addEventListener("click", async () => {
      try {
        const resp = await apiFetch(`/api/admin/map/stations/${encodeURIComponent(st.station_code)}`, {
          method: "PUT",
          body: JSON.stringify({
            occupant_mode: "livre",
            id_pessoa: "",
            custom_label: "",
            pc_id_pc: st.raw_pc_id_pc || "",
            observacoes: st.observacoes || "",
          }),
        });
        state.mapData = resp.map;
        showToast(`Estação ${st.station_code} marcada como Livre.`);
        refreshMapView();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    document.getElementById("map-station-form")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        const resp = await apiFetch(`/api/admin/map/stations/${encodeURIComponent(st.station_code)}`, {
          method: "PUT",
          body: JSON.stringify({
            occupant_mode: fd.get("occupant_mode"),
            id_pessoa: fd.get("id_pessoa") || "",
            custom_label: fd.get("custom_label") || "",
            pc_id_pc: fd.get("pc_id_pc") || "",
            observacoes: fd.get("observacoes") || "",
            move_from_previous: Boolean(fd.get("move_from_previous")),
          }),
        });
        state.mapData = resp.map;
        const movedMsg = resp.moved_from?.length ? ` (movido de ${resp.moved_from.join(", ")})` : "";
        showToast(`Estação ${st.station_code} atualizada${movedMsg}.`);
        refreshMapView();
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }
}

function refreshMapUnplacedCard() {
  const slot = document.getElementById("map-unplaced-slot");
  const data = state.mapData;
  if (!slot || !data) return;

  const list = data.unplaced_active_people || [];
  const selectedSt = data.stations.find((s) => s.station_code === state.mapSelectedStation);
  const canAutoPlace = selectedSt && (selectedSt.occupant_mode === "livre" || selectedSt.occupant_mode === "vazio");

  slot.innerHTML = `
    <div class="map-inspector-card">
      <div class="map-inspector-head" style="margin-bottom:10px;">
        <div>
          <h3 class="map-inspector-title" style="font-size:0.96rem;">Pessoas Ativas sem Posto</h3>
          <span class="muted" style="font-size:0.78rem;">${list.length} registro(s) sem mesa atribuída no mapa</span>
        </div>
      </div>
      ${list.length === 0
        ? '<p class="muted" style="font-size:0.84rem; margin:0;">Todos os membros ativos já possuem posto no mapa.</p>'
        : `<div class="map-unplaced-list">
            ${list
              .map(
                (p) => `
                <div class="map-unplaced-item">
                  <div>
                    <div><strong>${esc(p.nome)}</strong> <span class="muted">(${esc(p.id_pessoa)})</span></div>
                    <div class="map-unplaced-meta">${esc(p.categoria)}${p.entrada ? ` · ${esc(p.entrada)}` : ""}${p.pc_principal ? ` · ${esc(p.pc_principal)}` : ""}</div>
                  </div>
                  ${
                    state.isAdmin
                      ? `<button class="btn btn-sm btn-secondary js-map-assign-unplaced" data-pid="${esc(p.id_pessoa)}" type="button">${canAutoPlace ? `Alocar em ${esc(selectedSt.station_code)}` : `Selecionar em ${esc(state.mapSelectedStation)}`}</button>`
                      : ""
                  }
                </div>
              `
              )
              .join("")}
          </div>`
      }
    </div>
  `;

  if (state.isAdmin) {
    slot.querySelectorAll(".js-map-assign-unplaced").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const pid = btn.dataset.pid;
        const target = data.stations.find((s) => s.station_code === state.mapSelectedStation);
        if (!target || !pid) return;
        if (target.occupant_mode === "livre" || target.occupant_mode === "vazio") {
          try {
            const resp = await apiFetch(`/api/admin/map/stations/${encodeURIComponent(target.station_code)}`, {
              method: "PUT",
              body: JSON.stringify({
                occupant_mode: "person",
                id_pessoa: pid,
                custom_label: "",
                pc_id_pc: target.raw_pc_id_pc || "",
                observacoes: target.observacoes || "",
                move_from_previous: true,
              }),
            });
            state.mapData = resp.map;
            showToast(`${pid} alocada na estação ${target.station_code}.`);
            refreshMapView();
          } catch (err) {
            showToast(err.message, true);
          }
          return;
        }
        const modeSel = document.getElementById("map-edit-mode");
        const personSel = document.getElementById("map-edit-person");
        const labelInput = document.getElementById("map-edit-label");
        if (modeSel && personSel) {
          modeSel.value = "person";
          modeSel.dispatchEvent(new Event("change"));
          personSel.value = pid;
          if (labelInput) labelInput.value = "";
          showToast(`${pid} preenchida no formulário de ${target.station_code}. Clique em Salvar para substituir.`);
        }
      });
    });
  }
}

function refreshMapSummaryLegend() {
  const slot = document.getElementById("map-legend-slot");
  const data = state.mapData;
  if (!slot || !data) return;
  const s = data.summary || {};
  slot.innerHTML = `
    <div class="map-legend">
      <span class="map-legend-pill"><span class="map-swatch map-swatch-person"></span> Ocupadas: <strong>${s.person || 0}</strong></span>
      <span class="map-legend-pill"><span class="map-swatch map-swatch-livre"></span> Livres: <strong>${s.livre || 0}</strong></span>
      <span class="map-legend-pill"><span class="map-swatch map-swatch-ic"></span> PC IC: <strong>${s.pc_ic || 0}</strong></span>
      <span class="map-legend-pill"><span class="map-swatch map-swatch-servidor"></span> Servidor / Bancada: <strong>${s.servidor || 0}</strong></span>
      <span class="map-legend-pill"><span class="map-swatch map-swatch-vazio"></span> Vazias: <strong>${s.vazio || 0}</strong></span>
      <span class="map-legend-pill">Ativas no mapa / Sem posto: <strong>${s.pessoas_ativos_alocados || 0}</strong> / <strong>${s.pessoas_ativas_sem_posto || 0}</strong></span>
    </div>
  `;
}

function refreshMapView() {
  syncMapZoomClass();
  refreshMapSummaryLegend();
  refreshMapSwapBanner();
  refreshMapOverlay();
  refreshMapInspector();
  refreshMapUnplacedCard();
}

async function handleMapStationActivate(stationCode) {
  if (!stationCode) return;
  if (state.isAdmin && state.mapSwapSource) {
    if (state.mapSwapSource === stationCode) {
      state.mapSwapSource = "";
      refreshMapView();
      return;
    }
    try {
      const resp = await apiFetch("/api/admin/map/swap", {
        method: "POST",
        body: JSON.stringify({ source: state.mapSwapSource, target: stationCode }),
      });
      showToast(`Troca concluída: ${state.mapSwapSource} ↔ ${stationCode}.`);
      state.mapSwapSource = "";
      state.mapSelectedStation = stationCode;
      state.mapData = resp.map;
      refreshMapView();
    } catch (err) {
      showToast(err.message, true);
    }
    return;
  }
  state.mapSelectedStation = stationCode;
  refreshMapView();
}

function printLseeMap() {
  if (!state.isAdmin || state.activeTab !== "mapa") return;
  const cleanup = () => {
    state.mapPrintMode = false;
    document.body.classList.remove("printing-map");
    if (state.activeTab === "mapa") {
      refreshMapOverlay();
    }
  };
  window.addEventListener("afterprint", cleanup, { once: true });
  state.mapPrintMode = true;
  document.body.classList.add("printing-map");
  refreshMapOverlay();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      window.print();
    });
  });
}

async function renderMap() {
  const el = document.getElementById("tab-mapa");
  const endpoint = state.isAdmin ? "/api/admin/map" : "/api/public/map";
  const data = await apiFetch(endpoint);
  state.mapData = data;
  if (!data.stations.some((s) => s.station_code === state.mapSelectedStation)) {
    state.mapSelectedStation = data.stations[0]?.station_code || "D01";
  }

  const rooms = [...new Set(data.stations.map((s) => s.room))];
  const anonBanner = data.anonymized
    ? `<div class="banner-info">Modo público com <strong>anonimização por ID Pessoa</strong> ativo.</div>`
    : "";

  el.innerHTML = `
    ${anonBanner}
    <div id="map-legend-slot"></div>
    <div id="map-swap-banner-slot"></div>
    <div class="map-toolbar">
      <div class="map-toolbar-group">
        <input id="map-search" type="search" value="${esc(state.mapFilters.search)}" placeholder="Buscar estação, pessoa ou PC..." style="min-width:220px;" />
        <select id="map-room">
          <option value="">Todas as salas</option>
          ${rooms.map((r) => `<option value="${esc(r)}" ${state.mapFilters.room === r ? "selected" : ""}>${esc(r)}</option>`).join("")}
        </select>
        <select id="map-mode">
          <option value="">Todos os estados</option>
          ${Object.entries(MAP_MODE_LABELS)
            .map(([k, lbl]) => `<option value="${esc(k)}" ${state.mapFilters.mode === k ? "selected" : ""}>${esc(lbl)}</option>`)
            .join("")}
        </select>
        <label style="display:inline-flex; align-items:center; gap:6px; font-size:0.84rem; color:var(--text); cursor:pointer;">
          <input id="map-toggle-pcs" type="checkbox" ${state.mapShowPcs ? "checked" : ""} />
          Mostrar ID do PC no mapa
        </label>
      </div>
      <div class="map-toolbar-group">
        <div class="map-zoom-controls" aria-label="Zoom da planta">
          <button class="map-zoom-btn" data-zoom="fit" type="button">Ajustar</button>
          <button class="map-zoom-btn" data-zoom="100" type="button">100%</button>
          <button class="map-zoom-btn" data-zoom="135" type="button">135%</button>
          <button class="map-zoom-btn" data-zoom="175" type="button">175%</button>
        </div>
        ${state.isAdmin ? `<button id="btn-map-print" class="btn btn-sm btn-secondary" type="button" title="Imprimir ou salvar a planta do laboratório em PDF">Imprimir mapa</button>` : ""}
      </div>
    </div>
    <div class="map-workspace">
      <div class="map-canvas-card">
        <div class="map-viewport">
          <div id="lsee-map-stage" class="map-stage zoom-${esc(state.mapZoom)}">
            <img id="lsee-map-base" src="${esc(data.svg_base_url || "/static/img/layout_lsee_base.svg")}" alt="Planta fixa do LSEE" />
            <svg id="lsee-map-overlay" viewBox="0 0 2629.0798 4659.327" preserveAspectRatio="xMidYMid meet"></svg>
          </div>
        </div>
      </div>
      <aside class="map-sidebar">
        <div id="map-inspector-slot"></div>
        <div id="map-unplaced-slot"></div>
      </aside>
    </div>
  `;

  document.getElementById("map-search")?.addEventListener("input", (ev) => {
    state.mapFilters.search = ev.target.value;
    refreshMapOverlay();
  });
  document.getElementById("map-room")?.addEventListener("change", (ev) => {
    state.mapFilters.room = ev.target.value;
    refreshMapOverlay();
  });
  document.getElementById("map-mode")?.addEventListener("change", (ev) => {
    state.mapFilters.mode = ev.target.value;
    refreshMapOverlay();
  });
  document.getElementById("map-toggle-pcs")?.addEventListener("change", (ev) => {
    state.mapShowPcs = Boolean(ev.target.checked);
    refreshMapOverlay();
  });
  el.querySelectorAll(".map-zoom-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.mapZoom = btn.dataset.zoom || "100";
      syncMapZoomClass();
    });
  });
  document.getElementById("btn-map-print")?.addEventListener("click", printLseeMap);

  const overlay = document.getElementById("lsee-map-overlay");
  overlay?.addEventListener("click", (ev) => {
    const grp = ev.target.closest(".map-station");
    if (grp?.dataset.station) {
      handleMapStationActivate(grp.dataset.station);
    }
  });
  overlay?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      const grp = ev.target.closest(".map-station");
      if (grp?.dataset.station) {
        ev.preventDefault();
        handleMapStationActivate(grp.dataset.station);
      }
    }
  });

  refreshMapView();
}

/* ==================== DASHBOARD ==================== */
async function renderDashboard() {
  const el = document.getElementById("tab-dashboard");
  let cards, topLivres, semPc, fila, extraAdmin = "";
  let isAnon = false;

  const dashLogsCols = {
    timestamp: { type: "text" },
    actor: { type: "text" },
    action: { type: "text" },
    target: { type: "natural" },
    details: { type: "text" },
  };
  const dashTopCols = {
    rank: { type: "number" },
    id_pc: { type: "natural" },
    processador: { type: "natural" },
    ram_gb: { type: "number" },
    ssd_gb: { type: "number" },
    hd_gb: { type: "number" },
    possui_gpu: { type: "text" },
    score_total_fmt: { type: "number" },
    candidatos: { type: "number" },
  };
  const dashSemPcCols = {
    ordem: { type: "number" },
    nome: { type: "natural" },
    categoria: { type: "text" },
    entrada: { type: "entrada" },
  };
  const dashFilaCols = {
    ordem: { type: "number" },
    nome: { type: "natural" },
    categoria: { type: "text" },
    entrada: { type: "entrada" },
    pc_principal: { type: "natural", get: (p) => p.pc_principal || "" },
    score_pc_principal: { type: "number" },
  };

  if (state.isAdmin) {
    const data = await apiFetch("/api/admin/overview");
    const d = data.dashboard;
    cards = {
      pcs_individuais: d.pcs_individuais,
      em_uso: d.em_uso,
      livres: d.livres,
      manutencao: d.manutencao,
      pessoas_sem_pc: d.pessoas_sem_pc,
      pcs_incompletos: d.pcs_incompletos,
    };
    topLivres = d.top_pcs_livres;
    semPc = d.pessoas_sem_pc_lista.map((p, i) => ({
      ordem: i + 1,
      id_pessoa: p.id_pessoa,
      nome: p.nome,
      categoria: p.categoria,
      entrada: p.ano_entrada ? `${p.ano_entrada}/${p.semestre_entrada}` : "",
    }));
    fila = d.fila_prioridade.map((p) => ({
      ordem: p.ordem_prioridade,
      empate: p.empate_prioridade,
      id_pessoa: p.id_pessoa,
      nome: p.nome,
      categoria: p.categoria,
      entrada: p.ano_entrada ? `${p.ano_entrada}/${p.semestre_entrada}` : "",
      pc_principal: p.pc_principal,
      score_pc_principal: p.score_pc_principal_fmt,
    }));
    const recentLogs = applyTableSort("dash_logs", data.recent_logs.slice(0, 8), dashLogsCols);
    extraAdmin = `
      <div class="card">
        <div class="card-header"><h2 class="card-title">Atividade Recente (Auditoria)</h2></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("dash_logs", "timestamp", "Data/Hora (UTC)")}
                ${sortableTh("dash_logs", "actor", "Autor")}
                ${sortableTh("dash_logs", "action", "Ação")}
                ${sortableTh("dash_logs", "target", "Alvo")}
                ${sortableTh("dash_logs", "details", "Detalhes")}
              </tr>
            </thead>
            <tbody>
              ${recentLogs.map((l) => `
                <tr>
                  <td class="mono">${esc(l.timestamp.slice(0, 19).replace("T", " "))}</td>
                  <td>${esc(l.actor || "—")}</td>
                  <td><span class="badge badge-muted">${esc(l.action)}</span></td>
                  <td class="mono">${esc(l.target)}</td>
                  <td>${esc(l.details)}</td>
                </tr>
              `).join("") || '<tr><td colspan="5" class="muted">Sem registros ainda.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>`;
  } else {
    const data = await apiFetch("/api/public/dashboard");
    cards = data.cards;
    topLivres = data.top_pcs_livres;
    semPc = data.pessoas_sem_pc_lista;
    fila = data.fila_prioridade;
    isAnon = data.anonymized;
  }

  const sortedTopLivres = applyTableSort("dash_top", topLivres, dashTopCols);
  const sortedSemPc = applyTableSort("dash_sem_pc", semPc, dashSemPcCols);
  const sortedFila = applyTableSort("dash_fila", fila, dashFilaCols);

  el.innerHTML = `
    ${isAnon ? '<div class="notice">Modo de privacidade ativo: identificações pessoais exibidas por código (ID Pessoa).</div>' : ""}
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-label">PCs Individuais</div><div class="kpi-value">${cards.pcs_individuais}</div></div>
      <div class="kpi-card"><div class="kpi-label">Em uso</div><div class="kpi-value">${cards.em_uso}</div></div>
      <div class="kpi-card"><div class="kpi-label">Livres</div><div class="kpi-value">${cards.livres}</div></div>
      <div class="kpi-card"><div class="kpi-label">Manutenção</div><div class="kpi-value">${cards.manutencao}</div></div>
      <div class="kpi-card"><div class="kpi-label">Pessoas sem PC</div><div class="kpi-value">${cards.pessoas_sem_pc}</div></div>
      ${state.isAdmin ? `<div class="kpi-card"><div class="kpi-label">PCs Incompletos</div><div class="kpi-value">${cards.pcs_incompletos}</div><div class="kpi-sub"><a href="#" id="link-incompletos">Filtrar pendentes</a></div></div>` : ""}
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Top 5 PCs Livres</h2>
        <span class="muted" style="font-size:0.82rem;">Ordenados por Score Total</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              ${sortableTh("dash_top", "rank", "#")}
              ${sortableTh("dash_top", "id_pc", "PC")}
              ${sortableTh("dash_top", "processador", "Processador")}
              ${sortableTh("dash_top", "ram_gb", "RAM")}
              ${sortableTh("dash_top", "ssd_gb", "SSD")}
              ${sortableTh("dash_top", "hd_gb", "HD")}
              ${sortableTh("dash_top", "possui_gpu", "GPU")}
              ${sortableTh("dash_top", "score_total_fmt", "Score", scoreInfoButton("Como a nota é calculada"))}
              ${sortableTh("dash_top", "candidatos", "Candidatos Elegíveis")}
              <th>Ação</th>
            </tr>
          </thead>
          <tbody>
            ${sortedTopLivres.map((pc) => `
              <tr>
                <td>${pc.rank}</td>
                <td class="mono"><strong>${esc(pc.id_pc)}</strong></td>
                <td>${esc(pc.processador || "—")}</td>
                <td>${fmtNum(pc.ram_gb, " GB")}</td>
                <td>${fmtNum(pc.ssd_gb, " GB")}</td>
                <td>${fmtNum(pc.hd_gb, " GB")}</td>
                <td>${esc(pc.possui_gpu || "—")}</td>
                <td><strong>${fmtNum(pc.score_total_fmt)}</strong></td>
                <td>${pc.candidatos}</td>
                <td><button class="btn btn-sm btn-secondary js-sim-pc" data-pc="${esc(pc.id_pc)}" type="button">Simular</button></td>
              </tr>
            `).join("") || '<tr><td colspan="10" class="muted">Nenhum PC individual livre no momento.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-header"><h2 class="card-title">Pessoas sem PC</h2></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("dash_sem_pc", "ordem", "#")}
                ${sortableTh("dash_sem_pc", "nome", "Pessoa")}
                ${sortableTh("dash_sem_pc", "categoria", "Categoria")}
                ${sortableTh("dash_sem_pc", "entrada", "Entrada")}
              </tr>
            </thead>
            <tbody>
              ${sortedSemPc.map((p) => `
                <tr>
                  <td>${p.ordem}</td>
                  <td><strong>${esc(p.nome)}</strong></td>
                  <td>${esc(p.categoria)}</td>
                  <td>${esc(p.entrada || "—")}</td>
                </tr>
              `).join("") || '<tr><td colspan="4" class="muted">Ninguém aguardando PC no momento.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><h2 class="card-title">Fila de Prioridade (Ativos)</h2></div>
        <div class="table-wrap" style="max-height:380px;overflow-y:auto;">
          <table>
            <thead>
              <tr>
                ${sortableTh("dash_fila", "ordem", "Ordem")}
                ${sortableTh("dash_fila", "nome", "Pessoa")}
                ${sortableTh("dash_fila", "categoria", "Categoria")}
                ${sortableTh("dash_fila", "entrada", "Entrada")}
                ${sortableTh("dash_fila", "pc_principal", "PC Atual")}
                ${sortableTh("dash_fila", "score_pc_principal", "Score")}
              </tr>
            </thead>
            <tbody>
              ${sortedFila.map((p) => `
                <tr>
                  <td><strong>${p.ordem}</strong> ${p.empate ? '<span class="badge badge-warn">Empate</span>' : ""}</td>
                  <td>${esc(p.nome)}</td>
                  <td>${esc(p.categoria)}</td>
                  <td>${esc(p.entrada)}</td>
                  <td class="mono">${esc(p.pc_principal || "Sem PC")}</td>
                  <td>${fmtNum(p.score_pc_principal)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    ${extraAdmin}
  `;

  el.querySelectorAll(".js-sim-pc").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.simPc = btn.dataset.pc;
      switchTab("simulador");
    });
  });

  const linkInc = document.getElementById("link-incompletos");
  if (linkInc) {
    linkInc.addEventListener("click", (e) => {
      e.preventDefault();
      state.compFilters.pendencia = "INCOMPLETO";
      switchTab("computadores");
    });
  }
}

/* ==================== COMPUTADORES ==================== */
async function renderComputers() {
  const el = document.getElementById("tab-computadores");
  let items;
  if (state.isAdmin) {
    const data = await apiFetch("/api/admin/computers");
    items = data.items;
    state.caches.processors = data.processors;
  } else {
    const data = await apiFetch("/api/public/computers");
    items = data.items;
  }
  state.caches.computers = items;

  const f = state.compFilters;
  const filtered = items.filter((c) => {
    const q = f.search.toLowerCase();
    const matchText =
      !q ||
      (c.id_pc || "").toLowerCase().includes(q) ||
      (c.processador || "").toLowerCase().includes(q) ||
      (c.usuario_atual || "").toLowerCase().includes(q) ||
      (c.localizacao || "").toLowerCase().includes(q) ||
      (c.patrimonio || "").toLowerCase().includes(q);
    const matchStatus = !f.status || c.status === f.status;
    const matchTipo = !f.tipo || c.tipo_de_uso === f.tipo;
    const matchPend = !f.pendencia || c.validacao_dados === f.pendencia;
    return matchText && matchStatus && matchTipo && matchPend;
  });

  const compCols = {
    id_pc: { type: "natural", get: (c) => c.id_pc || "" },
    processador: { type: "natural" },
    ram_gb: { type: "number" },
    ssd_gb: { type: "number" },
    hd_gb: { type: "number" },
    possui_gpu: { type: "text" },
    tipo_de_uso: { type: "text" },
    status: { type: "pcStatus" },
    score: { type: "number", get: (c) => (state.isAdmin ? c.score_total_fmt : c.score_total) },
    usuario_atual: { type: "natural" },
    localizacao: { type: "natural" },
    pendencia: {
      type: "text",
      get: (c) => (c.validacao_dados === "OK" ? "0 OK" : c.validacao_dados ? `1 ${c.pendencia || c.validacao_dados}` : ""),
    },
  };
  const sorted = applyTableSort("computadores", filtered, compCols);

  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Inventário de Computadores (${filtered.length})</h2>
        ${state.isAdmin ? '<span class="row-actions"><button id="btn-comp-tutorial" class="btn btn-secondary btn-sm" type="button">Tutorial: Liberar PC</button><button id="btn-go-labels-pc" class="btn btn-secondary btn-sm" type="button">Etiquetas</button><button id="btn-new-pc" class="btn btn-primary btn-sm" type="button">+ Novo PC</button></span>' : ""}
      </div>
      <div class="card-body">
        <div class="toolbar">
          <input id="comp-search" class="input" type="search" placeholder="Buscar PC, CPU, usuário, local..." value="${esc(f.search)}" />
          <select id="comp-status" class="select">
            <option value="">Todos os Status</option>
            ${(state.isAdmin
              ? ["Livre", "Em uso", "Manutenção", "Indisponível", "Despatrimoniado"]
              : ["Livre", "Em uso"]
            )
              .map((s) => `<option value="${s}" ${f.status === s ? "selected" : ""}>${s}</option>`)
              .join("")}
          </select>
          <select id="comp-tipo" class="select">
            <option value="">Todos os Tipos</option>
            ${["Individual", "Compartilhado IC", "Especial", "Sala de Reunião"]
              .map((t) => `<option value="${t}" ${f.tipo === t ? "selected" : ""}>${t}</option>`)
              .join("")}
          </select>
          ${state.isAdmin ? `
            <select id="comp-pend" class="select">
              <option value="">Validação (Todas)</option>
              <option value="OK" ${f.pendencia === "OK" ? "selected" : ""}>OK</option>
              <option value="INCOMPLETO" ${f.pendencia === "INCOMPLETO" ? "selected" : ""}>INCOMPLETO</option>
            </select>
          ` : ""}
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("computadores", "id_pc", "ID_PC")}
                ${sortableTh("computadores", "processador", "Processador")}
                ${sortableTh("computadores", "ram_gb", "RAM")}
                ${sortableTh("computadores", "ssd_gb", "SSD")}
                ${sortableTh("computadores", "hd_gb", "HD")}
                ${sortableTh("computadores", "possui_gpu", "GPU")}
                ${sortableTh("computadores", "tipo_de_uso", "Tipo de Uso")}
                ${sortableTh("computadores", "status", "Status")}
                ${sortableTh("computadores", "score", "Score", scoreInfoButton("Como a nota é calculada"))}
                ${sortableTh("computadores", "usuario_atual", "Usuário Atual")}
                ${sortableTh("computadores", "localizacao", "Localização")}
                ${state.isAdmin ? `${sortableTh("computadores", "pendencia", "Validação / Pendência")}<th>Ações</th>` : ""}
              </tr>
            </thead>
            <tbody>
              ${sorted.map((c) => `
                <tr>
                  <td class="mono"><strong>${esc(c.id_pc || "(sem ID)")}</strong></td>
                  <td>${esc(c.processador || "—")}</td>
                  <td>${fmtNum(c.ram_gb, " GB")}</td>
                  <td>${fmtNum(c.ssd_gb, " GB")}</td>
                  <td>${fmtNum(c.hd_gb, " GB")}</td>
                  <td>${esc(c.possui_gpu || "—")}</td>
                  <td>${esc(c.tipo_de_uso || "—")}</td>
                  <td>${statusBadge(c.status)}</td>
                  <td><strong>${fmtNum(state.isAdmin ? c.score_total_fmt : c.score_total)}</strong></td>
                  <td>${esc(c.usuario_atual || "—")}</td>
                  <td>${esc(c.localizacao || "—")}</td>
                  ${state.isAdmin ? `
                    <td>
                      ${c.validacao_dados === "OK" ? '<span class="badge badge-ok">OK</span>' : c.validacao_dados ? `<span class="badge badge-incompleto">INCOMPLETO</span> <span class="muted" style="font-size:0.75rem;">${esc(c.pendencia)}</span>` : "—"}
                    </td>
                    <td>
                      <span class="row-actions">
                        <button class="btn btn-sm btn-secondary js-edit-pc" data-id="${c.id}" type="button">Editar</button>
                        <button class="btn btn-sm btn-secondary js-pc-label" data-id="${c.id}" type="button" title="Enviar este PC para o gerador de etiquetas">Etiqueta</button>
                      </span>
                    </td>
                  ` : ""}
                </tr>
              `).join("") || `<tr><td colspan="${state.isAdmin ? 13 : 11}" class="muted">Nenhum computador encontrado.</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  document.getElementById("comp-search").addEventListener("input", (e) => {
    state.compFilters.search = e.target.value;
    renderComputers();
  });
  document.getElementById("comp-status").addEventListener("change", (e) => {
    state.compFilters.status = e.target.value;
    renderComputers();
  });
  document.getElementById("comp-tipo").addEventListener("change", (e) => {
    state.compFilters.tipo = e.target.value;
    renderComputers();
  });

  if (state.isAdmin) {
    document.getElementById("comp-pend").addEventListener("change", (e) => {
      state.compFilters.pendencia = e.target.value;
      renderComputers();
    });
    document.getElementById("btn-comp-tutorial").addEventListener("click", openReleaseTutorialModal);
    document.getElementById("btn-go-labels-pc").addEventListener("click", () => {
      state.labels.mode = "pc";
      switchTab("etiquetas");
    });
    document.getElementById("btn-new-pc").addEventListener("click", () => openComputerModal(null));
    el.querySelectorAll(".js-edit-pc").forEach((btn) => {
      btn.addEventListener("click", () => {
        const pc = state.caches.computers.find((x) => x.id === Number(btn.dataset.id));
        if (pc) openComputerModal(pc);
      });
    });
    el.querySelectorAll(".js-pc-label").forEach((btn) => {
      btn.addEventListener("click", () => {
        const pc = state.caches.computers.find((x) => x.id === Number(btn.dataset.id));
        if (pc) {
          openEquipmentInLabels("pc", pc);
        }
      });
    });
  }
}

function openComputerModal(pc) {
  const isNew = !pc;
  const p = pc || {};
  const procs = state.caches.processors || [];
  openModal(isNew ? "Novo Computador" : `Editar ${p.id_pc || "Computador"}`, `
    <form id="form-pc">
      <div class="form-grid">
        <div class="form-field"><label>ID_PC</label><input class="input" name="id_pc" value="${esc(p.id_pc || "")}" placeholder="Ex: PC100" /></div>
        <div class="form-field"><label>Patrimônio</label><input class="input" name="patrimonio" value="${esc(p.patrimonio || "")}" /></div>
        <div class="form-field">
          <label>Processador</label>
          <select class="select" name="processador">
            <option value="">-- Não informado --</option>
            ${procs.map((pr) => `<option value="${esc(pr)}" ${p.processador === pr ? "selected" : ""}>${esc(pr)}</option>`).join("")}
          </select>
        </div>
        <div class="form-field"><label>RAM (GB)</label><input class="input" type="number" step="any" name="ram_gb" value="${p.ram_gb ?? ""}" /></div>
        <div class="form-field"><label>SSD (GB)</label><input class="input" type="number" step="any" name="ssd_gb" value="${p.ssd_gb ?? ""}" /></div>
        <div class="form-field"><label>HD (GB)</label><input class="input" type="number" step="any" name="hd_gb" value="${p.hd_gb ?? ""}" /></div>
        <div class="form-field">
          <label>Possui GPU</label>
          <select class="select" name="possui_gpu">
            <option value="">-- Selecione --</option>
            <option value="Sim" ${p.possui_gpu === "Sim" ? "selected" : ""}>Sim</option>
            <option value="Não" ${p.possui_gpu === "Não" ? "selected" : ""}>Não</option>
          </select>
        </div>
        <div class="form-field">
          <label>Tipo de Uso</label>
          <select class="select" name="tipo_de_uso">
            <option value="">-- Selecione --</option>
            ${["Individual", "Compartilhado IC", "Especial", "Sala de Reunião"]
              .map((t) => `<option value="${t}" ${p.tipo_de_uso === t ? "selected" : ""}>${t}</option>`)
              .join("")}
          </select>
        </div>
        <div class="form-field">
          <label>Status Manual (exceção)</label>
          <select class="select" name="status_manual">
            <option value="">Automático</option>
            ${["Manutenção", "Indisponível", "Despatrimoniado"]
              .map((s) => `<option value="${s}" ${p.status_manual === s ? "selected" : ""}>${s}</option>`)
              .join("")}
          </select>
        </div>
        <div class="form-field">
          <label>Localização</label>
          ${renderLocationSelect("localizacao", p.localizacao)}
        </div>
        <div class="form-field"><label>Projeto (para etiqueta)</label><input class="input" name="projeto" value="${esc(p.projeto || "")}" placeholder="Ex: FAPESP 2021/04872-9 ou -" /></div>
      </div>
      <div class="form-field" style="margin-top:10px;">
        <label>Observações</label>
        <textarea name="observacoes" rows="2">${esc(p.observacoes || "")}</textarea>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar</button>
      </div>
    </form>
  `);

  document.getElementById("form-pc").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = Object.fromEntries(fd.entries());
    try {
      if (isNew) {
        await apiFetch("/api/admin/computers", { method: "POST", body: JSON.stringify(payload) });
        showToast("Computador cadastrado!");
      } else {
        await apiFetch(`/api/admin/computers/${p.id}`, { method: "PUT", body: JSON.stringify(payload) });
        showToast("Computador atualizado!");
      }
      closeModal();
      renderComputers();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== SIMULADOR ==================== */
async function renderSimulator() {
  const el = document.getElementById("tab-simulador");
  const qs = state.simPc ? `?pc=${encodeURIComponent(state.simPc)}` : "";
  const data = state.isAdmin
    ? await apiFetch(`/api/admin/simulator${qs}`)
    : await apiFetch(`/api/public/simulator${qs}`);

  const pc = data.pc;
  const availablePcs = data.available_pcs || [];
  state.simPc = pc ? pc.id_pc : "";

  const simRowsCols = {
    ordem: { type: "number" },
    pessoa: { type: "natural" },
    categoria: { type: "text" },
    entrada: { type: "entrada" },
    pc_principal: { type: "natural", get: (r) => r.pc_principal || "" },
    score_atual: { type: "number", get: (r) => (state.isAdmin ? r.score_atual_fmt : r.score_atual) },
    score_oferecido: { type: "number", get: (r) => (state.isAdmin ? r.score_oferecido_fmt : r.score_oferecido) },
    ganho: { type: "number", get: (r) => (state.isAdmin ? r.ganho_fmt : r.ganho) },
    resultado: { type: "text" },
    ultimo_upgrade: { type: "text" },
  };
  const simRows = applyTableSort("sim_rows", data.rows || [], simRowsCols);

  const simDecisionCols = {
    created_at: { type: "text" },
    pc_oferta: { type: "natural" },
    id_pessoa: { type: "natural" },
    decisao: { type: "text" },
    observacoes: { type: "text" },
  };
  const simDecisions = state.isAdmin ? applyTableSort("sim_decisions", data.decisions || [], simDecisionCols) : [];

  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Simulador de Upgrade e Cascata</h2>
        <span class="muted" style="font-size:0.82rem;">Diferença mínima configurada: +${esc(data.diff_min_upgrade)} pontos</span>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <label for="sim-pc-select" style="font-weight:600;font-size:0.86rem;">PC em oferta:</label>
          <select id="sim-pc-select" class="select" ${availablePcs.length ? "" : "disabled"}>
            ${availablePcs.length
              ? availablePcs.map((c) => `
                  <option value="${esc(c.id_pc)}" ${pc && pc.id_pc === c.id_pc ? "selected" : ""}>
                    ${esc(c.id_pc)} · ${esc(c.status)} · Score ${fmtNum(c.score_total)} (${esc(c.processador || "sem CPU")})
                  </option>
                `).join("")
              : '<option value="">Nenhum PC livre disponível</option>'}
          </select>
        </div>

        ${pc ? `
          <div class="kpi-grid" style="margin-bottom:14px;">
            <div class="kpi-card"><div class="kpi-label">PC Ofertado</div><div class="kpi-value mono">${esc(pc.id_pc)}</div><div class="kpi-sub">${statusBadge(pc.status)} · ${esc(pc.tipo_de_uso)}</div></div>
            <div class="kpi-card"><div class="kpi-label">Score do PC ${scoreInfoButton("Como a nota é calculada")}</div><div class="kpi-value">${fmtNum(pc.score_total_fmt)}</div><div class="kpi-sub">${esc(pc.processador || "—")}</div></div>
            <div class="kpi-card"><div class="kpi-label">Memória / Disco</div><div class="kpi-value" style="font-size:1.1rem;">${fmtNum(pc.ram_gb, " GB RAM")}</div><div class="kpi-sub">SSD ${fmtNum(pc.ssd_gb, " GB")} · HD ${fmtNum(pc.hd_gb, " GB")} · GPU ${esc(pc.possui_gpu || "—")}</div></div>
          </div>
        ` : '<div class="notice">Não há computadores com status <strong>Livre</strong> disponíveis para oferta no momento.</div>'}

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("sim_rows", "ordem", "Ordem")}
                ${sortableTh("sim_rows", "pessoa", "Pessoa")}
                ${sortableTh("sim_rows", "categoria", "Categoria")}
                ${sortableTh("sim_rows", "entrada", "Entrada")}
                ${sortableTh("sim_rows", "pc_principal", "PC Principal")}
                ${sortableTh("sim_rows", "score_atual", "Score Atual")}
                ${sortableTh("sim_rows", "score_oferecido", "Score Oferecido")}
                ${sortableTh("sim_rows", "ganho", "Ganho")}
                ${sortableTh("sim_rows", "resultado", "Resultado")}
                ${sortableTh("sim_rows", "ultimo_upgrade", "Último Upgrade")}
                ${state.isAdmin ? "<th>Ação Admin</th>" : ""}
              </tr>
            </thead>
            <tbody>
              ${simRows.map((r) => `
                <tr>
                  <td>${r.ordem ?? "—"}</td>
                  <td><strong>${esc(r.pessoa)}</strong></td>
                  <td>${esc(r.categoria)}</td>
                  <td>${esc(r.entrada || "—")}</td>
                  <td class="mono">${esc(r.pc_principal || "Sem PC")}</td>
                  <td>${fmtNum(state.isAdmin ? r.score_atual_fmt : r.score_atual)}</td>
                  <td>${fmtNum(state.isAdmin ? r.score_oferecido_fmt : r.score_oferecido)}</td>
                  <td><strong>${r.ganho !== null && r.ganho !== undefined ? (Number(state.isAdmin ? r.ganho_fmt : r.ganho) > 0 ? `+${esc(state.isAdmin ? r.ganho_fmt : r.ganho)}` : esc(state.isAdmin ? r.ganho_fmt : r.ganho)) : "—"}</strong></td>
                  <td>${simResultBadge(r.resultado)}</td>
                  <td>${esc(r.ultimo_upgrade)}</td>
                  ${state.isAdmin ? `
                    <td>
                      <button class="btn btn-sm btn-primary js-sim-commit" data-pid="${esc(r.id_pessoa)}" data-nome="${esc(r.pessoa)}" type="button">Registrar</button>
                    </td>
                  ` : ""}
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    ${state.isAdmin ? `
      <div class="card">
        <div class="card-header"><h2 class="card-title">Histórico de Decisões Registradas</h2></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("sim_decisions", "created_at", "Data/Hora")}
                ${sortableTh("sim_decisions", "pc_oferta", "PC Ofertado")}
                ${sortableTh("sim_decisions", "id_pessoa", "ID Pessoa")}
                ${sortableTh("sim_decisions", "decisao", "Decisão")}
                ${sortableTh("sim_decisions", "observacoes", "Observações")}
              </tr>
            </thead>
            <tbody>
              ${simDecisions.map((d) => `
                <tr>
                  <td class="mono">${esc(d.created_at.slice(0, 19).replace("T", " "))}</td>
                  <td class="mono">${esc(d.pc_oferta)}</td>
                  <td class="mono">${esc(d.id_pessoa)}</td>
                  <td><span class="badge badge-muted">${esc(d.decisao)}</span></td>
                  <td>${esc(d.observacoes || "—")}</td>
                </tr>
              `).join("") || '<tr><td colspan="5" class="muted">Nenhuma decisão registrada pelo simulador ainda.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    ` : ""}
  `;

  document.getElementById("sim-pc-select").addEventListener("change", (e) => {
    state.simPc = e.target.value;
    renderSimulator();
  });

  if (state.isAdmin) {
    el.querySelectorAll(".js-sim-commit").forEach((btn) => {
      btn.addEventListener("click", () => openCommitModal(pc.id_pc, btn.dataset.pid, btn.dataset.nome));
    });
  }
}

function openCommitModal(pcOferta, idPessoa, nomePessoa) {
  openModal(`Registrar Decisão — ${pcOferta}`, `
    <p>Pessoa: <strong>${esc(nomePessoa)}</strong> (<span class="mono">${esc(idPessoa)}</span>)</p>
    <form id="form-commit">
      <div class="form-field">
        <label>Decisão</label>
        <select class="select" name="decisao" id="commit-decisao">
          <option value="Aceitou">Aceitou</option>
          <option value="Recusou">Recusou</option>
          <option value="Desistiu">Desistiu</option>
          <option value="Pendente">Pendente</option>
        </select>
      </div>
      <div class="form-field" id="wrap-efetivar" style="margin-top:10px;">
        <label><input type="checkbox" name="efetivar" checked /> Efetivar alocação agora (encerra PC antigo em cascata)</label>
      </div>
      <div class="form-field" style="margin-top:10px;">
        <label>Observações</label>
        <input class="input" name="observacoes" placeholder="Opcional" />
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Confirmar</button>
      </div>
    </form>
  `);

  document.getElementById("commit-decisao").addEventListener("change", (e) => {
    document.getElementById("wrap-efetivar").classList.toggle("hidden", e.target.value !== "Aceitou");
  });

  document.getElementById("form-commit").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const res = await apiFetch("/api/admin/simulator/commit", {
        method: "POST",
        body: JSON.stringify({
          pc_oferta: pcOferta,
          id_pessoa: idPessoa,
          decisao: fd.get("decisao"),
          efetivar: fd.get("efetivar") === "on",
          observacoes: fd.get("observacoes") || "",
        }),
      });
      closeModal();
      if (res.freed_pc) {
        showToast(`Upgrade efetivado! ${res.freed_pc} foi liberado para cascata.`);
        state.simPc = res.freed_pc;
      } else {
        showToast("Decisão registrada.");
      }
      renderSimulator();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== CRITÉRIOS E PROCESSADORES (ADMIN) ==================== */
async function renderCriterios() {
  const el = document.getElementById("tab-criterios");
  const [data, procResp] = await Promise.all([
    apiFetch("/api/admin/rules"),
    apiFetch("/api/admin/processors"),
  ]);
  const procs = procResp.items;

  const armRefPcs = (data.arm_ref_pcs || []).length ? ` (${data.arm_ref_pcs.join(", ")})` : "";
  const categories = applyTableSort("crit_cats", data.categories, {
    name: { type: "text" },
    priority_group: { type: "number" },
    participa_upgrade: { type: "bool" },
    precisa_pc: { type: "bool" },
  });
  const ramThresholds = applyTableSort("crit_ram", data.thresholds.ram || [], {
    min_value: { type: "number" },
    score: { type: "number" },
  });
  const armExamples = applyTableSort("crit_arm", data.arm_examples || [], {
    label: { type: "natural" },
    armaz_eq: { type: "number" },
    score: { type: "number" },
  });
  const sortedProcs = applyTableSort("crit_procs", procs, {
    name: { type: "natural" },
    threads: { type: "number" },
    multi: { type: "number" },
    single: { type: "number" },
    indice_cpu: { type: "number" },
    fonte: { type: "text" },
  });

  el.innerHTML = `
    <div class="notice">
      Fórmula automática: <strong>Score_Total = Score_CPU×${(data.pesos.cpu * 10).toFixed(2)} + Score_RAM×${(data.pesos.ram * 10).toFixed(2)} + Score_Armaz×${(data.pesos.armazenamento * 10).toFixed(2)} + Score_GPU×${(data.pesos.gpu * 10).toFixed(2)}</strong>
      (escala de 0 a 100; ref. CPU = <span class="mono">${esc(data.cpu_ref)}</span> · ref. Armaz = <span class="mono">${esc(data.arm_ref)} GB eq.</span>)
      ${scoreInfoButton("Ver explicação simplificada do cálculo")}
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <h2 class="card-title">Pesos do Score e Regras Gerais</h2>
          <button id="btn-goto-params" class="btn btn-secondary btn-sm" type="button">Alterar em Parâmetros</button>
        </div>
        <div class="card-body">
          <table>
            <tbody>
              <tr><td>Peso CPU</td><td><strong>${(data.pesos.cpu * 100).toFixed(1)}%</strong> (Ref. CPU atual: <span class="mono">${esc(data.cpu_ref)}</span>)</td></tr>
              <tr><td>Peso RAM</td><td><strong>${(data.pesos.ram * 100).toFixed(1)}%</strong></td></tr>
              <tr><td>Peso Armazenamento ${scoreInfoButton("Explicação simplificada do cálculo de Armazenamento")}</td><td><strong>${(data.pesos.armazenamento * 100).toFixed(1)}%</strong> (Ref. Individual: <span class="mono">${esc(data.arm_ref)} GB eq.</span>${esc(armRefPcs)})</td></tr>
              <tr><td>Peso GPU</td><td><strong>${(data.pesos.gpu * 100).toFixed(1)}%</strong></td></tr>
              <tr><td>Diferença mínima p/ upgrade</td><td><strong>+${esc(data.diferenca_minima_upgrade)} pontos</strong></td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h2 class="card-title">Prioridade por Categoria</h2>
          <button class="btn btn-primary btn-sm js-edit-criterios" type="button">Editar Categorias e RAM</button>
        </div>
        <div class="card-body">
          <table>
            <thead>
              <tr>
                ${sortableTh("crit_cats", "name", "Categoria")}
                ${sortableTh("crit_cats", "priority_group", "Grupo")}
                ${sortableTh("crit_cats", "participa_upgrade", "Participa Upgrade")}
                ${sortableTh("crit_cats", "precisa_pc", "Conta como Sem PC")}
              </tr>
            </thead>
            <tbody>
              ${categories.map((c) => `
                <tr>
                  <td><strong>${esc(c.name)}</strong></td>
                  <td>${c.priority_group ?? "—"}</td>
                  <td>${c.participa_upgrade ? "Sim" : "Não"}</td>
                  <td>${c.precisa_pc ? "Sim" : "Não"}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Faixa de RAM e Escala de Armazenamento</h2>
        <button class="btn btn-secondary btn-sm js-edit-criterios" type="button">Editar</button>
      </div>
      <div class="card-body">
        <div class="grid-2" style="margin-bottom:0;">
          <div>
            <strong>RAM mínima (GB)</strong>
            <table>
              <thead><tr>${sortableTh("crit_ram", "min_value", "GB")}${sortableTh("crit_ram", "score", "Score")}</tr></thead>
              <tbody>
                ${ramThresholds.map((t) => `<tr><td>${esc(t.min_value)} GB</td><td>${esc(t.score)}</td></tr>`).join("")}
              </tbody>
            </table>
          </div>
          <div>
            <strong>Armazenamento (Capacidade Equivalente) ${scoreInfoButton("Explicação simplificada do cálculo de Armazenamento")}</strong>
            <p class="muted" style="font-size:0.8rem;margin:6px 0 8px;">
              <span class="mono">GB_eq = SSD + ${esc(data.armaz_peso_hd)} × HD</span> · curva logarítmica até o teto <strong>${esc(data.score_maximo)}</strong> em <span class="mono">${esc(data.arm_ref)} GB eq.</span>
            </p>
            <table>
              <thead><tr>${sortableTh("crit_arm", "label", "Configuração exemplo")}${sortableTh("crit_arm", "armaz_eq", "GB eq.")}${sortableTh("crit_arm", "score", "Nota")}</tr></thead>
              <tbody>
                ${armExamples.map((ex) => `<tr><td>${esc(ex.label)}</td><td class="mono">${esc(ex.armaz_eq)}</td><td><strong>${esc(ex.score)}</strong></td></tr>`).join("")}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Catálogo de Processadores (Benchmarks)</h2>
        <button id="btn-new-proc" class="btn btn-primary btn-sm" type="button">+ Novo Processador</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              ${sortableTh("crit_procs", "name", "Processador")}
              ${sortableTh("crit_procs", "threads", "Threads")}
              ${sortableTh("crit_procs", "multi", "Multi")}
              ${sortableTh("crit_procs", "single", "Single")}
              ${sortableTh("crit_procs", "indice_cpu", "Índice CPU")}
              ${sortableTh("crit_procs", "fonte", "Fonte")}
              <th>Ações</th>
            </tr>
          </thead>
          <tbody>
            ${sortedProcs.map((p) => `
              <tr>
                <td><strong>${esc(p.name)}</strong></td>
                <td>${esc(p.threads)}</td>
                <td>${esc(p.multi)}</td>
                <td>${esc(p.single)}</td>
                <td class="mono"><strong>${esc(p.indice_cpu)}</strong></td>
                <td>${esc(p.fonte || "—")}</td>
                <td><button class="btn btn-sm btn-secondary js-edit-proc" data-id="${p.id}" type="button">Editar</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;

  document.getElementById("btn-goto-params")?.addEventListener("click", () => switchTab("parametros"));
  el.querySelectorAll(".js-edit-criterios").forEach((btn) => {
    btn.addEventListener("click", () => openCriteriosModal(data));
  });
  document.getElementById("btn-new-proc").addEventListener("click", () => openProcessorModal(null));
  el.querySelectorAll(".js-edit-proc").forEach((btn) => {
    btn.addEventListener("click", () => {
      const proc = procs.find((x) => x.id === Number(btn.dataset.id));
      if (proc) openProcessorModal(proc);
    });
  });
}

function openCriteriosModal(data) {
  const ramRows = [...(data.thresholds?.ram || [])].sort((a, b) => Number(a.min_value) - Number(b.min_value));
  const cats = (data.categories || []).map((c) => ({ ...c }));

  const ramRowHtml = (mv = "", sc = "") => `
    <tr class="js-crit-ram-row">
      <td><input class="input js-crit-ram-gb" type="number" min="0" step="any" value="${esc(mv)}" required style="width:100%;" /></td>
      <td><input class="input js-crit-ram-score" type="number" min="0" max="${esc(data.score_maximo || 10)}" step="any" value="${esc(sc)}" required style="width:100%;" /></td>
      <td style="width:44px;text-align:center;"><button class="btn btn-sm btn-danger js-crit-ram-del" type="button" title="Remover faixa">&times;</button></td>
    </tr>
  `;

  openModal("Editar Faixas de RAM e Categorias", `
    <p class="muted" style="margin-top:0;font-size:0.84rem;">
      Pesos, diferença mínima para upgrade e parâmetros de armazenamento ficam centralizados na aba <strong>Parâmetros &amp; Log</strong>.
    </p>
    <form id="form-criterios">
      <div class="grid-2" style="margin-bottom:12px;">
        <div class="card" style="margin-bottom:0;">
          <div class="card-header">
            <h3 class="card-title" style="font-size:0.9rem;">Score por Faixa de RAM (teto: ${esc(data.score_maximo)})</h3>
            <button id="btn-crit-add-ram" class="btn btn-sm btn-secondary" type="button">+ Faixa</button>
          </div>
          <div class="card-body">
            <table>
              <thead><tr><th>RAM mínima (GB)</th><th>Score</th><th></th></tr></thead>
              <tbody id="crit-ram-tbody">
                ${ramRows.map((t) => ramRowHtml(t.min_value, t.score)).join("")}
              </tbody>
            </table>
          </div>
        </div>

        <div class="card" style="margin-bottom:0;">
          <div class="card-header"><h3 class="card-title" style="font-size:0.9rem;">Prioridade por Categoria</h3></div>
          <div class="card-body">
            <table>
              <thead><tr><th>Categoria</th><th>Grupo</th><th>Upgrade</th><th>Sem PC</th></tr></thead>
              <tbody>
                ${cats.map((c, idx) => `
                  <tr class="js-crit-cat-row" data-name="${esc(c.name)}">
                    <td><strong>${esc(c.name)}</strong></td>
                    <td>
                      <select class="select js-crit-cat-group" aria-label="Grupo de ${esc(c.name)}">
                        <option value="" ${!c.priority_group ? "selected" : ""}>Sem grupo</option>
                        ${[1, 2, 3, 4].map((g) => `<option value="${g}" ${Number(c.priority_group) === g ? "selected" : ""}>Grupo ${g}</option>`).join("")}
                      </select>
                    </td>
                    <td style="text-align:center;"><input class="js-crit-cat-up" type="checkbox" ${c.participa_upgrade ? "checked" : ""} aria-label="Participa Upgrade ${idx}" /></td>
                    <td style="text-align:center;"><input class="js-crit-cat-sempc" type="checkbox" ${c.precisa_pc ? "checked" : ""} aria-label="Conta como Sem PC ${idx}" /></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="form-actions" style="margin-top:0;">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar Faixas e Categorias</button>
      </div>
    </form>
  `, true);

  const ramTbody = document.getElementById("crit-ram-tbody");
  ramTbody.addEventListener("click", (e) => {
    const delBtn = e.target.closest(".js-crit-ram-del");
    if (!delBtn) return;
    if (ramTbody.querySelectorAll(".js-crit-ram-row").length <= 1) {
      showToast("Mantenha pelo menos uma faixa de RAM.", true);
      return;
    }
    delBtn.closest(".js-crit-ram-row")?.remove();
  });
  document.getElementById("btn-crit-add-ram").addEventListener("click", () => {
    ramTbody.insertAdjacentHTML("beforeend", ramRowHtml("", ""));
  });

  document.getElementById("form-criterios").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ram_thresholds = [...ramTbody.querySelectorAll(".js-crit-ram-row")].map((tr) => ({
      min_value: Number(tr.querySelector(".js-crit-ram-gb").value),
      score: Number(tr.querySelector(".js-crit-ram-score").value),
    }));
    const categories = [...document.querySelectorAll(".js-crit-cat-row")].map((tr) => {
      const grpVal = tr.querySelector(".js-crit-cat-group").value;
      return {
        name: tr.dataset.name,
        priority_group: grpVal === "" ? null : Number(grpVal),
        participa_upgrade: tr.querySelector(".js-crit-cat-up").checked ? 1 : 0,
        precisa_pc: tr.querySelector(".js-crit-cat-sempc").checked ? 1 : 0,
      };
    });

    try {
      await apiFetch("/api/admin/rules", {
        method: "PUT",
        body: JSON.stringify({ ram_thresholds, categories }),
      });
      closeModal();
      showToast("Faixas de RAM e categorias atualizadas!");
      renderCriterios();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openProcessorModal(proc) {
  const isNew = !proc;
  const p = proc || {};
  openModal(isNew ? "Novo Processador" : `Editar ${p.name}`, `
    <div class="notice" style="margin-bottom:12px;">
      <strong>Protocolo de Avaliação (CPU-Z):</strong>
      <ol style="margin:6px 0 0 18px;padding:0;">
        <li>Ligue o computador e aguarde o Windows inicializar por completo.</li>
        <li>Execute o benchmark do <strong>CPU-Z 2 vezes consecutivas</strong> e anote <strong>Multi</strong> e <strong>Single</strong> apenas da <strong>2ª execução</strong>.</li>
        <li>Anote também o número total de <strong>Threads</strong> do processador.</li>
      </ol>
    </div>
    <form id="form-proc">
      <div class="form-grid">
        <div class="form-field"><label>Nome</label><input class="input" name="name" value="${esc(p.name || "")}" ${isNew ? "required" : "readonly"} /></div>
        <div class="form-field"><label>Threads</label><input class="input" type="number" name="threads" value="${p.threads ?? ""}" required /></div>
        <div class="form-field"><label>Multi</label><input class="input" type="number" step="any" name="multi" value="${p.multi ?? ""}" required /></div>
        <div class="form-field"><label>Single</label><input class="input" type="number" step="any" name="single" value="${p.single ?? ""}" required /></div>
        <div class="form-field"><label>Fonte</label><input class="input" name="fonte" value="${esc(p.fonte || "")}" /></div>
        <div class="form-field"><label>Data Referência</label><input class="input" name="data_referencia" value="${esc(p.data_referencia || "")}" /></div>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar</button>
      </div>
    </form>
  `);
  document.getElementById("form-proc").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      if (isNew) {
        await apiFetch("/api/admin/processors", { method: "POST", body: JSON.stringify(payload) });
      } else {
        await apiFetch(`/api/admin/processors/${p.id}`, { method: "PUT", body: JSON.stringify(payload) });
      }
      closeModal();
      showToast("Processador salvo!");
      renderCriterios();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== PESSOAS (ADMIN) ==================== */
async function renderPeople() {
  const el = document.getElementById("tab-pessoas");
  const data = await apiFetch("/api/admin/people");
  state.caches.people = data.items;
  const f = state.peopleFilters;
  const filtered = data.items.filter((p) => {
    const q = f.search.toLowerCase();
    const m1 = !q || p.nome.toLowerCase().includes(q) || p.id_pessoa.toLowerCase().includes(q) || (p.pc_principal || "").toLowerCase().includes(q);
    const m2 = !f.situacao || p.situacao === f.situacao;
    return m1 && m2;
  });
  const sorted = applyTableSort("pessoas", filtered, {
    id_pessoa: { type: "natural" },
    nome: { type: "text" },
    categoria: { type: "text" },
    entrada: { type: "entrada", get: (p) => (p.ano_entrada ? `${p.ano_entrada}/${p.semestre_entrada || 0}` : "") },
    situacao: { type: "text" },
    quantidade_pcs: { type: "number" },
    pc_principal: { type: "natural", get: (p) => p.pc_principal || "" },
    score_pc_principal_fmt: { type: "number" },
    ordem_prioridade: { type: "number" },
    pendencia: {
      type: "text",
      get: (p) => (p.validacao_dados === "OK" ? "0 OK" : `1 ${p.pendencia || p.validacao_dados || ""}`),
    },
  });

  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Cadastro de Pessoas (${filtered.length})</h2>
        <button id="btn-new-person" class="btn btn-primary btn-sm" type="button">+ Nova Pessoa</button>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <input id="ppl-search" class="input" type="search" placeholder="Buscar nome, ID ou PC..." value="${esc(f.search)}" />
          <select id="ppl-sit" class="select">
            <option value="">Todas as Situações</option>
            ${["Ativo", "Egresso com PC", "Egresso sem PC", "Inativo"].map((s) => `<option value="${s}" ${f.situacao === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("pessoas", "id_pessoa", "ID")}
                ${sortableTh("pessoas", "nome", "Nome")}
                ${sortableTh("pessoas", "categoria", "Categoria")}
                ${sortableTh("pessoas", "entrada", "Entrada")}
                ${sortableTh("pessoas", "situacao", "Situação")}
                ${sortableTh("pessoas", "quantidade_pcs", "Qtd PCs")}
                ${sortableTh("pessoas", "pc_principal", "PC Principal")}
                ${sortableTh("pessoas", "score_pc_principal_fmt", "Score")}
                ${sortableTh("pessoas", "ordem_prioridade", "Prioridade")}
                ${sortableTh("pessoas", "pendencia", "Pendência")}
                <th>Ações</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map((p) => `
                <tr>
                  <td class="mono">${esc(p.id_pessoa)}</td>
                  <td><strong>${esc(p.nome)}</strong></td>
                  <td>${esc(p.categoria)}</td>
                  <td>${p.ano_entrada ? `${p.ano_entrada}/${p.semestre_entrada}` : "—"}</td>
                  <td>${esc(p.situacao)}</td>
                  <td>${p.quantidade_pcs}</td>
                  <td class="mono">${esc(p.pc_principal || "—")}</td>
                  <td>${fmtNum(p.score_pc_principal_fmt)}</td>
                  <td>${p.ordem_prioridade ?? "—"} ${p.empate_prioridade ? '<span class="badge badge-warn">Empate</span>' : ""}</td>
                  <td>${p.validacao_dados === "OK" ? '<span class="badge badge-ok">OK</span>' : `<span class="badge badge-incompleto">${esc(p.pendencia)}</span>`}</td>
                  <td><button class="btn btn-sm btn-secondary js-edit-person" data-id="${p.id}" type="button">Editar</button></td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  document.getElementById("ppl-search").addEventListener("input", (e) => {
    state.peopleFilters.search = e.target.value;
    renderPeople();
  });
  document.getElementById("ppl-sit").addEventListener("change", (e) => {
    state.peopleFilters.situacao = e.target.value;
    renderPeople();
  });
  document.getElementById("btn-new-person").addEventListener("click", () => openPersonModal(null, data.categories));
  el.querySelectorAll(".js-edit-person").forEach((btn) => {
    btn.addEventListener("click", () => {
      const p = data.items.find((x) => x.id === Number(btn.dataset.id));
      if (p) openPersonModal(p, data.categories);
    });
  });
}

function openPersonModal(person, categories) {
  const isNew = !person;
  const p = person || {};
  openModal(isNew ? "Nova Pessoa" : `Editar ${p.nome}`, `
    <form id="form-person">
      <div class="form-grid">
        <div class="form-field"><label>ID Pessoa</label><input class="input" name="id_pessoa" value="${esc(p.id_pessoa || "")}" ${isNew ? "required" : "readonly"} placeholder="Ex: P034" /></div>
        <div class="form-field"><label>Nome</label><input class="input" name="nome" value="${esc(p.nome || "")}" required /></div>
        <div class="form-field">
          <label>Categoria</label>
          <select class="select" name="categoria">
            ${(categories || ["Doutorado/Pós-doc", "Mestrado", "IC", "Professor", "Outro"]).map((c) => `<option value="${esc(c)}" ${p.categoria === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
          </select>
        </div>
        <div class="form-field"><label>Ano de entrada</label><input class="input" type="number" name="ano_entrada" value="${p.ano_entrada ?? ""}" /></div>
        <div class="form-field">
          <label>Semestre</label>
          <select class="select" name="semestre_entrada">
            <option value="">-- Selecione --</option>
            <option value="1" ${Number(p.semestre_entrada) === 1 ? "selected" : ""}>1</option>
            <option value="2" ${Number(p.semestre_entrada) === 2 ? "selected" : ""}>2</option>
          </select>
        </div>
        <div class="form-field">
          <label>Situação</label>
          <select class="select" name="situacao">
            ${["Ativo", "Egresso com PC", "Egresso sem PC", "Inativo"].map((s) => `<option value="${s}" ${p.situacao === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </div>
        <div class="form-field"><label>Data saída</label><input class="input" type="date" name="data_saida" value="${esc(p.data_saida || "")}" /></div>
        <div class="form-field"><label>Último upgrade</label><input class="input" type="date" name="data_ultimo_upgrade" value="${esc(p.data_ultimo_upgrade || "")}" /></div>
      </div>
      <div class="form-field" style="margin-top:10px;"><label>Observações</label><textarea name="observacoes" rows="2">${esc(p.observacoes || "")}</textarea></div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar</button>
      </div>
    </form>
  `);
  document.getElementById("form-person").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      if (isNew) {
        await apiFetch("/api/admin/people", { method: "POST", body: JSON.stringify(payload) });
      } else {
        await apiFetch(`/api/admin/people/${p.id}`, { method: "PUT", body: JSON.stringify(payload) });
      }
      closeModal();
      showToast("Pessoa salva!");
      renderPeople();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== ALOCAÇÕES (ADMIN) ==================== */
async function renderAllocations() {
  const el = document.getElementById("tab-alocacoes");
  const data = await apiFetch("/api/admin/allocations");
  state.caches.allocations = data;
  const items = state.alocOnlyActive ? data.items.filter((a) => a.ativa) : data.items;
  const sorted = applyTableSort("alocacoes", items, {
    id_alocacao: { type: "natural" },
    pessoa_nome: { type: "text" },
    pc_id_pc: { type: "natural" },
    tipo: { type: "text" },
    data_inicio: { type: "text" },
    data_fim: { type: "text" },
    motivo_inicio: { type: "text" },
    ativa: { type: "text", get: (a) => (a.ativa ? "Ativa" : "Encerrada") },
    observacoes: { type: "text" },
  });

  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Alocações (${items.length})</h2>
        <span class="row-actions">
          <button id="btn-aloc-tutorial" class="btn btn-secondary btn-sm" type="button">Tutorial: Liberar PC</button>
          <button id="btn-new-aloc" class="btn btn-primary btn-sm" type="button">+ Nova Alocação</button>
        </span>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <label style="font-size:0.86rem;font-weight:600;">
            <input type="checkbox" id="aloc-only-active" ${state.alocOnlyActive ? "checked" : ""} /> Exibir somente ativas
          </label>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("alocacoes", "id_alocacao", "ID")}
                ${sortableTh("alocacoes", "pessoa_nome", "Pessoa")}
                ${sortableTh("alocacoes", "pc_id_pc", "PC")}
                ${sortableTh("alocacoes", "tipo", "Tipo")}
                ${sortableTh("alocacoes", "data_inicio", "Início")}
                ${sortableTh("alocacoes", "data_fim", "Fim")}
                ${sortableTh("alocacoes", "motivo_inicio", "Motivo Início")}
                ${sortableTh("alocacoes", "ativa", "Status")}
                ${sortableTh("alocacoes", "observacoes", "Observações")}
                <th>Ações</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map((a) => `
                <tr>
                  <td class="mono">${esc(a.id_alocacao)}</td>
                  <td><strong>${esc(a.pessoa_nome)}</strong></td>
                  <td class="mono">${esc(a.pc_id_pc)}</td>
                  <td>${esc(a.tipo)}</td>
                  <td class="mono">${esc(a.data_inicio || "—")}</td>
                  <td class="mono">${esc(a.data_fim || "—")}</td>
                  <td>${esc(a.motivo_inicio || "—")}</td>
                  <td>${a.ativa ? '<span class="badge badge-uso">Ativa</span>' : '<span class="badge badge-muted">Encerrada</span>'}</td>
                  <td>${esc(a.observacoes || "—")}</td>
                  <td>
                    <span class="row-actions">
                      <button class="btn btn-sm btn-secondary js-edit-aloc-obs" data-id="${a.id}" type="button">Editar Obs.</button>
                      ${a.ativa ? `<button class="btn btn-sm btn-danger js-close-aloc" data-id="${a.id}" data-label="${esc(a.id_alocacao)}" type="button">Encerrar</button>` : ""}
                    </span>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  document.getElementById("aloc-only-active").addEventListener("change", (e) => {
    state.alocOnlyActive = e.target.checked;
    renderAllocations();
  });
  document.getElementById("btn-aloc-tutorial").addEventListener("click", openReleaseTutorialModal);
  document.getElementById("btn-new-aloc").addEventListener("click", () => openNewAllocationModal(data.people, data.pcs));
  el.querySelectorAll(".js-edit-aloc-obs").forEach((btn) => {
    btn.addEventListener("click", () => {
      const aloc = data.items.find((x) => Number(x.id) === Number(btn.dataset.id));
      if (aloc) openEditAllocationObsModal(aloc);
    });
  });
  el.querySelectorAll(".js-close-aloc").forEach((btn) => {
    btn.addEventListener("click", () => openCloseAllocationModal(btn.dataset.id, btn.dataset.label));
  });
}

function openEditAllocationObsModal(aloc) {
  openModal(`Editar Observações — ${aloc.id_alocacao}`, `
    <p class="muted" style="margin-top:0;font-size:0.84rem;">
      <strong>${esc(aloc.pessoa_nome)}</strong> · <span class="mono">${esc(aloc.pc_id_pc)}</span> (${esc(aloc.tipo)})
    </p>
    <form id="form-edit-aloc-obs">
      <div class="form-field">
        <label for="aloc-obs-input">Observações</label>
        <textarea id="aloc-obs-input" class="input" name="observacoes" rows="4" placeholder="Escreva as observações desta alocação...">${esc(aloc.observacoes || "")}</textarea>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar Observações</button>
      </div>
    </form>
  `);
  setTimeout(() => document.getElementById("aloc-obs-input")?.focus(), 40);
  document.getElementById("form-edit-aloc-obs").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      await apiFetch(`/api/admin/allocations/${aloc.id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      closeModal();
      showToast("Observações da alocação atualizadas!");
      renderAllocations();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openNewAllocationModal(people, pcs) {
  openModal("Nova Alocação", `
    <form id="form-aloc">
      <div class="form-grid">
        <div class="form-field">
          <label>Pessoa</label>
          <select class="select" name="id_pessoa" required>
            ${people.map((p) => `<option value="${esc(p.id_pessoa)}">${esc(p.nome)} (${esc(p.id_pessoa)})</option>`).join("")}
          </select>
        </div>
        <div class="form-field">
          <label>Computador</label>
          <select class="select" name="pc_id_pc" required>
            ${pcs.map((c) => `<option value="${esc(c.id_pc)}">${esc(c.id_pc)} (${esc(c.status || c.tipo_de_uso)})</option>`).join("")}
          </select>
        </div>
        <div class="form-field">
          <label>Tipo</label>
          <select class="select" name="tipo">
            <option value="Principal">Principal</option>
            <option value="Remoto">Remoto</option>
            <option value="Excepcional">Excepcional</option>
          </select>
        </div>
        <div class="form-field"><label>Data Início</label><input class="input" type="date" name="data_inicio" value="${new Date().toISOString().slice(0, 10)}" /></div>
        <div class="form-field"><label>Motivo Início</label><input class="input" name="motivo_inicio" value="Alocação inicial" /></div>
      </div>
      <div class="form-field" style="margin-top:10px;"><label>Observações</label><input class="input" name="observacoes" /></div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Alocar</button>
      </div>
    </form>
  `);
  document.getElementById("form-aloc").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      await apiFetch("/api/admin/allocations", { method: "POST", body: JSON.stringify(payload) });
      closeModal();
      showToast("Alocação criada!");
      renderAllocations();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openCloseAllocationModal(id, label) {
  openModal(`Encerrar ${label}`, `
    <form id="form-close-aloc">
      <div class="form-grid">
        <div class="form-field"><label>Data Fim</label><input class="input" type="date" name="data_fim" value="${new Date().toISOString().slice(0, 10)}" required /></div>
        <div class="form-field"><label>Motivo Fim</label><input class="input" name="motivo_fim" value="Devolução / Saída" required /></div>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-danger" type="submit">Encerrar Alocação</button>
      </div>
    </form>
  `);
  document.getElementById("form-close-aloc").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      await apiFetch(`/api/admin/allocations/${id}/close`, { method: "POST", body: JSON.stringify(payload) });
      closeModal();
      showToast("Alocação encerrada!");
      renderAllocations();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== MONITORES (ADMIN) ==================== */
async function renderMonitors() {
  const el = document.getElementById("tab-monitores");
  const data = await apiFetch("/api/admin/monitors");
  state.caches.monitors = data;
  const sorted = applyTableSort("monitores", data.items, {
    id_monitor: { type: "natural" },
    marca_modelo: { type: "natural" },
    tipo: { type: "text" },
    tamanho: { type: "natural" },
    status: { type: "text" },
    associado_pc: { type: "natural" },
    usuario_associado: { type: "text" },
    localizacao: { type: "natural" },
    projeto: { type: "text" },
  });

  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Inventário de Monitores (${data.items.length})</h2>
        <span class="row-actions">
          <button id="btn-go-labels-mon" class="btn btn-secondary btn-sm" type="button">Etiquetas</button>
          <button id="btn-new-mon" class="btn btn-primary btn-sm" type="button">+ Novo Monitor</button>
        </span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              ${sortableTh("monitores", "id_monitor", "ID Monitor")}
              ${sortableTh("monitores", "marca_modelo", "Marca/Modelo")}
              ${sortableTh("monitores", "tipo", "Tipo")}
              ${sortableTh("monitores", "tamanho", "Tamanho")}
              ${sortableTh("monitores", "status", "Status")}
              ${sortableTh("monitores", "associado_pc", "PC Associado")}
              ${sortableTh("monitores", "usuario_associado", "Usuário")}
              ${sortableTh("monitores", "localizacao", "Localização")}
              ${sortableTh("monitores", "projeto", "Projeto")}
              <th>Ações</th>
            </tr>
          </thead>
          <tbody>
            ${sorted.map((m) => `
              <tr>
                <td class="mono">${esc(m.id_monitor)}</td>
                <td>${esc(m.marca_modelo || "—")}</td>
                <td>${esc(m.tipo || "—")}</td>
                <td>${esc(m.tamanho || "—")}</td>
                <td>${esc(m.status || "—")}</td>
                <td class="mono">${esc(m.associado_pc || "—")}</td>
                <td>${esc(m.usuario_associado || "—")}</td>
                <td>${esc(m.localizacao || "—")}</td>
                <td>${esc(m.projeto || "—")}</td>
                <td>
                  <span class="row-actions">
                    <button class="btn btn-sm btn-secondary js-edit-mon" data-id="${m.id}" type="button">Editar</button>
                    <button class="btn btn-sm btn-secondary js-mon-label" data-id="${m.id}" type="button" title="Enviar este monitor para o gerador de etiquetas">Etiqueta</button>
                    <button class="btn btn-sm btn-danger js-del-mon" data-id="${m.id}" type="button">Excluir</button>
                  </span>
                </td>
              </tr>
            `).join("") || '<tr><td colspan="10" class="muted">Nenhum monitor cadastrado na planilha ainda. Use "+ Novo Monitor" para iniciar o inventário.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
  `;

  document.getElementById("btn-go-labels-mon").addEventListener("click", () => {
    state.labels.mode = "monitor";
    switchTab("etiquetas");
  });
  document.getElementById("btn-new-mon").addEventListener("click", () => openMonitorModal(null, data.pcs));
  el.querySelectorAll(".js-edit-mon").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = data.items.find((x) => x.id === Number(btn.dataset.id));
      if (m) openMonitorModal(m, data.pcs);
    });
  });
  el.querySelectorAll(".js-mon-label").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = data.items.find((x) => x.id === Number(btn.dataset.id));
      if (m) openEquipmentInLabels("monitor", m);
    });
  });
  el.querySelectorAll(".js-del-mon").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Remover este monitor?")) return;
      await apiFetch(`/api/admin/monitors/${btn.dataset.id}`, { method: "DELETE" });
      showToast("Monitor removido.");
      renderMonitors();
    });
  });
}

function openMonitorModal(mon, pcs) {
  const isNew = !mon;
  const m = mon || {};
  openModal(isNew ? "Novo Monitor" : `Editar ${m.id_monitor}`, `
    <form id="form-mon">
      <div class="form-grid">
        <div class="form-field"><label>ID Monitor</label><input class="input" name="id_monitor" value="${esc(m.id_monitor || "")}" ${isNew ? "required" : "readonly"} placeholder="Ex: MN071" /></div>
        <div class="form-field"><label>Patrimônio</label><input class="input" name="patrimonio" value="${esc(m.patrimonio || "")}" /></div>
        <div class="form-field"><label>Marca/Modelo</label><input class="input" name="marca_modelo" value="${esc(m.marca_modelo || "")}" placeholder="Ex: LG 23MB35PH" /></div>
        <div class="form-field"><label>Tipo de Tela</label><input class="input" name="tipo" value="${esc(m.tipo || "")}" placeholder="Ex: LED Wide" /></div>
        <div class="form-field"><label>Tamanho</label><input class="input" name="tamanho" value="${esc(m.tamanho || "")}" placeholder='Ex: 23”' /></div>
        <div class="form-field"><label>Status</label><input class="input" name="status" value="${esc(m.status || "Em uso")}" /></div>
        <div class="form-field">
          <label>PC Associado</label>
          <select class="select" name="associado_pc">
            <option value="">-- Nenhum --</option>
            ${(pcs || []).map((pc) => `<option value="${esc(pc)}" ${m.associado_pc === pc ? "selected" : ""}>${esc(pc)}</option>`).join("")}
          </select>
        </div>
        <div class="form-field">
          <label>Localização</label>
          ${renderLocationSelect("localizacao", m.localizacao)}
        </div>
        <div class="form-field"><label>Projeto (para etiqueta)</label><input class="input" name="projeto" value="${esc(m.projeto || "")}" placeholder="Ex: FAPESP 16742-7/2018-2 ou -" /></div>
      </div>
      <div class="form-field" style="margin-top:10px;"><label>Observações</label><input class="input" name="observacoes" value="${esc(m.observacoes || "")}" /></div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar</button>
      </div>
    </form>
  `);
  document.getElementById("form-mon").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      if (isNew) await apiFetch("/api/admin/monitors", { method: "POST", body: JSON.stringify(payload) });
      else await apiFetch(`/api/admin/monitors/${m.id}`, { method: "PUT", body: JSON.stringify(payload) });
      closeModal();
      showToast("Monitor salvo!");
      renderMonitors();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== PATRIMÔNIOS (ADMIN) ==================== */
const PATRIMONY_DEFAULT_ORIGINS = [
  "Compra",
  "Doação",
  "Doação - Convênio",
  "Sispa",
  "Fabricação Própria",
];

function formatDateBR(iso) {
  const s = String(iso || "").trim();
  if (!s) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return `${m[3]}/${m[2]}/${m[1]}`;
  return s;
}

function formatMoneyBRL(val) {
  if (val === null || val === undefined || val === "") return "—";
  const num = Number(val);
  if (!Number.isFinite(num)) return "—";
  return num.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
}

function formatMoneyInputBR(val) {
  if (val === null || val === undefined || val === "") return "";
  const num = Number(val);
  if (!Number.isFinite(num)) return String(val);
  return num.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function renderWarrantyCell(isoDate) {
  const s = String(isoDate || "").trim();
  if (!s) return '<span class="muted">—</span>';
  const today = new Date().toISOString().slice(0, 10);
  const formatted = esc(formatDateBR(s));
  if (/^\d{4}-\d{2}-\d{2}$/.test(s) && s >= today) {
    return `${formatted} <span class="badge badge-livre" style="margin-left:4px;">Ativa</span>`;
  }
  return formatted;
}

async function renderPatrimonies(useCache = false) {
  const el = document.getElementById("tab-patrimonios");
  const prevSearch = document.getElementById("pat-search");
  const hadSearchFocus = Boolean(prevSearch && document.activeElement === prevSearch);
  const selStart = hadSearchFocus ? prevSearch.selectionStart : null;
  const selEnd = hadSearchFocus ? prevSearch.selectionEnd : null;

  let items = state.caches.patrimonies;
  if (!useCache || !Array.isArray(items) || items.length === 0) {
    const data = await apiFetch("/api/admin/patrimonies");
    items = Array.isArray(data.items) ? data.items : [];
    state.caches.patrimonies = items;
  }

  const responsaveis = [...new Set(items.map((x) => (x.responsavel || "").trim()).filter(Boolean))].sort((a, b) => collatorNat.compare(a, b));
  const origens = [...new Set(items.map((x) => (x.origem || "").trim()).filter(Boolean))].sort((a, b) => collatorNat.compare(a, b));
  const locais = [...new Set(items.map((x) => (x.local_item || "").trim()).filter(Boolean))].sort((a, b) => collatorNat.compare(a, b));

  const f = state.patrimonyFilters;
  const q = (f.search || "").trim().toLowerCase();
  const filtered = items.filter((x) => {
    const matchSearch = !q || [
      x.numero_patrimonio,
      x.nome_item,
      x.descricao,
      x.responsavel,
      x.origem,
      x.local_item,
    ].some((v) => String(v || "").toLowerCase().includes(q));
    const matchResp = !f.responsavel || (x.responsavel || "") === f.responsavel;
    const matchOrig = !f.origem || (x.origem || "") === f.origem;
    const matchLoc = !f.local || (x.local_item || "") === f.local;
    return matchSearch && matchResp && matchOrig && matchLoc;
  });

  const sorted = applyTableSort("patrimonios", filtered, {
    numero_patrimonio: { type: "natural" },
    nome_item: { type: "text" },
    descricao: { type: "text" },
    responsavel: { type: "text" },
    data_incorporacao: { type: "text" },
    fim_garantia: { type: "text" },
    valor_pago: { type: "number" },
    origem: { type: "text" },
    local_item: { type: "natural" },
  });

  const today = new Date().toISOString().slice(0, 10);
  const totalValor = filtered.reduce((acc, x) => acc + (Number(x.valor_pago) || 0), 0);
  const activeWarrantyCount = filtered.filter((x) => /^\d{4}-\d{2}-\d{2}$/.test(x.fim_garantia || "") && x.fim_garantia >= today).length;
  const filteredRespCount = new Set(filtered.map((x) => (x.responsavel || "").trim()).filter(Boolean)).size;
  const filteredOrigCount = new Set(filtered.map((x) => (x.origem || "").trim()).filter(Boolean)).size;
  const hasActiveFilter = Boolean(f.search || f.responsavel || f.origem || f.local);

  el.innerHTML = `
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Patrimônios</div>
        <div class="kpi-value">${filtered.length}</div>
        <div class="kpi-sub">${hasActiveFilter ? `de ${items.length} cadastrados` : "itens cadastrados no LAB"}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Valor Total</div>
        <div class="kpi-value kpi-value-money">${esc(formatMoneyBRL(totalValor))}</div>
        <div class="kpi-sub">${hasActiveFilter ? "soma dos itens filtrados" : "valor total incorporado"}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Responsáveis</div>
        <div class="kpi-value">${filteredRespCount}</div>
        <div class="kpi-sub">${filteredOrigCount} ${filteredOrigCount === 1 ? "origem" : "origens"}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Em Garantia</div>
        <div class="kpi-value">${activeWarrantyCount}</div>
        <div class="kpi-sub">com vencimento futuro</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Patrimônios do Laboratório (${filtered.length})</h2>
        <span class="row-actions">
          <button id="btn-pat-import" class="btn btn-secondary btn-sm" type="button">Importar CSV</button>
          <button id="btn-pat-export" class="btn btn-secondary btn-sm" type="button">Exportar CSV</button>
          <button id="btn-new-pat" class="btn btn-primary btn-sm" type="button">+ Novo Patrimônio</button>
        </span>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <input id="pat-search" class="input" type="search" placeholder="Buscar nº patrimônio, item, descrição, responsável ou local..." value="${esc(f.search)}" />
          <select id="pat-resp" class="select">
            <option value="">Todos Responsáveis</option>
            ${responsaveis.map((r) => `<option value="${esc(r)}" ${f.responsavel === r ? "selected" : ""}>${esc(r)}</option>`).join("")}
          </select>
          <select id="pat-orig" class="select">
            <option value="">Todas Origens</option>
            ${origens.map((o) => `<option value="${esc(o)}" ${f.origem === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
          </select>
          <select id="pat-local" class="select">
            <option value="">Todos Locais</option>
            ${locais.map((l) => `<option value="${esc(l)}" ${f.local === l ? "selected" : ""}>${esc(l)}</option>`).join("")}
          </select>
          ${hasActiveFilter ? '<button id="btn-pat-clear" class="btn btn-secondary btn-sm" type="button">Limpar filtros</button>' : ""}
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("patrimonios", "numero_patrimonio", "Nº Patrimônio")}
                ${sortableTh("patrimonios", "nome_item", "Nome do Item")}
                ${sortableTh("patrimonios", "descricao", "Breve Descrição")}
                ${sortableTh("patrimonios", "responsavel", "Responsável")}
                ${sortableTh("patrimonios", "data_incorporacao", "Incorporação")}
                ${sortableTh("patrimonios", "fim_garantia", "Fim Garantia")}
                ${sortableTh("patrimonios", "valor_pago", "Valor Pago")}
                ${sortableTh("patrimonios", "origem", "Origem")}
                ${sortableTh("patrimonios", "local_item", "Local")}
                <th>Ações</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map((p) => `
                <tr>
                  <td class="mono"><strong>${esc(p.numero_patrimonio)}</strong></td>
                  <td><strong>${esc(p.nome_item)}</strong></td>
                  <td class="pat-desc-cell">${esc(p.descricao || "—")}</td>
                  <td>${esc(p.responsavel || "—")}</td>
                  <td class="mono">${esc(formatDateBR(p.data_incorporacao))}</td>
                  <td class="mono">${renderWarrantyCell(p.fim_garantia)}</td>
                  <td class="mono pat-money-cell">${esc(formatMoneyBRL(p.valor_pago))}</td>
                  <td>${p.origem ? `<span class="badge badge-muted">${esc(p.origem)}</span>` : '<span class="muted">—</span>'}</td>
                  <td class="pat-local-cell">${esc(p.local_item || "—")}</td>
                  <td>
                    <span class="row-actions">
                      <button class="btn btn-sm btn-secondary js-edit-pat" data-id="${p.id}" type="button">Editar</button>
                      <button class="btn btn-sm btn-danger js-del-pat" data-id="${p.id}" type="button">Excluir</button>
                    </span>
                  </td>
                </tr>
              `).join("") || '<tr><td colspan="10" class="muted">Nenhum patrimônio encontrado. Use "+ Novo Patrimônio" ou "Importar CSV" para adicionar registros.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  const searchInput = document.getElementById("pat-search");
  if (hadSearchFocus && searchInput) {
    searchInput.focus({ preventScroll: true });
    if (selStart !== null && selEnd !== null) {
      searchInput.setSelectionRange(selStart, selEnd);
    }
  }
  searchInput.addEventListener("input", (e) => {
    state.patrimonyFilters.search = e.target.value;
    renderPatrimonies(true);
  });
  document.getElementById("pat-resp").addEventListener("change", (e) => {
    state.patrimonyFilters.responsavel = e.target.value;
    renderPatrimonies(true);
  });
  document.getElementById("pat-orig").addEventListener("change", (e) => {
    state.patrimonyFilters.origem = e.target.value;
    renderPatrimonies(true);
  });
  document.getElementById("pat-local").addEventListener("change", (e) => {
    state.patrimonyFilters.local = e.target.value;
    renderPatrimonies(true);
  });
  const clearBtn = document.getElementById("btn-pat-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      state.patrimonyFilters = { search: "", responsavel: "", origem: "", local: "" };
      renderPatrimonies(true);
    });
  }

  document.getElementById("btn-pat-import").addEventListener("click", openImportPatrimoniesModal);
  document.getElementById("btn-pat-export").addEventListener("click", async () => {
    try {
      await apiDownload("/api/admin/patrimonies/export-csv", null, "Patrimonios_LSEE.csv");
      showToast("Arquivo CSV de patrimônios gerado!");
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("btn-new-pat").addEventListener("click", () => openPatrimonyModal(null, items));
  el.querySelectorAll(".js-edit-pat").forEach((btn) => {
    btn.addEventListener("click", () => {
      const p = items.find((x) => x.id === Number(btn.dataset.id));
      if (p) openPatrimonyModal(p, items);
    });
  });
  el.querySelectorAll(".js-del-pat").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const p = items.find((x) => x.id === Number(btn.dataset.id));
      const label = p ? `${p.numero_patrimonio} (${p.nome_item})` : "este item";
      if (!confirm(`Remover o patrimônio ${label}?`)) return;
      try {
        await apiFetch(`/api/admin/patrimonies/${btn.dataset.id}`, { method: "DELETE" });
        showToast("Patrimônio removido.");
        state.caches.patrimonies = [];
        renderPatrimonies(false);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  });
}

function openPatrimonyModal(pat, allItems) {
  const isNew = !pat;
  const p = pat || {};
  const existingResps = [...new Set((allItems || []).map((x) => (x.responsavel || "").trim()).filter(Boolean))].sort((a, b) => collatorNat.compare(a, b));
  const existingOrigs = [...new Set([...PATRIMONY_DEFAULT_ORIGINS, ...(allItems || []).map((x) => (x.origem || "").trim()).filter(Boolean)])].sort((a, b) => collatorNat.compare(a, b));
  const existingLocs = [...new Set([...(allItems || []).map((x) => (x.local_item || "").trim()).filter(Boolean), ...FIXED_LOCATIONS])].sort((a, b) => collatorNat.compare(a, b));

  openModal(isNew ? "Novo Patrimônio" : `Editar Patrimônio ${p.numero_patrimonio}`, `
    <form id="form-pat">
      <div class="form-grid">
        <div class="form-field">
          <label>Número do Patrimônio *</label>
          <input class="input mono" name="numero_patrimonio" value="${esc(p.numero_patrimonio || "")}" required placeholder="Ex: 018.065545" />
        </div>
        <div class="form-field">
          <label>Nome do Item *</label>
          <input class="input" name="nome_item" value="${esc(p.nome_item || "")}" required placeholder="Ex: MICROCOMPUTADOR" />
        </div>
        <div class="form-field">
          <label>Responsável</label>
          <input class="input" name="responsavel" list="dl-pat-resp" value="${esc(p.responsavel || "")}" placeholder="Ex: Denis Vinicius Coury (88732)" />
          <datalist id="dl-pat-resp">
            ${existingResps.map((r) => `<option value="${esc(r)}"></option>`).join("")}
          </datalist>
        </div>
        <div class="form-field">
          <label>Origem do Item</label>
          <input class="input" name="origem" list="dl-pat-orig" value="${esc(p.origem || "")}" placeholder="Ex: Compra, Doação, Sispa..." />
          <datalist id="dl-pat-orig">
            ${existingOrigs.map((o) => `<option value="${esc(o)}"></option>`).join("")}
          </datalist>
        </div>
        <div class="form-field">
          <label>Data de Incorporação</label>
          <input class="input" type="date" name="data_incorporacao" value="${esc(p.data_incorporacao || "")}" />
        </div>
        <div class="form-field">
          <label>Fim da Garantia</label>
          <input class="input" type="date" name="fim_garantia" value="${esc(p.fim_garantia || "")}" />
        </div>
        <div class="form-field">
          <label>Valor Pago pelo Item (R$)</label>
          <input class="input mono" name="valor_pago" value="${esc(formatMoneyInputBR(p.valor_pago))}" placeholder="Ex: 1.250,00" />
        </div>
        <div class="form-field">
          <label>Local do Item</label>
          <input class="input" name="local_item" list="dl-pat-local" value="${esc(p.local_item || "")}" placeholder="Ex: Sala 26453 ou Mestrado" />
          <datalist id="dl-pat-local">
            ${existingLocs.map((l) => `<option value="${esc(l)}"></option>`).join("")}
          </datalist>
        </div>
      </div>
      <div class="form-field" style="margin-top:10px;">
        <label>Breve Descrição</label>
        <textarea class="textarea" name="descricao" rows="2" placeholder="Marca, modelo, tipo, configuração ou número de série...">${esc(p.descricao || "")}</textarea>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar</button>
      </div>
    </form>
  `, true);

  document.getElementById("form-pat").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      if (isNew) {
        await apiFetch("/api/admin/patrimonies", { method: "POST", body: JSON.stringify(payload) });
      } else {
        await apiFetch(`/api/admin/patrimonies/${p.id}`, { method: "PUT", body: JSON.stringify(payload) });
      }
      closeModal();
      showToast("Patrimônio salvo!");
      state.caches.patrimonies = [];
      renderPatrimonies(false);
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      resolve(dataUrl.includes(",") ? dataUrl.split(",", 2)[1] : dataUrl);
    };
    reader.onerror = () => reject(new Error(`Falha ao ler o arquivo ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

function openImportPatrimoniesModal() {
  let pendingFiles = [];
  openModal("Importar Patrimônios (.csv)", `
    <p class="help-intro" style="margin-top:0;">
      Selecione um ou mais arquivos <strong>.csv</strong> exportados do Mercúrio/USP (<code>relatorioPatrimonio</code>) ou do próprio painel.
      Itens com o mesmo <strong>Número do Patrimônio</strong> serão atualizados sem apagar os demais registros.
    </p>
    <div class="form-field">
      <label for="pat-csv-files">Selecionar arquivo(s) .csv</label>
      <input id="pat-csv-files" class="input" type="file" accept=".csv,text/csv" multiple />
    </div>
    <div id="pat-import-preview" style="margin-top:12px;"></div>
    <div class="form-actions">
      <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
      <button id="btn-pat-import-apply" class="btn btn-primary" type="button" disabled>Confirmar Importação</button>
    </div>
  `, true);

  document.getElementById("pat-csv-files").addEventListener("change", async (e) => {
    const fileList = Array.from(e.target.files || []);
    const previewEl = document.getElementById("pat-import-preview");
    const applyBtn = document.getElementById("btn-pat-import-apply");
    applyBtn.disabled = true;
    pendingFiles = [];
    if (!fileList.length) {
      previewEl.innerHTML = "";
      return;
    }
    previewEl.innerHTML = '<p class="muted">Lendo CSV(s) e comparando com o cadastro atual...</p>';
    try {
      const encoded = await Promise.all(
        fileList.map(async (f) => ({
          filename: f.name,
          content_base64: await readFileAsBase64(f),
        }))
      );
      const res = await apiFetch("/api/admin/patrimonies/import-csv", {
        method: "POST",
        body: JSON.stringify({ files: encoded, preview_only: true }),
      });
      pendingFiles = encoded;
      const filesSummary = (res.files || [])
        .map((fs) => `<span class="mono">${esc(fs.filename)}</span> (${fs.count})`)
        .join(" · ");
      const respSummary = Object.entries(res.counts_by_responsavel || {})
        .map(([k, v]) => `${esc(k)}: <strong>${v}</strong>`)
        .join("<br />");
      const sampleRows = (res.preview || []).slice(0, 6);
      previewEl.innerHTML = `
        <div class="notice" style="margin-bottom:10px;">
          <strong>Arquivos:</strong> ${filesSummary}<br />
          <strong>Total consolidado:</strong> ${res.total} itens · Novos: <strong>${res.added.length}</strong> · Atualizados: <strong>${res.updated.length}</strong> · Sem alteração: <strong>${res.unchanged}</strong>
          ${respSummary ? `<div style="margin-top:6px;font-size:0.82rem;">${respSummary}</div>` : ""}
        </div>
        ${sampleRows.length ? `
          <div class="table-wrap" style="max-height:220px;overflow:auto;">
            <table>
              <thead>
                <tr>
                  <th>Patrimônio</th>
                  <th>Item</th>
                  <th>Responsável</th>
                  <th>Valor</th>
                  <th>Local</th>
                </tr>
              </thead>
              <tbody>
                ${sampleRows.map((r) => `
                  <tr>
                    <td class="mono">${esc(r.numero_patrimonio)}</td>
                    <td>${esc(r.nome_item)}</td>
                    <td>${esc(r.responsavel || "—")}</td>
                    <td class="mono">${esc(formatMoneyBRL(r.valor_pago))}</td>
                    <td>${esc(r.local_item || "—")}</td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        ` : ""}
      `;
      applyBtn.disabled = false;
    } catch (err) {
      pendingFiles = [];
      previewEl.innerHTML = `<p class="muted" style="color:var(--danger);">${esc(err.message)}</p>`;
    }
  });

  document.getElementById("btn-pat-import-apply").addEventListener("click", async () => {
    if (!pendingFiles.length) return;
    const applyBtn = document.getElementById("btn-pat-import-apply");
    applyBtn.disabled = true;
    try {
      const res = await apiFetch("/api/admin/patrimonies/import-csv", {
        method: "POST",
        body: JSON.stringify({ files: pendingFiles, preview_only: false }),
      });
      closeModal();
      showToast(`Patrimônios importados (${res.added.length} novos, ${res.updated.length} atualizados)!`);
      state.caches.patrimonies = [];
      renderPatrimonies(false);
    } catch (err) {
      applyBtn.disabled = false;
      showToast(err.message, true);
    }
  });
}

/* ==================== ETIQUETAS (ADMIN) ==================== */
function formatCodeForLabel(raw, fallbackPrefix = "PC") {
  const cleaned = String(raw || "").trim();
  if (!cleaned) return fallbackPrefix;
  if (/^[A-Za-zÀ-ÿ]+\s+\d+/.test(cleaned)) return cleaned.replace(/\s+/, " ");
  const m = /^([A-Za-zÀ-ÿ]+?)\s*-?\s*(\d+.*)$/.exec(cleaned);
  return m ? `${m[1]} ${m[2]}` : cleaned;
}

function formatCapacityUnit(gb) {
  const val = Number(gb);
  if (!Number.isFinite(val) || val <= 0) return "";
  if (val === 1024) return "1 TB";
  if (val >= 1000 && val % 1000 === 0) return `${val / 1000} TB`;
  return `${Number.isInteger(val) ? val : String(val).replace(".", ",")} GB`;
}

function formatPcStorageLine(pc) {
  const hd = Number(pc?.hd_gb || 0);
  const ssd = Number(pc?.ssd_gb || 0);
  const hdText = hd > 0 ? formatCapacityUnit(hd) : "";
  const ssdText = ssd > 0 ? `${formatCapacityUnit(ssd)} SSD` : "";
  if (hdText && ssdText) return `${hdText} + ${ssdText}`;
  return hdText || ssdText || "-";
}

function formatPcLabelDraft(pc) {
  return {
    kind: "pc",
    code: formatCodeForLabel(pc?.id_pc || "", "PC"),
    lines: [
      { label: "Processador:", value: (pc?.processador || "").trim() || "-" },
      { label: "Memória:", value: pc?.ram_gb ? `${Number(pc.ram_gb)} GB` : "-" },
      { label: "HD:", value: pc ? formatPcStorageLine(pc) : "-" },
      { label: "Projeto:", value: (pc?.projeto || "").trim() || "-" },
    ],
  };
}

function formatMonitorLabelDraft(mon) {
  return {
    kind: "monitor",
    code: formatCodeForLabel(mon?.id_monitor || "", "MN"),
    lines: [
      { label: "Modelo:", value: (mon?.marca_modelo || "").trim() || "-" },
      { label: "Tipo:", value: (mon?.tipo || "").trim() || "-" },
      { label: "Tamanho:", value: (mon?.tamanho || "").trim() || "-" },
      { label: "Projeto:", value: (mon?.projeto || "").trim() || "-" },
    ],
  };
}

function formatPlacaLabelDraft(num = 1) {
  const codeNum = String(num).padStart(2, "0");
  return {
    kind: "placa",
    code: `Placa ${codeNum}`,
    lines: [
      { label: "Canais:", value: "6 entradas" },
      { label: "Entradas:", value: "CA ± 10 V pico · CC 0-10 V" },
      { label: "Saídas:", value: "1,5±1,5 V ou 0-3 V" },
      { label: "Filtro:", value: "1ª ordem 480 Hz" },
    ],
  };
}

function formatBlankFreeLabelDraft() {
  return {
    kind: "placa",
    code: "Item 01",
    lines: [
      { label: "Linha 1:", value: "" },
      { label: "Linha 2:", value: "" },
      { label: "Linha 3:", value: "" },
      { label: "Projeto:", value: "-" },
    ],
  };
}

function incrementLabelCode(code, offset) {
  if (!offset) return code;
  const m = /^(.*?)(\d+)(\s*)$/.exec(String(code || ""));
  if (!m) return code;
  const nextNum = Number(m[2]) + offset;
  const padded = String(nextNum).padStart(m[2].length, "0");
  return `${m[1]}${padded}${m[3]}`;
}

function cloneLabelData(lbl) {
  if (!lbl) return null;
  return {
    kind: lbl.kind || "pc",
    code: lbl.code || "",
    lines: (lbl.lines || []).map((ln) => ({ label: ln.label || "", value: ln.value || "" })),
  };
}

function findNextEmptySlot(startPage = 0, startSlot = 0) {
  const ls = state.labels;
  for (let p = startPage; p < ls.pages.length; p += 1) {
    const s0 = p === startPage ? startSlot : 0;
    for (let s = s0; s < 18; s += 1) {
      if (!ls.pages[p][s]) return { pageIdx: p, slotIdx: s };
    }
  }
  return null;
}

function openEquipmentInLabels(kind, item) {
  const ls = state.labels;
  ls.mode = kind;
  if (kind === "pc") {
    ls.selectedPcId = String(item.id);
    ls.draft = formatPcLabelDraft(item);
  } else {
    ls.selectedMonitorId = String(item.id);
    ls.draft = formatMonitorLabelDraft(item);
  }
  const free = findNextEmptySlot(ls.activePage, 0) || findNextEmptySlot(0, 0);
  if (free) {
    ls.activePage = free.pageIdx;
    ls.selectedSlot = free.slotIdx;
  }
  showToast(`Etiqueta de ${ls.draft.code} carregada — clique em uma posição ou use "Colocar neste espaço".`);
  switchTab("etiquetas");
}

function placeMultipleLabels(itemsList, startPage, startSlot, skipFilled) {
  const ls = state.labels;
  let p = startPage;
  let s = startSlot;
  for (const labelObj of itemsList) {
    while (p < ls.pages.length && skipFilled && ls.pages[p][s]) {
      s += 1;
      if (s >= 18) {
        p += 1;
        s = 0;
      }
    }
    if (p >= ls.pages.length) {
      ls.pages.push(Array(18).fill(null));
      s = 0;
    }
    ls.pages[p][s] = cloneLabelData(labelObj);
    ls.activePage = p;
    ls.selectedSlot = s;
    s += 1;
    if (s >= 18) {
      p += 1;
      s = 0;
    }
  }
}

function renderLabelCardPreviewHtml(label) {
  const code = label?.code || "";
  const codeSizePx = code.length > 6 ? Math.max(13, Math.round((23.3 * 6.2) / code.length)) : 23.3;
  const specsHtml = (label?.lines || [])
    .map((ln) => `<p><strong>${esc(ln.label)}</strong>${ln.value ? ` ${esc(ln.value)}` : ""}</p>`)
    .join("");
  return `
    <img class="label-card-logo" src="/static/img/etiqueta_logo.png" alt="Logo LSEE" />
    <div class="label-card-code" style="font-size:${codeSizePx}px;">${esc(code)}</div>
    <div class="label-card-line" aria-hidden="true"></div>
    <div class="label-card-specs">${specsHtml}</div>
  `;
}

function buildLabelsApiPayload() {
  const pages = [];
  for (const sheet of state.labels.pages) {
    const cells = [];
    for (let idx = 0; idx < 18; idx += 1) {
      const cell = sheet[idx];
      if (!cell) continue;
      cells.push({
        row: Math.floor(idx / 3),
        col: idx % 3,
        code: cell.code || "",
        lines: (cell.lines || []).map((ln) => ({ label: ln.label || "", value: ln.value || "" })),
      });
    }
    if (cells.length > 0) pages.push(cells);
  }
  return { pages };
}

function syncLabelsPaperScale() {
  const wrap = document.getElementById("labels-paper-wrap");
  if (!wrap) return;
  const stage = wrap.querySelector(".labels-paper-stage");
  const paper = wrap.querySelector(".labels-a4-paper");
  if (!stage || !paper) return;
  const cs = typeof getComputedStyle === "function" ? getComputedStyle(wrap) : null;
  const padX = cs ? (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0) : 20;
  const avail = wrap.clientWidth - padX;
  if (avail <= 0) return;
  const scale = Math.min(1, Math.max(0.35, avail / 630));
  stage.style.width = `${Math.round(630 * scale)}px`;
  stage.style.height = `${Math.round(891 * scale)}px`;
  paper.style.transform = scale < 0.999 ? `scale(${scale.toFixed(4)})` : "";
}

async function renderLabels() {
  const el = document.getElementById("tab-etiquetas");
  const [pcResp, monResp] = await Promise.all([
    apiFetch("/api/admin/computers"),
    apiFetch("/api/admin/monitors"),
  ]);
  state.caches.computers = pcResp.items || [];
  state.caches.processors = pcResp.processors || [];
  state.caches.monitors = monResp || { items: [], pcs: [] };

  const pcsSorted = [...state.caches.computers]
    .filter((c) => c.id_pc)
    .sort((a, b) => collatorNat.compare(a.id_pc || "", b.id_pc || ""));
  const monsSorted = [...(state.caches.monitors.items || [])]
    .sort((a, b) => collatorNat.compare(a.id_monitor || "", b.id_monitor || ""));

  const ls = state.labels;
  if (!ls.pages.length) ls.pages = [Array(18).fill(null)];
  if (ls.activePage >= ls.pages.length) ls.activePage = ls.pages.length - 1;
  if (ls.selectedSlot < 0 || ls.selectedSlot >= 18) ls.selectedSlot = 0;

  if (!ls.draft) {
    if (ls.mode === "monitor" && monsSorted.length) {
      ls.selectedMonitorId = String(monsSorted[0].id);
      ls.draft = formatMonitorLabelDraft(monsSorted[0]);
    } else if (ls.mode === "placa") {
      ls.draft = formatPlacaLabelDraft(1);
    } else if (pcsSorted.length) {
      ls.mode = "pc";
      ls.selectedPcId = String(pcsSorted[0].id);
      ls.draft = formatPcLabelDraft(pcsSorted[0]);
    } else {
      ls.draft = ls.mode === "monitor" ? formatMonitorLabelDraft(null) : formatPcLabelDraft(null);
    }
  }

  const activeSheet = ls.pages[ls.activePage];
  const filledCount = activeSheet.filter(Boolean).length;
  const totalCount = ls.pages.reduce((acc, sheet) => acc + sheet.filter(Boolean).length, 0);
  const selRow = Math.floor(ls.selectedSlot / 3) + 1;
  const selCol = (ls.selectedSlot % 3) + 1;
  const selectedCellFilled = Boolean(activeSheet[ls.selectedSlot]);

  const itemList = ls.mode === "pc" ? pcsSorted : monsSorted;
  const selectedItemId = ls.mode === "pc" ? ls.selectedPcId : ls.selectedMonitorId;
  const isFreeMode = ls.mode === "placa" || ls.draft.kind === "placa";

  el.innerHTML = `
    <div class="labels-layout">
      <div class="labels-editor-stack">
        <div class="card">
          <div class="card-header">
            <h2 class="card-title">Configurar Etiqueta</h2>
          </div>
          <div class="card-body">
            <div class="labels-mode-switch">
              <button class="labels-mode-btn ${ls.mode === "pc" ? "active" : ""}" id="btn-lbl-mode-pc" type="button">PCs</button>
              <button class="labels-mode-btn ${ls.mode === "monitor" ? "active" : ""}" id="btn-lbl-mode-mon" type="button">Monitores</button>
              <button class="labels-mode-btn ${ls.mode === "placa" ? "active" : ""}" id="btn-lbl-mode-placa" type="button">Geral</button>
            </div>

            ${ls.mode === "placa" ? `
              <div class="labels-btn-row" style="margin-top:0;margin-bottom:10px;">
                <button id="btn-lbl-preset-placa" class="btn btn-secondary btn-sm" type="button">Modelo: Placa (6 entradas)</button>
                <button id="btn-lbl-preset-blank" class="btn btn-secondary btn-sm" type="button">Rótulos livres</button>
              </div>
            ` : `
              <div class="form-field" style="margin-bottom:10px;">
                <label for="lbl-item-select">${ls.mode === "pc" ? "Selecionar PC do inventário" : "Selecionar Monitor do inventário"}</label>
                <select id="lbl-item-select" class="select">
                  <option value="">-- Preencher manualmente / em branco --</option>
                  ${itemList.map((item) => {
                    const labelText = ls.mode === "pc"
                      ? `${item.id_pc} · ${item.processador || "sem CPU"} · ${item.ram_gb ? `${item.ram_gb} GB` : "?"}`
                      : `${item.id_monitor} · ${item.marca_modelo || "sem modelo"} · ${item.tamanho || "?"}`;
                    return `<option value="${item.id}" ${String(item.id) === String(selectedItemId) ? "selected" : ""}>${esc(labelText)}</option>`;
                  }).join("")}
                </select>
              </div>
            `}

            <div class="form-field" style="margin-bottom:10px;">
              <label for="lbl-code">Código em destaque</label>
              <input id="lbl-code" class="input mono" value="${esc(ls.draft.code || "")}" placeholder="${ls.mode === "pc" ? "Ex: PC 063" : ls.mode === "monitor" ? "Ex: MN 071" : "Ex: Placa 01"}" />
            </div>

            ${(ls.draft.lines || []).map((ln, idx) => isFreeMode ? `
              <div class="labels-free-line-row">
                <input class="input js-lbl-line-label" data-idx="${idx}" value="${esc(ln.label || "")}" placeholder="Rótulo:" aria-label="Rótulo linha ${idx + 1}" />
                <input id="lbl-line-${idx}" class="input js-lbl-line-val" data-idx="${idx}" value="${esc(ln.value || "")}" placeholder="Valor" aria-label="Valor linha ${idx + 1}" />
              </div>
            ` : `
              <div class="form-field" style="margin-bottom:8px;">
                <label for="lbl-line-${idx}">${esc(ln.label)}</label>
                <input id="lbl-line-${idx}" class="input js-lbl-line-val" data-idx="${idx}" value="${esc(ln.value || "")}" />
              </div>
            `).join("")}

            ${selectedCellFilled ? `
              <div class="labels-btn-row">
                <button id="btn-lbl-update-slot" class="btn btn-sm btn-primary" type="button">Atualizar espaço L${selRow}·C${selCol}</button>
              </div>
            ` : ""}
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h2 class="card-title">Quantidade e Posição</h2>
            <span class="badge badge-public">Folha ${ls.activePage + 1} · L${selRow}·C${selCol} (#${ls.selectedSlot + 1})</span>
          </div>
          <div class="card-body">
            <div class="labels-qty-row">
              <div class="form-field">
                <label for="lbl-qty">Quantidade</label>
                <input id="lbl-qty" class="input" type="number" min="1" max="54" value="${ls.quantity}" />
              </div>
              <div style="display:flex;flex-direction:column;gap:4px;padding-bottom:4px;">
                <label style="font-size:0.81rem;font-weight:600;display:flex;align-items:center;gap:6px;">
                  <input id="lbl-skip-filled" type="checkbox" ${ls.skipFilled ? "checked" : ""} /> Pular espaços ocupados
                </label>
                <label style="font-size:0.81rem;font-weight:600;display:flex;align-items:center;gap:6px;">
                  <input id="lbl-auto-inc" type="checkbox" ${ls.autoIncrement ? "checked" : ""} /> Numerar sequência (+1)
                </label>
              </div>
            </div>

            <div class="labels-btn-row">
              <button id="btn-lbl-place-one" class="btn btn-primary btn-sm" type="button">Colocar em L${selRow}·C${selCol}</button>
              <button id="btn-lbl-fill-qty" class="btn btn-secondary btn-sm" type="button">Preencher ${ls.quantity}x a partir de L${selRow}·C${selCol}</button>
            </div>
            <div class="labels-btn-row">
              <button id="btn-lbl-fill-page" class="btn btn-secondary btn-sm" type="button">Preencher folha inteira (18)</button>
              <button id="btn-lbl-clear-page" class="btn btn-danger btn-sm" type="button">Limpar folha</button>
            </div>

            ${ls.mode !== "placa" ? `
              <details style="margin-top:12px;">
                <summary style="cursor:pointer;font-size:0.84rem;font-weight:600;color:var(--brand);">Adicionar vários ${ls.mode === "pc" ? "PCs" : "Monitores"} em sequência</summary>
                <div class="labels-batch-list">
                  ${itemList.map((item) => {
                    const main = ls.mode === "pc" ? item.id_pc : item.id_monitor;
                    const sub = ls.mode === "pc" ? (item.processador || "sem CPU") : (item.marca_modelo || "sem modelo");
                    return `
                      <label class="labels-batch-item">
                        <input type="checkbox" class="js-lbl-batch-cb" value="${item.id}" />
                        <span class="mono"><strong>${esc(main)}</strong></span>
                        <span class="muted">${esc(sub)}</span>
                      </label>
                    `;
                  }).join("") || '<span class="muted" style="font-size:0.8rem;">Nenhum item disponível.</span>'}
                </div>
                <div class="labels-btn-row">
                  <button id="btn-lbl-add-batch" class="btn btn-secondary btn-sm" type="button">Adicionar selecionados nos próximos livres</button>
                </div>
              </details>
            ` : ""}
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header labels-sheet-bar">
          <div>
            <h2 class="card-title">Prévia Visual da Folha A4 (3 × 6)</h2>
            <span class="muted" style="font-size:0.8rem;">Folha ${ls.activePage + 1}: ${filledCount}/18 preenchidos · Total: ${totalCount} etiqueta(s)</span>
          </div>
          <span class="row-actions">
            <button id="btn-lbl-pdf" class="btn btn-primary btn-sm" type="button" ${totalCount ? "" : "disabled"}>Baixar PDF (1:1)</button>
            <button id="btn-lbl-docx" class="btn btn-secondary btn-sm" type="button" ${totalCount ? "" : "disabled"}>Baixar .docx</button>
          </span>
        </div>
        <div class="card-body">
          <div class="labels-sheet-bar" style="margin-bottom:10px;">
            <div class="labels-page-pills">
              ${ls.pages.map((sheet, pIdx) => `
                <button class="btn btn-sm ${pIdx === ls.activePage ? "btn-primary" : "btn-secondary"} js-lbl-page-tab" data-page="${pIdx}" type="button">
                  Folha ${pIdx + 1} (${sheet.filter(Boolean).length})
                </button>
              `).join("")}
              <button id="btn-lbl-add-page" class="btn btn-sm btn-secondary" type="button">+ Nova folha</button>
              ${ls.pages.length > 1 ? '<button id="btn-lbl-del-page" class="btn btn-sm btn-danger" type="button">Remover folha</button>' : ""}
            </div>
          </div>

          <div class="notice" style="margin-bottom:12px;">
            Layout idêntico ao papel Pimaco 3×6 (<strong>210 × 297 mm</strong>, adesivos <strong>66,0 × 46,51 mm</strong>). Clique em qualquer espaço vazio para inserir a etiqueta configurada, ou clique em uma preenchida para selecionar/editar/remover.
          </div>

          <div class="labels-paper-wrap" id="labels-paper-wrap">
            <div class="labels-paper-stage">
              <div class="labels-a4-paper" role="grid" aria-label="Grade A4 de 18 etiquetas">
                <div class="labels-grid">
                  ${activeSheet.map((cell, idx) => {
                    const r = Math.floor(idx / 3) + 1;
                    const c = (idx % 3) + 1;
                    const isSel = idx === ls.selectedSlot;
                    if (!cell) {
                      return `
                        <div class="label-slot is-empty${isSel ? " is-selected" : ""}" data-slot="${idx}" role="gridcell" tabindex="0" title="Espaço L${r}·C${c} (#${idx + 1}) — clique para colocar">
                          <div class="label-empty-hint">
                            <span class="label-empty-pos">L${r}·C${c} (#${idx + 1})</span>
                            <span>+ Colocar aqui</span>
                          </div>
                        </div>
                      `;
                    }
                    return `
                      <div class="label-slot is-filled${isSel ? " is-selected" : ""}" data-slot="${idx}" role="gridcell" tabindex="0" title="${esc(cell.code)} em L${r}·C${c}">
                        ${renderLabelCardPreviewHtml(cell)}
                        <span class="label-slot-badge">L${r}·C${c}</span>
                        <button class="label-slot-clear js-lbl-clear-slot" data-slot="${idx}" type="button" title="Remover esta etiqueta">&times;</button>
                      </div>
                    `;
                  }).join("")}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;

  document.getElementById("btn-lbl-mode-pc").addEventListener("click", () => {
    ls.mode = "pc";
    const first = pcsSorted.find((x) => String(x.id) === String(ls.selectedPcId)) || pcsSorted[0];
    ls.selectedPcId = first ? String(first.id) : "";
    ls.draft = formatPcLabelDraft(first || null);
    renderLabels();
  });

  document.getElementById("btn-lbl-mode-mon").addEventListener("click", () => {
    ls.mode = "monitor";
    const first = monsSorted.find((x) => String(x.id) === String(ls.selectedMonitorId)) || monsSorted[0];
    ls.selectedMonitorId = first ? String(first.id) : "";
    ls.draft = formatMonitorLabelDraft(first || null);
    renderLabels();
  });

  document.getElementById("btn-lbl-mode-placa").addEventListener("click", () => {
    ls.mode = "placa";
    if (!ls.draft || ls.draft.kind !== "placa") {
      ls.draft = formatPlacaLabelDraft(1);
    }
    renderLabels();
  });

  document.getElementById("btn-lbl-preset-placa")?.addEventListener("click", () => {
    ls.draft = formatPlacaLabelDraft(1);
    renderLabels();
  });

  document.getElementById("btn-lbl-preset-blank")?.addEventListener("click", () => {
    ls.draft = formatBlankFreeLabelDraft();
    renderLabels();
  });

  document.getElementById("lbl-item-select")?.addEventListener("change", (e) => {
    const pickedId = e.target.value;
    if (ls.mode === "pc") {
      ls.selectedPcId = pickedId;
      const pc = pcsSorted.find((x) => String(x.id) === pickedId);
      ls.draft = formatPcLabelDraft(pc || null);
    } else {
      ls.selectedMonitorId = pickedId;
      const mon = monsSorted.find((x) => String(x.id) === pickedId);
      ls.draft = formatMonitorLabelDraft(mon || null);
    }
    const free = findNextEmptySlot(ls.activePage, 0) || findNextEmptySlot(0, 0);
    if (free) {
      ls.activePage = free.pageIdx;
      ls.selectedSlot = free.slotIdx;
    }
    renderLabels();
  });

  document.getElementById("lbl-code").addEventListener("input", (e) => {
    ls.draft.code = e.target.value;
  });
  el.querySelectorAll(".js-lbl-line-label").forEach((inp) => {
    inp.addEventListener("input", () => {
      const idx = Number(inp.dataset.idx);
      if (ls.draft.lines[idx]) {
        ls.draft.lines[idx].label = inp.value;
      }
    });
  });
  el.querySelectorAll(".js-lbl-line-val").forEach((inp) => {
    inp.addEventListener("input", () => {
      const idx = Number(inp.dataset.idx);
      if (ls.draft.lines[idx]) {
        ls.draft.lines[idx].value = inp.value;
      }
    });
  });

  document.getElementById("btn-lbl-update-slot")?.addEventListener("click", () => {
    ls.pages[ls.activePage][ls.selectedSlot] = cloneLabelData(ls.draft);
    showToast(`Espaço L${selRow}·C${selCol} atualizado.`);
    renderLabels();
  });

  document.getElementById("lbl-qty").addEventListener("change", (e) => {
    const v = Math.max(1, Math.min(54, Number(e.target.value) || 1));
    ls.quantity = v;
    renderLabels();
  });
  document.getElementById("lbl-skip-filled").addEventListener("change", (e) => {
    ls.skipFilled = Boolean(e.target.checked);
  });
  document.getElementById("lbl-auto-inc").addEventListener("change", (e) => {
    ls.autoIncrement = Boolean(e.target.checked);
  });

  document.getElementById("btn-lbl-place-one").addEventListener("click", () => {
    ls.pages[ls.activePage][ls.selectedSlot] = cloneLabelData(ls.draft);
    if (ls.autoIncrement) {
      ls.draft.code = incrementLabelCode(ls.draft.code, 1);
    }
    const next = findNextEmptySlot(ls.activePage, ls.selectedSlot + 1) || findNextEmptySlot(0, 0);
    if (next) {
      ls.activePage = next.pageIdx;
      ls.selectedSlot = next.slotIdx;
    }
    renderLabels();
  });

  document.getElementById("btn-lbl-fill-qty").addEventListener("click", () => {
    const qty = Math.max(1, Math.min(54, Number(document.getElementById("lbl-qty").value) || 1));
    ls.quantity = qty;
    const copies = Array.from({ length: qty }, (_, idx) => {
      const c = cloneLabelData(ls.draft);
      if (ls.autoIncrement) c.code = incrementLabelCode(c.code, idx);
      return c;
    });
    placeMultipleLabels(copies, ls.activePage, ls.selectedSlot, ls.skipFilled);
    renderLabels();
  });

  document.getElementById("btn-lbl-fill-page").addEventListener("click", () => {
    ls.pages[ls.activePage] = Array.from({ length: 18 }, (_, idx) => {
      const c = cloneLabelData(ls.draft);
      if (ls.autoIncrement) c.code = incrementLabelCode(c.code, idx);
      return c;
    });
    renderLabels();
  });

  document.getElementById("btn-lbl-clear-page").addEventListener("click", () => {
    ls.pages[ls.activePage] = Array(18).fill(null);
    ls.selectedSlot = 0;
    renderLabels();
  });

  document.getElementById("btn-lbl-add-batch")?.addEventListener("click", () => {
    const ids = [...el.querySelectorAll(".js-lbl-batch-cb:checked")].map((cb) => cb.value);
    if (!ids.length) {
      showToast("Selecione pelo menos um equipamento da lista.", true);
      return;
    }
    const batchLabels = ids.map((id) => {
      const found = itemList.find((x) => String(x.id) === id);
      return ls.mode === "pc" ? formatPcLabelDraft(found) : formatMonitorLabelDraft(found);
    });
    placeMultipleLabels(batchLabels, ls.activePage, ls.selectedSlot, true);
    showToast(`${batchLabels.length} etiqueta(s) adicionada(s) à folha.`);
    renderLabels();
  });

  el.querySelectorAll(".js-lbl-page-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      ls.activePage = Number(btn.dataset.page);
      ls.selectedSlot = 0;
      renderLabels();
    });
  });

  document.getElementById("btn-lbl-add-page").addEventListener("click", () => {
    if (ls.pages.length >= 20) {
      showToast("Máximo de 20 folhas atingido.", true);
      return;
    }
    ls.pages.push(Array(18).fill(null));
    ls.activePage = ls.pages.length - 1;
    ls.selectedSlot = 0;
    renderLabels();
  });

  document.getElementById("btn-lbl-del-page")?.addEventListener("click", () => {
    if (ls.pages.length <= 1) return;
    ls.pages.splice(ls.activePage, 1);
    ls.activePage = Math.max(0, ls.activePage - 1);
    ls.selectedSlot = 0;
    renderLabels();
  });

  el.querySelectorAll(".js-lbl-clear-slot").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const slot = Number(btn.dataset.slot);
      ls.pages[ls.activePage][slot] = null;
      ls.selectedSlot = slot;
      renderLabels();
    });
  });

  el.querySelectorAll(".label-slot").forEach((slotEl) => {
    slotEl.addEventListener("click", () => {
      const slot = Number(slotEl.dataset.slot);
      const existing = ls.pages[ls.activePage][slot];
      if (existing) {
        ls.selectedSlot = slot;
        ls.draft = cloneLabelData(existing);
        ls.mode = existing.kind || ls.mode;
      } else {
        ls.pages[ls.activePage][slot] = cloneLabelData(ls.draft);
        ls.selectedSlot = slot;
      }
      renderLabels();
    });
  });

  const exportLabels = async (fmt) => {
    const payload = buildLabelsApiPayload();
    if (!payload.pages.length) {
      showToast("Coloque pelo menos uma etiqueta na folha.", true);
      return;
    }
    const ext = fmt === "pdf" ? "pdf" : "docx";
    const endpoint = `/api/admin/labels/${ext}`;
    try {
      await apiDownload(endpoint, payload, `etiquetas_lsee.${ext}`);
      showToast(`Arquivo .${ext} gerado com sucesso!`);
    } catch (err) {
      showToast(err.message, true);
    }
  };

  document.getElementById("btn-lbl-pdf").addEventListener("click", () => exportLabels("pdf"));
  document.getElementById("btn-lbl-docx").addEventListener("click", () => exportLabels("docx"));
  syncLabelsPaperScale();
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(syncLabelsPaperScale);
  }
}

/* ==================== REDE (ADMIN) ==================== */
async function renderNetwork() {
  const el = document.getElementById("tab-rede");
  const data = await apiFetch("/api/admin/network");
  state.caches.network = data.items;
  const cats = [...new Set(data.items.map((x) => x.categoria))];
  const f = state.redeFilters;
  const filtered = data.items.filter((x) => {
    const q = f.search.toLowerCase();
    const m1 = !q || x.equipamento.toLowerCase().includes(q) || (x.ip || "").toLowerCase().includes(q) || (x.mac || "").toLowerCase().includes(q);
    const m2 = !f.categoria || x.categoria === f.categoria;
    return m1 && m2;
  });
  const sorted = applyTableSort("rede", filtered, {
    categoria: { type: "text" },
    equipamento: { type: "natural" },
    ip: { type: "ip" },
    mac: { type: "natural" },
    observacoes: { type: "text" },
  });

  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Cadastro de Rede — IP / MAC (${filtered.length})</h2>
        <span class="row-actions">
          <button id="btn-net-import" class="btn btn-secondary btn-sm" type="button">Importar .xlsx</button>
          <button id="btn-net-export" class="btn btn-secondary btn-sm" type="button">Exportar Excel (.xlsx)</button>
          <button id="btn-new-net" class="btn btn-primary btn-sm" type="button">+ Novo Equipamento</button>
        </span>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <input id="net-search" class="input" type="search" placeholder="Buscar equipamento, IP ou MAC..." value="${esc(f.search)}" />
          <select id="net-cat" class="select">
            <option value="">Todas Categorias</option>
            ${cats.map((c) => `<option value="${esc(c)}" ${f.categoria === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
          </select>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${sortableTh("rede", "categoria", "Categoria")}
                ${sortableTh("rede", "equipamento", "Equipamento")}
                ${sortableTh("rede", "ip", "IP")}
                ${sortableTh("rede", "mac", "MAC")}
                ${sortableTh("rede", "observacoes", "Observações")}
                <th>Ações</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map((n) => `
                <tr>
                  <td>${esc(n.categoria)}</td>
                  <td><strong>${esc(n.equipamento)}</strong></td>
                  <td class="mono">${esc(n.ip || "—")}</td>
                  <td class="mono">${esc(n.mac || "—")}</td>
                  <td>${esc(n.observacoes || "—")}</td>
                  <td>
                    <button class="btn btn-sm btn-secondary js-edit-net" data-id="${n.id}" type="button">Editar</button>
                    <button class="btn btn-sm btn-danger js-del-net" data-id="${n.id}" type="button">Excluir</button>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  document.getElementById("net-search").addEventListener("input", (e) => {
    state.redeFilters.search = e.target.value;
    renderNetwork();
  });
  document.getElementById("net-cat").addEventListener("change", (e) => {
    state.redeFilters.categoria = e.target.value;
    renderNetwork();
  });
  document.getElementById("btn-net-import").addEventListener("click", openImportNetworkModal);
  document.getElementById("btn-net-export").addEventListener("click", async () => {
    try {
      await apiDownload("/api/admin/network/export-xlsx", null, "Lista_de_IPs_LSEE.xlsx");
      showToast("Planilha .xlsx da lista de IPs gerada!");
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("btn-new-net").addEventListener("click", () => openNetworkModal(null));
  el.querySelectorAll(".js-edit-net").forEach((btn) => {
    btn.addEventListener("click", () => {
      const n = data.items.find((x) => x.id === Number(btn.dataset.id));
      if (n) openNetworkModal(n);
    });
  });
  el.querySelectorAll(".js-del-net").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Remover registro de rede?")) return;
      await apiFetch(`/api/admin/network/${btn.dataset.id}`, { method: "DELETE" });
      showToast("Registro removido.");
      renderNetwork();
    });
  });
}

function openImportNetworkModal() {
  let pendingB64 = "";
  let pendingName = "";
  openModal("Importar Lista de IPs (.xlsx)", `
    <p class="help-intro" style="margin-top:0;">
      Envie a planilha <strong>Lista de IPs.xlsx</strong> (da Roseli ou exportada pelo sistema) para conferir as alterações antes de aplicar.
    </p>
    <div class="form-field">
      <label for="net-xlsx-file">Selecionar arquivo .xlsx</label>
      <input id="net-xlsx-file" class="input" type="file" accept=".xlsx" />
    </div>
    <div id="net-import-preview" style="margin-top:12px;"></div>
    <div class="form-actions">
      <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
      <button id="btn-net-import-apply" class="btn btn-primary" type="button" disabled>Confirmar Importação</button>
    </div>
  `, true);

  document.getElementById("net-xlsx-file").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    const previewEl = document.getElementById("net-import-preview");
    const applyBtn = document.getElementById("btn-net-import-apply");
    applyBtn.disabled = true;
    if (!file) {
      previewEl.innerHTML = "";
      return;
    }
    pendingName = file.name;
    previewEl.innerHTML = '<p class="muted">Lendo planilha e comparando com o cadastro atual...</p>';
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const dataUrl = String(reader.result || "");
        pendingB64 = dataUrl.includes(",") ? dataUrl.split(",", 2)[1] : dataUrl;
        const res = await apiFetch("/api/admin/network/import-xlsx", {
          method: "POST",
          body: JSON.stringify({
            filename: pendingName,
            content_base64: pendingB64,
            preview_only: true,
          }),
        });
        const catSummary = Object.entries(res.counts_by_cat || {})
          .map(([k, v]) => `${esc(k)}: <strong>${v}</strong>`)
          .join(" · ");
        previewEl.innerHTML = `
          <div class="notice" style="margin-bottom:8px;">
            <strong>Aba lida:</strong> <span class="mono">${esc(res.sheet_name)}</span> · <strong>${res.total}</strong> registros (${catSummary})<br />
            Novos: <strong>${res.added.length}</strong> · Atualizados: <strong>${res.updated.length}</strong> · Sem alteração: <strong>${res.unchanged}</strong> · Removidos: <strong>${res.removed.length}</strong>
          </div>
          ${res.added.length ? `<p style="font-size:0.82rem;margin:4px 0;"><strong>Novos:</strong> ${esc(res.added.join(", "))}</p>` : ""}
          ${res.updated.length ? `<p style="font-size:0.82rem;margin:4px 0;"><strong>Atualizados:</strong> ${esc(res.updated.join(", "))}</p>` : ""}
          ${res.removed.length ? `<p style="font-size:0.82rem;margin:4px 0;"><strong>Removidos:</strong> ${esc(res.removed.join(", "))}</p>` : ""}
        `;
        applyBtn.disabled = false;
      } catch (err) {
        pendingB64 = "";
        previewEl.innerHTML = `<p class="muted" style="color:var(--danger);">${esc(err.message)}</p>`;
      }
    };
    reader.readAsDataURL(file);
  });

  document.getElementById("btn-net-import-apply").addEventListener("click", async () => {
    if (!pendingB64) return;
    try {
      const res = await apiFetch("/api/admin/network/import-xlsx", {
        method: "POST",
        body: JSON.stringify({
          filename: pendingName,
          content_base64: pendingB64,
          preview_only: false,
        }),
      });
      closeModal();
      showToast(`Lista de IPs atualizada (${res.total} registros)!`);
      renderNetwork();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== SOLICITAÇÕES DE MATERIAL (ADMIN) ==================== */
const PT_MONTHS_JS = [
  "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
  "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
];
const REQUESTS_STORAGE_KEY = "lsee_requests_editor_v1";

function emptyRequestItem() {
  return { material: "", quantidade: "", valor_unitario: "", patrimonio: "", valor: "", adicionado_em: "" };
}

function parseMoneyVal(raw) {
  const s = String(raw ?? "").replace(/R\$/gi, "").trim();
  if (!s) return null;
  const cleaned = s.includes(",") && s.includes(".")
    ? s.replace(/\./g, "").replace(",", ".")
    : s.replace(",", ".");
  const n = Number(cleaned);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

function parseQtyNumber(raw) {
  const s = String(raw ?? "").trim().replace(/\s+/g, " ");
  if (!s) return null;
  const m = /^(\d+(?:[.,]\d+)?)/.exec(s);
  if (!m) return null;
  const n = Number(m[1].replace(",", "."));
  return Number.isFinite(n) && n >= 0 ? n : null;
}

function fmtInputMoneyPtBr(num) {
  if (num === null || num === undefined || !Number.isFinite(num)) return "";
  return num.toFixed(2).replace(".", ",");
}

function fmtBrlJs(num) {
  if (num === null || num === undefined || !Number.isFinite(num)) return "";
  return `R$ ${num.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtDateBrShort(raw) {
  const s = String(raw || "").trim().slice(0, 10);
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : s;
}

function fmtReqDateJs(rs) {
  if (rs.blankDate || !(rs.data_solicitacao || "").trim()) {
    return "São Carlos,______ de _____________ de _______";
  }
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(rs.data_solicitacao.trim());
  if (m) {
    const mo = Number(m[2]);
    if (mo >= 1 && mo <= 12) {
      return `São Carlos, ${m[3]} de ${PT_MONTHS_JS[mo]} de ${m[1]}`;
    }
  }
  return `São Carlos, ${rs.data_solicitacao}`;
}

function recalcRequestItemTotal(it) {
  const unit = parseMoneyVal(it.valor_unitario);
  if (unit === null) return false;
  const qty = parseQtyNumber(it.quantidade);
  if (qty === null) return false;
  const total = Math.round(unit * qty * 100) / 100;
  it.valor = fmtInputMoneyPtBr(total);
  return true;
}

function readSavedRequestDraft() {
  try {
    const raw = window.localStorage.getItem(REQUESTS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_err) {
    return null;
  }
}

function persistRequestDraft(rs) {
  try {
    const payload = {
      mode: rs.mode || (rs.activeId ? "active" : "blank"),
      activeId: rs.activeId || null,
      activeCreatedAt: rs.activeCreatedAt || "",
      dirty: Boolean(rs.dirty),
      orientador: rs.orientador || "",
      laboratorio: rs.laboratorio ?? "LSEE",
      data_solicitacao: rs.data_solicitacao || "",
      blankDate: Boolean(rs.blankDate),
      items: (rs.items || []).map((it) => ({
        material: it.material || "",
        quantidade: it.quantidade || "",
        valor_unitario: it.valor_unitario ?? "",
        patrimonio: it.patrimonio || "",
        valor: it.valor ?? "",
        adicionado_em: it.adicionado_em || "",
      })),
      justificativa: rs.justificativa || "",
    };
    window.localStorage.setItem(REQUESTS_STORAGE_KEY, JSON.stringify(payload));
  } catch (_err) {
    // Ignora caso localStorage esteja indisponível
  }
}

function applyDraftDataToState(rs, draft, modeOverride = null, activeOverride = undefined) {
  rs.mode = modeOverride || draft.mode || "blank";
  if (activeOverride !== undefined) {
    rs.activeId = activeOverride ? activeOverride.id : null;
    rs.activeCreatedAt = activeOverride ? (activeOverride.created_at || "") : "";
  } else {
    rs.activeId = draft.activeId || null;
    rs.activeCreatedAt = draft.activeCreatedAt || "";
  }
  rs.dirty = Boolean(draft.dirty);
  rs.orientador = draft.orientador || "";
  rs.laboratorio = draft.laboratorio ?? "LSEE";
  rs.data_solicitacao = draft.data_solicitacao || new Date().toISOString().slice(0, 10);
  rs.blankDate = Boolean(draft.blankDate);
  const rows = Array.isArray(draft.items) && draft.items.length ? draft.items : [emptyRequestItem()];
  rs.items = rows.map((i) => ({
    material: i.material || "",
    quantidade: i.quantidade || "",
    valor_unitario: i.valor_unitario ?? "",
    patrimonio: i.patrimonio || "",
    valor: i.valor ?? "",
    adicionado_em: i.adicionado_em || "",
  }));
  rs.justificativa = draft.justificativa || "";
}

function applySavedRequestToState(rs, reqObj) {
  const created = reqObj.created_at || "";
  const createdDate = created.slice(0, 10);
  const rows = reqObj.items && reqObj.items.length ? reqObj.items : [emptyRequestItem()];
  rs.activeId = reqObj.id || null;
  rs.activeCreatedAt = created;
  rs.mode = reqObj.id ? "active" : "blank";
  rs.dirty = false;
  rs.orientador = reqObj.orientador || "";
  rs.laboratorio = reqObj.laboratorio || "";
  rs.data_solicitacao = reqObj.data_solicitacao || "";
  rs.blankDate = !reqObj.data_solicitacao;
  rs.items = rows.map((i) => ({
    material: i.material || "",
    quantidade: i.quantidade || "",
    valor_unitario: i.valor_unitario !== null && i.valor_unitario !== undefined
      ? fmtInputMoneyPtBr(Number(i.valor_unitario))
      : "",
    patrimonio: i.patrimonio || "",
    valor: i.valor !== null && i.valor !== undefined
      ? fmtInputMoneyPtBr(Number(i.valor))
      : "",
    adicionado_em: i.adicionado_em || createdDate,
  }));
  rs.justificativa = reqObj.justificativa || "";
  persistRequestDraft(rs);
}

function resetRequestEditorToBlank(rs) {
  rs.activeId = null;
  rs.activeCreatedAt = "";
  rs.mode = "blank";
  rs.dirty = false;
  rs.orientador = "";
  rs.laboratorio = "LSEE";
  rs.data_solicitacao = new Date().toISOString().slice(0, 10);
  rs.blankDate = false;
  rs.items = [emptyRequestItem(), emptyRequestItem(), emptyRequestItem()];
  rs.justificativa = "";
  persistRequestDraft(rs);
}

function hydrateRequestsFromHistory(rs, history) {
  if (rs.hydrated) return;
  const saved = readSavedRequestDraft();
  if (saved && saved.mode === "blank") {
    if (saved.dirty) {
      applyDraftDataToState(rs, saved, "blank", null);
    } else {
      resetRequestEditorToBlank(rs);
    }
  } else if (saved && saved.mode === "active" && saved.activeId) {
    const match = history.find((x) => x.id === Number(saved.activeId));
    if (match) {
      if (saved.dirty) {
        applyDraftDataToState(rs, saved, "active", match);
      } else {
        applySavedRequestToState(rs, match);
      }
    } else if (history.length) {
      applySavedRequestToState(rs, history[0]);
    }
  } else if (saved && saved.dirty) {
    applyDraftDataToState(rs, saved, "blank", null);
  } else if (history.length) {
    applySavedRequestToState(rs, history[0]);
  }
  rs.hydrated = true;
}

function buildRequestPreviewHtml(rs) {
  const filled = (rs.items || []).filter(
    (it) =>
      (it.material || "").trim()
      || (it.quantidade || "").trim()
      || (it.patrimonio || "").trim()
      || String(it.valor ?? "").trim()
      || String(it.valor_unitario ?? "").trim()
  );
  const totalRows = Math.max(8, filled.length);
  let totalSum = 0;
  let hasPrice = false;
  filled.forEach((it) => {
    const p = parseMoneyVal(it.valor);
    if (p !== null) {
      hasPrice = true;
      totalSum += p;
    }
  });

  const rowsHtml = Array.from({ length: totalRows }, (_, idx) => {
    const it = filled[idx];
    if (!it) return '<tr class="is-blank"><td></td><td></td><td></td><td></td></tr>';
    const p = parseMoneyVal(it.valor);
    return `
      <tr>
        <td>${esc(it.material)}</td>
        <td style="text-align:center;">${esc(it.quantidade)}</td>
        <td style="text-align:center;">${esc(it.patrimonio)}</td>
        <td style="text-align:right;">${p !== null ? esc(fmtBrlJs(p)) : ""}</td>
      </tr>
    `;
  }).join("");

  return `
    <div class="req-paper-preview">
      <div class="req-paper-head">
        <img src="/static/img/eesc_usp_logo.png" alt="EESC USP" />
        <div class="req-paper-inst">
          <div>UNIVERSIDADE DE SÃO PAULO</div>
          <div>ESCOLA DE ENGENHARIA DE SÃO CARLOS</div>
          <div>DEPARTAMENTO DE ENGENHARIA ELÉTRICA E DE COMPUTAÇÃO</div>
          <div>PROGRAMA DE PÓS-GRADUAÇÃO EM ENGENHARIA ELÉTRICA</div>
        </div>
      </div>
      <div class="req-paper-title">SOLICITAÇÃO DE AUXÍLIO<br />PARA LABORATÓRIO DE PESQUISA</div>
      <div class="req-paper-box">
        <div class="req-paper-box-row"><strong>Orientador:</strong> ${esc(rs.orientador)}</div>
        <div class="req-paper-box-row"><strong>Laboratório:</strong> ${esc(rs.laboratorio)}</div>
      </div>
      <table class="req-paper-tbl">
        <thead>
          <tr><th colspan="4" style="font-weight:400;text-align:center;">MATERIAL SOLICITADO</th></tr>
          <tr>
            <th style="width:44%;">Material</th>
            <th style="width:18%;">Quantidade</th>
            <th style="width:20%;">Patrimônio</th>
            <th style="width:18%;">Valor R$</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
          <tr>
            <td></td>
            <td></td>
            <td style="text-align:right;font-weight:700;">Total</td>
            <td style="text-align:right;font-weight:700;">${hasPrice ? esc(fmtBrlJs(totalSum)) : ""}</td>
          </tr>
        </tbody>
      </table>
      <div class="req-paper-obs">Obs.: Numero de patrimônios dos bens para tonners e suprimentos de informática</div>
      <div class="req-paper-box">
        <div class="req-paper-box-row" style="text-align:center;font-weight:700;">JUSTIFICATIVA</div>
        <div class="req-paper-just-body">${esc(rs.justificativa)}</div>
      </div>
      <div class="req-paper-footer">${esc(fmtReqDateJs(rs))}</div>
      <div class="req-paper-sig">Assinatura</div>
    </div>
  `;
}

function buildRequestApiPayload(rs) {
  return {
    orientador: rs.orientador || "",
    laboratorio: rs.laboratorio || "",
    data_solicitacao: rs.blankDate ? "" : (rs.data_solicitacao || ""),
    items: (rs.items || []).map((it) => ({
      material: it.material || "",
      quantidade: it.quantidade || "",
      valor_unitario: it.valor_unitario ?? "",
      patrimonio: it.patrimonio || "",
      valor: it.valor ?? "",
      adicionado_em: it.adicionado_em || "",
    })),
    justificativa: rs.justificativa || "",
  };
}

function openSaveRequestModal(rs, history, onSaved) {
  const defaultTargetId = rs.activeId && history.some((h) => h.id === Number(rs.activeId))
    ? Number(rs.activeId)
    : Number(history[0].id);

  const optionLabel = (h) => {
    const dt = fmtDateBrShort(h.created_at) || "Sem data";
    const ori = h.orientador || "Sem orientador";
    const summary = (h.items || []).slice(0, 2).map((i) => i.material).join(", ");
    const extra = (h.items || []).length > 2 ? ` +${(h.items || []).length - 2}` : "";
    return `#${h.id} · ${dt} · ${ori} (${summary}${extra})`;
  };

  openModal("Salvar Solicitação no Histórico", `
    <p class="help-intro" style="margin-top:0;">
      Escolha se deseja incorporar estes itens a uma solicitação já salva ou criar uma nova solicitação no histórico:
    </p>
    <label class="req-save-choice">
      <input type="radio" name="req-save-mode" value="incorporate" checked />
      <div style="flex:1;min-width:0;">
        <strong>Incorporar / Atualizar solicitação já salva</strong>
        <div style="margin-top:6px;">
          <select id="req-save-target" class="select" style="width:100%;">
            ${history.map((h) => `<option value="${h.id}" ${h.id === defaultTargetId ? "selected" : ""}>${esc(optionLabel(h))}</option>`).join("")}
          </select>
        </div>
        <p id="req-save-target-hint" class="muted" style="font-size:0.78rem;margin:6px 0 0;"></p>
      </div>
    </label>
    <label class="req-save-choice">
      <input type="radio" name="req-save-mode" value="new" />
      <div>
        <strong>Criar nova solicitação no histórico</strong>
        <p class="muted" style="font-size:0.78rem;margin:4px 0 0;">
          Salva como um novo registro separado com a data de hoje.
        </p>
      </div>
    </label>
    <div class="form-actions">
      <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
      <button id="btn-req-confirm-save" class="btn btn-primary" type="button">Salvar</button>
    </div>
  `);

  const targetSel = document.getElementById("req-save-target");
  const hintEl = document.getElementById("req-save-target-hint");
  const updateHint = () => {
    const mode = document.querySelector('input[name="req-save-mode"]:checked')?.value || "incorporate";
    targetSel.disabled = mode !== "incorporate";
    const tid = Number(targetSel.value);
    if (rs.activeId && tid === Number(rs.activeId)) {
      hintEl.textContent = `Atualiza a solicitação ativa #${tid}: mantém a data original da solicitação e dos itens já salvos, registrando a data de hoje apenas nos novos itens.`;
    } else {
      hintEl.textContent = `Incorpora os itens preenchidos agora ao final da solicitação #${tid}, preservando os itens antigos e suas datas originais (máx. 15 itens).`;
    }
  };
  document.querySelectorAll('input[name="req-save-mode"]').forEach((r) => r.addEventListener("change", updateHint));
  targetSel.addEventListener("change", updateHint);
  updateHint();

  document.getElementById("btn-req-confirm-save").addEventListener("click", async () => {
    const mode = document.querySelector('input[name="req-save-mode"]:checked')?.value || "incorporate";
    const basePayload = buildRequestApiPayload(rs);
    try {
      let res;
      if (mode === "new") {
        res = await apiFetch("/api/admin/requests", {
          method: "POST",
          body: JSON.stringify({ ...basePayload, reset_item_dates: true }),
        });
        showToast("Nova solicitação salva no histórico!");
      } else {
        const targetId = Number(targetSel.value);
        const mergeMode = rs.activeId && targetId === Number(rs.activeId) ? "replace" : "append";
        res = await apiFetch(`/api/admin/requests/${targetId}`, {
          method: "PUT",
          body: JSON.stringify({ ...basePayload, merge_mode: mergeMode }),
        });
        showToast(`Solicitação #${targetId} atualizada no histórico!`);
      }
      if (res?.request) {
        applySavedRequestToState(rs, res.request);
      }
      closeModal();
      onSaved();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

async function renderRequests() {
  const el = document.getElementById("tab-solicitacoes");
  const data = await apiFetch("/api/admin/requests");
  const rs = state.requests;
  rs.equipmentOptions = data.equipment_options || [];
  const history = data.items || [];
  hydrateRequestsFromHistory(rs, history);

  const renderItemDatePill = (it) => {
    if (it.adicionado_em) {
      return `<span class="req-item-date-pill" title="Data em que este item foi adicionado ao histórico">Adic. em ${esc(fmtDateBrShort(it.adicionado_em))}</span>`;
    }
    const hasData = (it.material || "").trim() || String(it.valor ?? "").trim() || String(it.valor_unitario ?? "").trim();
    if (hasData) {
      return `<span class="req-item-date-pill is-new" title="Ao salvar no histórico receberá a data de hoje">Novo item (data ao salvar)</span>`;
    }
    return `<span class="req-item-date-pill is-new" style="opacity:0.55;">Sem data</span>`;
  };

  const renderItemRow = (it, idx) => `
    <div class="req-item-card js-req-item-row" data-idx="${idx}">
      <div class="req-item-grid">
        <div class="req-cell">
          <span class="req-mini-lbl">Material</span>
          <input class="input js-req-mat" data-idx="${idx}" placeholder="Descrição (ex.: Toner HP, Cabo HDMI)" value="${esc(it.material)}" />
        </div>
        <div class="req-cell">
          <span class="req-mini-lbl">Qtd.</span>
          <input class="input js-req-qtd" data-idx="${idx}" placeholder="Ex.: 2" value="${esc(it.quantidade)}" />
        </div>
        <div class="req-cell">
          <span class="req-mini-lbl" title="Oculto na prévia e nos documentos exportados; calcula Valor R$">Unit. R$ <em class="req-hidden-tag">(oculto)</em></span>
          <input class="input js-req-unit" data-idx="${idx}" placeholder="Ex.: 50,00" value="${esc(it.valor_unitario ?? "")}" title="Valor unitário em R$ (não aparece no documento; multiplica pela quantidade)" />
        </div>
        <div class="req-cell">
          <span class="req-mini-lbl">Valor R$</span>
          <input class="input js-req-val" data-idx="${idx}" placeholder="Auto / R$" value="${esc(it.valor ?? "")}" title="Valor total deste item em R$ (calculado ou manual)" />
        </div>
        <button class="btn btn-sm btn-danger js-req-del-row" data-idx="${idx}" type="button" title="Remover item">&times;</button>
      </div>
      <div class="req-item-subgrid">
        <input class="input mono js-req-pat" data-idx="${idx}" placeholder="Patrimônio" value="${esc(it.patrimonio)}" />
        <select class="select js-req-pat-pick" data-idx="${idx}" aria-label="Puxar patrimônio do inventário">
          <option value="">-- Puxar patrimônio do inventário (opcional) --</option>
          ${rs.equipmentOptions.map((eq) => `<option value="${esc(eq.patrimonio)}">${esc(eq.label)}</option>`).join("")}
        </select>
        ${renderItemDatePill(it)}
      </div>
    </div>
  `;

  const activeBanner = rs.activeId
    ? `<div class="req-active-banner">
         <span>Solicitação ativa no editor: <strong>#${rs.activeId}</strong> (criada em ${esc(fmtDateBrShort(rs.activeCreatedAt))})</span>
         ${rs.dirty ? '<span class="muted">Alterações não salvas</span>' : ""}
       </div>`
    : `<div class="req-active-banner" style="color:var(--text-muted);background:var(--surface-alt);border-color:var(--border);">
         <span>Nova solicitação em preenchimento (ainda não salva no histórico)</span>
       </div>`;

  el.innerHTML = `
    <div class="requests-layout">
      <div class="card">
        <div class="card-header">
          <h2 class="card-title">Criar Solicitação de Material (EESC / USP)</h2>
          <button id="btn-req-clear" class="btn btn-secondary btn-sm" type="button">Limpar / Nova Solicitação</button>
        </div>
        <div class="card-body">
          ${activeBanner}
          <div class="form-grid" style="margin-bottom:10px;">
            <div class="form-field">
              <label for="req-orientador">Orientador</label>
              <input id="req-orientador" class="input" value="${esc(rs.orientador)}" placeholder="Nome do professor orientador" />
            </div>
            <div class="form-field">
              <label for="req-laboratorio">Laboratório</label>
              <input id="req-laboratorio" class="input" value="${esc(rs.laboratorio)}" placeholder="LSEE" />
            </div>
            <div class="form-field">
              <label for="req-date">Data (São Carlos)</label>
              <input id="req-date" class="input" type="date" value="${esc(rs.data_solicitacao)}" ${rs.blankDate ? "disabled" : ""} />
            </div>
            <div class="form-field" style="justify-content:flex-end;">
              <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
                <input id="req-blank-date" type="checkbox" ${rs.blankDate ? "checked" : ""} /> Deixar data em branco p/ assinar depois
              </label>
            </div>
          </div>

          <div class="card-header" style="padding:8px 0;border:none;">
            <strong style="font-size:0.88rem;">Material Solicitado</strong>
            <button id="btn-req-add-row" class="btn btn-secondary btn-sm" type="button">+ Adicionar item</button>
          </div>
          <div id="req-items-container" class="req-items-wrap">
            ${rs.items.map(renderItemRow).join("")}
          </div>

          <div class="form-field" style="margin-top:12px;">
            <label for="req-just">Justificativa</label>
            <textarea id="req-just" class="input" rows="3" style="width:100%;resize:vertical;" placeholder="Descreva a necessidade dos materiais para o laboratório...">${esc(rs.justificativa)}</textarea>
          </div>

          <div class="labels-btn-row" style="margin-top:14px;">
            <button id="btn-req-save" class="btn btn-secondary" type="button">Salvar no Histórico</button>
            <button id="btn-req-pdf" class="btn btn-primary" type="button">Exportar PDF</button>
            <button id="btn-req-docx" class="btn btn-secondary" type="button">Exportar Word (.docx)</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><h2 class="card-title">Prévia do Formulário Oficial</h2></div>
        <div class="card-body" id="req-preview-slot">
          ${buildRequestPreviewHtml(rs)}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px;">
      <div class="card-header"><h2 class="card-title">Histórico de Solicitações Salvas (${history.length})</h2></div>
      <div class="card-body">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Criado em</th>
                <th>Orientador</th>
                <th>Laboratório</th>
                <th>Itens (com data de inclusão)</th>
                <th>Total</th>
                <th>Ações</th>
              </tr>
            </thead>
            <tbody>
              ${history.map((h) => {
                const itemsHtml = (h.items || []).map((i) => {
                  const dt = fmtDateBrShort(i.adicionado_em || (h.created_at || "").slice(0, 10));
                  return `<span class="req-hist-item-chip">${esc(i.material)}${dt ? ` <span class="req-hist-item-date">(${esc(dt)})</span>` : ""}</span>`;
                }).join("; ") || "—";
                const isActive = rs.activeId && Number(rs.activeId) === Number(h.id);
                return `
                <tr>
                  <td class="mono">#${h.id} · ${esc(fmtDateBrShort(h.created_at) || "—")} ${isActive ? '<span class="badge badge-public">Ativa</span>' : ""}</td>
                  <td>${esc(h.orientador || "—")}</td>
                  <td>${esc(h.laboratorio || "—")}</td>
                  <td>${itemsHtml}</td>
                  <td class="mono">${h.total_valor !== null && h.total_valor !== undefined ? esc(fmtBrlJs(Number(h.total_valor))) : "—"}</td>
                  <td>
                    <span class="row-actions">
                      <button class="btn btn-sm btn-secondary js-req-load" data-id="${h.id}" type="button">Carregar</button>
                      <button class="btn btn-sm btn-primary js-req-hist-pdf" data-id="${h.id}" type="button">PDF</button>
                      <button class="btn btn-sm btn-secondary js-req-hist-docx" data-id="${h.id}" type="button">Word</button>
                      <button class="btn btn-sm btn-danger js-req-hist-del" data-id="${h.id}" type="button">Excluir</button>
                    </span>
                  </td>
                </tr>
              `;
              }).join("") || '<tr><td colspan="6" class="muted">Nenhuma solicitação salva ainda.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  const refreshPreview = () => {
    const slot = document.getElementById("req-preview-slot");
    if (slot) slot.innerHTML = buildRequestPreviewHtml(rs);
  };

  const markChanged = () => {
    rs.dirty = true;
    persistRequestDraft(rs);
    refreshPreview();
  };

  document.getElementById("req-orientador").addEventListener("input", (e) => {
    rs.orientador = e.target.value;
    markChanged();
  });
  document.getElementById("req-laboratorio").addEventListener("input", (e) => {
    rs.laboratorio = e.target.value;
    markChanged();
  });
  document.getElementById("req-date").addEventListener("change", (e) => {
    rs.data_solicitacao = e.target.value;
    markChanged();
  });
  document.getElementById("req-blank-date").addEventListener("change", (e) => {
    rs.blankDate = Boolean(e.target.checked);
    document.getElementById("req-date").disabled = rs.blankDate;
    markChanged();
  });
  document.getElementById("req-just").addEventListener("input", (e) => {
    rs.justificativa = e.target.value;
    markChanged();
  });

  el.querySelectorAll(".js-req-mat").forEach((inp) => {
    inp.addEventListener("input", () => {
      rs.items[Number(inp.dataset.idx)].material = inp.value;
      markChanged();
    });
  });
  el.querySelectorAll(".js-req-qtd").forEach((inp) => {
    inp.addEventListener("input", () => {
      const idx = Number(inp.dataset.idx);
      const it = rs.items[idx];
      it.quantidade = inp.value;
      if (recalcRequestItemTotal(it)) {
        const valInp = el.querySelector(`.js-req-val[data-idx="${idx}"]`);
        if (valInp) valInp.value = it.valor;
      }
      markChanged();
    });
  });
  el.querySelectorAll(".js-req-unit").forEach((inp) => {
    inp.addEventListener("input", () => {
      const idx = Number(inp.dataset.idx);
      const it = rs.items[idx];
      it.valor_unitario = inp.value;
      if (recalcRequestItemTotal(it)) {
        const valInp = el.querySelector(`.js-req-val[data-idx="${idx}"]`);
        if (valInp) valInp.value = it.valor;
      }
      markChanged();
    });
  });
  el.querySelectorAll(".js-req-val").forEach((inp) => {
    inp.addEventListener("input", () => {
      rs.items[Number(inp.dataset.idx)].valor = inp.value;
      markChanged();
    });
  });
  el.querySelectorAll(".js-req-pat").forEach((inp) => {
    inp.addEventListener("input", () => {
      rs.items[Number(inp.dataset.idx)].patrimonio = inp.value;
      markChanged();
    });
  });
  el.querySelectorAll(".js-req-pat-pick").forEach((sel) => {
    sel.addEventListener("change", () => {
      const idx = Number(sel.dataset.idx);
      const val = sel.value;
      if (!val) return;
      rs.items[idx].patrimonio = val;
      const patInp = el.querySelector(`.js-req-pat[data-idx="${idx}"]`);
      if (patInp) patInp.value = val;
      markChanged();
    });
  });
  el.querySelectorAll(".js-req-del-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (rs.items.length <= 1) {
        rs.items[0] = emptyRequestItem();
      } else {
        rs.items.splice(Number(btn.dataset.idx), 1);
      }
      rs.dirty = true;
      persistRequestDraft(rs);
      renderRequests();
    });
  });

  document.getElementById("btn-req-add-row").addEventListener("click", () => {
    if (rs.items.length >= 15) {
      showToast("Máximo de 15 itens por solicitação.", true);
      return;
    }
    rs.items.push(emptyRequestItem());
    rs.dirty = true;
    persistRequestDraft(rs);
    renderRequests();
  });

  document.getElementById("btn-req-clear").addEventListener("click", () => {
    resetRequestEditorToBlank(rs);
    rs.hydrated = true;
    renderRequests();
  });

  const exportReq = async (fmt, customPayload = null) => {
    const ext = fmt === "pdf" ? "pdf" : "docx";
    try {
      await apiDownload(`/api/admin/requests/${ext}`, customPayload || buildRequestApiPayload(rs), `Solicitacao_Auxilio_LSEE.${ext}`);
      showToast(`Solicitação gerada em .${ext}!`);
    } catch (err) {
      showToast(err.message, true);
    }
  };

  document.getElementById("btn-req-save").addEventListener("click", async () => {
    const hasAnyInput = (rs.items || []).some(
      (it) =>
        (it.material || "").trim()
        || (it.quantidade || "").trim()
        || (it.patrimonio || "").trim()
        || String(it.valor ?? "").trim()
        || String(it.valor_unitario ?? "").trim()
    ) || (rs.justificativa || "").trim() || (rs.orientador || "").trim();
    if (!hasAnyInput) {
      showToast("Preencha ao menos um item de material antes de gerar ou salvar.", true);
      return;
    }
    if (!history.length) {
      try {
        const res = await apiFetch("/api/admin/requests", {
          method: "POST",
          body: JSON.stringify(buildRequestApiPayload(rs)),
        });
        if (res?.request) applySavedRequestToState(rs, res.request);
        showToast("Solicitação salva no histórico!");
        renderRequests();
      } catch (err) {
        showToast(err.message, true);
      }
      return;
    }
    openSaveRequestModal(rs, history, () => renderRequests());
  });
  document.getElementById("btn-req-pdf").addEventListener("click", () => exportReq("pdf"));
  document.getElementById("btn-req-docx").addEventListener("click", () => exportReq("docx"));

  el.querySelectorAll(".js-req-load").forEach((btn) => {
    btn.addEventListener("click", () => {
      const h = history.find((x) => x.id === Number(btn.dataset.id));
      if (!h) return;
      applySavedRequestToState(rs, h);
      rs.hydrated = true;
      renderRequests();
      showToast(`Solicitação #${h.id} carregada no editor.`);
    });
  });
  el.querySelectorAll(".js-req-hist-pdf").forEach((btn) => {
    btn.addEventListener("click", () => {
      const h = history.find((x) => x.id === Number(btn.dataset.id));
      if (h) exportReq("pdf", h);
    });
  });
  el.querySelectorAll(".js-req-hist-docx").forEach((btn) => {
    btn.addEventListener("click", () => {
      const h = history.find((x) => x.id === Number(btn.dataset.id));
      if (h) exportReq("docx", h);
    });
  });
  el.querySelectorAll(".js-req-hist-del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const delId = Number(btn.dataset.id);
      if (!confirm("Remover esta solicitação do histórico?")) return;
      await apiFetch(`/api/admin/requests/${delId}`, { method: "DELETE" });
      if (rs.activeId && Number(rs.activeId) === delId) {
        resetRequestEditorToBlank(rs);
      }
      showToast("Solicitação removida.");
      renderRequests();
    });
  });
}

function openNetworkModal(item) {
  const isNew = !item;
  const n = item || {};
  openModal(isNew ? "Novo Equipamento de Rede" : `Editar ${n.equipamento}`, `
    <form id="form-net">
      <div class="form-grid">
        <div class="form-field"><label>Categoria</label><input class="input" name="categoria" value="${esc(n.categoria || "Computadores")}" required /></div>
        <div class="form-field"><label>Equipamento</label><input class="input" name="equipamento" value="${esc(n.equipamento || "")}" required /></div>
        <div class="form-field"><label>IP</label><input class="input" name="ip" value="${esc(n.ip || "")}" /></div>
        <div class="form-field"><label>MAC</label><input class="input" name="mac" value="${esc(n.mac || "")}" /></div>
      </div>
      <div class="form-field" style="margin-top:10px;"><label>Observações</label><input class="input" name="observacoes" value="${esc(n.observacoes || "")}" /></div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar</button>
      </div>
    </form>
  `);
  document.getElementById("form-net").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      if (isNew) await apiFetch("/api/admin/network", { method: "POST", body: JSON.stringify(payload) });
      else await apiFetch(`/api/admin/network/${n.id}`, { method: "PUT", body: JSON.stringify(payload) });
      closeModal();
      showToast("Rede atualizada!");
      renderNetwork();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openEditSelfAdminModal(user, onSaved) {
  openModal("Editar Meu Perfil de Administrador", `
    <p class="muted" style="margin-top:0;font-size:0.84rem;">
      Apenas o próprio administrador pode editar seu Número USP e nome.
    </p>
    <form id="form-edit-self-adm">
      <div class="form-field">
        <label for="edit-self-nusp">Número USP</label>
        <input id="edit-self-nusp" class="input mono" name="numero_usp" value="${esc(user.numero_usp || "")}" placeholder="Ex.: 1234567" inputmode="numeric" required />
      </div>
      <div class="form-field" style="margin-top:10px;">
        <label for="edit-self-nome">Nome / Identificação</label>
        <input id="edit-self-nome" class="input" name="nome" value="${esc(user.nome || "")}" placeholder="Ex.: Cleiton" required />
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Salvar Perfil</button>
      </div>
    </form>
  `);
  document.getElementById("form-edit-self-adm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      const res = await apiFetch(`/api/admin/users/${user.id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      if (res?.user) {
        state.currentUser = { ...(state.currentUser || {}), ...res.user };
      }
      closeModal();
      updateAuthUI();
      pollCollab(true);
      showToast("Perfil atualizado!");
      if (onSaved) onSaved();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openDismissAdminModal(user, onSaved) {
  openModal(`Demitir Administrador — ${user.nome}`, `
    <p class="muted" style="margin-top:0;font-size:0.84rem;">
      A solicitação com o motivo será enviada para <strong>${esc(user.nome)}</strong> (NUSP <span class="mono">${esc(user.numero_usp)}</span>), que terá <strong>7 dias</strong> para aceitar ou recusar. Sem resposta após 7 dias, o acesso é desativado automaticamente.
    </p>
    <form id="form-dismiss-adm">
      <div class="form-field">
        <label for="dismiss-reason">Motivo da demissão</label>
        <textarea id="dismiss-reason" class="input" name="reason" rows="3" minlength="5" required placeholder="Descreva o motivo desta solicitação de demissão..."></textarea>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-danger" type="submit">Enviar solicitação de demissão</button>
      </div>
    </form>
  `);
  setTimeout(() => document.getElementById("dismiss-reason")?.focus(), 40);
  document.getElementById("form-dismiss-adm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      await apiFetch(`/api/admin/users/${user.id}/dismiss`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      closeModal();
      pollCollab(true);
      showToast("Solicitação de demissão enviada (prazo de 7 dias).");
      if (onSaved) onSaved();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openSelfResignModal(user, onSaved) {
  openModal("Pedir Demissão de Administrador", `
    <p class="muted" style="margin-top:0;font-size:0.84rem;">
      Seu pedido será enviado para aprovação dos demais administradores ativos. Você só será desativado se outro administrador aprovar dentro de <strong>7 dias</strong>; caso expire sem resposta, você permanecerá ativo.
    </p>
    <form id="form-self-resign">
      <div class="form-field">
        <label for="resign-reason">Motivo (opcional)</label>
        <textarea id="resign-reason" class="input" name="reason" rows="3" placeholder="Ex.: Conclusão das atividades no laboratório..."></textarea>
      </div>
      <div class="form-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-danger" type="submit">Enviar pedido de demissão</button>
      </div>
    </form>
  `);
  setTimeout(() => document.getElementById("resign-reason")?.focus(), 40);
  document.getElementById("form-self-resign").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      await apiFetch("/api/admin/resignations", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      closeModal();
      pollCollab(true);
      showToast("Pedido de demissão enviado para aprovação (prazo de 7 dias).");
      if (onSaved) onSaved();
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

function openRespondDismissalModal(d) {
  const rtype = d.request_type || "dismissal";
  let modalTitle = "Solicitação de Demissão Recebida";
  let introHtml = `<strong>${esc(d.requested_by_nome || "Administrador")}</strong> enviou uma solicitação para encerrar o seu acesso administrativo. Você tem até <strong>${esc(fmtDateTimeLocalShort(d.expires_at))}</strong> (7 dias) para responder.`;
  let reasonLbl = "Motivo informado:";
  let helpHtml = "<strong>Aceitar demissão</strong> desativa seu acesso e encerra sua sessão imediatamente. <strong>Recusar demissão</strong> mantém seu acesso ativo. Sem resposta após 7 dias, a conta é desativada automaticamente.";
  let rejectLabel = "Recusar demissão";
  let acceptLabel = "Aceitar demissão";

  if (rtype === "resignation") {
    modalTitle = "Pedido de Demissão para Aprovação";
    introHtml = `<strong>${esc(d.target_nome || d.requested_by_nome || "Administrador")}</strong> (NUSP <span class="mono">${esc(d.target_numero_usp)}</span>) pediu demissão do cargo administrativo. Prazo para análise: <strong>${esc(fmtDateTimeLocalShort(d.expires_at))}</strong> (7 dias).`;
    helpHtml = "<strong>Aprovar demissão</strong> desativa o acesso desse administrador. <strong>Recusar pedido</strong> mantém o acesso ativo. Sem resposta após 7 dias, o pedido expira e ele não será demitido.";
    rejectLabel = "Recusar pedido";
    acceptLabel = "Aprovar demissão";
  } else if (rtype === "reactivation") {
    modalTitle = "Solicitação de Retorno de Administrador";
    introHtml = `O ex-administrador <strong>${esc(d.target_nome || "Administrador")}</strong> (NUSP <span class="mono">${esc(d.target_numero_usp)}</span>) tentou acessar o sistema e solicitou reativação da conta.`;
    reasonLbl = "Motivo para retornar:";
    helpHtml = "<strong>Aprovar retorno</strong> reativa o Número USP para login administrativo. <strong>Recusar retorno</strong> mantém o acesso bloqueado.";
    rejectLabel = "Recusar retorno";
    acceptLabel = "Aprovar retorno";
  }

  openModal(modalTitle, `
    <p class="muted" style="margin-top:0;font-size:0.86rem;">${introHtml}</p>
    <div style="background:var(--surface-alt);border:1px solid var(--border);border-radius:8px;padding:12px;margin:12px 0;">
      <div style="font-size:0.78rem;color:var(--muted);margin-bottom:4px;">${esc(reasonLbl)}</div>
      <div style="white-space:pre-wrap;font-size:0.9rem;">${esc(d.reason)}</div>
    </div>
    <p class="muted" style="font-size:0.8rem;margin-bottom:14px;">${helpHtml}</p>
    <div class="form-actions">
      <button class="btn btn-secondary" id="btn-dismissal-reject" type="button">${esc(rejectLabel)}</button>
      <button class="btn btn-danger" id="btn-dismissal-accept" type="button">${esc(acceptLabel)}</button>
    </div>
  `);
  const handleDecision = async (decision) => {
    try {
      const res = await apiFetch(`/api/admin/dismissals/${d.id}/respond`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      closeModal();
      if (res?.logged_out) {
        stopCollabPolling();
        state.isAdmin = false;
        state.currentUser = null;
        try {
          const stAuth = await apiFetch("/api/auth/status");
          if (Array.isArray(stAuth?.responsibles)) {
            state.publicResponsibles = stAuth.responsibles;
          }
        } catch {
          // mantém lista atual
        }
        updateAuthUI();
        showToast("Demissão aceita. Sua sessão administrativa foi encerrada.");
        loadActiveTab();
      } else {
        state.collab.pending_dismissals = (state.collab.pending_dismissals || []).filter(
          (x) => Number(x.id) !== Number(d.id)
        );
        renderCollabUI();
        pollCollab(true);
        if (rtype === "reactivation") {
          showToast(decision === "accept" ? "Retorno aprovado! O administrador já pode fazer login." : "Solicitação de retorno recusada.");
        } else if (rtype === "resignation") {
          showToast(decision === "accept" ? "Pedido de demissão aprovado." : "Pedido de demissão recusado.");
        } else {
          showToast("Demissão recusada. Seu acesso foi mantido.");
        }
        if (state.activeTab === "parametros") renderParamsAndAudit();
      }
    } catch (err) {
      showToast(err.message, true);
    }
  };
  document.getElementById("btn-dismissal-reject")?.addEventListener("click", () => handleDecision("reject"));
  document.getElementById("btn-dismissal-accept")?.addEventListener("click", () => handleDecision("accept"));
}

const DISMISSAL_STATUS_META = {
  pending: { label: "Pendente", cls: "badge-warn" },
  accepted: { label: "Aprovada / Aceita", cls: "badge-danger" },
  rejected: { label: "Recusada", cls: "badge-livre" },
  expired: { label: "Expirada (7d)", cls: "badge-muted" },
};

const DISMISSAL_TYPE_META = {
  dismissal: "Demissão",
  resignation: "Pedido próprio",
  reactivation: "Retorno",
};

/* ==================== PARÂMETROS & LOG (ADMIN) ==================== */
async function renderParamsAndAudit() {
  const el = document.getElementById("tab-parametros");
  const [pData, aData, uData] = await Promise.all([
    apiFetch("/api/admin/params"),
    apiFetch("/api/admin/audit"),
    apiFetch("/api/admin/users"),
  ]);
  const admins = uData.items || [];
  const dismissals = uData.dismissals || [];
  const activeAdminCount = admins.filter((x) => x.is_active).length;
  state.publicResponsibles = admins.filter((x) => x.is_active).map((x) => x.nome);

  const paramMap = Object.fromEntries((pData.params || []).map((pm) => [pm.key, pm]));
  const weightDefs = [
    { key: "peso_cpu", label: "Peso CPU (%)", fallback: "0.45" },
    { key: "peso_ram", label: "Peso RAM (%)", fallback: "0.3" },
    { key: "peso_armazenamento", label: "Peso Armazenamento (%)", fallback: "0.225" },
    { key: "peso_gpu", label: "Peso GPU (%)", fallback: "0.025" },
  ];
  const ruleKeys = new Set([
    "peso_cpu",
    "peso_ram",
    "peso_armazenamento",
    "peso_gpu",
    "diferenca_minima_upgrade",
    "score_maximo",
    "armaz_peso_hd",
    "armaz_escala_log",
    "public_anonymize_names",
  ]);
  const pctVal = (k, fb) => Number((Number(String(paramMap[k]?.value ?? fb).replace(",", ".")) * 100).toFixed(2));
  const otherParams = [
    paramMap.diferenca_minima_upgrade || { key: "diferenca_minima_upgrade", label: "Diferença mínima para upgrade", value: "3" },
    paramMap.score_maximo || { key: "score_maximo", label: "Score máximo componente", value: "10" },
    paramMap.armaz_peso_hd || { key: "armaz_peso_hd", label: "Peso relativo do HD (Armazenamento)", value: "0.25" },
    paramMap.armaz_escala_log || { key: "armaz_escala_log", label: "Fator de suavização log (GB_eq)", value: "60" },
    paramMap.public_anonymize_names || { key: "public_anonymize_names", label: "Anonimizar nomes na visão pública (exibir ID Pessoa)?", value: "Não" },
  ].filter((pm) => ruleKeys.has(pm.key));

  el.innerHTML = `
    <div class="card">
      <div class="card-header"><h2 class="card-title">Administradores Autorizados (Login por Número USP)</h2></div>
      <div class="card-body">
        <p class="muted" style="margin-top:0;font-size:0.84rem;">
          Todos os administradores usam a mesma senha administrativa, porém o login só é liberado para os <strong>Números USP</strong> cadastrados e ativos abaixo. Apenas o próprio administrador edita seu Número USP e nome. Ao clicar em <strong>Demitir</strong> outro ADM, ele tem 7 dias para aceitar ou recusar (se não responder em 7 dias, é desativado automaticamente); já o pedido próprio de demissão expira sem desligar caso não haja aprovação em 7 dias.
        </p>
        <form id="form-add-admin-user" class="admin-inline-form">
          <div class="form-field" style="min-width:160px;">
            <label for="new-adm-nusp">Número USP</label>
            <input id="new-adm-nusp" class="input mono" name="numero_usp" placeholder="Ex.: 1234567" inputmode="numeric" required />
          </div>
          <div class="form-field" style="flex:1;min-width:210px;">
            <label for="new-adm-nome">Nome / Identificação</label>
            <input id="new-adm-nome" class="input" name="nome" placeholder="Ex.: Cleiton" required />
          </div>
          <button class="btn btn-primary" type="submit">+ Cadastrar ADM</button>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Número USP</th>
                <th>Nome</th>
                <th>Status</th>
                <th>Cadastrado por</th>
                <th>Ações</th>
              </tr>
            </thead>
            <tbody>
              ${admins.map((u) => {
                const isSelf = state.currentUser && Number(state.currentUser.id) === Number(u.id);
                let actionHtml = '<span class="muted">Inativo</span>';
                if (isSelf) {
                  const resignPart = u.pending_resignation
                    ? `<span class="badge badge-warn">Pedido enviado (até ${esc(fmtDateLocalBr(u.pending_resignation.expires_at))})</span>`
                    : `<button class="btn btn-sm btn-secondary js-self-resign" type="button" ${activeAdminCount <= 1 ? 'disabled title="Cadastre outro administrador ativo antes de pedir demissão"' : ""}>Pedir demissão</button>`;
                  actionHtml = `<button class="btn btn-sm btn-primary js-edit-self-adm" data-id="${u.id}" type="button">Editar meu perfil</button> ${resignPart}`;
                } else if (u.pending_dismissal) {
                  actionHtml = `<span class="badge badge-warn">Demissão pendente (até ${esc(fmtDateLocalBr(u.pending_dismissal.expires_at))})</span>`;
                } else if (u.pending_resignation) {
                  actionHtml = `<span class="badge badge-warn">Pediu demissão</span> <button class="btn btn-sm btn-danger js-respond-dismissal-row" data-id="${u.pending_resignation.id}" type="button">Analisar</button>`;
                } else if (u.pending_reactivation) {
                  actionHtml = `<span class="badge badge-warn">Pediu retorno</span> <button class="btn btn-sm btn-primary js-respond-dismissal-row" data-id="${u.pending_reactivation.id}" type="button">Analisar</button>`;
                } else if (u.is_active) {
                  actionHtml = `<button class="btn btn-sm btn-danger js-dismiss-adm" data-id="${u.id}" type="button">Demitir</button>`;
                }
                return `
                  <tr>
                    <td class="mono">${esc(u.numero_usp)}</td>
                    <td><strong>${esc(u.nome)}</strong> ${isSelf ? '<span class="badge badge-public">Você</span>' : ""}</td>
                    <td><span class="badge ${u.is_active ? "badge-livre" : "badge-danger"}">${u.is_active ? "Ativo" : "Inativo"}</span></td>
                    <td>${esc(u.created_by || "—")}</td>
                    <td>
                      <span class="row-actions">
                        ${actionHtml}
                      </span>
                    </td>
                  </tr>
                `;
              }).join("") || '<tr><td colspan="5" class="muted">Nenhum administrador cadastrado ainda.</td></tr>'}
            </tbody>
          </table>
        </div>
        ${dismissals.length ? `
          <h3 style="margin:18px 0 8px;font-size:0.95rem;">Solicitações de Demissão e Retorno</h3>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tipo</th>
                  <th>Administrador</th>
                  <th>Solicitado por</th>
                  <th>Motivo</th>
                  <th>Prazo (7 dias)</th>
                  <th>Situação</th>
                </tr>
              </thead>
              <tbody>
                ${dismissals.map((d) => {
                  const rtype = d.request_type || "dismissal";
                  let st = DISMISSAL_STATUS_META[d.status] || { label: d.status, cls: "badge-muted" };
                  if (d.status === "expired") {
                    st = rtype === "dismissal"
                      ? { label: "Expirada (7d · Desativado)", cls: "badge-danger" }
                      : { label: "Expirada (7d · Mantido)", cls: "badge-muted" };
                  } else if (d.status === "accepted" && rtype === "reactivation") {
                    st = { label: "Retorno aprovado", cls: "badge-livre" };
                  }
                  const selfUid = Number(state.currentUser?.id || 0);
                  const canRespond = d.status === "pending" && selfUid > 0 && (
                    (rtype === "dismissal" && Number(d.target_user_id) === selfUid) ||
                    ((rtype === "resignation" || rtype === "reactivation") && Number(d.target_user_id) !== selfUid)
                  );
                  const prazoStr = d.expires_at
                    ? `${fmtDateLocalBr(d.created_at)} → ${fmtDateLocalBr(d.expires_at)}`
                    : `${fmtDateLocalBr(d.created_at)} (retorno)`;
                  return `
                    <tr>
                      <td><span class="badge badge-muted">${esc(DISMISSAL_TYPE_META[rtype] || rtype)}</span></td>
                      <td><strong>${esc(d.target_nome)}</strong> <span class="mono muted">(NUSP ${esc(d.target_numero_usp)})</span></td>
                      <td>${esc(d.requested_by_nome || "—")}</td>
                      <td>${esc(d.reason)}</td>
                      <td class="mono">${esc(prazoStr)}</td>
                      <td>
                        <span class="badge ${st.cls}">${esc(st.label)}</span>
                        ${canRespond ? `<button class="btn btn-sm btn-danger js-respond-dismissal-row" data-id="${d.id}" type="button" style="margin-left:6px;">Responder</button>` : ""}
                      </td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
          </div>
        ` : ""}
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Parâmetros do Sistema e Privacidade</h2>
        <span id="params-weight-sum" class="badge badge-ok">Soma dos pesos: 100.0%</span>
      </div>
      <div class="card-body">
        <p class="muted" style="margin-top:0;font-size:0.84rem;">
          Configuração principal dos pesos do score, regras numéricas gerais e privacidade pública (os critérios por faixa de RAM, categorias e benchmarks de CPU ficam na aba <strong>Critérios</strong>).
        </p>
        <form id="form-params">
          <div class="form-grid">
            ${weightDefs.map((wd) => `
              <div class="form-field">
                <label>${esc(wd.label)}</label>
                <input class="input js-param-weight" type="number" min="0" max="100" step="0.1" name="${esc(wd.key)}" value="${pctVal(wd.key, wd.fallback)}" required />
              </div>
            `).join("")}
            ${otherParams.map((pm) => {
              if (pm.key === "public_anonymize_names") {
                return `
                  <div class="form-field">
                    <label>${esc(pm.label)}</label>
                    <select class="select" name="${esc(pm.key)}">
                      <option value="Não" ${pm.value === "Não" ? "selected" : ""}>Não (exibir nome curto)</option>
                      <option value="Sim" ${pm.value === "Sim" ? "selected" : ""}>Sim (exibir apenas ID Pessoa)</option>
                    </select>
                  </div>`;
              }
              return `
                <div class="form-field">
                  <label>${esc(pm.label)}</label>
                  <input class="input" type="number" step="any" name="${esc(pm.key)}" value="${esc(pm.value)}" required />
                </div>`;
            }).join("")}
          </div>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">Salvar Parâmetros</button>
          </div>
        </form>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><h2 class="card-title">Log Completo de Auditoria</h2></div>
      <div class="table-wrap" style="max-height:420px;overflow-y:auto;">
        <table>
          <thead>
            <tr>
              ${sortableTh("audit_logs", "timestamp", "Data/Hora (UTC)")}
              ${sortableTh("audit_logs", "actor", "Autor")}
              ${sortableTh("audit_logs", "action", "Ação")}
              ${sortableTh("audit_logs", "target", "Alvo")}
              ${sortableTh("audit_logs", "details", "Detalhes")}
            </tr>
          </thead>
          <tbody>
            ${applyTableSort("audit_logs", aData.items, {
              timestamp: { type: "text" },
              actor: { type: "text" },
              action: { type: "text" },
              target: { type: "natural" },
              details: { type: "text" },
            }).map((l) => `
              <tr>
                <td class="mono">${esc(l.timestamp.slice(0, 19).replace("T", " "))}</td>
                <td>${esc(l.actor || "—")}</td>
                <td><span class="badge badge-muted">${esc(l.action)}</span></td>
                <td class="mono">${esc(l.target)}</td>
                <td>${esc(l.details)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;

  document.getElementById("form-add-admin-user").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    try {
      await apiFetch("/api/admin/users", { method: "POST", body: JSON.stringify(payload) });
      showToast("Administrador cadastrado!");
      renderParamsAndAudit();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el.querySelectorAll(".js-edit-self-adm").forEach((btn) => {
    btn.addEventListener("click", () => {
      const u = admins.find((x) => Number(x.id) === Number(btn.dataset.id));
      if (u) openEditSelfAdminModal(u, () => renderParamsAndAudit());
    });
  });

  el.querySelectorAll(".js-self-resign").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.currentUser) openSelfResignModal(state.currentUser, () => renderParamsAndAudit());
    });
  });

  el.querySelectorAll(".js-dismiss-adm").forEach((btn) => {
    btn.addEventListener("click", () => {
      const u = admins.find((x) => Number(x.id) === Number(btn.dataset.id));
      if (u) openDismissAdminModal(u, () => renderParamsAndAudit());
    });
  });

  el.querySelectorAll(".js-respond-dismissal-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      const d = dismissals.find((x) => Number(x.id) === Number(btn.dataset.id));
      if (d) openRespondDismissalModal(d);
    });
  });

  const updateParamWeightSum = () => {
    const sum = [...el.querySelectorAll(".js-param-weight")].reduce(
      (acc, inp) => acc + (Number(inp.value) || 0),
      0
    );
    const badge = document.getElementById("params-weight-sum");
    if (!badge) return;
    const valid = Math.abs(sum - 100) <= 0.2;
    badge.textContent = `Soma dos pesos: ${sum.toFixed(1)}%`;
    badge.className = `badge ${valid ? "badge-ok" : "badge-danger"}`;
  };
  el.querySelectorAll(".js-param-weight").forEach((inp) => {
    inp.addEventListener("input", updateParamWeightSum);
  });
  updateParamWeightSum();

  document.getElementById("form-params").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(e.target).entries());
    const wKeys = ["peso_cpu", "peso_ram", "peso_armazenamento", "peso_gpu"];
    const wSum = wKeys.reduce((acc, k) => acc + (Number(payload[k]) || 0), 0);
    if (Math.abs(wSum - 100) > 0.2) {
      showToast(`A soma dos 4 pesos deve ser 100% (atual: ${wSum.toFixed(1)}%).`, true);
      return;
    }
    for (const k of wKeys) {
      payload[k] = String(Number((Number(payload[k]) / 100).toFixed(6)));
    }
    try {
      await apiFetch("/api/admin/params", { method: "PUT", body: JSON.stringify({ params: payload }) });
      showToast("Parâmetros atualizados e scores recalculados!");
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

/* ==================== MANUAL DO ADMINISTRADOR ==================== */
function openAdminHelpModal() {
  const sections = [
    {
      tab: "dashboard",
      title: "1. Dashboard",
      items: [
        "Indicadores ao vivo de PCs, pessoas sem PC e auditoria recente.",
        "Use \"Filtrar pendentes\" para corrigir PCs incompletos e \"Simular\" no Top 5 Livres."
      ]
    },
    {
      tab: "computadores",
      title: "2. Computadores",
      items: [
        "Cadastre/edite PCs, local e patrimônio. Clique nos cabeçalhos das colunas para ordenar.",
        "Status \"Automático\" define Livre/Em uso; Manutenção, Indisponível e Despatrimoniado vão para o fim da fila."
      ]
    },
    {
      tab: "mapa",
      title: "3. Mapa do LSEE",
      items: [
        "Planta fixa com 38 postos: clique em um posto para vincular pessoa/PC ou alocar quem está sem lugar.",
        "Permite trocar dois postos rapidamente e usar \"Imprimir mapa\" para impressão/PDF."
      ]
    },
    {
      tab: "simulador",
      title: "4. Simulador",
      items: [
        "Escolha um PC Livre para ver quem atinge o ganho mínimo (+3 pontos por padrão).",
        "Clique em \"Registrar\" para salvar a decisão; se efetivar um \"Aceitou\", o PC antigo entra na cascata."
      ]
    },
    {
      tab: "criterios",
      title: "5. Critérios & CPUs",
      items: [
        "Clique em \"Editar Categorias e RAM\" para ajustar faixas de RAM e prioridades por categoria (os pesos do score e parâmetros gerais ficam em Parâmetros & Log).",
        "Em \"+ Novo Processador\", siga o protocolo CPU-Z (2 execuções) para recalcular as notas automaticamente."
      ]
    },
    {
      tab: "pessoas",
      title: "6. Pessoas",
      items: [
        "Gerencia membros, categoria, entrada e fila de prioridade (empates ficam sinalizados).",
        "Na saída do laboratório, mude a Situação para Egresso ou Inativo."
      ]
    },
    {
      tab: "alocacoes",
      title: "7. Alocações",
      items: [
        "Histórico de entregas e devoluções; bloqueia alocar PC já em uso e inclui o botão \"Tutorial: Liberar PC\".",
        "Use \"Editar Obs.\" para atualizar observações ou \"Encerrar\" para liberar o PC mantendo todo o histórico."
      ]
    },
    {
      tab: "monitores",
      title: "8. Monitores",
      items: [
        "Cadastro de monitores (modelo, tipo, tamanho, projeto e patrimônio); associe ao PC para herdar o usuário."
      ]
    },
    {
      tab: "patrimonios",
      title: "9. Patrimônios",
      items: [
        "Inventário patrimonial do laboratório com filtros por responsável, origem e local, soma automática do valor incorporado e controle de garantia.",
        "Permite importar um ou mais relatórios CSV do Mercúrio/USP com prévia (atualizando sem apagar itens existentes) e exportar em CSV."
      ]
    },
    {
      tab: "etiquetas",
      title: "10. Etiquetas",
      items: [
        "Monte etiquetas de PCs, Monitores ou Geral na grade Pimaco 3×6 (18 posições/folha).",
        "Visualize a prévia em escala real e exporte em PDF (1:1) ou Word (.docx) pronto para imprimir."
      ]
    },
    {
      tab: "rede",
      title: "11. Rede (IP / MAC)",
      items: [
        "Tabela interna de IPs e MACs com exportação em Excel (.xlsx) para a Roseli e importação com prévia."
      ]
    },
    {
      tab: "solicitacoes",
      title: "12. Solicitações",
      items: [
        "Gere solicitações oficiais EESC/USP com campo de Valor Unitário oculto no documento (cálculo automático do total), rascunho persistente ao recarregar, data por item no histórico, incorporação/criação via pop-up e exportação em PDF ou Word (.docx)."
      ]
    },
    {
      tab: "parametros",
      title: "13. Parâmetros & Log",
      items: [
        "Configuração principal de pesos do score, regras numéricas, privacidade, cadastro/demissão de ADMs (com pedidos próprios e aprovação de retorno) e auditoria completa.",
        "Use o selo verde no topo e o mini chat à direita para conversar quando outro administrador estiver logado."
      ]
    }
  ];

  openModal("Instruções — Modo Administrador", `
    <p class="help-intro">
      Resumo rápido de cada aba (clique em "Abrir aba" para ir direto):
    </p>
    <div class="help-grid">
      ${sections.map((sec) => {
        const isCurrent = sec.tab === state.activeTab;
        return `
          <div class="help-card ${isCurrent ? "is-current" : ""}">
            <div class="help-card-head">
              <h3>${esc(sec.title)} ${isCurrent ? '<span class="badge badge-public">Aba atual</span>' : ""}</h3>
              <button class="btn btn-sm btn-secondary js-help-jump" data-tab="${esc(sec.tab)}" type="button">Abrir aba</button>
            </div>
            <ul class="help-list">
              ${sec.items.map((it) => `<li>${esc(it)}</li>`).join("")}
            </ul>
          </div>`;
      }).join("")}
    </div>
    <div class="help-tip">
      <strong>Dica:</strong> clique em <strong>Sair do Admin</strong> ao encerrar em PCs compartilhados. Troca de senha no servidor: <span class="mono">python server.py --set-password</span>.
    </div>
    <div class="form-actions">
      <button class="btn btn-primary" type="button" onclick="closeModal()">Fechar</button>
    </div>
  `, true);

  document.querySelectorAll(".js-help-jump").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetTab = btn.dataset.tab;
      closeModal();
      switchTab(targetTab);
    });
  });
}

/* ==================== AUTH & INICIALIZAÇÃO ==================== */
async function openLoginModal() {
  try {
    const st = await apiFetch("/api/auth/status");
    state.needsBootstrap = Boolean(st.needs_bootstrap);
  } catch {
    // mantém último valor conhecido
  }

  openModal("Autenticação do Administrador", `
    <p class="muted" style="margin-top:0;">
      ${state.needsBootstrap
        ? "Primeiro acesso: informe seu Número USP, Nome e a senha administrativa para cadastrar o 1º administrador autorizado."
        : "Informe o Número USP cadastrado como administrador e a senha administrativa."}
    </p>
    <form id="form-login">
      <div class="form-field">
        <label for="login-nusp">Número USP</label>
        <input class="input mono" type="text" name="numero_usp" id="login-nusp" placeholder="Ex.: 1234567" inputmode="numeric" required autofocus />
      </div>
      ${state.needsBootstrap ? `
      <div class="form-field" style="margin-top:10px;">
        <label for="login-nome">Nome / Identificação</label>
        <input class="input" type="text" name="nome" id="login-nome" placeholder="Ex.: Cleiton" required />
      </div>` : ""}
      <div class="form-field" style="margin-top:10px;">
        <label for="login-password">Senha do Administrador</label>
        <input class="input" type="password" name="password" id="login-password" required />
      </div>
      <div id="login-reactivation-box" class="hidden" style="margin-top:14px;padding:12px;border:1px solid #fda4af;border-radius:8px;background:var(--surface-alt);">
        <p id="login-reactivation-info" class="muted" style="margin:0 0 8px;font-size:0.83rem;"></p>
        <div class="form-field">
          <label for="login-return-reason">Motivo para retornar</label>
          <textarea class="input" id="login-return-reason" rows="3" minlength="5" placeholder="Explique por que solicita a reativação do seu acesso administrativo..."></textarea>
        </div>
        <div class="form-actions" style="margin-top:10px;margin-bottom:0;">
          <button class="btn btn-primary" id="btn-submit-return" type="button">Enviar solicitação de retorno</button>
        </div>
      </div>
      <div class="form-actions" id="login-main-actions">
        <button class="btn btn-secondary" type="button" onclick="closeModal()">Cancelar</button>
        <button class="btn btn-primary" type="submit">Entrar</button>
      </div>
    </form>
  `);
  setTimeout(() => document.getElementById("login-nusp")?.focus(), 40);
  document.getElementById("btn-submit-return")?.addEventListener("click", async () => {
    const nusp = document.getElementById("login-nusp")?.value || "";
    const password = document.getElementById("login-password")?.value || "";
    const reason = (document.getElementById("login-return-reason")?.value || "").trim();
    if (reason.length < 5) {
      showToast("Informe o motivo para retornar (mínimo de 5 caracteres).", true);
      document.getElementById("login-return-reason")?.focus();
      return;
    }
    try {
      const res = await apiFetch("/api/auth/request-return", {
        method: "POST",
        body: JSON.stringify({ numero_usp: nusp, password, reason }),
      });
      closeModal();
      showToast(res?.message || "Solicitação de retorno enviada! Aguarde aprovação de um administrador ativo.");
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("form-login").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      numero_usp: fd.get("numero_usp") || "",
      nome: fd.get("nome") || "",
      password: fd.get("password") || "",
      active_tab: state.activeTab,
    };
    try {
      const res = await apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
      invalidateApiCache();
      state.isAdmin = Boolean(res.is_admin);
      state.needsBootstrap = false;
      state.currentUser = res.user || null;
      if (Array.isArray(res.responsibles)) {
        state.publicResponsibles = res.responsibles;
      }
      try {
        localStorage.setItem("lsee_admin_hint", "1");
      } catch {
        // ignora
      }
      closeModal();
      updateAuthUI();
      applyAuthDismissals(res.pending_dismissals);
      if (res.collab) {
        applyCollabData(res.collab);
        startCollabPolling(false);
      } else {
        startCollabPolling(true);
      }
      showToast(state.currentUser?.nome ? `Bem-vindo(a), ${state.currentUser.nome}!` : "Modo Administrador ativado.");
      loadActiveTab();
    } catch (err) {
      if (err?.data?.code === "BOOTSTRAP_NAME_REQUIRED") {
        state.needsBootstrap = true;
        openLoginModal();
      } else if (err?.data?.code === "ADMIN_REACTIVATION_REQUIRED") {
        const box = document.getElementById("login-reactivation-box");
        const info = document.getElementById("login-reactivation-info");
        const reasonEl = document.getElementById("login-return-reason");
        if (box && info) {
          box.classList.remove("hidden");
          const pend = err.data.pending_return;
          const userLabel = err.data.nome ? `${err.data.nome} (NUSP ${err.data.numero_usp})` : `NUSP ${err.data.numero_usp}`;
          info.innerHTML = pend
            ? `A conta de <strong>${esc(userLabel)}</strong> está desativada. Já existe uma solicitação de retorno aguardando aprovação; você pode atualizar o motivo abaixo se desejar.`
            : `A conta de <strong>${esc(userLabel)}</strong> foi desativada. Escreva abaixo o <strong>motivo para retornar</strong> para enviar uma solicitação aos administradores ativos.`;
          if (reasonEl && pend?.reason && !reasonEl.value) {
            reasonEl.value = pend.reason;
          }
          setTimeout(() => reasonEl?.focus(), 40);
        }
      }
      showToast(err.message, true);
    }
  });
}

function syncTableWrapOverflow(wrap) {
  if (!wrap) return;
  const prev = wrap.previousElementSibling;
  const topBar = prev && prev.classList.contains("table-scroll-top") ? prev : null;
  const overflowing = wrap.scrollWidth > wrap.clientWidth + 2;
  wrap.classList.toggle("is-overflowing", overflowing);
  if (!topBar || !topBar.firstElementChild) return;
  if (overflowing) {
    topBar.classList.remove("hidden");
    topBar.firstElementChild.style.width = `${wrap.scrollWidth}px`;
    topBar.scrollLeft = wrap.scrollLeft;
  } else {
    topBar.classList.add("hidden");
  }
}

function enhanceWideTables(container = document) {
  container.querySelectorAll(".table-wrap").forEach((wrap) => {
    const tbl = wrap.querySelector("table");
    if (!tbl) return;
    const lastTh = tbl.querySelector("thead tr th:last-child");
    if (lastTh && /^(Ação|Ações)/i.test(lastTh.textContent.trim())) {
      tbl.classList.add("table-sticky-actions");
    }
    if (wrap.dataset.enhanced !== "1" && wrap.parentNode) {
      wrap.dataset.enhanced = "1";
      const topBar = document.createElement("div");
      topBar.className = "table-scroll-top hidden";
      const spacer = document.createElement("div");
      spacer.className = "table-scroll-top-inner";
      topBar.appendChild(spacer);
      wrap.parentNode.insertBefore(topBar, wrap);

      let syncingScroll = false;
      topBar.addEventListener("scroll", () => {
        if (syncingScroll) return;
        syncingScroll = true;
        wrap.scrollLeft = topBar.scrollLeft;
        requestAnimationFrame(() => {
          syncingScroll = false;
        });
      });
      wrap.addEventListener("scroll", () => {
        if (syncingScroll) return;
        syncingScroll = true;
        topBar.scrollLeft = wrap.scrollLeft;
        requestAnimationFrame(() => {
          syncingScroll = false;
        });
      });
    }
    syncTableWrapOverflow(wrap);
  });
}

async function init() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  document.getElementById("mobile-tab-select")?.addEventListener("change", (e) => {
    if (e.target.value) switchTab(e.target.value);
  });

  document.querySelector(".brand-logo-link")?.addEventListener("click", (e) => {
    e.preventDefault();
    switchTab("dashboard");
  });

  const modalBackdrop = document.getElementById("modal-backdrop");
  let backdropDown = false;
  let backdropUp = false;
  document.getElementById("modal-close").addEventListener("click", closeModal);
  modalBackdrop.addEventListener("mousedown", (e) => {
    backdropDown = e.target === modalBackdrop;
    backdropUp = false;
  });
  modalBackdrop.addEventListener("mouseup", (e) => {
    backdropUp = e.target === modalBackdrop;
  });
  modalBackdrop.addEventListener("click", (e) => {
    if (backdropDown && backdropUp && e.target === modalBackdrop) {
      closeModal();
    }
    backdropDown = false;
    backdropUp = false;
  });

  const mainContainer = document.querySelector("main.container");
  if (mainContainer && typeof MutationObserver !== "undefined") {
    let tableRaf = 0;
    const obs = new MutationObserver(() => {
      if (tableRaf) return;
      tableRaf = requestAnimationFrame(() => {
        tableRaf = 0;
        enhanceWideTables(mainContainer);
      });
    });
    obs.observe(mainContainer, { childList: true, subtree: true });
  }
  window.addEventListener("resize", () => {
    document.querySelectorAll("main.container .table-wrap").forEach(syncTableWrapOverflow);
    syncLabelsPaperScale();
  });
  let mobileChatTapOrigin = null;
  document.addEventListener("pointerdown", (e) => {
    if (!state.isAdmin || !state.collab.chatOpen || !isMobileChatViewport()) {
      mobileChatTapOrigin = null;
      return;
    }
    if (e.target && typeof e.target.closest === "function" && e.target.closest("#admin-chat-widget, #admin-presence-pill")) {
      mobileChatTapOrigin = null;
      return;
    }
    mobileChatTapOrigin = { x: Number(e.clientX || 0), y: Number(e.clientY || 0) };
  });
  document.addEventListener("pointerup", (e) => {
    if (!mobileChatTapOrigin) return;
    const dx = Math.abs(Number(e.clientX || 0) - mobileChatTapOrigin.x);
    const dy = Math.abs(Number(e.clientY || 0) - mobileChatTapOrigin.y);
    mobileChatTapOrigin = null;
    if (dx < 12 && dy < 12) {
      maybeCloseMobileChatOnOutsideTap(e.target);
    }
  });
  document.addEventListener("click", (e) => {
    maybeCloseMobileChatOnOutsideTap(e.target);
    const sortBtn = e.target.closest(".sort-btn");
    if (sortBtn) {
      const { sortTable, sortCol } = sortBtn.dataset;
      if (sortTable && sortCol) {
        toggleTableSort(sortTable, sortCol);
      }
      return;
    }
    if (e.target.closest(".js-score-info")) {
      openScoreInfoModal();
    }
  });

  document.getElementById("btn-usage-rules")?.addEventListener("click", openUsageRulesModal);
  document.getElementById("btn-admin-login").addEventListener("click", openLoginModal);
  document.getElementById("btn-admin-help").addEventListener("click", openAdminHelpModal);
  document.getElementById("admin-presence-pill")?.addEventListener("click", () => toggleAdminChat(true));
  document.getElementById("btn-chat-toggle")?.addEventListener("click", () => toggleAdminChat());
  document.getElementById("btn-chat-close")?.addEventListener("click", () => toggleAdminChat(false));
  document.getElementById("admin-chat-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const inp = document.getElementById("admin-chat-input");
    const txt = (inp?.value || "").trim();
    if (!txt) return;
    try {
      await sendAdminChatMessage(txt);
      if (inp) inp.value = "";
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("btn-admin-logout").addEventListener("click", async () => {
    const out = await apiFetch("/api/auth/logout", { method: "POST" });
    stopCollabPolling();
    invalidateApiCache();
    try {
      localStorage.removeItem("lsee_admin_hint");
    } catch {
      // ignora
    }
    state.isAdmin = false;
    state.currentUser = null;
    if (Array.isArray(out?.responsibles)) {
      state.publicResponsibles = out.responsibles;
    }
    updateAuthUI();
    showToast("Sessão administrativa encerrada.");
    loadActiveTab();
  });

  const hydrateAuthStatus = async () => {
    try {
      const auth = await apiFetch("/api/auth/status");
      state.isAdmin = Boolean(auth.is_admin);
      state.needsBootstrap = Boolean(auth.needs_bootstrap);
      state.currentUser = auth.user || null;
      state.collab.pending_dismissals = Array.isArray(auth.pending_dismissals) ? auth.pending_dismissals : [];
      state.publicResponsibles = Array.isArray(auth.responsibles) ? auth.responsibles : [];
      try {
        if (state.isAdmin) localStorage.setItem("lsee_admin_hint", "1");
        else localStorage.removeItem("lsee_admin_hint");
      } catch {
        // ignora
      }
      updateAuthUI();
      if (state.isAdmin) {
        if (auth.collab) {
          applyCollabData(auth.collab);
          startCollabPolling(false);
        } else {
          startCollabPolling(true);
        }
      }
    } catch {
      state.isAdmin = false;
      state.currentUser = null;
      updateAuthUI();
    }
  };

  const hasAdminHint = (() => {
    try {
      return localStorage.getItem("lsee_admin_hint") === "1";
    } catch {
      return false;
    }
  })();

  startPageLoader();
  try {
    if (hasAdminHint) {
      await hydrateAuthStatus();
      await loadActiveTab();
    } else {
      await Promise.all([hydrateAuthStatus(), loadActiveTab()]);
      if (state.isAdmin) {
        await loadActiveTab();
      }
    }
  } finally {
    stopPageLoader();
  }
}

window.closeModal = closeModal;
document.addEventListener("DOMContentLoaded", init);
