let state = null;
let analysis = null;
let optimization = null;
let optimizerProfile = null;
let leagueStrength = null;
let activePosition = "ALL";
let activePlan = 0;
let lastAutomaticOptimization = null;
let toastTimer = null;
let lastSyncAt = null;
let syncInFlight = false;
let syncPollTimer = null;
const liveSyncIntervalMs = 3000;
const defaultChartSettings = {metric: "VOR_Points", yMin: null, yMax: null, limit: 18};
let chartSettings = {...defaultChartSettings, ...JSON.parse(localStorage.getItem("draft-chart-settings") || "{}")};
const storedBoardState = localStorage.getItem("draft-board-open");
let boardOpen = storedBoardState == null
  ? window.matchMedia("(min-width: 1500px)").matches
  : storedBoardState === "true";

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;");

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 4200);
}

function formatNumber(value) {
  return value == null ? "—" : Number(value).toFixed(1);
}

function signedNumber(value) {
  if (value == null) return "—";
  return `${Number(value) >= 0 ? "+" : ""}${formatNumber(value)}`;
}

function renderSyncStatus(error = false) {
  const syncBadge = $("#sync-badge");
  if (error) {
    syncBadge.className = "status-dot error";
    syncBadge.textContent = "API offline — manual ready";
  } else if (state.simulate_api_down) {
    syncBadge.className = "status-dot error";
    syncBadge.textContent = "Simulated API outage — manual fallback";
  } else if (!state.sync_available) {
    syncBadge.className = "status-dot";
    syncBadge.textContent = `${state.site} manual only`;
  } else if (state.mode === "live") {
    const lastSync = lastSyncAt ? ` · ${formatLastRun(lastSyncAt)}` : "";
    syncBadge.className = "status-dot ready";
    syncBadge.textContent = `${state.site} live · 3s poll${lastSync}`;
  } else {
    syncBadge.className = "status-dot ready";
    syncBadge.textContent = `${state.site} read-only sync`;
  }
}

function renderHeader() {
  $("#league-title").textContent = `${state.league} · ${state.year}`;
  $("#mode-badge").textContent = `${state.mode} mode`;
  renderSyncStatus();
  $("#sync-button").disabled = !state.sync_available;
  $("#pick-number").textContent = `#${state.current_pick}`;
  $("#round-label").textContent = `Round ${state.round}, pick ${state.round_pick}`;
  const turnCounter = $("#next-pick");
  const turnNumber = $("#picks-away-number");
  const turnLabel = $("#picks-away-label");
  turnCounter.classList.toggle("my-turn", state.on_the_clock);
  turnCounter.classList.toggle("complete", state.draft_complete);
  if (state.draft_complete || state.next_own_pick == null) {
    turnNumber.textContent = "DONE";
    turnLabel.textContent = "draft complete";
  } else if (state.on_the_clock) {
    turnNumber.textContent = "NOW";
    turnLabel.textContent = "your turn · optimizer running";
  } else {
    turnNumber.textContent = state.picks_away;
    turnLabel.textContent = `pick${state.picks_away === 1 ? "" : "s"} until your turn · pick #${state.next_own_pick}`;
  }
  $(".deck-clock").classList.toggle("my-turn", state.on_the_clock);
  $("#workbook-path").textContent = state.workbook_path;
  $("#drafted-path").textContent = state.drafted_path;
  $("#reset-practice").hidden = state.mode !== "practice";
}

function renderOutlook() {
  $("#outlook").innerHTML = state.position_outlook.map((item) => `
    <article class="outlook-card">
      <span class="pos">${item.position}</span>
      <strong title="${escapeHtml(item.player)}">${escapeHtml(item.player)}</strong>
      <span class="drop">${item.dropoff == null ? "Last option" : `-${formatNumber(item.dropoff)} by next pick`}</span>
    </article>`).join("");
}

function renderPlayers() {
  const query = $("#search").value.trim().toLowerCase();
  const players = state.players.filter((player) => {
    const positionMatch = activePosition === "ALL"
      || player.Position === activePosition
      || (activePosition === "FLEX" && ["RB", "WR"].includes(player.Position));
    const queryMatch = !query || `${player.Player} ${player.Team}`.toLowerCase().includes(query);
    return positionMatch && queryMatch;
  });
  const fallbackSummary = state.adp_fallback_count
    ? ` · ${state.adp_fallback_count} consensus ADP fallback${state.adp_fallback_count === 1 ? "" : "s"}`
    : "";
  $("#board-summary").textContent = `${players.length} shown · ${state.players.length} available${fallbackSummary}`;
  $("#board-count").textContent = state.players.length;
  $("#metric-heading").textContent = state.metric.replaceAll("_", " ");
  $("#players").innerHTML = players.slice(0, 250).map((player) => `
    <tr class="${player.Excluded ? "excluded-player" : ""}">
      <td><div class="player-name">${escapeHtml(player.Player)}</div><div class="player-team">${escapeHtml(player.Team)}</div></td>
      <td><span class="pos-pill pos-${escapeHtml(player.Position)}">${escapeHtml(player.Position)}</span></td>
      <td>${player.Bye ?? "—"}</td>
      <td>${formatNumber(player.ADP)}</td>
      <td><strong>${formatNumber(player[state.metric])}</strong></td>
      <td><div class="player-actions"><button class="icon-button avoid-button" data-exclusion-player="${escapeHtml(player.Player)}" data-exclusion-state="${player.Excluded ? "false" : "true"}">${player.Excluded ? "Restore" : "Avoid"}</button><button class="button draft-button" data-draft="${escapeHtml(player.Player)}">Mark taken</button></div></td>
    </tr>`).join("");
  $("#player-options").innerHTML = state.players.map((player) =>
    `<option value="${escapeHtml(player.Player)}"></option>`).join("");
}

function renderRoster() {
  $("#roster-count").textContent = state.roster.length;
  const roster = $("#roster");
  roster.classList.toggle("empty", !state.roster.length);
  roster.innerHTML = state.roster.length ? state.roster.map((player) => `
    <div class="roster-item">
      <span class="pos-pill pos-${escapeHtml(player.Position)}">${escapeHtml(player.Position)}</span>
      <span class="player-name">${escapeHtml(player.Player)}</span>
      <span class="log-meta">Bye ${player.Bye ?? "—"}</span>
    </div>`).join("") : "No players yet";
}

function renderLog() {
  $("#pick-count").textContent = state.picks.length;
  const log = $("#draft-log");
  log.classList.toggle("empty", !state.picks.length);
  const picks = [...state.picks].sort((a, b) => b.number - a.number).slice(0, 24);
  log.innerHTML = picks.length ? picks.map((pick) => `
    <div class="log-item">
      <span class="log-pick">#${pick.number}<small>ADP ${formatNumber(pick.adp)}</small></span>
      <div><div class="player-name">${escapeHtml(pick.player)}${pick.mine ? " ★" : ""}</div><div class="log-meta">${escapeHtml(pick.source)}${pick.locked ? " · locked" : ""}</div></div>
      <div><button class="icon-button" data-replace="${pick.number}" data-player="${escapeHtml(pick.player)}">Edit</button><button class="icon-button" data-undo="${pick.number}">Undo</button></div>
    </div>`).join("") : `Waiting for pick ${state.current_pick}`;
}

function renderMetric() {
  $("#metric").innerHTML = state.metrics.map((name) =>
    `<option value="${name}" ${name === state.metric ? "selected" : ""}>${name.replaceAll("_", " ")}</option>`).join("");
}

function renderLeagueStrength() {
  const container = $("#league-strength");
  const badge = $("#league-strength-status");
  if (!leagueStrength?.active) {
    badge.textContent = state.round < 4
      ? `Starts round 4 · currently round ${state.round}`
      : "Waiting for fast optimizer mode";
    badge.className = "optimizer-profile";
    container.className = "league-strength-grid empty";
    container.textContent = leagueStrength?.message || "Loads automatically in fast optimizer mode.";
    return;
  }
  badge.textContent = `Live · round ${leagueStrength.round} · after every pick`;
  badge.className = "optimizer-profile fast";
  container.className = "league-strength-grid";
  container.innerHTML = leagueStrength.teams.map((team) => `
    <article class="league-team ${team.mine ? "mine" : ""}">
      <div class="league-team-heading">
        <span class="league-rank">#${team.rank}</span>
        <strong>${escapeHtml(team.label)}</strong>
        <span class="league-total">${formatNumber(team.starter_points)} est. pts/game</span>
      </div>
      <div class="league-fill">${team.filled}/${team.starter_slots} projected starters · ${team.forecasted_starters} forecast · ${signedNumber(team.value_over_average)} ppg vs comparable starters${team.untracked_count ? ` · ${team.untracked_count} K/DST` : ""}</div>
      <div class="league-lineup">
        ${team.lineup.map((player) => `
          <div class="league-player ${player.projected ? "forecast" : ""}" title="${formatNumber(player.points)} ppg · ${signedNumber(player.value_over_average)} versus ${escapeHtml(player.slot)} average of ${formatNumber(player.comparison_average)} ppg">
            <span>${escapeHtml(player.slot)}</span><strong>${player.projected ? `Projected · ${formatNumber(player.points)} ppg` : escapeHtml(player.player)}</strong>
            <em class="league-value ${Number(player.value_over_average) >= 0 ? "positive" : "negative"}">${signedNumber(player.value_over_average)}</em>
          </div>`).join("")}
        ${team.missing.map((slot) => `<div class="league-player missing"><span>${escapeHtml(slot)}</span><strong>Open</strong></div>`).join("")}
      </div>
    </article>`).join("");
}

function niceTickStep(range, targetTicks = 6) {
  if (!Number.isFinite(range) || range <= 0) return 1;
  const rough = range / targetTicks;
  const power = 10 ** Math.floor(Math.log10(rough));
  const fraction = rough / power;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return niceFraction * power;
}

function tickLabel(value, step) {
  const decimals = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step)));
  return Number(value.toFixed(decimals)).toLocaleString();
}

function xTickStep(maxRank) {
  if (maxRank <= 12) return 1;
  if (maxRank <= 24) return 2;
  if (maxRank <= 40) return 5;
  return 10;
}

function renderChart() {
  if (!analysis) return;
  const colors = {QB: "#bf616a", RB: "#a3be8c", WR: "#81a1c1", TE: "#ebcb8b"};
  const allPoints = analysis.series.flatMap((series) => series.points);
  const allMarkers = analysis.series.flatMap((series) => series.markers);
  const values = [...allPoints, ...allMarkers].map((point) => Number(point.value));
  if (!values.length) return;
  const automaticMax = Math.max(...values);
  const automaticMin = Math.min(...values, 0);
  const requestedMax = chartSettings.yMax == null ? automaticMax : Number(chartSettings.yMax);
  const requestedMin = chartSettings.yMin == null ? automaticMin : Number(chartSettings.yMin);
  const yStep = niceTickStep(requestedMax - requestedMin);
  const maxValue = chartSettings.yMax == null ? Math.ceil(requestedMax / yStep) * yStep : requestedMax;
  const minValue = chartSettings.yMin == null ? Math.floor(requestedMin / yStep) * yStep : requestedMin;
  const maxRank = analysis.limit;
  const left = 55, right = 915, top = 20, bottom = 285;
  const x = (rank) => left + ((rank - 1) / Math.max(1, maxRank - 1)) * (right - left);
  const y = (value) => bottom - ((value - minValue) / Math.max(1, maxValue - minValue)) * (bottom - top);
  let svg = "";
  const firstYTick = Math.ceil(minValue / yStep) * yStep;
  for (let value = firstYTick; value <= maxValue + yStep * .001; value += yStep) {
    const py = y(value);
    svg += `<line class="chart-grid" x1="${left}" y1="${py}" x2="${right}" y2="${py}"></line>`;
    svg += `<line class="chart-tick" x1="${left - 5}" y1="${py}" x2="${left}" y2="${py}"></line>`;
    svg += `<text class="chart-axis-label" x="${left - 9}" y="${py + 3}" text-anchor="end">${tickLabel(value, yStep)}</text>`;
  }
  svg += `<line class="chart-axis" x1="${left}" y1="${top}" x2="${left}" y2="${bottom}"></line>`;
  svg += `<line class="chart-axis" x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}"></line>`;
  svg += `<text class="chart-axis-label" x="14" y="${(top + bottom) / 2}" text-anchor="middle" transform="rotate(-90 14 ${(top + bottom) / 2})">${escapeHtml(analysis.metric.replaceAll("_", " "))}</text>`;
  const rankStep = xTickStep(maxRank);
  for (let rank = 1; rank <= maxRank; rank += rankStep) {
    const px = x(rank);
    svg += `<line class="chart-grid chart-grid-vertical" x1="${px}" y1="${top}" x2="${px}" y2="${bottom}"></line>`;
    svg += `<line class="chart-tick" x1="${px}" y1="${bottom}" x2="${px}" y2="${bottom + 5}"></line>`;
    svg += `<text class="chart-axis-label" x="${x(rank)}" y="${bottom + 22}" text-anchor="middle">${rank}</text>`;
  }
  if ((maxRank - 1) % rankStep !== 0) {
    svg += `<line class="chart-tick" x1="${x(maxRank)}" y1="${bottom}" x2="${x(maxRank)}" y2="${bottom + 5}"></line>`;
    svg += `<text class="chart-axis-label" x="${x(maxRank)}" y="${bottom + 22}" text-anchor="middle">${maxRank}</text>`;
  }
  svg += `<text class="chart-axis-label" x="${(left + right) / 2}" y="${bottom + 40}" text-anchor="middle">Position rank</text>`;
  analysis.series.forEach((series) => {
    const color = colors[series.position];
    const path = series.points.map((point, index) => `${index ? "L" : "M"}${x(point.rank)},${y(point.value)}`).join(" ");
    svg += `<path class="chart-line" stroke="${color}" d="${path}"></path>`;
    series.markers.forEach((marker, index) => {
      if (!marker.rank || marker.rank > maxRank) return;
      const px = x(marker.rank), py = y(marker.value);
      svg += `<circle class="chart-marker ${index ? "following" : ""}" stroke="${color}" cx="${px}" cy="${py}" r="${index ? 7 : 9}"></circle>`;
      svg += `<text class="chart-marker-label" x="${px + 8}" y="${py - 10}">${escapeHtml(marker.player)} · #${marker.pick}</text>`;
    });
  });
  $("#dropoff-chart").innerHTML = svg;
  $("#chart-legend").innerHTML = analysis.series.map((series) => `
    <span class="legend-item"><span class="legend-swatch" style="background:${colors[series.position]}"></span>${series.position}${series.dropoff == null ? "" : ` · ${formatNumber(series.dropoff)} drop`}</span>`).join("");
  const recommended = analysis.recommended;
  $("#chart-recommendation").textContent = recommended
    ? `Largest next-turn drop: ${recommended.player} (${recommended.position}, ${formatNumber(recommended.dropoff)})`
    : "Not enough future picks to calculate a drop-off";
}

function renderChartSettings() {
  if (!analysis) return;
  $("#chart-metric").innerHTML = analysis.metrics.map((metric) =>
    `<option value="${metric}" ${metric === chartSettings.metric ? "selected" : ""}>${metric.replaceAll("_", " ")}</option>`).join("");
  $("#chart-y-min").value = chartSettings.yMin ?? "";
  $("#chart-y-max").value = chartSettings.yMax ?? "";
  $("#chart-rank-limit").value = chartSettings.limit;
}

function renderOptimization() {
  const results = $("#optimizer-results");
  const tabs = $("#plan-tabs");
  if (!optimization) {
    tabs.innerHTML = "";
    results.className = "optimizer-results empty";
    results.textContent = "No projection yet";
    updateDeckLead();
    return;
  }
  if (!optimization.plans.length) {
    tabs.innerHTML = "";
    results.className = "optimizer-results empty";
    results.textContent = optimization.message || "No complete plan available.";
    updateDeckLead();
    return;
  }
  activePlan = Math.min(activePlan, optimization.plans.length - 1);
  tabs.innerHTML = optimization.plans.map((plan, index) => `
    <button class="${index === activePlan ? "active" : ""}" data-plan="${index}">Plan ${index + 1}</button>`).join("");
  const plan = optimization.plans[activePlan];
  results.className = "optimizer-results";
  results.innerHTML = `
    <p class="plan-score">${optimization.metric.replaceAll("_", " ")}: ${formatNumber(plan.total)} · risk-adjusted ${formatNumber(plan.risk_adjusted_total)}</p>
    <div class="plan-picks">${plan.selections.map((pick) => `
      <div class="plan-pick">
        <span class="pick-label">${pick.pick_label}</span>
        <span class="pos-pill pos-${escapeHtml(pick.position)}">${escapeHtml(pick.slot)}</span>
        <span><strong>${escapeHtml(pick.player)}</strong><span class="log-meta"> ADP ${pick.adp ?? "—"}${pick.backup ? ` · fallback ${escapeHtml(pick.backup)}` : ""}</span></span>
        <span class="probability">${Math.round(pick.probability * 100)}% avail.</span>
        <button class="icon-button avoid-button" data-exclusion-player="${escapeHtml(pick.player)}" data-exclusion-state="true">Avoid</button>
      </div>`).join("")}</div>`;
  updateDeckLead();
}

function renderExcludedPlayers() {
  const excluded = state.excluded_players || [];
  $("#excluded-list").innerHTML = excluded.length
    ? `<span class="excluded-label">Avoiding</span>${excluded.map((player) => `<button class="excluded-chip" data-exclusion-player="${escapeHtml(player)}" data-exclusion-state="false">${escapeHtml(player)} ×</button>`).join("")}`
    : "";
}

function formatLastRun(value) {
  if (!value) return "not run yet";
  return new Date(value).toLocaleTimeString([], {
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

function renderOptimizerProfile() {
  const profile = optimizerProfile || state?.optimizer_profile;
  const badge = $("#optimizer-profile");
  if (!profile?.runs) {
    badge.textContent = "Own picks auto · not run yet";
    badge.className = "optimizer-profile";
    return;
  }
  const autoMode = profile.auto_every_pick
    ? "Every pick auto"
    : "Own picks auto";
  badge.textContent = `Last run ${formatLastRun(profile.last_run_at)} · ${profile.last_ms} ms · ${autoMode}`;
  badge.className = `optimizer-profile ${profile.auto_every_pick ? "fast" : "profiled"}`;
}

function updateDeckLead() {
  const first = optimization?.plans?.[0]?.selections?.[0];
  if (first) {
    $("#deck-lead").textContent = first.player;
    $("#deck-lead-detail").textContent = `${first.slot} at ${first.pick_label} · ${Math.round(first.probability * 100)}% available${first.backup ? ` · fallback ${first.backup}` : ""}`;
    return;
  }
  const recommendation = analysis?.recommended;
  $("#deck-lead").textContent = recommendation?.player || "Calculating…";
  $("#deck-lead-detail").textContent = recommendation
    ? `Largest ${recommendation.position} drop before your following pick`
    : "Projected team plan loads automatically";
}

function render() {
  renderHeader();
  renderMetric();
  renderOutlook();
  renderPlayers();
  renderRoster();
  renderLog();
  renderOptimization();
  renderOptimizerProfile();
  renderExcludedPlayers();
  renderLeagueStrength();
  $("#manual-mine").checked = state.on_the_clock;
}

async function loadAnalysis() {
  try {
    const params = new URLSearchParams({metric: chartSettings.metric, limit: chartSettings.limit});
    analysis = await request(`/api/analysis?${params}`);
    renderChartSettings();
    renderChart();
    updateDeckLead();
  } catch (error) {
    $("#chart-recommendation").textContent = "Chart unavailable";
    toast(error.message, true);
  }
}

async function runOptimization(automatic = false) {
  const button = $("#optimize-button");
  button.disabled = true;
  button.textContent = "Optimizing…";
  $("#optimizer-status").textContent = automatic
    ? `Automatically optimizing for pick ${state.current_pick}…`
    : `Recalculating from pick ${state.current_pick}…`;
  try {
    optimization = await request("/api/optimize", {method: "POST", body: "{}"});
    const responseProfile = optimization.profile;
    if (responseProfile) {
      optimizerProfile = responseProfile;
      state.optimizer_profile = responseProfile;
    }
    activePlan = 0;
    renderOptimization();
    renderOptimizerProfile();
    const profile = optimizerProfile || state?.optimizer_profile;
    const autoMode = profile?.auto_every_pick
      ? "automatic after every pick"
      : "automatic on your picks";
    $("#optimizer-status").textContent = profile?.runs
      ? `Last run ${formatLastRun(profile.last_run_at)} in ${profile.last_ms} ms · ${autoMode}.`
      : `Projected from pick ${state.current_pick} using ${optimization.metric.replaceAll("_", " ")} · ${autoMode}.`;
    if (automatic) lastAutomaticOptimization = `${state.current_pick}:${state.metric}`;
  } catch (error) {
    $("#optimizer-status").textContent = "Optimization failed; draft tracking is still available.";
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Optimize";
  }
}

async function loadLeagueStrength() {
  try {
    leagueStrength = await request("/api/league-strength");
    renderLeagueStrength();
  } catch (error) {
    $("#league-strength-status").textContent = "Tracker unavailable";
    $("#league-strength").textContent = "Draft tracking and optimization are still available.";
  }
}

async function afterStateChange({forceOptimization = false} = {}) {
  render();
  await loadAnalysis();
  const key = `${state.current_pick}:${state.metric}`;
  const everyPick = Boolean(
    (optimizerProfile || state.optimizer_profile)?.auto_every_pick
  );
  const shouldRun = state.on_the_clock || everyPick;
  if (forceOptimization || (shouldRun && key !== lastAutomaticOptimization)) {
    await runOptimization(true);
  } else if (optimization) {
    const profile = optimizerProfile || state.optimizer_profile;
    $("#optimizer-status").textContent = `Last run ${formatLastRun(profile?.last_run_at)} · waiting for ${everyPick ? "the next pick" : "your next pick"}.`;
  }
  await loadLeagueStrength();
}

async function loadState() {
  try {
    state = await request("/api/state");
    optimizerProfile = state.optimizer_profile || optimizerProfile;
    await afterStateChange();
  } catch (error) { toast(error.message, true); }
}

async function markPlayerTaken(player, mine = null) {
  if (!player) return toast("Choose a player first.", true);
  try {
    const body = {player};
    if (mine != null) body.mine = mine;
    const payload = await request("/api/picks", {method: "POST", body: JSON.stringify(body)});
    state = payload.state;
    $("#manual-player").value = "";
    await afterStateChange();
    toast(`${player} marked taken.`);
  } catch (error) { toast(error.message, true); }
}

async function markUntrackedTaken(position) {
  try {
    const payload = await request("/api/picks/untracked", {
      method: "POST",
      body: JSON.stringify({position}),
    });
    state = payload.state;
    await afterStateChange();
    toast(`${position === "DST" ? "D/ST" : position} pick recorded without a player lookup.`);
  } catch (error) { toast(error.message, true); }
}

document.addEventListener("click", async (event) => {
  const drafted = event.target.closest("[data-draft]");
  if (drafted) return markPlayerTaken(drafted.dataset.draft);
  const untracked = event.target.closest("[data-untracked]");
  if (untracked) return markUntrackedTaken(untracked.dataset.untracked);
  const exclusion = event.target.closest("[data-exclusion-player]");
  if (exclusion) {
    try {
      const excluded = exclusion.dataset.exclusionState === "true";
      const payload = await request("/api/exclusions", {
        method: "POST",
        body: JSON.stringify({
          player: exclusion.dataset.exclusionPlayer,
          excluded,
        }),
      });
      state = payload.state;
      await afterStateChange({forceOptimization: true});
      toast(`${payload.player} ${excluded ? "will be avoided" : "is back in consideration"}.`);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const plan = event.target.closest("[data-plan]");
  if (plan) {
    activePlan = Number(plan.dataset.plan);
    return renderOptimization();
  }
  const undo = event.target.closest("[data-undo]");
  if (undo) {
    try {
      const payload = await request(`/api/picks/${undo.dataset.undo}`, {method: "DELETE"});
      state = payload.state;
      await afterStateChange();
      toast(`Pick ${undo.dataset.undo} undone.`);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const replace = event.target.closest("[data-replace]");
  if (replace) {
    const player = window.prompt(`Replace ${replace.dataset.player} with:`);
    if (!player) return;
    try {
      const payload = await request(`/api/picks/${replace.dataset.replace}`, {
        method: "PUT", body: JSON.stringify({player}),
      });
      state = payload.state;
      await afterStateChange();
      toast(`Pick ${replace.dataset.replace} corrected and locked.`);
    } catch (error) { toast(error.message, true); }
  }
});

$("#manual-submit").addEventListener("click", () =>
  markPlayerTaken($("#manual-player").value.trim(), $("#manual-mine").checked));
$("#manual-player").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("#manual-submit").click();
});
$("#search").addEventListener("input", renderPlayers);
$("#optimize-button").addEventListener("click", () => runOptimization(false));

$("#chart-apply").addEventListener("click", async () => {
  const minimum = $("#chart-y-min").value === "" ? null : Number($("#chart-y-min").value);
  const maximum = $("#chart-y-max").value === "" ? null : Number($("#chart-y-max").value);
  const limit = Number($("#chart-rank-limit").value);
  if (minimum != null && maximum != null && maximum <= minimum) {
    return toast("Y maximum must be greater than Y minimum.", true);
  }
  if (!Number.isInteger(limit) || limit < 3 || limit > 60) {
    return toast("Position ranks must be a whole number from 3 to 60.", true);
  }
  chartSettings = {metric: $("#chart-metric").value, yMin: minimum, yMax: maximum, limit};
  localStorage.setItem("draft-chart-settings", JSON.stringify(chartSettings));
  await loadAnalysis();
  toast("Chart settings applied.");
});

$("#chart-reset").addEventListener("click", async () => {
  chartSettings = {...defaultChartSettings};
  localStorage.setItem("draft-chart-settings", JSON.stringify(chartSettings));
  await loadAnalysis();
  toast("Chart settings reset.");
});

function setBoard(open, {focus = true} = {}) {
  boardOpen = open;
  document.body.classList.toggle("board-visible", open);
  $("#board-drawer").setAttribute("aria-hidden", String(!open));
  const button = $("#board-open");
  button.firstChild.textContent = open ? "Close board " : "Open board ";
  button.setAttribute("aria-expanded", String(open));
  localStorage.setItem("draft-board-open", String(open));
  if (open && focus) setTimeout(() => $("#search").focus(), 220);
}

$("#board-open").addEventListener("click", () => setBoard(!boardOpen));
$("#board-close").addEventListener("click", () => setBoard(false));
$("#board-overlay").addEventListener("click", () => setBoard(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setBoard(false);
});

$("#position-filters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-position]");
  if (!button) return;
  activePosition = button.dataset.position;
  document.querySelectorAll("#position-filters button").forEach((item) =>
    item.classList.toggle("active", item === button));
  renderPlayers();
});

$("#metric").addEventListener("change", async (event) => {
  try {
    const payload = await request("/api/settings", {
      method: "PUT", body: JSON.stringify({metric: event.target.value}),
    });
    state = payload.state;
    await afterStateChange({forceOptimization: true});
  } catch (error) { toast(error.message, true); }
});

$("#sync-button").addEventListener("click", async () => {
  if (syncInFlight) return toast("A Sleeper sync is already running.");
  const button = $("#sync-button");
  syncInFlight = true;
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    const payload = await request("/api/sync", {method: "POST", body: "{}"});
    state = payload.state;
    lastSyncAt = new Date().toISOString();
    await afterStateChange();
    renderSyncStatus();
    if (payload.conflicts.length || payload.unmatched.length) {
      toast(`Synced with ${payload.conflicts.length} conflict(s) and ${payload.unmatched.length} out-of-pool selection(s).`, Boolean(payload.conflicts.length));
    } else {
      toast(`Synced ${payload.added.length} new pick${payload.added.length === 1 ? "" : "s"}.`);
    }
  } catch (error) {
    toast(error.message, true);
    renderSyncStatus(true);
  } finally {
    syncInFlight = false;
    button.disabled = !state.sync_available;
    button.textContent = "Sync now";
  }
});

$("#sheet-refresh").addEventListener("click", async () => {
  try {
    const payload = await request("/api/spreadsheet/refresh", {method: "POST", body: "{}"});
    state = payload.state;
    await afterStateChange();
    toast(`Imported ${payload.added.length} deletion(s); restored ${payload.removed.length}.`);
  } catch (error) { toast(error.message, true); }
});

$("#reset-practice").addEventListener("click", async () => {
  if (state.mode !== "practice") return toast("Live drafts cannot be reset here.", true);
  if (!window.confirm("Clear every practice pick and restore the practice spreadsheet?")) return;
  try {
    const payload = await request("/api/reset", {method: "POST", body: "{}"});
    state = payload.state;
    optimization = null;
    lastAutomaticOptimization = null;
    await afterStateChange();
    toast("Practice draft reset.");
  } catch (error) { toast(error.message, true); }
});

setBoard(boardOpen, {focus: false});
loadState().then(() => scheduleLiveSync(1000));

function scheduleLiveSync(delay = liveSyncIntervalMs) {
  clearTimeout(syncPollTimer);
  syncPollTimer = setTimeout(pollLiveDraft, delay);
}

async function pollLiveDraft() {
  if (!state?.sync_available || state.mode !== "live" || syncInFlight) {
    scheduleLiveSync();
    return;
  }
  syncInFlight = true;
  try {
    const payload = await request("/api/sync", {method: "POST", body: "{}"});
    lastSyncAt = new Date().toISOString();
    renderSyncStatus();
    const draftChanged = (
      payload.state.current_pick !== state.current_pick
      || payload.state.picks.length !== state.picks.length
    );
    if (draftChanged) {
      const previousPick = state.current_pick;
      state = payload.state;
      await afterStateChange();
      const message = payload.added.length
        ? `Auto-synced ${payload.added.length} new pick${payload.added.length === 1 ? "" : "s"}.`
        : `Caught up from pick ${previousPick} to pick ${state.current_pick}.`;
      toast(message);
    }
  } catch (_) {
    renderSyncStatus(true);
  } finally {
    syncInFlight = false;
    scheduleLiveSync();
  }
}
