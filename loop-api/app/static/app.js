// Loop — Four-Panel Console (strict-auth, Supabase-backed)
// Works with public/console.html provided in this iteration.
// - Human logins (A/B) via Supabase email/password
// - Bot operator login via Supabase (protected /api/bot/process)
// - Preview = dry_run=true (no writes); Publish = dry_run=false (writes + processed flag)
// - Same-thread chat by default (you can still override per-user threads in the UI)

(function () {
  // ----------------------- Config Defaults -----------------------
  const DEFAULTS = {
    API_BASE: 'https://loopasync.com', // Netlify proxy to backend
    LOOP_ID: 'bc52f715-1ba2-4c47-908f-51dc70e79e5d',
    THREAD_ID_SHARED: '80770009-d6a9-490b-a531-293132175827',
    // Known A/B profile UUIDs (will be overwritten by Supabase login response anyway)
    A_UUID: '7f5a795e-e026-4c40-b1f3-5e6b35ac454b',
    B_UUID: 'fae29f2b-a11b-4a14-8d45-2d161030440b',
    // Supabase defaults you provided (can be edited in the UI at runtime)
    SB_URL: 'https://yayaoxjotevczzyeahjc.supabase.co',
    SB_ANON: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlheWFveGpvdGV2Y3p6eWVhaGpjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc2NzI3NDIsImV4cCI6MjA3MzI0ODc0Mn0.jsq6FMNboaaga8YHKwZhdhayZP9EPTekj27y_beLrq0',
    BOT_EMAIL: 'bot@loop.bot',
    BOT_PASS: 'password',
    A_EMAIL: 'a2@loop.test',
    A_PASS: 'password',
    B_EMAIL: 'b2@loop.test',
    B_PASS: 'password',
    PROCESS_LIMIT: 10
  };

  // ----------------------- DOM helpers ---------------------------
  const $ = (id) => document.getElementById(id);
  const statusEl = $('status');
  const logEl = $('console');

  function setStatus(t) { if (statusEl) statusEl.textContent = t; }
  function log(...args) {
    if (!logEl) return;
    const line = args.map(a => {
      try { return typeof a === 'string' ? a : JSON.stringify(a, null, 2); }
      catch { return String(a); }
    }).join(' ');
    logEl.textContent += `\n${line}`;
    logEl.scrollTop = logEl.scrollHeight;
  }
  function escapeHtml(s) {
    return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
  }
  const fmtTime = (d) => d.toLocaleTimeString([], { hour12:false });

  // ----------------------- Elements ------------------------------
  const apiBase = $('apiBase');

  // IDs & message areas
  const userAId = $('userAId');
  const userAText = $('userAText');
  const messagesA = $('messagesA');

  const userBId = $('userBId');
  const userBText = $('userBText');
  const messagesB = $('messagesB');

  const botToAPreview = $('botToAPreview');
  const botToBPreview = $('botToBPreview');

  // Preview behaviour toggles (from console.html)
  const previewOnSendEl = $('previewOnSend');          // default checked
  const previewAfterPublishEl = $('previewAfterPublish'); // default unchecked
  const previewNowBtn = $('previewNowBtn');

  // Optional labels
  const botAPreviewMeta = $('botAPreviewMeta');
  const botBPreviewMeta = $('botBPreviewMeta');

  // Loop & thread inputs
  const loopIdEl = $('loopId');
  const threadIdAEl = $('threadIdA');
  const threadIdBEl = $('threadIdB');
  const btnSaveLoopThread = $('btnSaveLoopThread');
  const loopThreadStatus = $('loopThreadStatus');

  // Supabase settings + bot login
  const sbUrlEl = $('sbUrl');
  const sbAnonEl = $('sbAnon');
  const botEmailEl = $('botEmail');
  const botPassEl = $('botPass');
  const autoLoginBotEl = $('autoLoginBot');
  const btnInitSupabase = $('btnInitSupabase');
  const botStatusEl = $('botStatus');

  // Human logins
  const sbEmailAEl = $('sbEmailA');
  const sbPassAEl = $('sbPassA');
  const sbEmailBEl = $('sbEmailB');
  const sbPassBEl = $('sbPassB');
  const rememberSessionsEl = $('rememberSessions');
  const btnLoginA = $('btnLoginA');
  const btnLoginB = $('btnLoginB');
  const loginAStatus = $('loginAStatus');
  const loginBStatus = $('loginBStatus');

  // Manual JWT paste (still supported for debugging)
  const jwtAEl = $('jwtA');
  const jwtBEl = $('jwtB');
  const jwtOpEl = $('jwtOperator');

  // Save/Clear config
  const saveCfgBtn = $('saveCfgBtn');
  const clearCfgBtn = $('clearCfgBtn');

  // ----------------------- State -------------------------------
  let supabaseClient = null;
  let uidA = null, uidB = null, uidBot = null; // set by Supabase auth
  const STORAGE_KEY = 'loop_four_panel_cfg_v4';

  const lastState = {
    A: { lastBotMsgId: null, lastRefresh: null, lastPreviewAt: null },
    B: { lastBotMsgId: null, lastRefresh: null, lastPreviewAt: null },
  };

  // ----------------------- Storage ------------------------------
  // function saveCfg() {
  //   const cfg = {
  //     apiBase: apiBase?.value?.trim(),
  //     loopId: loopIdEl?.value?.trim(),
  //     threadIdA: threadIdAEl?.value?.trim(),
  //     threadIdB: threadIdBEl?.value?.trim(),
  //     sbUrl: sbUrlEl?.value?.trim(),
  //     sbAnon: sbAnonEl?.value?.trim(),
  //     botEmail: botEmailEl?.value?.trim(),
  //     botPass: botPassEl?.value?.trim(),
  //     sbEmailA: sbEmailAEl?.value?.trim(),
  //     sbPassA: sbPassAEl?.value?.trim(),
  //     sbEmailB: sbEmailBEl?.value?.trim(),
  //     sbPassB: sbPassBEl?.value?.trim(),
  //     remember: !!(rememberSessionsEl?.checked),
  //     previewOnSend: !!(previewOnSendEl?.checked),
  //     previewAfterPublish: !!(previewAfterPublishEl?.checked),
  //   };
  //   try {
  //     localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
  //     log('✅ Saved config.');
  //   } catch { /* ignore */ }
  // }

//   function saveCfg() {
//   // safe getter for .value (avoids null/undefined errors)
//   const safeVal = (el) => (el && typeof el.value === 'string' ? el.value.trim() : '');
//   const safeCheck = (el) => !!(el && el.checked);

//   const cfg = {
//     apiBase: safeVal(apiBase),
//     loopId: safeVal(loopIdEl),
//     threadIdA: safeVal(threadIdAEl),
//     threadIdB: safeVal(threadIdBEl),
//     sbUrl: safeVal(sbUrlEl),
//     sbAnon: safeVal(sbAnonEl),
//     botEmail: safeVal(botEmailEl),
//     botPass: safeVal(botPassEl),
//     sbEmailA: safeVal(sbEmailAEl),
//     sbPassA: safeVal(sbPassAEl),
//     sbEmailB: safeVal(sbEmailBEl),
//     sbPassB: safeVal(sbPassBEl),
//     remember: safeCheck(rememberSessionsEl),
//     previewOnSend: safeCheck(previewOnSendEl),
//     previewAfterPublish: safeCheck(previewAfterPublishEl),
//   };

//   try {
//     localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
//     log('✅ Saved config.');
//   } catch (err) {
//     console.error('saveCfg error:', err);
//     // Optional debug: list any missing or broken elements
//     [
//       'apiBase', 'loopIdEl', 'threadIdAEl', 'threadIdBEl',
//       'sbUrlEl', 'sbAnonEl', 'botEmailEl', 'botPassEl',
//       'sbEmailAEl', 'sbPassAEl', 'sbEmailBEl', 'sbPassBEl',
//       'rememberSessionsEl', 'previewOnSendEl', 'previewAfterPublishEl'
//     ].forEach(id => {
//       try {
//         const el = eval(id);
//         if (!el) console.warn(`⚠️ Missing element: ${id}`);
//       } catch (e) {
//         console.warn(`⚠️ Error checking ${id}:`, e);
//       }
//     });
//   }
// }

  // function loadCfg() {
  //   const raw = localStorage.getItem(STORAGE_KEY);
  //   if (!raw) return;
  //   try {
  //     const cfg = JSON.parse(raw);
  //     if (cfg.apiBase && apiBase) apiBase.value = cfg.apiBase;
  //     if (cfg.loopId && loopIdEl) loopIdEl.value = cfg.loopId;
  //     if (cfg.threadIdA && threadIdAEl) threadIdAEl.value = cfg.threadIdA;
  //     if (cfg.threadIdB && threadIdBEl) threadIdBEl.value = cfg.threadIdB;
  //     if (cfg.sbUrl && sbUrlEl) sbUrlEl.value = cfg.sbUrl;
  //     if (cfg.sbAnon && sbAnonEl) sbAnonEl.value = cfg.sbAnon;
  //     if (cfg.botEmail && botEmailEl) botEmailEl.value = cfg.botEmail;
  //     if (cfg.botPass && botPassEl) botPassEl.value = cfg.botPass;
  //     if (cfg.sbEmailA && sbEmailAEl) sbEmailAEl.value = cfg.sbEmailA;
  //     if (cfg.sbPassA && sbPassAEl) sbPassAEl.value = cfg.sbPassA;
  //     if (cfg.sbEmailB && sbEmailBEl) sbEmailBEl.value = cfg.sbEmailB;
  //     if (cfg.sbPassB && sbPassBEl) sbPassBEl.value = cfg.sbPassB;
  //     if (typeof cfg.remember === 'boolean' && rememberSessionsEl) rememberSessionsEl.checked = cfg.remember;
  //     if (typeof cfg.previewOnSend === 'boolean' && previewOnSendEl) previewOnSendEl.checked = cfg.previewOnSend;
  //     if (typeof cfg.previewAfterPublish === 'boolean' && previewAfterPublishEl) previewAfterPublishEl.checked = cfg.previewAfterPublish;
  //     log('ℹ️ Loaded saved config.');
  //   } catch { /* ignore */ }
  // }

  function saveCfg() {
  // Re-query DOM each call so we never read from stale/null references
  const getEl = (id) => document.getElementById(id);
  const val = (id) => {
    const el = getEl(id);
    return (el && typeof el.value === 'string') ? el.value.trim() : '';
  };
  const checked = (id) => {
    const el = getEl(id);
    return !!(el && el.checked);
  };

  const cfg = {
    apiBase: val('apiBase'),
    loopId: val('loopId'),
    threadIdA: val('threadIdA'),
    threadIdB: val('threadIdB'),
    sbUrl: val('sbUrl'),
    sbAnon: val('sbAnon'),
    botEmail: val('botEmail'),
    botPass: val('botPass'),
    sbEmailA: val('sbEmailA'),
    sbPassA: val('sbPassA'),
    sbEmailB: val('sbEmailB'),
    sbPassB: val('sbPassB'),
    remember: checked('rememberSessions'),
    previewOnSend: checked('previewOnSend'),
    previewAfterPublish: checked('previewAfterPublish'),
  };

  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
    log('✅ Saved config.');
  } catch (err) {
    console.error('saveCfg error:', err);
  }
}

  function loadCfg() {
    const getEl = (id) => document.getElementById(id);
    const setVal = (id, v) => {
      const el = getEl(id);
      if (el && typeof v === 'string') el.value = v;
    };
    const setChecked = (id, v) => {
      const el = getEl(id);
      if (el && typeof v === 'boolean') el.checked = v;
    };

    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    try {
      const cfg = JSON.parse(raw);
      setVal('apiBase', cfg.apiBase);
      setVal('loopId', cfg.loopId);
      setVal('threadIdA', cfg.threadIdA);
      setVal('threadIdB', cfg.threadIdB);
      setVal('sbUrl', cfg.sbUrl);
      setVal('sbAnon', cfg.sbAnon);
      setVal('botEmail', cfg.botEmail);
      setVal('botPass', cfg.botPass);
      setVal('sbEmailA', cfg.sbEmailA);
      setVal('sbPassA', cfg.sbPassA);
      setVal('sbEmailB', cfg.sbEmailB);
      setVal('sbPassB', cfg.sbPassB);
      setChecked('rememberSessions', cfg.remember);
      setChecked('previewOnSend', cfg.previewOnSend);
      setChecked('previewAfterPublish', cfg.previewAfterPublish);
      log('ℹ️ Loaded saved config.');
    } catch { /* ignore */ }
  }

  function clearCfg() {
    try { localStorage.removeItem(STORAGE_KEY); } catch {}
    log('🧹 Cleared saved config.');
  }

  // ----------------------- HTTP helpers -------------------------
  function assert(v, msg) { if (!v) throw new Error(msg); }

  function baseUrl() {
    const raw = (apiBase?.value || DEFAULTS.API_BASE).trim();
    const clean = raw.replace(/\/+$/, '');
    assert(/^https?:\/\//.test(clean), 'API Base must be http(s) URL.');
    return clean;
  }

  async function apiPost(path, body, headers = {}) {
    const url = `${baseUrl()}${path}`;
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(body ?? {})
    });
    const text = await res.text();
    let json; try { json = JSON.parse(text); } catch { json = text; }
    if (!res.ok) { const e = new Error(`HTTP ${res.status} ${res.statusText}`); e.response = json; throw e; }
    return json;
  }

  async function apiGet(path, headers = {}) {
    const url = `${baseUrl()}${path}`;
    const res = await fetch(url, { method: 'GET', headers: { ...headers } });
    const text = await res.text();
    let json; try { json = JSON.parse(text); } catch { json = text; }
    if (!res.ok) { const e = new Error(`HTTP ${res.status} ${res.statusText}`); e.response = json; throw e; }
    return json;
  }

  // ----------------------- JWT helpers --------------------------
  function getUserJwtById(uid) {
    const aId = userAId?.value?.trim();
    const bId = userBId?.value?.trim();
    if (uid && aId && uid === aId) return (jwtAEl?.value || '').trim();
    if (uid && bId && uid === bId) return (jwtBEl?.value || '').trim();
    return '';
  }
  function getOperatorJwt() { return (jwtOpEl?.value || '').trim(); }

  // ----------------------- Rendering ----------------------------
  function renderFeed(container, items) {
    const arr = Array.isArray(items) ? items : [];
    const rows = arr
      .sort((a,b) => new Date(b.created_at) - new Date(a.created_at))
      .map(m => {
        const ts = new Date(m.created_at).toLocaleString();
        const meta = `aud:${m.audience}  by:${(m.created_by||'').slice(0,8)}  to:${(m.recipient_profile_id||'').slice(0,8)}  id:${(m.id||'').slice(0,8)}`;
        return `<div class="msg">
          <div class="small muted">${ts}</div>
          <div style="margin:6px 0 8px 0;">${escapeHtml(m.content ?? '')}</div>
          <div class="small">${meta}</div>
        </div>`;
      }).join('');
    container.innerHTML = rows || `<span class="muted">No messages.</span>`;
  }

  function renderSinglePreview(container, text) {
    container.innerHTML = text
      ? `<div class="msg"><div>${escapeHtml(text)}</div></div>`
      : `<span class="muted">No preview available.</span>`;
  }

  function showNoNewUpdates(container, lastAt) {
    const ts = lastAt ? fmtTime(lastAt) : fmtTime(new Date());
    container.innerHTML = `<span class="muted">No new updates since last refresh at ${ts}.</span>`;
  }

  function extractPreviews(res) {
    const previews = {};
    const items = res?.items ?? [];
    for (const it of items) {
      const list = it.previews || it.proposed || it.bot_to_user_preview || [];
      if (Array.isArray(list)) {
        for (const p of list) {
          const rid = p?.recipient_profile_id || p?.recipient || p?.to;
          const text = p?.content || p?.text;
          if (rid && text) previews[rid] = text;
        }
      }
    }
    return previews;
  }

  function clearPreviewFor(userKey) {
    if (userKey === 'A' && botToAPreview) {
      botToAPreview.innerHTML = '<span class="muted">No preview available.</span>';
    }
    if (userKey === 'B' && botToBPreview) {
      botToBPreview.innerHTML = '<span class="muted">No preview available.</span>';
    }
  }

  function setPreviewMeta(userKey, date) {
    const ts = date ? fmtTime(date) : '--:--:--';
    if (userKey === 'A' && botAPreviewMeta) botAPreviewMeta.textContent = `Last preview — ${ts}`;
    if (userKey === 'B' && botBPreviewMeta) botBPreviewMeta.textContent = `Last preview — ${ts}`;
  }

  function processLimit() { return DEFAULTS.PROCESS_LIMIT; } // simple, no UI field

  function threadForUserId(userId) {
    // Prefer per-user thread fields; else shared thread id
    const aId = userAId?.value?.trim();
    const bId = userBId?.value?.trim();
    const tA = threadIdAEl?.value?.trim();
    const tB = threadIdBEl?.value?.trim();
    if (userId && aId && userId === aId && tA) return tA;
    if (userId && bId && userId === bId && tB) return tB;
    return DEFAULTS.THREAD_ID_SHARED;
  }

  // ----------------------- High-level ops ------------------------
  async function sendAs(userId, text) {
    const tId = threadForUserId(userId);
    assert(tId, 'Thread ID required.');
    assert(userId, 'User ID required.');
    assert(text, 'Message text required.');

    const userJwt = getUserJwtById(userId);
    assert(userJwt, 'Missing JWT for sender. Login or paste the token first.');

    setStatus('sending…');
    const res = await apiPost(
      '/api/send_message',
      { thread_id: tId, user_id: userId, content: text },
      { 'Authorization': `Bearer ${userJwt}` }
    );
    log('📨 /api/send_message →', res);
    setStatus('idle');
    return res;
  }

  async function refreshPreviews() {
    const tId = DEFAULTS.THREAD_ID_SHARED;
    assert(tId, 'Thread ID required.');

    // Operator must be logged in (JWT set by bot login)
    const operatorJwt = getOperatorJwt();
    assert(operatorJwt, 'Missing Bot Operator JWT. Login the bot first.');

    setStatus('previewing…');
    try {
      const res = await apiPost(
        `/api/bot/process?thread_id=${encodeURIComponent(tId)}&limit=${processLimit()}&dry_run=true`,
        {},
        { 'Authorization': `Bearer ${operatorJwt}` }
      );
      log('🤖 preview /api/bot/process (dry_run=true) →', { stats: res?.stats, items: (res?.items||[]).length });
      const previews = extractPreviews(res);
      renderSinglePreview(botToAPreview, previews[userAId.value.trim()] || '');
      renderSinglePreview(botToBPreview, previews[userBId.value.trim()] || '');

      const now = new Date();
      lastState.A.lastPreviewAt = now;
      lastState.B.lastPreviewAt = now;
      setPreviewMeta('A', now);
      setPreviewMeta('B', now);
    } catch (e) {
      log('❌ preview error:', e.message, e.response || '');
    } finally {
      setStatus('idle');
    }
  }

  async function publishThenFetchFor(userKey /* 'A'|'B' */) {
    const tId = DEFAULTS.THREAD_ID_SHARED;
    assert(tId, 'Thread ID required.');
    const operatorJwt = getOperatorJwt();
    assert(operatorJwt, 'Missing Bot Operator JWT. Login the bot first.');

    const recipient = (userKey === 'A') ? userAId.value.trim() : userBId.value.trim();
    const container = (userKey === 'A') ? messagesA : messagesB;

    setStatus('publishing…');
    try {
      await apiPost(
        `/api/bot/process?thread_id=${encodeURIComponent(tId)}&limit=${processLimit()}&dry_run=false`,
        {},
        { 'Authorization': `Bearer ${operatorJwt}` }
      );
      log('✅ published latest bot messages.');
    } catch (e) {
      log('❌ publish error:', e.message, e.response || '');
    } finally {
      setStatus('idle');
    }

    // Fetch and render for the recipient
    let items = [];
    try {
      const userJwt = getUserJwtById(recipient);
      assert(userJwt, 'Missing JWT for recipient. Login or paste the token first.');
      const res = await apiGet(
        `/api/get_messages?thread_id=${encodeURIComponent(tId)}&user_id=${encodeURIComponent(recipient)}`,
        { 'Authorization': `Bearer ${userJwt}` }
      );
      items = Array.isArray(res?.items) ? res.items : [];
      log('📥 /api/get_messages →', `user:${recipient.slice(0,8)} count:${items.length}`);
    } catch (e) {
      log('❌ fetch inbox error:', e.message, e.response || '');
    }

    const botItems = items
      .filter(m => m.audience === 'bot_to_user' && m.recipient_profile_id === recipient)
      .sort((a,b) => new Date(b.created_at) - new Date(a.created_at));

    const newest = botItems[0] || null;
    const state = lastState[userKey];
    const prevId = state.lastBotMsgId;
    state.lastRefresh = new Date();

    if (!newest || newest.id === prevId) {
      showNoNewUpdates(container, state.lastRefresh);
    } else {
      state.lastBotMsgId = newest.id;
      renderFeed(container, items);
      clearPreviewFor(userKey);
    }

    if (previewAfterPublishEl?.checked) {
      try { await refreshPreviews(); } catch (_) {}
    }
  }

  // ----------------------- Supabase ------------------------------
  async function ensureSupabase() {
    const url = (sbUrlEl?.value?.trim() || DEFAULTS.SB_URL);
    const anon = (sbAnonEl?.value?.trim() || DEFAULTS.SB_ANON);
    if (!url || !anon) { log('ℹ️ Fill Supabase URL & Anon key to enable login.'); return false; }
    if (!supabaseClient && window.supabase) {
      supabaseClient = window.supabase.createClient(url, anon, { auth: { persistSession: false } });
      log('🔌 Supabase client initialised.');
    }
    return !!supabaseClient;
  }

  function attachTokenRefresh() {
    if (!supabaseClient) return;
    supabaseClient.auth.onAuthStateChange((_evt, session) => {
      const token = session?.access_token;
      const uid = session?.user?.id;
      if (!token || !uid) return;
      // If bot user, refresh operator token
      if (uid === uidBot && jwtOpEl) {
        jwtOpEl.value = token;
        log('🔄 Bot token refreshed');
      }
      // If A/B, refresh their textareas
      if (uid === uidA && jwtAEl) { jwtAEl.value = token; }
      if (uid === uidB && jwtBEl) { jwtBEl.value = token; }
    });
  }

  async function loginBot() {
    if (!supabaseClient) return;
    try {
      const email = (botEmailEl?.value?.trim() || DEFAULTS.BOT_EMAIL);
      const pass = (botPassEl?.value?.trim() || DEFAULTS.BOT_PASS);
      const { data, error } = await supabaseClient.auth.signInWithPassword({ email, password: pass });
      if (error) throw error;
      const token = data.session.access_token;
      uidBot = data.session.user.id;
      if (jwtOpEl) jwtOpEl.value = token;
      if (botStatusEl) botStatusEl.textContent = `✅ Bot logged in (${email})`;
      log('🔐 Operator token set');
    } catch (e) {
      if (botStatusEl) botStatusEl.textContent = `❌ Bot login failed`;
      log('❌ Bot login failed:', e.message || e);
    }
  }

  async function loginUser(role, email, password) {
    if (!supabaseClient) return null;
    try {
      const { data, error } = await supabaseClient.auth.signInWithPassword({ email, password });
      if (error) throw error;
      const token = data.session.access_token;
      const id = data.session.user.id;

      if (role === 'A') {
        uidA = id;
        if (jwtAEl) jwtAEl.value = token;
        if (userAId) userAId.value = id;
        if (loginAStatus) loginAStatus.textContent = `✅ A logged in (${email})`;
      } else if (role === 'B') {
        uidB = id;
        if (jwtBEl) jwtBEl.value = token;
        if (userBId) userBId.value = id;
        if (loginBStatus) loginBStatus.textContent = `✅ B logged in (${email})`;
      }
      log(`✅ ${role} logged in — id=${id}`);
      return { token, id };
    } catch (e) {
      alert(`${role} login failed: ${e.message || e}`);
      return null;
    }
  }

  // ----------------------- Wiring -------------------------------
  function bind() {
    // Save/Clear
    saveCfgBtn && saveCfgBtn.addEventListener('click', saveCfg);
    clearCfgBtn && clearCfgBtn.addEventListener('click', clearCfg);
    previewOnSendEl && previewOnSendEl.addEventListener('change', saveCfg);
    previewAfterPublishEl && previewAfterPublishEl.addEventListener('change', saveCfg);

    // Supabase init + bot login
    if (btnInitSupabase) btnInitSupabase.addEventListener('click', async () => {
      if (!await ensureSupabase()) return;
      attachTokenRefresh();
      await loginBot();
      saveCfg();
    });
    if (autoLoginBotEl && autoLoginBotEl.checked) {
      (async () => {
        if (!await ensureSupabase()) return;
        attachTokenRefresh();
        await loginBot();
      })();
    }

    // Human login buttons
    btnLoginA && btnLoginA.addEventListener('click', async () => {
      if (!await ensureSupabase()) return;
      attachTokenRefresh();
      const email = (sbEmailAEl?.value?.trim() || DEFAULTS.A_EMAIL);
      const pass = (sbPassAEl?.value?.trim() || DEFAULTS.A_PASS);
      await loginUser('A', email, pass);
      if (rememberSessionsEl?.checked) saveCfg();
    });
    btnLoginB && btnLoginB.addEventListener('click', async () => {
      if (!await ensureSupabase()) return;
      attachTokenRefresh();
      const email = (sbEmailBEl?.value?.trim() || DEFAULTS.B_EMAIL);
      const pass = (sbPassBEl?.value?.trim() || DEFAULTS.B_PASS);
      await loginUser('B', email, pass);
      if (rememberSessionsEl?.checked) saveCfg();
    });

    // Loop/thread save
    btnSaveLoopThread && btnSaveLoopThread.addEventListener('click', () => {
      if (loopIdEl && !loopIdEl.value) loopIdEl.value = DEFAULTS.LOOP_ID;
      if (threadIdAEl && !threadIdAEl.value) threadIdAEl.value = DEFAULTS.THREAD_ID_SHARED;
      if (threadIdBEl && !threadIdBEl.value) threadIdBEl.value = DEFAULTS.THREAD_ID_SHARED;
      if (loopThreadStatus) loopThreadStatus.textContent = 'Saved.';
      saveCfg();
    });

    // Send buttons
    $('sendABtn')?.addEventListener('click', async () => {
      try {
        const text = userAText.value.trim();
        await sendAs(userAId.value.trim(), text);
        userAText.value = '';
        if (previewOnSendEl?.checked !== false) await refreshPreviews();
      } catch (e) { log('❌ send A error:', e.message, e.response || ''); setStatus('error'); }
    });
    $('sendBBtn')?.addEventListener('click', async () => {
      try {
        const text = userBText.value.trim();
        await sendAs(userBId.value.trim(), text);
        userBText.value = '';
        if (previewOnSendEl?.checked !== false) await refreshPreviews();
      } catch (e) { log('❌ send B error:', e.message, e.response || ''); setStatus('error'); }
    });

    // Refresh (publish+fetch) buttons
    $('refreshABtn')?.addEventListener('click', async () => {
      try { await publishThenFetchFor('A'); }
      catch (e) { log('❌ refresh A feed error:', e.message, e.response || ''); setStatus('error'); }
    });
    $('refreshBBtn')?.addEventListener('click', async () => {
      try { await publishThenFetchFor('B'); }
      catch (e) { log('❌ refresh B feed error:', e.message, e.response || ''); setStatus('error'); }
    });

    // Manual preview
    previewNowBtn && previewNowBtn.addEventListener('click', async () => {
      try { await refreshPreviews(); } catch (e) { log('❌ manual preview error:', e.message, e.response || ''); }
    });

    // Manual JWT paste buttons (keep legacy behaviour)
    $('useJwtA')?.addEventListener('click', () => log('🔐 A token set'));
    $('useJwtB')?.addEventListener('click', () => log('🔐 B token set'));
    $('useJwtOperator')?.addEventListener('click', () => log('🔐 Operator token set'));
  }

  // ----------------------- Init -------------------------------
  async function init() {
    // Prefill UI with safe defaults on first load
    if (apiBase && !apiBase.value) apiBase.value = DEFAULTS.API_BASE;
    if (loopIdEl && !loopIdEl.value) loopIdEl.value = DEFAULTS.LOOP_ID;
    if (threadIdAEl && !threadIdAEl.value) threadIdAEl.value = DEFAULTS.THREAD_ID_SHARED;
    if (threadIdBEl && !threadIdBEl.value) threadIdBEl.value = DEFAULTS.THREAD_ID_SHARED;

    if (sbUrlEl && !sbUrlEl.value) sbUrlEl.value = DEFAULTS.SB_URL;
    if (sbAnonEl && !sbAnonEl.value) sbAnonEl.value = DEFAULTS.SB_ANON;
    if (botEmailEl && !botEmailEl.value) botEmailEl.value = DEFAULTS.BOT_EMAIL;
    if (botPassEl && !botPassEl.value) botPassEl.value = DEFAULTS.BOT_PASS;
    if (sbEmailAEl && !sbEmailAEl.value) sbEmailAEl.value = DEFAULTS.A_EMAIL;
    if (sbPassAEl && !sbPassAEl.value) sbPassAEl.value = DEFAULTS.A_PASS;
    if (sbEmailBEl && !sbEmailBEl.value) sbEmailBEl.value = DEFAULTS.B_EMAIL;
    if (sbPassBEl && !sbPassBEl.value) sbPassBEl.value = DEFAULTS.B_PASS;

    // Load any saved config after seeding defaults
    loadCfg();

    // If user IDs are empty (first run), seed known IDs (they'll be overwritten by login)
    if (userAId && !userAId.value) userAId.value = DEFAULTS.A_UUID;
    if (userBId && !userBId.value) userBId.value = DEFAULTS.B_UUID;

    bind();

    // Initial feed pulls (if JWTs already present)
    try {
      const aUid = userAId.value.trim();
      const bUid = userBId.value.trim();
      const tId = DEFAULTS.THREAD_ID_SHARED;

      const aJwt = getUserJwtById(aUid);
      const bJwt = getUserJwtById(bUid);

      const [aRes, bRes] = await Promise.all([
        apiGet(`/api/get_messages?thread_id=${encodeURIComponent(tId)}&user_id=${encodeURIComponent(aUid)}`, aJwt ? { 'Authorization': `Bearer ${aJwt}` } : {}),
        apiGet(`/api/get_messages?thread_id=${encodeURIComponent(tId)}&user_id=${encodeURIComponent(bUid)}`, bJwt ? { 'Authorization': `Bearer ${bJwt}` } : {}),
      ]);

      const aItems = Array.isArray(aRes?.items) ? aRes.items : [];
      const bItems = Array.isArray(bRes?.items) ? bRes.items : [];
      if (messagesA) renderFeed(messagesA, aItems);
      if (messagesB) renderFeed(messagesB, bItems);

      const latestBotA = aItems.filter(m => m.audience==='bot_to_user' && m.recipient_profile_id === aUid)
                               .sort((a,b)=> new Date(b.created_at)-new Date(a.created_at))[0];
      const latestBotB = bItems.filter(m => m.audience==='bot_to_user' && m.recipient_profile_id === bUid)
                               .sort((a,b)=> new Date(b.created_at)-new Date(a.created_at))[0];
      lastState.A.lastBotMsgId = latestBotA?.id || null;
      lastState.B.lastBotMsgId = latestBotB?.id || null;

      // Try an initial preview if operator JWT already present
      if (getOperatorJwt()) await refreshPreviews();
    } catch (e) {
      log('❌ initial load error:', e.message, e.response || '');
    }

    setStatus('idle');
    log('🟢 Ready. Send as A/B → previews update immediately. Refresh A/B feed → publish then fetch.');
  }

  document.addEventListener('DOMContentLoaded', init);
})();