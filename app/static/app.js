(() => {
  const toastContainer = () => document.getElementById('toastContainer');
  function toast(message, type = 'success', ms = 3200) {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    toastContainer()?.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 250); }, ms);
  }

  function busy(button, on) {
    if (!button) return;
    if (on) {
      if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
      button.textContent = button.dataset.busyText || 'Working…';
      button.disabled = true;
      button.classList.add('is-loading');
      button.setAttribute('aria-busy', 'true');
    } else {
      button.textContent = button.dataset.originalText || button.textContent;
      button.disabled = false;
      button.classList.remove('is-loading');
      button.removeAttribute('aria-busy');
    }
  }

  document.addEventListener('pointerdown', e => {
    const b = e.target.closest('button');
    if (b && !b.disabled) b.classList.add('was-clicked');
  });
  document.addEventListener('pointerup', e => {
    const b = e.target.closest('button');
    if (b) setTimeout(() => b.classList.remove('was-clicked'), 140);
  });

  function buildFormData(form, submitter) {
    const fd = new FormData(form);
    if (form.id === 'batchForm') {
      // Explicitly copy external form-associated checkboxes. This is more reliable
      // across browsers than depending on FormData(form) to discover them.
      fd.delete('ids');
      document.querySelectorAll('.pick:checked').forEach(cb => fd.append('ids', cb.value));
    }
    if (submitter?.name) fd.set(submitter.name, submitter.value);
    return fd;
  }

  async function trackActionJob(jobId, button) {
    const progress = document.getElementById('actionProgress');
    if (progress) {
      progress.classList.remove('hidden');
      progress.textContent = 'Starting…';
    }
    let failures = 0;
    const poll = async () => {
      try {
        const r = await fetch(`/api/actions/${encodeURIComponent(jobId)}`, {headers: {'Accept':'application/json'}, cache:'no-store'});
        if (!r.ok) throw new Error(`Action status failed (${r.status})`);
        const data = await r.json();
        const j = data.job || {};
        const done = j.completed || 0;
        const total = j.total || 0;
        const label = j.action === 'ignore'
          ? `${done}/${total} processed · ${j.ignored || 0} ignored`
          : `${done}/${total} processed · ${j.applied || 0} applied · ${j.skipped || 0} skipped · ${j.failed || 0} failed`;
        if (progress) progress.textContent = label;
        if (j.status === 'completed') {
          busy(button, false);
          const kind = (j.failed || 0) > 0 ? 'error' : 'success';
          toast(j.action === 'ignore'
            ? `Finished: ${j.ignored || 0} ignored`
            : `Finished: ${j.applied || 0} applied, ${j.skipped || 0} skipped, ${j.failed || 0} failed`, kind, 4200);
          setTimeout(() => window.location.reload(), 550);
          return;
        }
        if (j.status === 'failed') {
          busy(button, false);
          toast(j.error || 'Batch action failed', 'error', 5000);
          return;
        }
        failures = 0;
        setTimeout(poll, 900);
      } catch (err) {
        failures += 1;
        if (failures >= 5) {
          busy(button, false);
          toast(err.message || 'Lost contact with action job', 'error', 5000);
          return;
        }
        setTimeout(poll, 1500);
      }
    };
    poll();
  }

  document.addEventListener('submit', async e => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    const submitter = e.submitter || form.querySelector('button[type="submit"],button:not([type])');
    const confirmMessage = form.dataset.confirm;
    if (confirmMessage && !window.confirm(confirmMessage)) {
      e.preventDefault();
      return;
    }
    if (!form.hasAttribute('data-ajax')) {
      busy(submitter, true);
      return;
    }
    e.preventDefault();
    busy(submitter, true);
    try {
      const fd = buildFormData(form, submitter);
      if (form.id === 'batchForm' && !fd.getAll('ids').length) {
        throw new Error('Select at least one applicable suggestion first');
      }
      // Do not use form.action here. A form control named "action" shadows the
      // HTMLFormElement.action property in browsers and turns the URL into an
      // HTMLInputElement (which stringifies as "[object HTMLInputElement]").
      const actionUrl = form.getAttribute('action') || window.location.pathname;
      const response = await fetch(actionUrl, {
        method: (form.getAttribute('method') || 'POST').toUpperCase(), body: fd,
        headers: {'X-Requested-With': 'fetch', 'Accept': 'application/json'}
      });
      let data = {};
      try { data = await response.json(); } catch (_) {}
      if (!response.ok || data.ok === false) throw new Error(data.error || `Request failed (${response.status})`);

      if (data.job_id) {
        toast(data.message || 'Batch action started');
        trackActionJob(data.job_id, submitter);
        return;
      }
      if (form.hasAttribute('data-scan-form')) {
        const scanId = data.scan_id;
        toast(`Scan #${scanId} started`);
        activateScan(scanId);
        busy(submitter, false);
        return;
      }
      if (form.hasAttribute('data-remove-card')) {
        const card = form.closest('[data-suggestion-card]');
        if (card) {
          card.classList.add('card-success');
          setTimeout(() => card.remove(), 260);
        }
        toast(data.status === 'ignored' ? 'Suggestion ignored' : 'Change applied');
        refreshStats();
      } else if (form.dataset.redirect) {
        toast(data.message || 'Saved');
        setTimeout(() => window.location.href = form.dataset.redirect, 250);
      } else if (form.hasAttribute('data-reload-on-success')) {
        toast(data.message || `${data.completed ?? ''} ${data.action ?? 'Done'}`.trim());
        setTimeout(() => window.location.reload(), 350);
      } else {
        toast(data.message || 'Saved');
        busy(submitter, false);
      }
    } catch (err) {
      toast(err.message || 'Something went wrong', 'error');
      busy(submitter, false);
    }
  });

  const selectAll = document.getElementById('selectAll');
  const selectedCount = document.getElementById('selectedCount');
  function updateSelectedCount() {
    const n = document.querySelectorAll('.pick:checked').length;
    if (selectedCount) selectedCount.textContent = `${n} selected`;
  }
  selectAll?.addEventListener('change', e => {
    document.querySelectorAll('.pick').forEach(x => x.checked = e.target.checked);
    updateSelectedCount();
  });
  document.querySelectorAll('.pick').forEach(x => x.addEventListener('change', updateSelectedCount));

  document.getElementById('selectChangedFields')?.addEventListener('click', () => {
    document.querySelectorAll('[data-field-row].changed input[name="apply_fields"]').forEach(x => x.checked = true);
    toast('Changed fields selected');
  });

  let scanTimer = null;
  let lastScanFingerprint = '';
  function renderScan(payload) {
    const s = payload.scan;
    if (!s) return;
    const lastEvent = (payload.events || []).at(-1)?.id || 0;
    const fingerprint = [s.id, s.status, s.updated_at, s.processed_count, s.flagged_count, s.error_count, lastEvent].join('|');
    if (fingerprint === lastScanFingerprint) return;
    lastScanFingerprint = fingerprint;

    const box = document.getElementById('liveScan');
    if (!box) return;
    box.classList.remove('hidden');
    box.dataset.scanId = s.id;
    document.getElementById('scanProgressBar').style.width = `${s.percent || 0}%`;
    document.getElementById('scanPercent').textContent = `${s.percent || 0}%`;
    document.getElementById('scanProcessed').textContent = `${s.processed_count || 0} / ${s.item_count || 0}`;
    document.getElementById('scanFlagged').textContent = s.flagged_count || 0;
    document.getElementById('scanErrors').textContent = s.error_count || 0;
    const aiReq = document.getElementById('scanAIRequests'); if (aiReq) aiReq.textContent = s.ai_requests || 0;
    const aiCache = document.getElementById('scanAICache'); if (aiCache) aiCache.textContent = s.ai_cache_hits || 0;
    const aiTokens = document.getElementById('scanAITokens'); if (aiTokens) aiTokens.textContent = (s.ai_input_tokens || 0) + (s.ai_output_tokens || 0);
    const aiCost = document.getElementById('scanAICost'); if (aiCost && aiCost.textContent.trim() !== 'Local') aiCost.textContent = `$${Number(s.ai_estimated_cost || 0).toFixed(4)}`;
    const aiReason = document.getElementById('scanAIReasoning'); if (aiReason) aiReason.textContent = s.ai_reasoning_tokens || 0;
    const aiBudget = document.getElementById('scanAIBudget'); if (aiBudget) aiBudget.textContent = Number(s.ai_budget_remaining) < 0 ? 'Unlimited' : `$${Number(s.ai_budget_remaining || 0).toFixed(4)}`;
    document.getElementById('scanCurrentTitle').textContent = s.current_title || (s.status === 'completed' ? 'Finished' : 'Preparing…');
    document.getElementById('scanStatusText').textContent = s.progress_message || '';
    const pill = document.getElementById('scanStatusPill');
    pill.textContent = s.status;
    pill.className = `status-pill status-${s.status}`;
    const feed = document.getElementById('scanEvents');
    if (feed) {
      feed.innerHTML = (payload.events || []).slice(-10).map(ev =>
        `<div class="event event-${ev.level || 'info'}"><span>${new Date((ev.created_at || 0)*1000).toLocaleTimeString()}</span><div>${escapeHtml(ev.message || '')}</div></div>`
      ).join('');
      feed.scrollTop = feed.scrollHeight;
    }
    if (['completed','failed','interrupted'].includes(s.status)) {
      if (scanTimer) clearTimeout(scanTimer);
      scanTimer = null;
      refreshStats();
      toast(s.status === 'completed' ? `Scan complete — ${s.flagged_count} books need review` : `Scan ${s.status}`, s.status === 'completed' ? 'success' : 'error');
      return;
    }
  }

  function escapeHtml(text) {
    const d = document.createElement('div'); d.textContent = text; return d.innerHTML;
  }

  async function pollScan(id) {
    if (!id) return;
    try {
      const r = await fetch(`/api/scans/${id}`, {headers: {'Accept':'application/json'}, cache:'no-store'});
      if (r.ok) renderScan(await r.json());
    } catch (_) {}
    if (scanTimer !== null) scanTimer = setTimeout(() => pollScan(id), 2000);
  }
  function activateScan(id) {
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = true;
    lastScanFingerprint = '';
    pollScan(id);
  }
  const live = document.getElementById('liveScan');
  if (live?.dataset.scanId) activateScan(live.dataset.scanId);

  async function refreshStats() {
    try {
      const r = await fetch('/api/dashboard-stats', {cache:'no-store'});
      if (!r.ok) return;
      const d = await r.json();
      const p = document.getElementById('pendingStat'); if (p) p.textContent = d.pending;
      const a = document.getElementById('appliedStat'); if (a) a.textContent = d.applied;
      const c = document.getElementById('confidenceStat'); if (c) c.textContent = `${Math.round((d.avg_conf || 0)*100)}%`;
      const l = document.getElementById('latestCount'); if (l) l.textContent = d.latest?.item_count || 0;
      const q = document.getElementById('qualityStat'); if (q) q.textContent = Math.round(d.quality?.avg || 0);
      const du = document.getElementById('duplicateStat'); if (du) du.textContent = d.duplicate_count || 0;
      const cp = document.getElementById('coverPendingStat'); if (cp) cp.textContent = d.cover_pending || 0;
      const ps = document.getElementById('pairingStat'); if (ps) ps.textContent = d.pairing_pending || 0;
      const lk = document.getElementById('lockedStat'); if (lk) lk.textContent = d.locked_count || 0;
      const cm = document.getElementById('completeStat'); if (cm) cm.textContent = d.complete_count || 0;
    } catch (_) {}
  }
})();
