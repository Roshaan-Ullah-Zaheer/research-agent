/* ============================================================
   Scout — AI Research Agent · frontend controller
   Talks to GET /api/research?topic=... over Server-Sent Events.
   ============================================================ */
(function () {
  'use strict';

  /* ---------- DOM references ---------- */
  const form = document.getElementById('research-form');
  const input = document.getElementById('topic-input');
  const runBtn = document.getElementById('run-btn');
  const timelineEl = document.getElementById('timeline');
  const timelineStatus = document.getElementById('timeline-status');
  const emptyState = document.getElementById('empty-state');
  const reportArticle = document.getElementById('report-article');
  const liveBadge = document.getElementById('live-badge');
  const downloadActions = document.getElementById('download-actions');
  const downloadMdBtn = document.getElementById('download-md');
  const downloadPdfBtn = document.getElementById('download-pdf');
  const errorBanner = document.getElementById('error-banner');
  const errorMessage = document.getElementById('error-message');
  const errorDismiss = document.getElementById('error-dismiss');
  const chips = document.querySelectorAll('.chip');
  const flowEl = document.getElementById('flow');
  const processEl = document.getElementById('process');
  const reportCard = document.getElementById('report-card');
  const jumpBtn = document.getElementById('jump-latest');

  /* ---------- Timeline phase definitions ---------- */
  const PHASES = [
    { key: 'planning', label: 'Planning', desc: 'Breaking the question into sub-questions' },
    { key: 'searching', label: 'Searching', desc: 'Querying the web for each sub-question' },
    { key: 'reading', label: 'Reading', desc: 'Extracting facts from the sources' },
    { key: 'writing', label: 'Writing', desc: 'Drafting a cited report' },
    { key: 'reviewing', label: 'Reviewing', desc: 'Checking the draft for gaps' },
    { key: 'revising', label: 'Revising', desc: 'Running another round of research', optional: true },
    { key: 'done', label: 'Done', desc: 'Report complete' },
  ];
  const PHASE_ORDER = PHASES.reduce((acc, p, i) => ((acc[p.key] = i), acc), {});

  /* ---------- Run state ---------- */
  let es = null;
  let isRunning = false;
  let currentTopic = '';
  let finalMarkdown = '';
  let finalSources = [];
  let maxReachedIdx = -1;
  const seenSourceIds = new Set();

  const checkIcon =
    '<svg class="check" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>';

  /* ============================================================
     Helpers
     ============================================================ */
  function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str == null ? '' : String(str);
    return d.innerHTML;
  }

  function slugify(text) {
    return (
      String(text)
        .toLowerCase()
        .trim()
        .replace(/[^\w\s-]/g, '')
        .replace(/[\s_-]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 60) || 'research-report'
    );
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, '');
    } catch (e) {
      return '';
    }
  }

  function faviconFor(url) {
    const host = hostOf(url);
    return 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=32';
  }

  /* ============================================================
     Timeline rendering
     ============================================================ */
  function buildTimeline(includeRevising) {
    timelineEl.innerHTML = '';
    PHASES.forEach((phase) => {
      if (phase.optional && !includeRevising) return;
      const li = document.createElement('li');
      li.className = 'tl-step';
      li.dataset.phase = phase.key;
      li.innerHTML =
        '<span class="tl-marker">' +
        '<span class="spinner"></span>' +
        checkIcon +
        '</span>' +
        '<p class="tl-title">' +
        escapeHtml(phase.label) +
        '</p>' +
        '<p class="tl-msg"></p>' +
        '<div class="tl-detail"></div>';
      timelineEl.appendChild(li);
    });
  }

  function stepEl(phaseKey) {
    return timelineEl.querySelector('.tl-step[data-phase="' + phaseKey + '"]');
  }

  function detailEl(phaseKey) {
    const s = stepEl(phaseKey);
    return s ? s.querySelector('.tl-detail') : null;
  }

  /* Ensure the optional Revising step exists (inserted before Done). */
  function ensureRevisingStep() {
    if (stepEl('revising')) return;
    const phase = PHASES.find((p) => p.key === 'revising');
    const li = document.createElement('li');
    li.className = 'tl-step';
    li.dataset.phase = 'revising';
    li.innerHTML =
      '<span class="tl-marker"><span class="spinner"></span>' +
      checkIcon +
      '</span><p class="tl-title">' +
      escapeHtml(phase.label) +
      '</p><p class="tl-msg"></p><div class="tl-detail"></div>';
    const doneStep = stepEl('done');
    timelineEl.insertBefore(li, doneStep);
  }

  /* Drive marker states from the current active phase.
     The agent can loop back (reviewing -> revising -> writing), so we track the
     furthest phase reached: any step at or before that high-water mark stays
     complete even when the active phase moves backward during a revision round. */
  function setActivePhase(phaseKey, message) {
    if (phaseKey === 'revising') ensureRevisingStep();

    const activeIdx = PHASE_ORDER[phaseKey];
    if (activeIdx > maxReachedIdx) maxReachedIdx = activeIdx;
    const steps = timelineEl.querySelectorAll('.tl-step');

    steps.forEach((step) => {
      const idx = PHASE_ORDER[step.dataset.phase];
      step.classList.remove('is-active', 'is-complete', 'is-pending');
      if (idx === activeIdx) {
        step.classList.add('is-active');
        if (message) {
          const msg = step.querySelector('.tl-msg');
          if (msg) msg.textContent = message;
        }
      } else if (idx < activeIdx || idx <= maxReachedIdx) {
        step.classList.add('is-complete');
      } else {
        step.classList.add('is-pending');
      }
    });

    if (phaseKey === 'done') {
      steps.forEach((s) => {
        s.classList.remove('is-active', 'is-pending');
        s.classList.add('is-complete');
      });
    }
  }

  function markAllComplete() {
    timelineEl.querySelectorAll('.tl-step').forEach((s) => {
      s.classList.remove('is-active', 'is-pending');
      s.classList.add('is-complete');
    });
  }

  /* ============================================================
     Event handlers (per event "type")
     ============================================================ */
  function handleStatus(data) {
    const phase = data.phase;
    if (!phase) return;

    if (phase === 'error') {
      showError(data.message || 'The agent reported an error.');
      return;
    }
    if (phase === 'revising') ensureRevisingStep();
    if (PHASE_ORDER[phase] !== undefined) {
      setActivePhase(phase, data.message || '');
      setStatusPill(phase);
    }
  }

  function setStatusPill(phaseKey) {
    const phase = PHASES.find((p) => p.key === phaseKey);
    const label = phase ? phase.label : phaseKey;
    timelineStatus.textContent = phaseKey === 'done' ? 'Complete' : label + '…';
    timelineStatus.classList.toggle('text-accent-300', phaseKey !== 'done');
    timelineStatus.classList.toggle('text-emerald-300', phaseKey === 'done');
  }

  function handlePlan(data) {
    const detail = detailEl('planning');
    if (!detail) return;
    detail.innerHTML = '';
    (data.questions || []).forEach((q) => {
      const item = document.createElement('div');
      item.className = 'plan-item';
      item.innerHTML = '<span class="q-dot"></span><span>' + escapeHtml(q) + '</span>';
      detail.appendChild(item);
    });
  }

  function addSourceChip(src) {
    if (src == null || seenSourceIds.has(src.id)) return;
    seenSourceIds.add(src.id);

    const detail = detailEl('searching');
    if (!detail) return;

    const a = document.createElement('a');
    a.className = 'source-chip';
    a.href = src.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.title = (src.title || src.url) + (src.snippet ? '\n\n' + src.snippet : '');
    a.innerHTML =
      '<img src="' +
      escapeHtml(faviconFor(src.url)) +
      '" alt="" loading="lazy" onerror="this.style.visibility=\'hidden\'" />' +
      '<span class="chip-title">' +
      escapeHtml(src.title || hostOf(src.url) || src.url) +
      '</span>' +
      '<svg class="ext" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 7h10v10"></path><path d="M7 17 17 7"></path></svg>';
    detail.appendChild(a);
  }

  function handleSearch(data) {
    /* "search" events also carry results we can surface as chips. */
    (data.results || []).forEach((r, i) => {
      const id = r.id != null ? r.id : 's-' + (data.query || '') + '-' + i;
      addSourceChip({ id: id, title: r.title, url: r.url });
    });
  }

  function handleReview(data) {
    const detail = detailEl('reviewing');
    if (!detail) return;
    detail.innerHTML = '';

    if (data.summary) {
      const s = document.createElement('p');
      s.className = 'review-summary';
      s.textContent = data.summary;
      detail.appendChild(s);
    }

    if (data.needs_more) {
      const wrap = document.createElement('div');
      wrap.className = 'review-gaps';
      let html =
        '<div class="gaps-head">' +
        '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 9v4"></path><path d="M12 17h.01"></path><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"></path></svg>' +
        'Found gaps — running another search round</div>';
      if (data.gaps && data.gaps.length) {
        html += '<ul>' + data.gaps.map((g) => '<li>' + escapeHtml(g) + '</li>').join('') + '</ul>';
      }
      wrap.innerHTML = html;
      detail.appendChild(wrap);
    }
  }

  /* Live draft preview while the writer streams. */
  function handleDraft(data) {
    if (!data.markdown) return;
    showReportArea();
    liveBadge.classList.remove('hidden');
    liveBadge.classList.add('flex');
    reportArticle.innerHTML = renderMarkdown(data.markdown) + '<span class="draft-cursor"></span>';
  }

  function handleReport(data) {
    finalMarkdown = data.markdown || '';
    finalSources = Array.isArray(data.sources) ? data.sources : [];
    showReportArea();
    liveBadge.classList.add('hidden');
    liveBadge.classList.remove('flex');

    let html = renderMarkdown(finalMarkdown);
    html += renderSourcesSection(finalSources);
    reportArticle.innerHTML = html;

    downloadActions.classList.remove('hidden');
    downloadActions.classList.add('flex');
  }

  /* ============================================================
     Markdown + citation rendering
     ============================================================ */
  function renderMarkdown(md) {
    let html;
    if (window.marked && typeof window.marked.parse === 'function') {
      window.marked.setOptions({ breaks: true, gfm: true });
      html = window.marked.parse(md);
    } else {
      html = '<p>' + escapeHtml(md).replace(/\n/g, '<br>') + '</p>';
    }
    return styleCitations(html);
  }

  /* Wrap bracketed [n] / [n, m] citations in styled superscript spans,
     while leaving real markdown links (already <a> tags) untouched. */
  function styleCitations(html) {
    return html.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, function (match, nums) {
      const parts = nums.split(',').map((n) => n.trim());
      return parts
        .map((n) => '<a href="#source-' + n + '" class="citation" data-cite="' + n + '">' + n + '</a>')
        .join('');
    });
  }

  function renderSourcesSection(sources) {
    if (!sources || !sources.length) return '';
    let items = sources
      .map(function (s) {
        const host = hostOf(s.url);
        return (
          '<li id="source-' +
          escapeHtml(String(s.id)) +
          '"><span class="src-num">' +
          escapeHtml(String(s.id)) +
          '</span><span><a href="' +
          escapeHtml(s.url) +
          '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(s.title || s.url) +
          '</a>' +
          (host ? '<span class="src-host">' + escapeHtml(host) + '</span>' : '') +
          '</span></li>'
        );
      })
      .join('');
    return '<section class="report-sources"><h2>Sources</h2><ol>' + items + '</ol></section>';
  }

  /* ============================================================
     Panel visibility
     ============================================================ */
  function showProcess() {
    emptyState.classList.add('hidden');
    processEl.classList.remove('hidden');
    reportCard.classList.add('hidden');
  }

  function showReportArea() {
    emptyState.classList.add('hidden');
    processEl.classList.remove('hidden');
    reportCard.classList.remove('hidden');
  }

  function showEmptyState() {
    emptyState.classList.remove('hidden');
    processEl.classList.add('hidden');
    reportCard.classList.add('hidden');
    reportArticle.innerHTML = '';
  }

  /* ============================================================
     Error handling
     ============================================================ */
  function showError(message) {
    errorMessage.textContent = message || 'The research run could not be completed. Please try again.';
    errorBanner.classList.add('show');
    errorBanner.classList.remove('hidden');
    timelineStatus.textContent = 'Error';
    timelineStatus.classList.remove('text-accent-300', 'text-emerald-300');
    timelineStatus.classList.add('text-red-300');
    timelineEl.querySelectorAll('.tl-step.is-active').forEach((s) => s.classList.remove('is-active'));
    finishRun();
  }

  function hideError() {
    errorBanner.classList.remove('show');
    errorBanner.classList.add('hidden');
    timelineStatus.classList.remove('text-red-300');
  }

  /* ============================================================
     Run lifecycle
     ============================================================ */
  function startRun(topic) {
    if (isRunning) return;
    topic = (topic || '').trim();
    if (!topic) {
      input.focus();
      return;
    }

    currentTopic = topic;
    input.value = topic;
    isRunning = true;
    finalMarkdown = '';
    finalSources = [];
    maxReachedIdx = -1;
    seenSourceIds.clear();

    hideError();
    setRunningUI(true);
    buildTimeline(false);
    setActivePhase('planning', 'Starting up…');
    setStatusPill('planning');
    showProcess();
    stickToBottom = true;
    flowEl.scrollTop = 0;
    if (jumpBtn) jumpBtn.classList.add('hidden');
    downloadActions.classList.add('hidden');
    downloadActions.classList.remove('flex');

    try {
      es = new EventSource('/api/research?topic=' + encodeURIComponent(topic));
    } catch (e) {
      showError('Could not connect to the research service.');
      return;
    }

    es.onmessage = function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      dispatch(data);
    };

    es.onerror = function () {
      /* If we already finished cleanly, ignore the close. */
      if (!isRunning) return;
      if (finalMarkdown) {
        finishRun();
        return;
      }
      showError('The connection to the research service was lost. Please try again.');
    };
  }

  function dispatch(data) {
    if (!data || typeof data !== 'object') return;
    switch (data.type) {
      case 'status':
        handleStatus(data);
        break;
      case 'plan':
        handlePlan(data);
        break;
      case 'search':
        handleSearch(data);
        break;
      case 'source':
        addSourceChip(data);
        break;
      case 'draft':
        handleDraft(data);
        break;
      case 'review':
        handleReview(data);
        break;
      case 'report':
        handleReport(data);
        break;
      case 'done':
        completeRun();
        break;
      case 'error':
        showError(data.message);
        break;
      default:
        break;
    }
  }

  function completeRun() {
    setActivePhase('done');
    markAllComplete();
    setStatusPill('done');
    liveBadge.classList.add('hidden');
    liveBadge.classList.remove('flex');
    if (finalMarkdown) {
      downloadActions.classList.remove('hidden');
      downloadActions.classList.add('flex');
    }
    finishRun();
    // Settle on the finished report (its title), and stop auto-following.
    stickToBottom = false;
    if (jumpBtn) jumpBtn.classList.add('hidden');
    if (reportCard && !reportCard.classList.contains('hidden')) {
      reportCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function finishRun() {
    isRunning = false;
    setRunningUI(false);
    if (es) {
      es.close();
      es = null;
    }
  }

  function setRunningUI(running) {
    runBtn.disabled = running;
    runBtn.classList.toggle('is-loading', running);
    const label = runBtn.querySelector('.btn-label');
    const loading = runBtn.querySelector('.btn-loading');
    if (label) label.classList.toggle('hidden', running);
    if (loading) {
      loading.classList.toggle('hidden', !running);
      loading.classList.toggle('inline-flex', running);
    }
    input.disabled = running;
    chips.forEach((c) => (c.disabled = running));
    if (running) timelineStatus.textContent = 'Running…';
  }

  /* ============================================================
     Downloads
     ============================================================ */
  function buildMarkdownDownload() {
    let md = finalMarkdown || '';
    if (finalSources && finalSources.length) {
      md = md.replace(/\s*$/, '');
      md += '\n\n## Sources\n\n';
      md += finalSources
        .map(function (s) {
          return '[' + s.id + '] ' + (s.title || s.url) + ' — ' + s.url;
        })
        .join('\n');
      md += '\n';
    }
    return md;
  }

  function downloadMarkdown() {
    if (!finalMarkdown) return;
    const blob = new Blob([buildMarkdownDownload()], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = slugify(currentTopic) + '.md';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function downloadPdf() {
    if (!finalMarkdown || !window.html2pdf) return;

    /* Clone the rendered article and apply a print-friendly light theme. */
    const clone = reportArticle.cloneNode(true);
    clone.classList.add('pdf-export');
    const cursor = clone.querySelector('.draft-cursor');
    if (cursor) cursor.remove();

    const wrapper = document.createElement('div');
    wrapper.style.position = 'fixed';
    wrapper.style.left = '-9999px';
    wrapper.style.top = '0';
    wrapper.style.width = '794px';
    wrapper.appendChild(clone);
    document.body.appendChild(wrapper);

    const original = downloadPdfBtn.innerHTML;
    downloadPdfBtn.disabled = true;
    downloadPdfBtn.innerHTML = '<span class="spinner-sm"></span>';

    const opts = {
      margin: [12, 12, 14, 12],
      filename: slugify(currentTopic) + '.pdf',
      image: { type: 'jpeg', quality: 0.98 },
      html2canvas: { scale: 2, backgroundColor: '#ffffff', useCORS: true },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak: { mode: ['avoid-all', 'css', 'legacy'] },
    };

    window
      .html2pdf()
      .set(opts)
      .from(clone)
      .save()
      .then(cleanup)
      .catch(cleanup);

    function cleanup() {
      if (wrapper.parentNode) document.body.removeChild(wrapper);
      downloadPdfBtn.disabled = false;
      downloadPdfBtn.innerHTML = original;
    }
  }

  /* ============================================================
     Wire up events
     ============================================================ */
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    startRun(input.value);
  });

  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      if (isRunning) return;
      startRun(chip.dataset.topic || chip.textContent);
    });
  });

  errorDismiss.addEventListener('click', hideError);
  downloadMdBtn.addEventListener('click', downloadMarkdown);
  downloadPdfBtn.addEventListener('click', downloadPdf);

  /* Smooth-scroll to a source when a citation is clicked. */
  reportArticle.addEventListener('click', function (e) {
    const cite = e.target.closest('.citation');
    if (!cite) return;
    const id = cite.getAttribute('data-cite');
    const target = document.getElementById('source-' + id);
    if (target) {
      e.preventDefault();
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.style.transition = 'background 0.3s ease';
      target.style.background = 'rgba(139, 92, 246, 0.1)';
      setTimeout(() => (target.style.background = ''), 1200);
    }
  });

  /* ============================================================
     Auto-scroll: follow the newest content while running; pause the
     moment the user scrolls up; resume when they return to the bottom.
     ============================================================ */
  let stickToBottom = true;

  function nearBottom() {
    return flowEl.scrollHeight - flowEl.scrollTop - flowEl.clientHeight < 90;
  }
  function scrollToBottom(smooth) {
    flowEl.scrollTo({ top: flowEl.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  }
  function updateJumpBtn() {
    if (jumpBtn) jumpBtn.classList.toggle('hidden', !(isRunning && !stickToBottom));
  }

  flowEl.addEventListener('scroll', function () {
    stickToBottom = nearBottom();
    updateJumpBtn();
  });
  if (jumpBtn) {
    jumpBtn.addEventListener('click', function () {
      stickToBottom = true;
      scrollToBottom(true);
      updateJumpBtn();
    });
  }
  // As steps / sources / the report stream in, keep the latest in view.
  new MutationObserver(function () {
    if (stickToBottom) scrollToBottom(false);
    updateJumpBtn();
  }).observe(flowEl, { childList: true, subtree: true, characterData: true });

  /* Build an initial (idle) timeline so it's ready when a run starts. */
  buildTimeline(false);
})();
