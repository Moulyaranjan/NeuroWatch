(() => {
  const $ = (id) => document.getElementById(id);
  const axes = ['ax', 'ay', 'az', 'gx', 'gy', 'gz'];
  const axisColors = {
    ax: '#2878d0',
    ay: '#16a6a1',
    az: '#8b65cc',
    gx: '#e76f51',
    gy: '#239b70',
    gz: '#d99420',
  };
  const savedProfileId = window.localStorage.getItem('neurowatch.selectedProfile') || '';
  const queryProfileId = new URLSearchParams(window.location.search).get('profile_id') || '';
  let selectedProfile = queryProfileId || savedProfileId;
  let latestState = null;
  let requestInFlight = false;
  let endingSession = false;
  let historyProfile = null;
  let selectedSessionId = '';
  let historyInFlight = false;
  let reportRequestInFlight = false;
  let latestReport = null;
  const requestedReportId = /^\/session-report\/(monitor-[a-f0-9]{32})$/.exec(window.location.pathname)?.[1] || null;

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character]);

  const numeric = (value) => {
    const result = Number(value);
    return value !== null && value !== undefined && Number.isFinite(result) ? result : null;
  };

  const format = (value, digits = 2) => {
    const result = numeric(value);
    return result === null ? '—' : result.toFixed(digits);
  };

  function setTextIfChanged(id, value) {
    const element = $(id);
    if (element.textContent !== value) element.textContent = value;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { cache: 'no-store', ...options });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  function showMessage(message, isError = false) {
    const target = $('profile-message');
    target.hidden = !message;
    target.textContent = message || '';
    target.style.color = isError ? '#b93845' : '#526780';
  }

  function selectedStateUrl() {
    return selectedProfile
      ? `/api/dashboard-state?profile_id=${encodeURIComponent(selectedProfile)}`
      : '/api/dashboard-state';
  }

  function dashboardUrl(profileId = selectedProfile) {
    return profileId ? `/?profile_id=${encodeURIComponent(profileId)}` : '/';
  }

  async function loadProfiles() {
    const result = await request('/api/profiles');
    const select = $('profile-select');
    const previous = selectedProfile;
    select.innerHTML = '<option value="">Choose a profile</option>';
    result.profiles.forEach((profile) => {
      const option = document.createElement('option');
      option.value = profile.profile_id;
      option.textContent = `${profile.display_name} (${profile.profile_id})`;
      select.appendChild(option);
    });
    selectedProfile = result.profiles.some((profile) => profile.profile_id === previous) ? previous : '';
    if (selectedProfile) window.localStorage.setItem('neurowatch.selectedProfile', selectedProfile);
    else window.localStorage.removeItem('neurowatch.selectedProfile');
    select.value = selectedProfile;
  }

  function drawChart(id, series, options = {}) {
    const canvas = $(id);
    const ctx = canvas.getContext('2d');
    const width = Math.max(260, canvas.clientWidth || 260);
    const height = 220;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const points = series.flatMap((line) => line.points || []).filter((point) => numeric(point.value) !== null);
    const references = options.referenceLines || (options.reference ? [options.reference] : []);
    if (!points.length) {
      ctx.fillStyle = '#65758b';
      ctx.font = '12px Arial';
      ctx.textAlign = 'center';
      ctx.fillText(options.empty || 'No measurements available yet.', width / 2, height / 2);
      return;
    }

    const timestamps = points.map((point) => Date.parse(point.timestamp)).filter(Number.isFinite);
    const firstTime = timestamps.length ? Math.min(...timestamps) : 0;
    const lastTime = timestamps.length ? Math.max(...timestamps) : firstTime;
    const pad = { left: 48, right: 12, top: 14, bottom: 28 };
    const chartWidth = width - pad.left - pad.right;
    const chartHeight = height - pad.top - pad.bottom;
    const rangeValues = points.map((point) => Number(point.value));
    references.forEach((reference) => rangeValues.push(
      reference.mean - reference.std,
      reference.mean + reference.std,
      reference.mean,
    ));
    let min = Math.min(...rangeValues);
    let max = Math.max(...rangeValues);
    if (max - min < 1e-9) {
      const margin = Math.max(Math.abs(max) * 0.05, 0.5);
      min -= margin;
      max += margin;
    } else {
      const margin = (max - min) * 0.12;
      min -= margin;
      max += margin;
    }
    const y = (value) => pad.top + (max - value) * chartHeight / (max - min);
    const x = (point, index, linePoints) => {
      const stamp = Date.parse(point.timestamp);
      if (Number.isFinite(stamp) && lastTime > firstTime) return pad.left + (stamp - firstTime) * chartWidth / (lastTime - firstTime);
      return pad.left + (linePoints.length < 2 ? chartWidth : chartWidth * index / (linePoints.length - 1));
    };

    ctx.font = '10px Arial';
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = max - (max - min) * tick / 4;
      const yPos = pad.top + chartHeight * tick / 4;
      ctx.strokeStyle = '#e5ebf2';
      ctx.beginPath();
      ctx.moveTo(pad.left, yPos);
      ctx.lineTo(width - pad.right, yPos);
      ctx.stroke();
      ctx.fillStyle = '#65758b';
      ctx.textAlign = 'right';
      ctx.fillText(value.toFixed(2), pad.left - 6, yPos + 3);
    }

    references.forEach((reference) => {
      const center = y(reference.mean);
      const top = y(reference.mean + reference.std);
      const bottom = y(reference.mean - reference.std);
      ctx.fillStyle = `${reference.color}1A`;
      ctx.fillRect(pad.left, top, chartWidth, Math.max(1, bottom - top));
      ctx.strokeStyle = reference.color;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.left, center);
      ctx.lineTo(width - pad.right, center);
      ctx.stroke();
      ctx.setLineDash([]);
    });

    series.forEach((line) => {
      const valid = (line.points || [])
        .filter((point) => numeric(point.value) !== null)
        .slice()
        .sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
      if (!valid.length) return;
      ctx.beginPath();
      ctx.strokeStyle = line.color;
      ctx.lineWidth = 2.4;
      valid.forEach((point, index) => {
        const xPos = x(point, index, valid);
        const yPos = y(Number(point.value));
        if (options.connect === false) {
          ctx.fillStyle = line.color;
          ctx.beginPath();
          ctx.arc(xPos, yPos, 4, 0, Math.PI * 2);
          ctx.fill();
        } else {
          if (index === 0) ctx.moveTo(xPos, yPos);
          else ctx.lineTo(xPos, yPos);
        }
      });
      if (options.connect !== false) {
        ctx.stroke();
        if (valid.length <= 80) {
          valid.forEach((point, index) => {
            ctx.beginPath();
            ctx.arc(x(point, index, valid), y(Number(point.value)), 2, 0, Math.PI * 2);
            ctx.fillStyle = line.color;
            ctx.fill();
          });
        }
      }
    });

    ctx.fillStyle = '#65758b';
    ctx.textAlign = 'left';
    const formatDateAxis = (timestamp) => options.dateAxis
      ? new Date(timestamp).toLocaleDateString()
      : new Date(timestamp).toLocaleTimeString();
    ctx.fillText(timestamps.length ? formatDateAxis(firstTime) : 'earlier sessions', pad.left, height - 7);
    ctx.textAlign = 'right';
    ctx.fillText(timestamps.length ? formatDateAxis(lastTime) : 'latest sessions', width - pad.right, height - 7);
  }

  function drawRawCharts(history) {
    const seriesFor = (keys) => keys.map((key, index) => ({
      name: key,
      color: axisColors[key],
      points: history.map((packet) => ({
        timestamp: packet.received_at || packet.timestamp,
        value: packet.values?.[key],
      })),
    }));
    drawChart('ac', seriesFor(['ax', 'ay', 'az']), { empty: 'Waiting for real acceleration packets.' });
    drawChart('gyro-chart', seriesFor(['gx', 'gy', 'gz']), { empty: 'Waiting for real gyroscope packets.' });
  }

  function drawMovementFeatureTrend(canvasId, trends, feature, baseline, empty) {
    const points = (trends || [])
      .map((entry) => ({
        timestamp: entry.ended_at,
        value: entry.movement_feature_summary?.values?.[feature],
      }))
      .filter((point) => numeric(point.value) !== null);
    const reference = baseline?.feature_statistics?.[feature];
    drawChart(canvasId, [{
      color: '#2878d0',
      points,
    }], {
      reference: reference
        ? { mean: reference.mean, std: reference.standard_deviation }
        : null,
      dateAxis: true,
      empty,
    });
  }

  function renderBaseline(baseline) {
    const target = $('baseline-statistics');
    if (!baseline) {
      target.innerHTML = '<div class="muted">No saved baseline for this profile.</div>';
      $('profile-baseline-state').textContent = selectedProfile ? 'Uncalibrated' : 'No profile selected';
      $('profile-summary').textContent = selectedProfile ? 'Calibration is required before monitoring.' : 'Choose a profile to inspect its baseline and history.';
      return;
    }
    const quality = baseline.quality || {};
    $('profile-baseline-state').textContent = quality.reliable ? 'Reliable baseline saved' : 'Baseline quality needs review';
    $('profile-summary').textContent = `Profile ${baseline.profile_id} · last calibrated ${new Date(baseline.completed_at).toLocaleString()} · ${baseline.sample_count} samples · ${quality.status || 'quality unavailable'} · placement ${baseline.sensor_placement || 'unknown'} · ${format(baseline.sampling_information?.sample_rate_hz, 1)} Hz`;
    target.innerHTML = Object.entries(baseline.axes || {}).map(([axis, statistic]) =>
      `<div><span>${escapeHtml(axis.toUpperCase())} · ${axis.startsWith('a') ? 'm/s²' : 'deg/s'}</span><strong>Mean ${format(statistic.mean, 3)}</strong><small>SD ${format(statistic.standard_deviation, 3)} · range ${format(statistic.minimum, 3)}–${format(statistic.maximum, 3)}</small></div>`
    ).join('');
  }

  function renderSessionReport(report) {
    if (!report) return;
    latestReport = report;
    selectedSessionId = report.session.session_id;
    const deviation = report.deviation_summary || {};
    const unitAcceleration = report.units_verified ? 'm/s²' : 'unit unverified';
    const unitGyroscope = report.units_verified ? 'deg/s' : 'unit unverified';
    $('session-accel-unit').textContent = unitAcceleration;
    $('session-gyro-unit').textContent = unitGyroscope;
    $('session-accel-magnitude-unit').textContent = unitAcceleration;
    $('session-gyro-magnitude-unit').textContent = unitGyroscope;
    $('completed-session-state').textContent = deviation.status === 'insufficient_data'
      ? 'Insufficient data to assess'
      : 'Experimental comparison';
    $('report-page-title').textContent = `Session analysis · ${report.profile.display_name}`;
    $('post-session-heading').textContent = `Session ${report.session.session_id}`;
    $('completed-session-meta').textContent =
      `${report.session.duration_seconds}s · ${report.packet_counts.accepted} accepted · ${report.packet_counts.rejected} rejected`;
    $('download-session-report').disabled = false;
    $('session-overview').innerHTML = [
      `<strong>${escapeHtml(report.profile.display_name)} (${escapeHtml(report.profile.profile_id)})</strong>`,
      `<div>Session ${escapeHtml(report.session.session_id)} · ${escapeHtml(new Date(report.session.started_at).toLocaleString())} – ${escapeHtml(new Date(report.session.ended_at).toLocaleString())}</div>`,
      `<div>Duration ${format(report.session.duration_seconds, 1)} seconds · baseline ${escapeHtml(report.baseline.calibration_id)} · calibrated ${escapeHtml(new Date(report.baseline.calibrated_at).toLocaleString())}</div>`,
      `<div>${report.packet_counts.received} received in monitoring context · ${report.packet_counts.accepted} accepted · ${report.packet_counts.rejected} rejected · effective ${format(report.effective_sample_rate_hz, 2)} Hz · quality ${escapeHtml(report.quality.status || 'unavailable')}</div>`,
    ].join('');
    $('session-deviation-summary').innerHTML = [
      `<div>${escapeHtml(deviation.explanation || 'No accepted data available for comparison.')}</div>`,
      ...(report.insights || []).map((entry) =>
        `<div>Most changed: ${escapeHtml(entry.feature)} ${entry.kind === 'variability' ? `variability changed by ${format(entry.variability_difference, 3)} ${escapeHtml(entry.unit)} (${format(entry.variability_percentage_difference, 1)}%)` : `mean changed by ${format(entry.absolute_difference, 3)} ${escapeHtml(entry.unit)} (${format(entry.standardized_deviation, 2)} baseline SD)`}.</div>`
      ),
      ...(report.variability_insights || []).map((entry) =>
        `<div>Variability change: ${escapeHtml(entry.feature)} changed by ${format(entry.variability_difference, 3)} ${escapeHtml(entry.unit)} (${format(entry.variability_percentage_difference, 1)}%).</div>`
      ),
      ...(report.fallback_insights || []).map((entry) =>
        `<div>Mean change for ${escapeHtml(entry.feature)} was ${format(entry.absolute_difference, 3)} ${escapeHtml(entry.unit)}${entry.percentage_difference == null ? '' : ` (${format(entry.percentage_difference, 1)}% of baseline mean)`}; standardized change is unavailable because baseline variability is near zero.</div>`
      ),
      `<small>${escapeHtml(deviation.interpretation || 'Experimental movement comparison only; not a medical diagnosis.')}</small>`,
    ].join('');
    const model = report.model_analysis || {};
    const modelOutput = model.output?.class;
    $('session-ml-status').textContent = [
      `Model ${model.model_name || 'unavailable'}${model.model_version ? ` · ${model.model_version}` : ''}`,
      `status ${model.state || 'unavailable'}`,
      `data quality ${model.data_quality || report.movement_feature_summary?.status || 'unavailable'}`,
      modelOutput ? `movement-pattern class ${modelOutput}` : 'no class output',
      'no calibrated confidence available',
    ].join(' · ');
    $('session-ml-features').innerHTML = Object.entries(
      report.movement_feature_comparisons?.features || {}
    ).slice(0, 8).map(([name, comparison]) =>
      `<div><span>${escapeHtml(name.replaceAll('_', ' '))} · ${escapeHtml(comparison.unit)}</span>` +
      `<strong>${format(comparison.session_mean, 3)}</strong>` +
      `<small>Baseline ${format(comparison.baseline_mean, 3)} · absolute change ${format(comparison.absolute_difference, 3)} · relative change ${comparison.relative_change_percent == null ? 'unavailable' : `${format(comparison.relative_change_percent, 1)}%`}</small></div>`
    ).join('') || '<div class="muted">Comparable feature values are unavailable for this session.</div>';
    const change = report.longitudinal_change || {};
    $('session-persistent-change').textContent =
      `Experimental persistent-change flag: ${change.state || 'insufficient_data'} · ` +
      `${change.valid_session_count || 0}/${change.required_session_count || 3} valid sessions · ` +
      `${change.message || 'Thresholds are exploratory and not clinically validated.'}` +
      (change.state === 'persistent_deviation'
        ? ' Persistent concerns should be reviewed with a clinician; this is not evidence of disease progression.'
        : '');
    drawMovementFeatureTrend(
      'session-ml-trend-chart',
      report.longitudinal_history || [],
      $('ml-trend-feature').value,
      report.baseline.movement_feature_summary,
      'No valid repeated-session feature measurements are available.'
    );

    const samples = (report.samples || []).slice().sort((left, right) =>
      Date.parse(left.sensor_timestamp || left.timestamp) - Date.parse(right.sensor_timestamp || right.timestamp)
    );
    const toSeries = (axis, color) => ({
      name: `${axis.toUpperCase()} session`,
      color,
      points: samples.map((sample) => ({
        timestamp: sample.sensor_timestamp || sample.timestamp,
        value: sample.values?.[axis],
      })),
    });
    const axisReferenceLines = (keys) => keys.map((axis, index) => ({
      mean: report.baseline.axes[axis].mean,
      std: report.baseline.axes[axis].standard_deviation,
      color: axisColors[axis],
    }));
    drawChart('session-accel-chart', ['ax', 'ay', 'az'].map((axis) => toSeries(axis, axisColors[axis])), {
      referenceLines: axisReferenceLines(['ax', 'ay', 'az']),
      empty: 'This completed session has no accepted acceleration samples.',
    });
    drawChart('session-gyro-chart', ['gx', 'gy', 'gz'].map((axis) => toSeries(axis, axisColors[axis])), {
      referenceLines: axisReferenceLines(['gx', 'gy', 'gz']),
      empty: 'This completed session has no accepted gyroscope samples.',
    });

    const magnitudeSeries = (axes, label, color) => ({
      name: `${label} session`,
      color,
      points: samples.map((sample) => {
        const values = axes.map((axis) => numeric(sample.values?.[axis]));
        return {
          timestamp: sample.sensor_timestamp || sample.timestamp,
          value: values.some((value) => value === null)
            ? null
            : Math.sqrt(values.reduce((sum, value) => sum + value * value, 0)),
        };
      }),
    });
    drawChart('session-accel-magnitude-chart', [
      magnitudeSeries(['ax', 'ay', 'az'], 'Acceleration', '#2878d0'),
    ], {
      referenceLines: [{
        mean: report.baseline.features.acceleration_magnitude.mean,
        std: report.baseline.features.acceleration_magnitude.standard_deviation,
        color: '#2878d0',
      }],
      empty: 'This completed session has no accepted samples for magnitude comparison.',
    });
    drawChart('session-gyro-magnitude-chart', [
      magnitudeSeries(['gx', 'gy', 'gz'], 'Gyroscope', '#e76f51'),
    ], {
      referenceLines: [{
        mean: report.baseline.features.gyroscope_magnitude.mean,
        std: report.baseline.features.gyroscope_magnitude.standard_deviation,
        color: '#e76f51',
      }],
      empty: 'This completed session has no accepted samples for magnitude comparison.',
    });

    $('session-deviation-rows').innerHTML = axes.map((axis) => {
      const base = report.baseline.axes[axis];
      const measured = report.statistics.axes?.[axis];
      const comparison = report.comparisons.axes[axis];
      const unit = report.units_verified ? comparison.unit : 'unit unverified';
      const cells = [
        ['Axis', axis.toUpperCase()],
        ['Baseline mean ± SD', `${format(base.mean, 3)} ± ${format(base.standard_deviation, 3)} ${unit}`],
        ['Baseline min–max', `${format(base.minimum, 3)}–${format(base.maximum, 3)} ${unit}`],
        ['Session mean ± SD', measured ? `${format(measured.mean, 3)} ± ${format(measured.standard_deviation, 3)} ${unit}` : 'Unavailable'],
        ['Session min–max', measured ? `${format(measured.minimum, 3)}–${format(measured.maximum, 3)} ${unit}` : 'Unavailable'],
        ['Absolute diff', `${format(comparison.absolute_difference, 3)} ${unit}`],
        ['Difference %', comparison.percentage_difference == null ? 'Unavailable' : `${format(comparison.percentage_difference, 1)}%`],
        ['SD change', `${format(comparison.variability_difference, 3)} ${unit}`],
        ['SD change %', comparison.variability_percentage_difference == null ? 'Unavailable' : `${format(comparison.variability_percentage_difference, 1)}%`],
        ['Standardized', comparison.standardized_deviation == null ? 'Unavailable' : format(comparison.standardized_deviation, 2)],
      ];
      return cells.map(([label, value]) => `<td data-label="${escapeHtml(label)}">${escapeHtml(value)}</td>`).join('');
    }).join('');

    const magnitudeNames = [
      ['acceleration_magnitude', 'Acceleration', unitAcceleration],
      ['gyroscope_magnitude', 'Gyroscope', unitGyroscope],
    ];
    $('session-magnitude-statistics').innerHTML = magnitudeNames.map(([name, label, unit]) => {
      const comparison = report.comparisons.magnitudes[name];
      const base = comparison.baseline;
      const measured = comparison.session;
      return `<div><span>${label} · ${unit}</span><strong>Mean ${measured ? format(measured.mean, 3) : 'Unavailable'} ± ${measured ? format(measured.standard_deviation, 3) : '—'}</strong>` +
        `<small>Baseline ${format(base.mean, 3)} ± ${format(base.standard_deviation, 3)} · min ${format(base.minimum, 3)} · max ${format(base.maximum, 3)} · range ${format(base.range, 3)}<br>` +
        `Session min ${measured ? format(measured.minimum, 3) : '—'} · max ${measured ? format(measured.maximum, 3) : '—'} · range ${measured ? format(measured.range, 3) : 'Unavailable'} · mean diff ${comparison.absolute_difference == null ? '—' : format(comparison.absolute_difference, 3)} · SD change ${comparison.variability_difference == null ? '—' : format(comparison.variability_difference, 3)} · SD change % ${comparison.variability_percentage_difference == null ? 'unavailable' : `${format(comparison.variability_percentage_difference, 1)}%`} · mean diff % ${comparison.percentage_difference == null ? 'unavailable' : `${format(comparison.percentage_difference, 1)}%`} · z ${comparison.standardized_deviation == null ? 'unavailable' : format(comparison.standardized_deviation, 2)}</small></div>`;
    }).join('');
    const notes = report.limitations || [];
    $('session-limitations').innerHTML = notes.length
      ? notes.map((note) => `<article>${escapeHtml(note)}</article>`).join('')
      : '';
    $('session-events').innerHTML = report.events?.length
      ? report.events.map((event) =>
        `<article><strong>${escapeHtml(event.severity)}</strong> · ${escapeHtml(new Date(event.timestamp).toLocaleString())}<small>${escapeHtml(event.description)}</small></article>`
      ).join('')
      : '<div class="muted">No movement-change events were detected during this session.</div>';
    $('completed-session-select').value = selectedSessionId;
    $('return-to-dashboard').href = dashboardUrl(report.profile.profile_id);
  }

  function clearSessionReport(message = 'The completed-session analysis will appear here after a monitoring session ends.') {
    selectedSessionId = '';
    $('completed-session-state').textContent = 'No completed session selected';
    $('completed-session-meta').textContent = 'End a monitoring session to create a report.';
    $('download-session-report').disabled = true;
    $('session-overview').textContent = message;
    $('session-deviation-summary').textContent = 'No completed session selected.';
    $('session-deviation-rows').innerHTML = '<tr><td class="empty" colspan="10">Select a completed session to review its analysis.</td></tr>';
    $('session-magnitude-statistics').innerHTML = '';
    $('session-events').innerHTML = '';
    $('session-limitations').innerHTML = '';
    $('session-ml-status').textContent = message;
    $('session-ml-features').innerHTML = '';
    $('session-persistent-change').textContent = 'Experimental repeated-session change flag unavailable.';
    $('completed-session-select').value = '';
    drawChart('session-accel-chart', [], { empty: message });
    drawChart('session-gyro-chart', [], { empty: message });
    drawChart('session-accel-magnitude-chart', [], { empty: message });
    drawChart('session-gyro-magnitude-chart', [], { empty: message });
    drawChart('session-ml-trend-chart', [], { empty: message });
  }

  async function loadSessionHistory() {
    if (!selectedProfile) {
      $('completed-session-select').innerHTML = '<option value="">Select a completed session</option>';
      $('completed-session-select').disabled = true;
      $('session-trend-history').innerHTML = '<div class="muted">Select a profile to view completed sessions.</div>';
      clearSessionReport();
      $('session-report-message').textContent = 'No report selected.';
      historyProfile = null;
      return;
    }
    if (historyInFlight) return;
    historyInFlight = true;
    $('session-report-message').textContent = 'Loading completed-session history…';
    $('session-report-message').style.color = '#526780';
    try {
      const result = await request(`/api/session/history?profile_id=${encodeURIComponent(selectedProfile)}`);
      const sessions = result.completed_sessions || [];
      $('session-trend-history').innerHTML = sessions.length
        ? sessions.map((session) => {
          const action = session.report_available
            ? `<a href="/session-report/${encodeURIComponent(session.session_id)}?profile_id=${encodeURIComponent(session.profile_id)}">View Report</a>`
            : '<span class="muted">Report unavailable</span>';
          return `<article><div><strong>${escapeHtml(session.display_name)} (${escapeHtml(session.profile_id)})</strong><small>${escapeHtml(new Date(session.ended_at).toLocaleString())} · ${format(session.duration_seconds, 1)}s · ${session.sample_count} accepted samples · session ${escapeHtml(session.session_id)}</small></div>${action}</article>`;
        }).join('')
        : '<div class="muted">No completed sessions are available for this profile.</div>';
      const select = $('completed-session-select');
      const requested = selectedSessionId;
      select.innerHTML = '<option value="">Select a completed session</option>';
      sessions.forEach((session) => {
        const option = document.createElement('option');
        option.value = session.session_id;
        option.textContent = `${session.display_name} (${session.profile_id}) · ${new Date(session.ended_at).toLocaleString()} · ${session.duration_seconds}s · ${session.sample_count} samples · ${session.report_available ? 'report available' : 'report unavailable'}`;
        option.disabled = !session.report_available;
        select.appendChild(option);
      });
      const availableSessions = sessions.filter((session) => session.report_available);
      select.disabled = availableSessions.length === 0;
      if (!availableSessions.length) {
        $('session-report-message').textContent = 'No completed session report is available for this profile yet.';
      }
      const selected = sessions.some((session) => session.session_id === requested) ? requested : '';
      if (selected) {
        select.value = selected;
        selectedSessionId = selected;
      } else if (historyProfile !== selectedProfile) {
        select.value = '';
      }
      historyProfile = selectedProfile;
    } catch (error) {
      clearSessionReport();
      $('session-report-message').textContent = `Could not load completed-session history: ${error.message}`;
      $('session-report-message').style.color = '#b93845';
      historyProfile = selectedProfile;
    } finally {
      historyInFlight = false;
    }
  }

  async function loadSessionAnalysis(sessionId, profileId = selectedProfile) {
    if (!sessionId) return;
    const profileQuery = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : '';
    const report = await request(
      `/api/session/${encodeURIComponent(sessionId)}/analysis${profileQuery}`
    );
    renderSessionReport(report);
    $('session-report-message').textContent = 'Saved session analysis loaded.';
    $('session-report-message').style.color = '#526780';
    return report;
  }

  function renderCalibration(calibration) {
    const active = ['collecting', 'paused_stale'].includes(calibration.state);
    const belongsToSelected = calibration.profile_id === selectedProfile;
    const monitoringState = latestState?.profile_state?.monitoring?.state;
    const canStart = Boolean(selectedProfile) && !active && !['active', 'stale', 'waiting_for_sensor'].includes(monitoringState);
    $('start-calibration').disabled = !canStart;
    $('cancel-calibration').disabled = !active || !belongsToSelected;
    $('calibration-duration').disabled = active;
    $('calibration-state').textContent = active
      ? `${calibration.state === 'paused_stale' ? 'Paused · sensor data stale' : 'Calibration collecting'}${belongsToSelected ? '' : ` for profile ${calibration.profile_id}`}`
      : `Calibration ${String(calibration.state || 'idle').replaceAll('_', ' ')}`;
    const progress = belongsToSelected ? Number(calibration.progress_percent || 0) : 0;
    $('calibration-progress').style.width = `${Math.max(0, Math.min(100, progress))}%`;
    $('calibration-details').textContent = belongsToSelected
      ? `Elapsed ${Math.floor(calibration.elapsed_seconds || 0)}s · Remaining ${Math.ceil(calibration.remaining_seconds || 0)}s · ${calibration.accepted_sample_count || 0} accepted · ${calibration.rejected_packet_count || 0} rejected packets`
      : calibration.profile_id ? `Calibration belongs to ${calibration.profile_id}; it will not be assigned to the selected profile.` : 'Elapsed 0s · Remaining 0s · 0 accepted samples';
    const result = belongsToSelected ? calibration.result : null;
    const quality = result?.quality;
    $('calibration-quality').textContent = calibration.last_invalid_reason
      ? `Last packet excluded: ${calibration.last_invalid_reason}`
      : quality
        ? `Result: ${result.state} · ${quality.status} signal · ${result.accepted_sample_count} samples · ${result.duration_seconds}s requested${result.incomplete_reason ? ` · ${result.incomplete_reason}` : ''}`
        : '';
  }

  function renderMLAnalysis(profileState, modelStatus) {
    const trends = profileState.trends || [];
    const latest = trends[trends.length - 1];
    const latestModel = latest?.model_analysis || {};
    const outputClass = latestModel.output?.class;
    const baselineFeatures = profileState.baseline?.movement_feature_summary;
    const featureComparisons = latest?.movement_feature_comparisons?.features || {};
    const modelName = modelStatus?.model_name || latestModel.model_name;
    const modelVersion = modelStatus?.model_version || latestModel.model_version;
    $('ml-model-status').textContent = modelStatus
      ? `${modelStatus.state}${modelName ? ` · ${modelName}` : ''}${modelVersion ? ` · ${modelVersion}` : ''}`
      : 'Model status unavailable';
    $('ml-model-name').textContent = outputClass
      ? `Movement-pattern class: ${outputClass}`
      : 'No trained-model class output';
    $('ml-model-message').textContent = latestModel.message
      || modelStatus?.message
      || 'Classification is unavailable until a compatible model is trained and installed.';
    const quality = latestModel.data_quality || latest?.movement_feature_summary?.status || 'insufficient_data';
    $('ml-data-quality').textContent = `Latest completed-session data quality: ${quality}`;
    $('ml-baseline-status').textContent = baselineFeatures?.status === 'available'
      ? `Feature reference ${baselineFeatures.version} · ${baselineFeatures.window_count} windows`
      : 'No feature baseline available';
    const comparisons = Object.entries(featureComparisons).slice(0, 3).map(([name, value]) =>
      `${name.replaceAll('_', ' ')}: ${format(value.absolute_difference, 3)} ${value.unit}` +
      `${value.relative_change_percent == null ? '' : ` (${format(value.relative_change_percent, 1)}%)`}`
    );
    $('ml-baseline-comparison').textContent = comparisons.length
      ? comparisons.join(' · ')
      : 'Feature comparisons require a valid calibration and completed session.';
    const change = latest?.longitudinal_change || {};
    $('ml-persistent-change').textContent =
      `Experimental repeated-session flag: ${change.state || 'insufficient_data'} · ` +
      `${change.valid_session_count || 0}/${change.required_session_count || 3} valid sessions. ` +
      `${change.message || 'Thresholds are exploratory, not clinically validated.'}` +
      (change.state === 'persistent_deviation'
        ? ' Review persistent concerns with a clinician; this is not evidence of disease progression.'
        : '');
    drawMovementFeatureTrend(
      'ml-feature-trend-chart',
      trends,
      $('ml-trend-feature').value,
      baselineFeatures,
      'No valid repeated-session feature measurements are available.'
    );
  }

  function renderMonitoring(profileState, comparison, modelStatus = latestState?.ml_analysis_status) {
    const monitor = profileState.monitoring || {};
    const active = ['active', 'stale', 'waiting_for_sensor'].includes(monitor.state);
    const sameProfile = monitor.profile_id === selectedProfile;
    $('start-monitoring').disabled = !selectedProfile || !profileState.baseline || active;
    $('end-monitoring').disabled = !active || !sameProfile;
    $('monitoring-state').textContent = !active
      ? 'Monitoring is not active.'
      : `${sameProfile ? 'Monitoring' : 'Another profile is monitoring'} · ${monitor.profile_id} · ${monitor.state} · ${Math.floor(monitor.duration_seconds || 0)}s · ${monitor.sample_count || 0} packets`;

    const profile = profileState.selected_profile;
    $('feature-profile-label').textContent = profile ? `${profile.display_name} (${profile.profile_id})` : 'Select a calibrated profile';
    const baseline = profileState.baseline?.features?.window_statistics || {};
    const liveHistory = profileState.comparison_history || [];
    const featureGraph = (canvas, feature, color, empty) => {
      const reference = baseline[feature];
      drawChart(canvas, [{
        color,
        points: liveHistory.map((entry) => ({ timestamp: entry.timestamp, value: entry.features?.[feature] })),
      }], {
        reference: reference ? { mean: reference.mean, std: reference.standard_deviation } : null,
        empty: reference ? empty : 'No saved baseline. Calibrate this profile before comparison.',
      });
    };
    featureGraph('accel-feature-chart', 'acceleration_magnitude', '#2878d0', 'Waiting for enough live samples.');
    featureGraph('gyro-feature-chart', 'gyroscope_magnitude', '#e76f51', 'Waiting for enough live samples.');

    const trends = profileState.trends || [];
    const trendGraph = (canvas, feature, color, empty) => {
      const entriesForFeature = trends
        .filter((entry) => entry.session_type === 'monitoring' && numeric(entry.features?.[feature]) !== null)
        .map((entry) => ({
          timestamp: entry.ended_at,
          value: entry.features[feature],
          session_id: entry.session_id,
        }));
      drawChart(canvas, [{
        color,
        points: entriesForFeature,
      }], { empty, connect: false });
    };
    trendGraph('accel-trend-chart', 'acceleration_magnitude', '#2878d0', 'No completed sessions with recorded acceleration summaries yet.');
    trendGraph('gyro-trend-chart', 'gyroscope_magnitude', '#e76f51', 'No completed sessions with recorded gyroscope summaries yet.');
    renderMLAnalysis(profileState, modelStatus);
    if (comparison) {
      const score = numeric(comparison.score);
      const deviations = comparison.feature_deviations_z || {};
      setTextIfChanged('mstate', String(comparison.state || 'insufficient_data').replaceAll('_', ' '));
      setTextIfChanged('msummary', score === null
        ? comparison.interpretation || 'Insufficient data for a reliable comparison.'
        : `${comparison.interpretation} Score ${score.toFixed(2)}; acceleration z ${format(deviations.acceleration_magnitude, 2)}, gyroscope z ${format(deviations.gyroscope_magnitude, 2)}.${comparison.state !== 'normal' && !comparison.sustained ? ` Persistence pending: ${comparison.persistence_window_count || 0}/3 qualifying windows.` : ''}`);
    } else {
      setTextIfChanged('mstate', profileState.baseline ? 'insufficient_data' : 'uncalibrated');
      setTextIfChanged('msummary', profileState.baseline
        ? 'Start monitoring and collect valid real packets for a fixed-baseline comparison.'
        : 'No saved baseline for this person; no movement comparison is available.');
    }
    const events = profileState.recent_events || [];
    $('change-events').innerHTML = events.length
      ? events.slice().reverse().map((event) => `<article><strong>${escapeHtml(event.severity)}</strong> · ${escapeHtml(new Date(event.timestamp).toLocaleString())}<small>Experimental score ${format(event.score)} · signal ${escapeHtml(event.signal_quality?.status || 'unknown')} · ${escapeHtml(event.description)}</small></article>`).join('')
      : '<div class="muted">No sustained change events for this profile yet.</div>';

    const calibrationHistory = profileState.calibration_history || [];
    $('calibration-history').innerHTML = calibrationHistory.length
      ? calibrationHistory.slice().reverse().map((entry) => {
        const values = entry.movement_feature_summary?.values || {};
        return `<article><strong>${escapeHtml(entry.state.replaceAll('_', ' '))}</strong> · ${escapeHtml(new Date(entry.completed_at).toLocaleString())}<small>${entry.duration_seconds}s requested · ${entry.accepted_sample_count} accepted samples · ${escapeHtml(entry.quality?.status || 'quality unavailable')} quality · baseline ${entry.baseline_available ? 'saved' : 'not saved'} · gyro RMS ${format(values.gyroscope_magnitude_rms, 3)} · accel RMS ${format(values.acceleration_magnitude_rms, 3)}${entry.incomplete_reason ? ` · ${escapeHtml(entry.incomplete_reason)}` : ''}</small></article>`;
      }).join('')
      : '<div class="muted">No completed calibration sessions.</div>';
  }

  function renderTemperature(temperature) {
    const state = temperature?.state || 'absent';
    const temp = numeric(temperature?.temperature_c);
    $('temperature-state').textContent = temp === null
      ? `Sensor temperature: ${state === 'implausible' ? 'implausible reading excluded' : state === 'stale' ? 'last reading is stale' : 'not reported'}; this is not body temperature.`
      : `Sensor/chip temperature: ${temp.toFixed(1)} °C · not body temperature.`;
    $('temperature-chart-note').textContent = temp === null
      ? 'Temperature is optional and not sent by the current firmware.'
      : 'Measured sensor/chip temperature only; no thermal risk threshold is applied.';
    drawChart('temperature-chart', [{
      color: '#e8bc79',
      points: (temperature?.history || []).map((entry) => ({ timestamp: entry.timestamp, value: entry.temperature_c })),
    }], { empty: 'Temperature is not sent by the current firmware.' });
  }

  function render(payload) {
    latestState = payload;
    const profileState = payload.profile_state || {};
    const calibration = profileState.calibration || { state: 'idle' };
    const monitoring = profileState.monitoring || {};
    const activeCalibration = ['collecting', 'paused_stale'].includes(calibration.state);
    const activeMonitoring = ['active', 'stale', 'waiting_for_sensor'].includes(monitoring.state);
    const activeProfile = profileState.selected_profile?.profile_id;
    if (!selectedProfile && activeProfile && (activeCalibration || activeMonitoring)) {
      selectedProfile = activeProfile;
      $('profile-select').value = selectedProfile;
    }
    const baseline = profileState.baseline;
    renderBaseline(baseline);
    renderCalibration(calibration);
    renderMonitoring(profileState, profileState.comparison);
    renderTemperature(payload.sensor_temperature);
    const diagnostics = payload.sensor_diagnostics || {};
    $('invalid-data-state').textContent = diagnostics.invalid_packet_count
      ? `${diagnostics.invalid_packet_count} rejected packet(s); last at ${new Date(diagnostics.last_invalid_at).toLocaleTimeString()} — ${diagnostics.last_invalid_reason}`
      : 'No invalid sensor packets rejected.';
    drawRawCharts((payload.history || []).filter((packet) =>
      packet.values && axes.every((axis) => numeric(packet.values[axis]) !== null)
    ).slice(-100));

    const latest = (payload.history || []).slice(-1)[0];
    const invalidAt = Date.parse(diagnostics.last_invalid_at);
    const latestAt = latest ? Date.parse(latest.received_at || latest.timestamp) : 0;
    if (Number.isFinite(invalidAt) && invalidAt > latestAt) {
      $('device').textContent = 'Invalid sensor packet rejected';
      $('conn').textContent = '⚠ Invalid sensor packet';
      $('conn').style.color = '#d9535f';
    }
    if (latest) {
      const units = latest.units || {};
      axes.forEach((axis) => {
        $(`unit-${axis}`).textContent = units[axis] || 'unit unknown';
      });
      $('accel-chart-unit').textContent = ['ax', 'ay', 'az'].every((axis) => units[axis])
        ? [...new Set(['ax', 'ay', 'az'].map((axis) => units[axis]))].join(', ')
        : 'unit unknown';
      $('gyro-chart-unit').textContent = ['gx', 'gy', 'gz'].every((axis) => units[axis])
        ? [...new Set(['gx', 'gy', 'gz'].map((axis) => units[axis]))].join(', ')
        : 'unit unknown';
      const allUnitsMatch = ['ax', 'ay', 'az'].every((axis) => ['m/s^2', 'm/s²'].includes(units[axis]))
        && ['gx', 'gy', 'gz'].every((axis) => ['deg/s', '°/s'].includes(units[axis]));
      $('profile-units-warning').textContent = allUnitsMatch
        ? ''
        : 'Axis units are missing or differ from the expected firmware declaration; these packets are excluded from calibration.';
    } else {
      $('profile-units-warning').textContent = '';
      axes.forEach((axis) => { $(`unit-${axis}`).textContent = 'unit unknown'; });
      $('accel-chart-unit').textContent = 'unit unknown';
      $('gyro-chart-unit').textContent = 'unit unknown';
    }
  }

  async function refresh() {
    if (requestInFlight) return;
    requestInFlight = true;
    try {
      const requestedProfile = selectedProfile;
      const payload = await request(selectedStateUrl());
      if (requestedProfile !== selectedProfile) return;
      const returnedProfile = payload.profile_state?.selected_profile?.profile_id || '';
      if (requestedProfile && returnedProfile !== requestedProfile) return;
      render(payload);
      if (selectedProfile && historyProfile !== selectedProfile) await loadSessionHistory();
    } catch (error) {
      showMessage(`Could not load profile/session state: ${error.message}`, true);
    } finally {
      requestInFlight = false;
    }
  }

  async function withAction(action, successMessage, buttonId) {
    const button = buttonId ? $(buttonId) : null;
    if (button) button.disabled = true;
    try {
      await action();
      showMessage(successMessage);
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      await refresh();
    }
  }

  window.addEventListener('DOMContentLoaded', async () => {
    try {
      if (requestedReportId) {
        document.body.classList.add('report-mode');
        $('session-report-message').textContent = 'Loading saved session analysis…';
        const report = await loadSessionAnalysis(requestedReportId, null);
        selectedProfile = report.profile.profile_id;
        selectedSessionId = report.session.session_id;
        window.localStorage.setItem('neurowatch.selectedProfile', selectedProfile);
        $('completed-session-state').textContent = 'Session Completed';
        $('session-report-message').textContent = 'Saved session analysis loaded.';
        await loadProfiles();
        $('return-to-dashboard').href = dashboardUrl(selectedProfile);
        await loadSessionHistory();
        document.title = `NeuroWatch | Session ${report.session.session_id}`;
      } else {
        await loadProfiles();
      }
      $('profile-select').addEventListener('change', async (event) => {
        selectedProfile = event.target.value;
        if (selectedProfile) window.localStorage.setItem('neurowatch.selectedProfile', selectedProfile);
        else window.localStorage.removeItem('neurowatch.selectedProfile');
        selectedSessionId = '';
        historyProfile = null;
        await loadSessionHistory();
        await refresh();
      });
      $('completed-session-select').addEventListener('change', async (event) => {
        selectedSessionId = event.target.value;
        if (!selectedSessionId) return;
        if (requestedReportId) {
          window.location.assign(`/session-report/${encodeURIComponent(selectedSessionId)}?profile_id=${encodeURIComponent(selectedProfile)}`);
          return;
        }
        $('session-report-message').textContent = 'Loading saved session analysis…';
        try {
          await loadSessionAnalysis(selectedSessionId);
        } catch (error) {
          clearSessionReport();
          $('session-report-message').textContent = `Could not load session report: ${error.message}`;
          $('session-report-message').style.color = '#b93845';
        }
      });
      $('ml-trend-feature').addEventListener('change', () => {
        if (latestState?.profile_state) {
          renderMLAnalysis(latestState.profile_state, latestState.ml_analysis_status);
        }
        if (latestReport) {
          drawMovementFeatureTrend(
            'session-ml-trend-chart',
            latestReport.longitudinal_history || [],
            $('ml-trend-feature').value,
            latestReport.baseline.movement_feature_summary,
            'No valid repeated-session feature measurements are available.'
          );
        }
      });
      $('create-profile-form').addEventListener('submit', (event) => {
        event.preventDefault();
        withAction(async () => {
          const payload = {
            display_name: $('profile-name').value.trim(),
            profile_id: $('profile-id').value.trim() || undefined,
          };
          const created = await request('/api/profiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          await loadProfiles();
          selectedProfile = created.profile_id;
          window.localStorage.setItem('neurowatch.selectedProfile', selectedProfile);
          $('profile-select').value = selectedProfile;
          $('profile-name').value = '';
          $('profile-id').value = '';
          historyProfile = null;
        }, 'Profile created. Select it to calibrate; no other profile baseline will be reused.', 'create-profile-submit');
      });
      $('start-calibration').addEventListener('click', () => withAction(async () => {
        if (!selectedProfile) throw new Error('Select or create a profile first.');
        await request(`/api/profiles/${encodeURIComponent(selectedProfile)}/calibrations`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            duration_seconds: Number($('calibration-duration').value),
            sensor_placement: $('sensor-placement').value,
          }),
        });
      }, 'Calibration started. The backend timer and accepted real packets determine completion.', 'start-calibration'));
      $('cancel-calibration').addEventListener('click', () => withAction(
        () => request('/api/calibration', { method: 'DELETE' }),
        'Calibration cancelled. No incomplete baseline was saved.',
        'cancel-calibration',
      ));
      $('start-monitoring').addEventListener('click', () => withAction(async () => {
        if (!selectedProfile) throw new Error('Select a calibrated profile first.');
        await request(`/api/profiles/${encodeURIComponent(selectedProfile)}/monitoring`, { method: 'POST' });
      }, 'Monitoring started using the selected profile’s saved, fixed baseline.', 'start-monitoring'));
      $('end-monitoring').addEventListener('click', async () => {
        if (endingSession) return;
        endingSession = true;
        const button = $('end-monitoring');
        button.disabled = true;
        let navigating = false;
        $('completed-session-state').textContent = 'Finalizing session…';
        $('session-report-message').textContent = 'Saving sensor readings and calculating the baseline comparison…';
        try {
          const result = await request('/api/session/end', { method: 'POST' });
          const report = result.report || result.profile_session;
          if (!report?.session?.session_id) throw new Error('The server ended the session but did not return a saved report.');
          const sessionId = report.session.session_id;
          const reportProfileId = report.profile.profile_id;
          await request(
            `/api/session/${encodeURIComponent(sessionId)}/analysis?profile_id=${encodeURIComponent(reportProfileId)}`
          );
          $('completed-session-state').textContent = 'Session Completed';
          showMessage('Session completed. Opening the saved analysis report…');
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          navigating = true;
          window.location.assign(`/session-report/${encodeURIComponent(sessionId)}?profile_id=${encodeURIComponent(reportProfileId)}`);
        } catch (error) {
          navigating = false;
          endingSession = false;
          $('completed-session-state').textContent = 'Session completion failed';
          $('session-report-message').textContent = `Could not complete the session report: ${error.message}`;
          $('session-report-message').style.color = '#b93845';
          showMessage(`Session could not be completed: ${error.message}`, true);
        } finally {
          if (!navigating) {
            button.disabled = false;
            await refresh();
          }
        }
      });
      $('download-session-report').addEventListener('click', async () => {
        if (!selectedSessionId || reportRequestInFlight) return;
        const button = $('download-session-report');
        reportRequestInFlight = true;
        button.disabled = true;
        $('session-report-message').textContent = 'Generating PDF report…';
        try {
          const response = await fetch(
            `/api/session/${encodeURIComponent(selectedSessionId)}/report.pdf?profile_id=${encodeURIComponent(selectedProfile)}`,
            { cache: 'no-store' },
          );
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail || `${response.status} ${response.statusText}`);
          }
          const blob = await response.blob();
          if (blob.type !== 'application/pdf') throw new Error('The server response was not a PDF report.');
          const objectUrl = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = objectUrl;
          link.download = `neurowatch-${selectedSessionId}.pdf`;
          link.click();
          window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
          $('session-report-message').textContent = 'PDF report downloaded.';
          $('session-report-message').style.color = '#526780';
        } catch (error) {
          $('session-report-message').textContent = `PDF report could not be generated: ${error.message}`;
          $('session-report-message').style.color = '#b93845';
        } finally {
          reportRequestInFlight = false;
          button.disabled = !selectedSessionId;
        }
      });
      window.addEventListener('resize', () => {
        if (latestState) render(latestState);
        if (latestReport) renderSessionReport(latestReport);
      }, { passive: true });
      if (requestedReportId) await refresh();
      else {
        await refresh();
        window.setInterval(refresh, 1000);
      }
    } catch (error) {
      if (requestedReportId) {
        document.body.classList.add('report-mode');
        $('session-report-message').textContent = `Could not load this saved session report: ${error.message}`;
        $('session-report-message').style.color = '#b93845';
        $('session-overview').textContent = 'The requested report is unavailable.';
        $('session-deviation-summary').textContent = 'Return to the dashboard and select an available completed session.';
        $('return-to-dashboard').href = dashboardUrl(queryProfileId || savedProfileId);
      } else {
        showMessage(`Profile controls could not start: ${error.message}`, true);
      }
    }
  });
})();
