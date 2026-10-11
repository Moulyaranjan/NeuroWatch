const state = {
  history: [],
  baseline: null,
  alerts: [],
  backendConnected: false,
  socketConnected: false,
  sessionStartedAt: null,
  sessionTimer: null,
  reconnectTimer: null,
  lastRenderedPacket: null,
};

const socketUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/dashboard`;
const AXES = ['ax', 'ay', 'az', 'gx', 'gy', 'gz'];
const canvasIds = ['homeTrendChart', 'stepsTrendChart', 'paceTrendChart', 'accelChart', 'gyroChart', 'magnitudeChart'];
const names = {
  home: 'Your movement, understood.',
  timeline: 'Activity Timeline',
  progress: 'My Progress',
  heart: 'Heart & Sensors',
  insights: 'Movement Insights',
  sessions: 'Sessions',
  research: 'Research Lab',
  quality: 'Device & Data Quality',
  about: 'Settings & About',
};

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character]);
}

function showToast(message, kind = 'success') {
  const region = el('toast-region');
  const toast = document.createElement('div');
  toast.className = `toast toast-${kind}`;
  toast.setAttribute('role', 'status');
  toast.textContent = message;
  region.appendChild(toast);
  window.setTimeout(() => toast.remove(), 3800);
}

function setDot(element, stateName) {
  if (!element) return;
  element.classList.remove('is-connected', 'is-stale', 'is-error', 'state-muted');
  if (stateName) element.classList.add(stateName);
}

function setConnectionStatus(label, status) {
  el('connection-label').textContent = label;
  setDot(el('connection-dot'), status);
}

function safeDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatTimestamp(value, options = {}) {
  const date = safeDate(value);
  if (!date) return value ? String(value) : 'Unavailable';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
    ...options,
  }).format(date);
}

function formatTime(value) {
  const date = safeDate(value);
  return date ? new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date) : '—';
}

function formatNumber(value, digits = 2) {
  const number = Number(value);
  return value !== null && value !== undefined && Number.isFinite(number) ? number.toFixed(digits) : 'Unavailable';
}

function finiteValue(value) {
  const number = Number(value);
  return value !== null && value !== undefined && Number.isFinite(number) ? number : null;
}

function currentPacketAge(latest) {
  const date = safeDate(latest?.timestamp);
  if (!date) return null;
  return Math.max(0, Date.now() - date.getTime());
}

function getLatest() {
  return state.history.length ? state.history[state.history.length - 1] : null;
}

function isFresh(latest) {
  const age = currentPacketAge(latest);
  return age !== null && age <= 30000;
}

function activityKind(activity) {
  const label = String(activity || '').toLowerCase();
  if (label.includes('walk')) return 'walking';
  if (label.includes('turn') || label.includes('rotat')) return 'turning';
  if (label.includes('station') || label.includes('rest')) return 'stationary';
  return 'unknown';
}

function readableActivity(activity) {
  if (!activity) return 'Unclassified';
  return String(activity).replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function magnitude(values, keys) {
  const readings = keys.map((key) => finiteValue(values?.[key]));
  if (readings.some((reading) => reading === null)) return null;
  return Math.sqrt(readings.reduce((sum, reading) => sum + (reading * reading), 0));
}

function completeHistory(history) {
  return Array.isArray(history) ? history.filter((event) => event && typeof event === 'object') : [];
}

function setPage(target) {
  const page = names[target] ? target : 'home';
  document.querySelectorAll('.nav-button').forEach((button) => {
    const active = button.dataset.target === page;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  });
  document.querySelectorAll('.page-section').forEach((section) => {
    const active = section.id === page;
    section.classList.toggle('active', active);
    section.setAttribute('aria-hidden', active ? 'false' : 'true');
  });
  el('page-title').textContent = names[page];
  const activeButton = document.querySelector(`.nav-button[data-target="${page}"]`);
  el('page-subtitle').textContent = activeButton?.dataset.subtitle || '';
  window.scrollTo({ top: 0, behavior: 'smooth' });
  resizeCharts();
}

function bindNavigation() {
  document.querySelectorAll('.nav-button[data-target]').forEach((button) => {
    button.addEventListener('click', () => setPage(button.dataset.target));
  });
  document.querySelectorAll('[data-jump]').forEach((button) => {
    button.addEventListener('click', () => setPage(button.dataset.jump));
  });
  document.querySelectorAll('.brand[href="#home"]').forEach((brand) => {
    brand.addEventListener('click', (event) => {
      event.preventDefault();
      setPage('home');
    });
  });
}

function updateHero(latest) {
  const fresh = isFresh(latest);
  const connected = state.socketConnected;
  const source = latest?.source || 'unavailable';
  const sessionActive = Boolean(state.sessionStartedAt);
  const kicker = el('hero-state');
  kicker.textContent = source === 'simulated'
    ? 'SIMULATED DATA · NOT HARDWARE'
    : sessionActive
      ? (fresh ? 'SESSION IN PROGRESS' : 'SESSION IN PROGRESS · NO FRESH PACKET')
      : (fresh ? 'MOVEMENT RECORDED' : 'SESSION READY');
  kicker.parentElement.classList.toggle('is-live', fresh && connected);
  el('hero-title').textContent = 'A clearer picture of how you move.';

  if (!latest) {
    el('hero-summary').textContent = 'Start a session and connect your movement sensor. Your readings will appear here as they arrive.';
    el('hero-note').textContent = 'No readings yet. This dashboard only displays data received from your device.';
  } else if (!fresh) {
    el('hero-summary').textContent = `The last movement reading arrived ${formatTimestamp(latest.timestamp)}. Waiting for a fresh packet before showing live status.`;
    el('hero-note').textContent = 'The last packet is more than 30 seconds old. Check the device connection before interpreting measurements.';
  } else if (source === 'simulated') {
    el('hero-summary').textContent = 'Simulated data is present, but it is separate from real device measurements and does not indicate a connected movement sensor.';
    el('hero-note').textContent = 'Simulation data is not a hardware reading and should not be interpreted as observed movement.';
  } else {
    const activity = latest.activity ? ` Activity reported: ${readableActivity(latest.activity)}.` : ' Activity has not been classified by the sensor.';
    el('hero-summary').textContent = `Movement data is arriving from ${latest.sensor || 'a connected sensor'} (${source}).${activity}`;
    el('hero-note').textContent = `${state.history.length} packet${state.history.length === 1 ? '' : 's'} available in this live history window. Measurements are not a clinical assessment.`;
  }

  el('session-indicator').textContent = sessionActive ? 'ACTIVE' : 'READY';
  el('session-indicator').classList.toggle('is-active', sessionActive);
  el('start-session').disabled = sessionActive;
  el('end-session').disabled = !sessionActive;

  el('latest-packet').textContent = latest ? formatTimestamp(latest.timestamp) : 'No data';
  el('activity-type').textContent = latest?.activity ? readableActivity(latest.activity) : 'Not classified';

  if (sessionActive) {
    el('duration-caption').textContent = 'Since this session started';
    updateElapsed();
  } else {
    el('session-duration').innerHTML = '—<small>elapsed</small>';
    el('active-duration').textContent = '—';
    el('duration-unit').textContent = 'elapsed';
    el('duration-caption').textContent = 'Start a session to begin';
  }
}

function updateElapsed() {
  if (!state.sessionStartedAt) return;
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.sessionStartedAt.getTime()) / 1000));
  const hours = Math.floor(elapsedSeconds / 3600);
  const minutes = Math.floor((elapsedSeconds % 3600) / 60);
  const seconds = elapsedSeconds % 60;
  const formatted = hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
  el('session-duration').innerHTML = `${formatted}<small>elapsed</small>`;
  el('active-duration').textContent = formatted;
  el('duration-unit').textContent = 'session time';
}

function updateMetrics(latest) {
  const steps = finiteValue(latest?.step_count);
  const pace = finiteValue(latest?.cadence);
  const heartRate = finiteValue(latest?.heart_rate);
  const heartValid = heartRate !== null && heartRate > 0;
  const simulated = latest?.source === 'simulated';

  el('step-count').textContent = steps === null ? '—' : new Intl.NumberFormat().format(steps);
  el('steps-caption').textContent = simulated ? 'Simulation · not a device reading' : steps === null ? 'Waiting for a step count' : 'Latest sensor-reported count';
  el('cadence').textContent = pace !== null ? pace.toFixed(1) : '—';
  el('pace-caption').textContent = simulated ? 'Simulation · not a device reading' : pace !== null ? 'Reported by the movement sensor' : 'Measurement unavailable';
  el('heart-rate').textContent = heartValid ? String(Math.round(heartRate)) : '—';
  el('heart-caption').textContent = simulated ? 'Simulation · not a device reading' : heartValid ? 'Estimate received from sensor' : 'Optional sensor not reporting';
}

function renderLegend(targetId) {
  const legend = el(targetId);
  if (!legend) return;
  legend.innerHTML = `
    <span class="legend-item"><i class="legend-dot walking"></i><span><strong>Walking</strong> — reported walking label</span></span>
    <span class="legend-item"><i class="legend-dot turning"></i><span><strong>Turning</strong> — reported turning / rotation label</span></span>
    <span class="legend-item"><i class="legend-dot stationary"></i><span><strong>Stationary</strong> — reported stationary / rest label</span></span>
    <span class="legend-item"><i class="legend-dot unknown"></i><span><strong>Unclassified</strong> — no supported label</span></span>`;
}

function renderTimeline(history) {
  const home = el('home-timeline');
  const full = el('activity-timeline');
  const entries = history.slice(-12);
  renderLegend('timeline-legend');
  renderLegend('full-timeline-legend');

  if (!entries.length) {
    const empty = '<div class="timeline-empty"><div><div class="empty-illustration" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div><div class="empty-copy">Movement events will appear here after a real sensor packet arrives. No activity intervals are being estimated.</div></div></div>';
    home.innerHTML = empty;
    full.innerHTML = '<div class="event-timeline-empty"><div><div class="empty-illustration" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div><div class="empty-copy">Waiting for a real session. Recorded timestamps and supported activity labels will appear here.</div></div></div>';
    el('session-range').textContent = 'No session data';
    return;
  }

  const rail = entries.map((event) => {
    const kind = activityKind(event.activity);
    const time = formatTime(event.timestamp);
    return `<div class="rail-event" title="${escapeHtml(formatTimestamp(event.timestamp))}"><i class="rail-dot ${kind}"></i><span class="rail-label">${escapeHtml(readableActivity(event.activity))}<time>${escapeHtml(time)}</time></span></div>`;
  }).join('');
  home.innerHTML = `<div class="event-rail" aria-label="Latest ${entries.length} sensor events">${rail}</div>`;

  full.innerHTML = entries.slice().reverse().map((event) => {
    const kind = activityKind(event.activity);
    const timestamp = formatTimestamp(event.timestamp);
    const description = event.activity
      ? `Activity label received in the packet: ${readableActivity(event.activity)}.`
      : 'The packet did not include an activity classification.';
    return `<article class="event-record"><time class="event-time" datetime="${escapeHtml(event.timestamp || '')}">${escapeHtml(timestamp)}</time><span class="event-pin"><i class="${kind}"></i></span><div class="event-description"><strong>${escapeHtml(readableActivity(event.activity))}</strong><small>${escapeHtml(description)} Packet source: ${escapeHtml(event.source || 'unavailable')} · Sensor: ${escapeHtml(event.sensor || 'unknown')}</small></div></article>`;
  }).join('');

  const first = entries[0].timestamp;
  const last = entries[entries.length - 1].timestamp;
  el('session-range').textContent = `${formatTimestamp(first)} — ${formatTimestamp(last)}`;
}

function historiesBySession(history) {
  const grouped = new Map();
  history.forEach((event) => {
    const key = event.session_id || 'session-id-unavailable';
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(event);
  });
  return Array.from(grouped.entries()).reverse();
}

function renderSessions(history) {
  const sessions = historiesBySession(history);
  const allPackets = history.length;
  el('session-count').textContent = String(sessions.length);
  el('packet-count').textContent = String(allPackets);
  el('sessions-last-seen').textContent = history.length ? formatTimestamp(history[history.length - 1].timestamp) : 'No data';

  if (!sessions.length) {
    el('session-history').innerHTML = '<div class="empty-state">No recorded session packets yet. Start a session and connect the movement sensor to create a record.</div>';
    return;
  }

  el('session-history').innerHTML = sessions.map(([sessionId, records]) => {
    const first = records[0];
    const last = records[records.length - 1];
    const readingsWithSteps = records.filter((entry) => finiteValue(entry.step_count) !== null);
    const readingsWithPace = records.filter((entry) => finiteValue(entry.cadence) !== null);
    const readingsWithHeart = records.filter((entry) => finiteValue(entry.heart_rate) !== null && finiteValue(entry.heart_rate) > 0);
    const values = last.values || {};
    const observedActivity = last.activity ? readableActivity(last.activity) : 'Unclassified';
    return `<article class="session-record">
      <div class="session-record-head"><div><h3>${escapeHtml(sessionId)}</h3><time>${escapeHtml(formatTimestamp(first.timestamp))} — ${escapeHtml(formatTimestamp(last.timestamp))}</time></div><span class="quiet-badge">${records.length} PACKET${records.length === 1 ? '' : 'S'}</span></div>
      <div class="session-record-stats">
        <div><span>Latest reported steps</span><strong>${last.step_count == null ? 'Unavailable' : escapeHtml(new Intl.NumberFormat().format(last.step_count))}</strong></div>
        <div><span>Latest walking pace</span><strong>${last.cadence == null ? 'Unavailable' : `${escapeHtml(formatNumber(last.cadence, 1))} steps/min`}</strong></div>
        <div><span>Heart-rate readings</span><strong>${readingsWithHeart.length ? `${readingsWithHeart.length} packet${readingsWithHeart.length === 1 ? '' : 's'}` : 'None reported'}</strong></div>
        <div><span>Activity label</span><strong>${escapeHtml(observedActivity)}</strong></div>
      </div>
      <div class="session-record-stats session-evidence">
        <div><span>Packets with step count</span><strong>${readingsWithSteps.length} / ${records.length}</strong></div>
        <div><span>Packets with pace</span><strong>${readingsWithPace.length} / ${records.length}</strong></div>
        <div><span>Latest sensor</span><strong>${escapeHtml(last.sensor || 'Unknown')}</strong></div>
        <div><span>Acceleration magnitude</span><strong>${formatNumber(magnitude(values, ['ax', 'ay', 'az']), 3)} m/s²</strong></div>
      </div>
    </article>`;
  }).join('');
}

function rollingBaseline() {
  if (!state.history.length) return null;
  const entries = state.history.slice(-50);
  const totals = Object.fromEntries(AXES.map((key) => [key, 0]));
  const counts = Object.fromEntries(AXES.map((key) => [key, 0]));
  entries.forEach((event) => AXES.forEach((key) => {
    const value = finiteValue(event.values?.[key]);
    if (value !== null) {
      totals[key] += value;
      counts[key] += 1;
    }
  }));
  return Object.fromEntries(AXES.map((key) => [key, counts[key] ? finiteValue(state.baseline?.[key]) ?? totals[key] / counts[key] : null]));
}

function baselineComparison() {
  const baseline = rollingBaseline();
  const latest = getLatest();
  const recent = state.history.slice(-50);
  const currentMagnitude = latest ? magnitude(latest.values, ['ax', 'ay', 'az']) : null;
  const baselineMagnitude = baseline ? magnitude(baseline, ['ax', 'ay', 'az']) : null;
  return {
    baseline,
    count: recent.length,
    currentMagnitude,
    baselineMagnitude,
    difference: currentMagnitude !== null && baselineMagnitude !== null ? currentMagnitude - baselineMagnitude : null,
  };
}

function renderInsights() {
  const latest = getLatest();
  const comparison = baselineComparison();
  const enough = comparison.count >= 2 && comparison.difference !== null;
  const serverObservation = state.alerts.find((message) => typeof message === 'string' && message.trim());
  const observation = !latest
    ? 'More data is needed before a comparison can be made.'
    : !enough
      ? 'One packet is available. More readings are needed before comparing movement patterns.'
      : serverObservation?.toLowerCase().includes('deviation')
        ? 'Movement intensity differed from the current rolling reference.'
        : 'No threshold-crossing difference was reported for the analyzed feature.';
  const description = !latest
    ? 'No sensor packet is available. Start a real session to collect readings.'
    : !enough
      ? 'The system needs multiple packets to calculate a difference. No historical or clinical conclusion is inferred.'
      : `Latest acceleration magnitude is ${formatNumber(comparison.currentMagnitude, 3)} m/s². The rolling reference is ${formatNumber(comparison.baselineMagnitude, 3)} m/s²; observed difference is ${comparison.difference >= 0 ? '+' : ''}${formatNumber(comparison.difference, 3)} m/s².`;
  const why = !latest
    ? 'Reason: the backend has not received movement measurements.'
    : `Reason: the backend compares the latest acceleration-vector magnitude with the magnitude of its per-axis mean over up to 50 recent packets. ${serverObservation ? `Backend observation: ${serverObservation}` : ''}`;
  const confidence = !latest ? 'No data' : `${comparison.count} packet${comparison.count === 1 ? '' : 's'} in rolling window`;
  const mainMarkup = `<div class="insight-card"><span class="insight-symbol">${latest ? '↗' : 'i'}</span><div class="insight-text"><strong>${escapeHtml(observation)}</strong><p>${escapeHtml(description)}</p><div class="insight-meta"><span>${escapeHtml(confidence)}</span><span>${comparison.count ? 'Rolling reference' : 'No baseline available'}</span></div></div></div>`;
  el('home-insight').innerHTML = mainMarkup;
  el('insight-list').innerHTML = `
    <article class="insight-card-large"><span class="insight-symbol">${latest ? '↗' : 'i'}</span><div><h3>${escapeHtml(observation)}</h3><p>${escapeHtml(description)}</p><p class="insight-why">${escapeHtml(why)}</p></div><div class="insight-reliability">Supporting data<strong>${escapeHtml(confidence)}</strong></div></article>
    ${!latest ? '<div class="empty-state">No personal baseline is available. Begin by recording real movement data; the current backend reference is calculated from recent packets rather than a separate baseline session.</div>' : `<div class="explanation-panel"><span class="explanation-mark">i</span><p><strong>Interpretation limit:</strong> This rolling packet-level comparison is not a validated result. ${escapeHtml(why.replace('Reason: ', ''))}</p></div>`}`;
  el('baseline-overview').innerHTML = AXES.slice(0, 3).map((key) => {
    const val = finiteValue(comparison.baseline?.[key]);
    const latestVal = finiteValue(latest?.values?.[key]);
    const difference = val !== null && latestVal !== null ? latestVal - val : null;
    const label = { ax: 'Acceleration X', ay: 'Acceleration Y', az: 'Acceleration Z' }[key];
    return `<div class="baseline-stat"><span>${label}</span><strong>${val === null ? 'Unavailable' : `${formatNumber(val, 3)} m/s²`}</strong><small>${difference === null ? 'No current difference' : `Latest packet: ${difference >= 0 ? '+' : ''}${formatNumber(difference, 3)} m/s²`}</small></div>`;
  }).join('');
}

function recordTimeLabel(record) {
  return formatTime(record.timestamp);
}

function buildSeries(entries, fields) {
  return fields.map((field) => ({
    name: field.name,
    color: field.color,
    values: entries.map((entry) => field.get(entry)),
  }));
}

function drawChart(canvasId, entries, series, unit, options = {}) {
  const canvas = el(canvasId);
  if (!canvas) return;
  const wrapper = canvas.parentElement;
  const empty = wrapper.querySelector('.chart-empty');
  const minPoints = options.minPoints ?? 2;
  const hasEnough = entries.length >= minPoints && series.some((line) => line.values.filter((value) => value !== null).length >= minPoints);
  if (empty) empty.hidden = hasEnough;
  canvas.hidden = !hasEnough;
  if (!hasEnough) return;

  const rect = canvas.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) return;
  const ratio = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const width = rect.width;
  const height = rect.height;
  const padding = { top: 16, right: 12, bottom: 25, left: options.compact ? 12 : 41 };
  const plotWidth = Math.max(20, width - padding.left - padding.right);
  const plotHeight = Math.max(20, height - padding.top - padding.bottom);
  const allValues = series.flatMap((line) => line.values.filter((value) => value !== null));
  let low = Math.min(...allValues);
  let high = Math.max(...allValues);
  const gap = high === low ? Math.max(Math.abs(high) * 0.12, 1) : (high - low) * 0.15;
  low -= gap;
  high += gap;
  const mapX = (index) => padding.left + (index / Math.max(entries.length - 1, 1)) * plotWidth;
  const mapY = (value) => padding.top + ((high - value) / (high - low)) * plotHeight;

  ctx.clearRect(0, 0, width, height);
  ctx.font = '9px "DM Sans", sans-serif';
  ctx.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = padding.top + (plotHeight * index) / 3;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.strokeStyle = 'rgba(169,195,235,0.1)';
    ctx.stroke();
    if (!options.compact) {
      const label = (high - ((high - low) * index) / 3).toFixed(1);
      ctx.fillStyle = 'rgba(139,158,183,0.82)';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, padding.left - 8, y);
    }
  }

  entries.forEach((entry, index) => {
    if (index !== 0 && index !== entries.length - 1 && (entries.length < 4 || index % Math.ceil(entries.length / 4) !== 0)) return;
    const x = mapX(index);
    ctx.beginPath();
    ctx.moveTo(x, padding.top + plotHeight);
    ctx.lineTo(x, padding.top + plotHeight + 3);
    ctx.strokeStyle = 'rgba(169,195,235,0.25)';
    ctx.stroke();
  });

  const points = [];
  series.forEach((line) => {
    ctx.beginPath();
    let started = false;
    line.values.forEach((value, index) => {
      if (value === null) {
        started = false;
        return;
      }
      const x = mapX(index);
      const y = mapY(value);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
      points.push({ x, y, value, name: line.name, color: line.color, timestamp: entries[index].timestamp });
    });
    ctx.lineWidth = 1.8;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = line.color;
    ctx.stroke();

    const valid = line.values.map((value, index) => ({ value, index })).filter((item) => item.value !== null);
    if (valid.length === 1) {
      const point = valid[0];
      ctx.beginPath();
      ctx.arc(mapX(point.index), mapY(point.value), 3, 0, Math.PI * 2);
      ctx.fillStyle = line.color;
      ctx.fill();
    }
  });
  canvas.__chartPoints = points;
  canvas.__chartEntries = entries;
  canvas.__chartUnit = unit;
  canvas.__chartPadding = padding;
  canvas.__chartWidth = width;
  canvas.title = `Chart with ${entries.length} recorded packets. Hover to inspect values.`;

  if (entries.length > 1 && !options.compact) {
    ctx.fillStyle = 'rgba(139,158,183,0.8)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText(recordTimeLabel(entries[0]), padding.left, height - padding.bottom + 8);
    ctx.textAlign = 'right';
    ctx.fillText(recordTimeLabel(entries[entries.length - 1]), width - padding.right, height - padding.bottom + 8);
  }

  if (canvas.__chartTooltipListener) return;
  canvas.__chartTooltipListener = true;
  canvas.addEventListener('pointermove', (event) => {
    const pointsAtHover = canvas.__chartPoints || [];
    if (!pointsAtHover.length) return;
    const bounds = canvas.getBoundingClientRect();
    const cursorX = event.clientX - bounds.left;
    const nearest = pointsAtHover.reduce((best, point) => Math.abs(point.x - cursorX) < Math.abs(best.x - cursorX) ? point : best);
    canvas.title = `${nearest.name}: ${nearest.value.toFixed(3)} ${canvas.__chartUnit} · ${formatTimestamp(nearest.timestamp)}`;
  });
  canvas.addEventListener('pointerleave', () => {
    canvas.title = `Chart with ${(canvas.__chartEntries || []).length} recorded packets. Hover to inspect values.`;
  });
}

function renderCharts() {
  const history = state.history;
  const withSteps = history.filter((entry) => finiteValue(entry.step_count) !== null);
  const withPace = history.filter((entry) => finiteValue(entry.cadence) !== null);
  el('steps-count-label').textContent = `${withSteps.length} observation${withSteps.length === 1 ? '' : 's'}`;
  el('pace-count-label').textContent = `${withPace.length} observation${withPace.length === 1 ? '' : 's'}`;
  el('steps-observation').textContent = withSteps.length >= 2 ? `${withSteps.length} recorded step-count values shown.` : 'At least two recorded values are needed for a trend.';
  el('pace-observation').textContent = withPace.length >= 2 ? `${withPace.length} recorded pace values shown.` : 'At least two reported pace values are needed for a trend.';
  el('trend-summary').textContent = withSteps.length >= 2 ? `${withSteps.length} step-count observations are available.` : 'A trend will appear as your device records sessions.';
  el('trend-detail').textContent = withSteps.length >= 2 ? 'Observed packet values; these are not daily totals.' : 'No historical chart is shown without multiple recorded values.';

  drawChart('homeTrendChart', withSteps, buildSeries(withSteps, [
    { name: 'Steps', color: '#70e1ce', get: (entry) => finiteValue(entry.step_count) },
  ]), 'steps', { compact: true, minPoints: 2 });
  drawChart('stepsTrendChart', withSteps, buildSeries(withSteps, [
    { name: 'Steps', color: '#70e1ce', get: (entry) => finiteValue(entry.step_count) },
  ]), 'steps');
  drawChart('paceTrendChart', withPace, buildSeries(withPace, [
    { name: 'Walking pace', color: '#ad9cff', get: (entry) => finiteValue(entry.cadence) },
  ]), 'steps/min');

  const imuEntries = history.filter((entry) => magnitude(entry.values, ['ax', 'ay', 'az']) !== null);
  drawChart('accelChart', imuEntries, buildSeries(imuEntries, [
    { name: 'X', color: '#79baff', get: (entry) => finiteValue(entry.values?.ax) },
    { name: 'Y', color: '#70e1ce', get: (entry) => finiteValue(entry.values?.ay) },
    { name: 'Z', color: '#e8bc79', get: (entry) => finiteValue(entry.values?.az) },
  ]), 'm/s²', { minPoints: 1 });
  drawChart('gyroChart', history.filter((entry) => magnitude(entry.values, ['gx', 'gy', 'gz']) !== null), buildSeries(history.filter((entry) => magnitude(entry.values, ['gx', 'gy', 'gz']) !== null), [
    { name: 'X', color: '#ad9cff', get: (entry) => finiteValue(entry.values?.gx) },
    { name: 'Y', color: '#79baff', get: (entry) => finiteValue(entry.values?.gy) },
    { name: 'Z', color: '#70e1ce', get: (entry) => finiteValue(entry.values?.gz) },
  ]), 'deg/s', { minPoints: 1 });
  drawChart('magnitudeChart', imuEntries, buildSeries(imuEntries, [
    { name: 'Magnitude', color: '#70e1ce', get: (entry) => magnitude(entry.values, ['ax', 'ay', 'az']) },
  ]), 'm/s²', { minPoints: 1 });

  el('home-trend-empty').hidden = withSteps.length >= 2;
  el('steps-empty').hidden = withSteps.length >= 2;
  el('pace-empty').hidden = withPace.length >= 2;
  el('accel-empty').hidden = imuEntries.length > 0;
  const gyroEntries = history.filter((entry) => magnitude(entry.values, ['gx', 'gy', 'gz']) !== null);
  el('gyro-empty').hidden = gyroEntries.length > 0;
  el('magnitude-empty').hidden = imuEntries.length > 0;
}

function renderHeart(latest) {
  const value = finiteValue(latest?.heart_rate);
  const valid = value !== null && value > 0;
  const freshness = finiteValue(latest?.sensor_freshness_ms);
  const lastSeen = latest?.timestamp ? formatTimestamp(latest.timestamp) : 'No recent reading';
  el('heart-current').textContent = valid ? `${Math.round(value)} bpm` : '—';
  el('heart-rate').textContent = valid ? String(Math.round(value)) : '—';
  el('heart-caption').textContent = valid ? 'Estimate received from sensor' : 'Optional sensor not reporting';
  el('heart-page-value').textContent = valid ? `${Math.round(value)} bpm` : 'Measurement unavailable';
  el('heart-page-copy').textContent = valid
    ? `A heart-rate estimate was included in a packet from ${latest.sensor || 'a sensor'}. This is a reported measurement, not a clinical assessment.`
    : 'No valid heart-rate estimate has been received from a connected sensor.';
  el('heart-quality-badge').textContent = valid ? (isFresh(latest) ? 'RECENT SENSOR ESTIMATE' : 'LAST ESTIMATE IS STALE') : 'WAITING FOR DATA';
  el('heart-last-seen').textContent = valid ? `Last received ${lastSeen}` : 'No recent reading';
  el('hr-quality').textContent = valid ? (isFresh(latest) ? 'Recent reading' : 'Stale reading') : 'Unavailable';
  el('hr-freshness').textContent = freshness === null ? 'Packet freshness was not provided.' : `Device-reported freshness: ${formatNumber(freshness, 1)} ms`;
  el('heart-recent-note').textContent = valid ? `Source: ${latest.source || 'unavailable'} · Received ${lastSeen}` : 'No valid reading received.';
  el('heart-device-state').textContent = valid ? 'Estimate reported' : 'Not reporting';
  el('heart-device-note').textContent = valid ? 'A value was included in the latest packet; the sensor identity is not separately confirmed.' : 'The backend has not received a valid heart-rate estimate.';
  el('home-heart-state').textContent = valid ? (isFresh(latest) ? 'Estimate received recently' : 'Last estimate is stale') : 'No heart-rate estimate';
  setDot(el('home-heart-dot'), valid && isFresh(latest) ? 'is-connected' : 'state-muted');
}

function rawValue(label, value, unit, digits = 3) {
  const result = value === null || value === undefined ? 'Unavailable' : typeof value === 'number' ? formatNumber(value, digits) : escapeHtml(value);
  return `<div class="raw-value"><span class="raw-value-label">${escapeHtml(label)}</span><strong>${result}</strong><small>${escapeHtml(unit)}</small></div>`;
}

function diagnosticRow(label, value) {
  return `<div class="diagnostic-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function renderResearch(latest) {
  const values = latest?.values || {};
  const units = latest?.units || {};
  const accMagnitude = magnitude(values, ['ax', 'ay', 'az']);
  const gyroMagnitude = magnitude(values, ['gx', 'gy', 'gz']);
  el('research-live-state').textContent = latest?.source === 'simulated'
    ? 'SIMULATION · NOT HARDWARE'
    : latest
      ? (isFresh(latest) ? 'RECEIVING PACKETS' : 'STALE PACKET')
      : 'AWAITING SENSOR DATA';
  el('research-live-state').parentElement.classList.toggle('is-live', Boolean(latest && isFresh(latest)));

  const fields = [
    ['Acceleration X', 'ax', units.ax || 'm/s²'],
    ['Acceleration Y', 'ay', units.ay || 'm/s²'],
    ['Acceleration Z', 'az', units.az || 'm/s²'],
    ['Angular velocity X', 'gx', units.gx || 'deg/s'],
    ['Angular velocity Y', 'gy', units.gy || 'deg/s'],
    ['Angular velocity Z', 'gz', units.gz || 'deg/s'],
  ];
  el('research-values').innerHTML = latest
    ? fields.map(([label, key, unit]) => rawValue(label, finiteValue(values[key]), unit)).join('') +
      rawValue('Acceleration magnitude', accMagnitude, 'm/s²') +
      rawValue('Angular velocity magnitude', gyroMagnitude, 'deg/s')
    : '<div class="empty-state">Raw sensor values will appear when an IMU packet is received.</div>';

  const age = currentPacketAge(latest);
  const sequence = latest?.sequence_number;
  const frequencyText = 'Unavailable — packet arrival times are not a sampling-frequency measurement.';
  el('packet-diagnostics').innerHTML = latest
    ? diagnosticRow('Sensor identifier', latest.sensor || 'Unknown') +
      diagnosticRow('Data source', latest.source || 'Unavailable') +
      diagnosticRow('Sequence number', sequence === null || sequence === undefined ? 'Not provided' : String(sequence)) +
      diagnosticRow('Packet timestamp', formatTimestamp(latest.timestamp)) +
      diagnosticRow('Packet age (browser)', age === null ? 'Unavailable' : `${Math.round(age)} ms`) +
      diagnosticRow('Device freshness field', latest.sensor_freshness_ms == null ? 'Not provided' : `${formatNumber(latest.sensor_freshness_ms, 1)} ms`) +
      diagnosticRow('Sampling frequency', frequencyText)
    : '<div class="empty-state">No packet diagnostics yet.</div>';

  const stepValue = latest?.step_count;
  const activity = latest?.activity ? readableActivity(latest.activity) : 'Not classified';
  const eventCount = state.history.filter((entry) => finiteValue(entry.step_count) !== null).length;
  el('model-output').innerHTML = latest
    ? diagnosticRow('Step count from packet', stepValue == null ? 'Not provided' : String(stepValue)) +
      diagnosticRow('Packets with step count', `${eventCount} of ${state.history.length} available`) +
      diagnosticRow('Activity label from packet', activity) +
      diagnosticRow('Derived acceleration magnitude', accMagnitude === null ? 'Unavailable' : `${formatNumber(accMagnitude, 3)} m/s²`) +
      diagnosticRow('Model confidence', 'Not provided') +
      diagnosticRow('Step-detection events', 'Not provided as individual events')
    : '<div class="empty-state">Step and activity outputs will appear only when the backend receives them.</div>';

  const baseline = rollingBaseline();
  el('research-baseline').innerHTML = AXES.map((key) => {
    const unit = key.startsWith('a') ? 'm/s²' : 'deg/s';
    const label = key.startsWith('a') ? `Acceleration ${key.slice(1).toUpperCase()}` : `Angular velocity ${key.slice(1).toUpperCase()}`;
    const baselineValue = finiteValue(baseline?.[key]);
    const latestValue = finiteValue(values[key]);
    const difference = baselineValue !== null && latestValue !== null ? latestValue - baselineValue : null;
    return `<div class="baseline-axis" title="Rolling per-axis arithmetic mean across up to 50 recent sensor packets."><span>${escapeHtml(label)} · mean</span><strong>${baselineValue === null ? 'Unavailable' : formatNumber(baselineValue, 3)}</strong><small>${escapeHtml(unit)} · latest − mean: ${difference === null ? 'n/a' : `${difference >= 0 ? '+' : ''}${formatNumber(difference, 3)}`}</small></div>`;
  }).join('');
}

function statusClass(status) {
  if (status === 'Connected' || status === 'Reporting' || status === 'Online') return 'status-connected';
  if (status === 'Stale') return 'status-stale';
  return '';
}

function deviceCard(icon, title, status, lastSeen, freshness, quality, message, tooltip) {
  return `<article class="device-card">
    <div class="device-card-icon" title="${escapeHtml(tooltip)}">${icon}</div>
    <div class="device-card-main">
      <div class="device-card-title"><h3>${escapeHtml(title)}</h3><span class="device-status ${statusClass(status)}"><i class="state-dot ${status === 'Connected' || status === 'Reporting' || status === 'Online' ? 'is-connected' : status === 'Stale' ? 'is-stale' : 'state-muted'}"></i>${escapeHtml(status)}</span></div>
      <p class="device-card-message">${escapeHtml(message)}</p>
      <div class="device-card-details"><div><span>Last seen</span><strong>${escapeHtml(lastSeen)}</strong></div><div><span>Data freshness</span><strong>${escapeHtml(freshness)}</strong></div><div><span>Measurement reliability</span><strong>${escapeHtml(quality)}</strong></div><div><span>Evidence</span><strong>${escapeHtml(tooltip)}</strong></div></div>
    </div>
  </article>`;
}

function renderQuality(latest) {
  const fresh = isFresh(latest);
  const age = currentPacketAge(latest);
  const lastSeen = latest ? formatTimestamp(latest.timestamp) : 'No packet received';
  const browserFreshness = age === null ? 'Unavailable' : fresh ? `${Math.round(age / 1000)} sec · fresh` : `${Math.round(age / 1000)} sec · stale`;
  const deviceFreshness = latest?.sensor_freshness_ms == null ? 'Not provided' : `${formatNumber(latest.sensor_freshness_ms, 1)} ms`;
  const source = latest?.source || 'unavailable';
  const sensorId = String(latest?.sensor || '').toLowerCase();
  const mpuEvidence = ['real', 'uncalibrated'].includes(latest?.source) &&
    sensorId.includes('mpu6050') &&
    Boolean(latest?.values && AXES.every((key) => finiteValue(latest.values[key]) !== null));
  const heartValue = finiteValue(latest?.heart_rate);
  const heartEvidence = heartValue !== null && heartValue > 0;
  const movementStatus = !mpuEvidence ? 'Waiting' : fresh ? 'Reporting' : 'Stale';
  const movementMessage = !latest
    ? 'No movement-sensor packet has been received.'
    : !mpuEvidence
      ? `Latest packet identifies “${latest.sensor || 'unknown'}”; it does not confirm a complete MPU6050 reading.`
      : fresh
        ? 'Complete X/Y/Z acceleration and angular-velocity values were received recently.'
        : 'The last complete movement reading is older than 30 seconds. Check the device before interpreting it.';
  const gatewayStatus = 'Unknown';
  const gatewayMessage = latest
    ? 'A sensor packet reached the API, but the current payload does not separately identify or handshake with an ESP32 gateway.'
    : 'No sensor packet has reached the API. Gateway identity and connection cannot be confirmed.';

  el('device-grid').innerHTML = [
    deviceCard('<svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m14 0h3M2 15h3m14 0h3M9 9h6v6H9z"/></svg>', 'ESP32 gateway', gatewayStatus, latest ? lastSeen : 'No packet', 'Gateway freshness unavailable', 'Unavailable', gatewayMessage, 'The API does not expose gateway identity or a device handshake.'),
    deviceCard('<svg viewBox="0 0 24 24"><path d="M3 12h4l2.2-5 4.1 10 2.4-5H21"/><circle cx="12" cy="12" r="9"/></svg>', 'MPU6050 movement sensor', movementStatus, mpuEvidence ? lastSeen : 'No confirmed MPU6050 packet', mpuEvidence ? `${browserFreshness} · device ${deviceFreshness}` : 'Unavailable', mpuEvidence ? (source === 'real' ? 'Complete axes received' : `Source: ${source}`) : 'Unavailable', movementMessage, 'Confirmed only by complete axis values in a packet identified as mpu6050.'),
    deviceCard('<svg viewBox="0 0 24 24"><path d="M20.8 8.6c0 5-8.8 11-8.8 11s-8.8-6-8.8-11A4.6 4.6 0 0 1 12 6.4a4.6 4.6 0 0 1 8.8 2.2Z"/></svg>', 'MAX30102 heart-rate sensor', heartEvidence ? (fresh ? 'Reporting' : 'Stale') : 'Not reporting', heartEvidence ? lastSeen : 'No estimate received', heartEvidence ? browserFreshness : 'Unavailable', heartEvidence ? 'Estimate included in packet' : 'Unavailable', heartEvidence ? 'A positive finite heart-rate estimate was included. Sensor identity is not separately confirmed.' : 'No valid heart-rate estimate has been received. Physical sensor connection cannot be confirmed.', 'A reading is treated as present only when a positive finite heart-rate value is included.'),
    deviceCard('<svg viewBox="0 0 24 24"><path d="M5 4h14v16H5zM8 8h8m-8 4h8m-8 4h5"/></svg>', 'FSR pressure sensors', 'Unknown', 'Not reported', 'Unavailable', 'Unavailable', 'Force-sensor values are not part of the current API payload; status cannot be determined.', 'FSR support is not present in the current sensor payload schema.'),
    deviceCard('<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="1"/><circle cx="16" cy="12" r="1"/><circle cx="10" cy="18" r="1"/></svg>', 'NeuroWatch backend', state.backendConnected ? 'Online' : 'Unavailable', state.backendConnected ? 'Health endpoint responded' : 'Health endpoint not responding', state.backendConnected ? 'API request succeeded' : 'Unavailable', state.backendConnected ? 'API reachable' : 'Unavailable', state.backendConnected ? 'The FastAPI health endpoint responded. This indicates backend connectivity only—not sensor connectivity.' : 'Unable to reach the FastAPI health endpoint.', 'Backend status is determined by GET /api/health; it does not imply any device is connected.'),
  ].join('');

  el('quality-summary').textContent = mpuEvidence ? (fresh ? 'Recent sensor readings' : 'Readings are stale') : 'Measurement unavailable';
  el('quality-detail').textContent = mpuEvidence ? (fresh ? `Last packet ${Math.round(age / 1000)} seconds ago.` : 'No fresh movement packet in the last 30 seconds.') : 'A complete movement-sensor packet is needed.';
  el('home-sensor-state').textContent = mpuEvidence ? (fresh ? 'Reporting recently' : 'Last reading is stale') : 'Waiting for MPU6050 data';
  setDot(el('home-sensor-dot'), mpuEvidence ? (fresh ? 'is-connected' : 'is-stale') : 'state-muted');
  el('home-backend-state').textContent = state.backendConnected ? 'API reachable' : 'Unavailable';
  setDot(el('home-backend-dot'), state.backendConnected ? 'is-connected' : 'is-error');
  el('quality-source-badge').textContent = latest ? String(source).toUpperCase() : 'NO DATA';

  const rows = [
    ['Latest packet', lastSeen, latest ? 'Timestamp reported by sensor payload.' : 'No packet available.'],
    ['Packet age', browserFreshness, 'Calculated in the browser from the packet timestamp.'],
    ['Device freshness field', deviceFreshness, 'Optional sensor_freshness_ms field from payload.'],
    ['Source', source, 'Payload source label; simulation remains disabled by default.'],
    ['Axis completeness', mpuEvidence ? '6 of 6 IMU axes' : latest ? 'Not confirmed' : 'No data', 'Complete axis set is required to confirm an MPU6050 packet.'],
    ['Session packets', String(state.history.length), 'Current dashboard history window (up to 50 records).'],
  ];
  el('quality-diagnostics').innerHTML = rows.map(([label, value, detail]) => `<div class="quality-diagnostic"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><p>${escapeHtml(detail)}</p></div>`).join('');
}

function renderState(payload) {
  state.history = completeHistory(payload.history);
  state.baseline = payload.baseline && typeof payload.baseline === 'object' ? payload.baseline : null;
  state.alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
  const latest = getLatest();

  const fresh = isFresh(latest);
  const sensorConnected = Boolean(latest && fresh && ['real', 'uncalibrated'].includes(latest.source));
  const sensorLabel = latest?.source === 'simulated'
    ? 'Simulation data · not hardware'
    : sensorConnected
      ? (latest.source === 'uncalibrated' ? 'Uncalibrated sensor reporting' : 'Movement sensor reporting')
      : latest && !fresh
        ? 'Stale sensor readings'
        : 'Waiting for device';
  el('sidebar-device').textContent = sensorLabel;
  el('sidebar-source').textContent = latest ? `Last packet · ${formatTime(latest.timestamp)}` : 'No live sensor data';
  setDot(el('sidebar-dot'), sensorConnected ? (fresh ? 'is-connected' : 'is-stale') : latest && !fresh ? 'is-stale' : 'state-muted');

  if (state.socketConnected) setConnectionStatus('Live updates connected', 'is-connected');
  else if (state.backendConnected) setConnectionStatus('Backend connected', 'is-stale');
  else setConnectionStatus('Backend unavailable', 'is-error');

  updateHero(latest);
  updateMetrics(latest);
  renderTimeline(state.history);
  renderSessions(state.history);
  renderInsights();
  renderHeart(latest);
  renderResearch(latest);
  renderQuality(latest);
  renderCharts();
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}${detail ? `: ${detail}` : ''}`);
  }
  return response.json();
}

async function checkBackend() {
  try {
    await requestJson('/api/health');
    state.backendConnected = true;
  } catch (error) {
    state.backendConnected = false;
    console.error('Backend health check failed', error);
  }
  if (state.history.length) renderState({
    history: state.history,
    baseline: state.baseline,
    alerts: state.alerts,
  });
  else setConnectionStatus(state.backendConnected ? (state.socketConnected ? 'Live updates connected' : 'Backend connected') : 'Backend unavailable', state.backendConnected ? 'is-connected' : 'is-error');
}

async function fetchState() {
  try {
    const payload = await requestJson('/api/dashboard-state');
    state.backendConnected = true;
    renderState(payload);
  } catch (error) {
    console.error('Failed to fetch dashboard state', error);
    state.backendConnected = false;
    setConnectionStatus('Backend unavailable', 'is-error');
    showToast('Unable to load dashboard data. Check that the local backend is running.', 'error');
  }
}

function connectSocket() {
  if (state.reconnectTimer) {
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  const socket = new WebSocket(socketUrl);
  socket.addEventListener('open', () => {
    state.socketConnected = true;
    setConnectionStatus('Live updates connected', 'is-connected');
  });
  socket.addEventListener('message', (event) => {
    try {
      renderState(JSON.parse(event.data));
    } catch (error) {
      console.error('Invalid dashboard message', error);
      showToast('Received an unreadable dashboard update.', 'error');
    }
  });
  socket.addEventListener('close', () => {
    state.socketConnected = false;
    setConnectionStatus(state.backendConnected ? 'Reconnecting live updates' : 'Backend unavailable', state.backendConnected ? 'is-stale' : 'is-error');
    state.reconnectTimer = window.setTimeout(connectSocket, 1800);
  });
  socket.addEventListener('error', (error) => {
    state.socketConnected = false;
    console.error('Dashboard WebSocket error', error);
    setConnectionStatus('Live updates unavailable', 'is-error');
  });
}

async function startSession() {
  const button = el('start-session');
  button.disabled = true;
  try {
    const payload = await requestJson('/api/session/start', { method: 'POST' });
    state.sessionStartedAt = safeDate(payload.timestamp) || new Date();
    if (!state.sessionTimer) state.sessionTimer = window.setInterval(updateElapsed, 1000);
    updateHero(getLatest());
    showToast('Session started. Waiting for sensor readings.');
  } catch (error) {
    button.disabled = false;
    console.error('Unable to start session', error);
    showToast('Could not start the session. Please check the backend connection.', 'error');
  }
}

async function endSession() {
  const button = el('end-session');
  button.disabled = true;
  try {
    await requestJson('/api/session/end', { method: 'POST' });
    state.sessionStartedAt = null;
    if (state.sessionTimer) {
      window.clearInterval(state.sessionTimer);
      state.sessionTimer = null;
    }
    updateHero(getLatest());
    showToast('Session ended. Recorded data remains available to export.');
  } catch (error) {
    button.disabled = false;
    console.error('Unable to end session', error);
    showToast('Could not end the session. Please check the backend connection.', 'error');
  }
}

async function exportSession() {
  try {
    const response = await fetch('/api/session/export');
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`${response.status} ${response.statusText}${detail ? `: ${detail}` : ''}`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const disposition = response.headers.get('content-disposition');
    const match = disposition && disposition.match(/filename="?([^";]+)"?/i);
    link.href = url;
    link.download = match ? match[1] : 'neurowatch_session.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('CSV export downloaded.');
  } catch (error) {
    console.error('Unable to export session', error);
    showToast('CSV export failed. Please check the backend connection.', 'error');
  }
}

function resizeCharts() {
  if (!state.history.length) return;
  requestAnimationFrame(renderCharts);
}

window.addEventListener('DOMContentLoaded', async () => {
  bindNavigation();
  el('start-session').addEventListener('click', startSession);
  el('end-session').addEventListener('click', endSession);
  ['export-button', 'sessions-export', 'research-export'].forEach((id) => el(id).addEventListener('click', exportSession));
  window.addEventListener('resize', resizeCharts, { passive: true });

  await Promise.all([fetchState(), checkBackend()]);
  connectSocket();
  window.setInterval(checkBackend, 20000);
});
