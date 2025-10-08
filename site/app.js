// Application Configuration
const CONFIG = {
  // Call the Render API directly (Netlify proxy not required)
  // baseURL: "https://loop-f3oe.onrender.com",
  baseURL: "https://loop-f3oe.onrender.com",  // <-- use local API for testing

  original_demo: {
    loop_id: "e94bd651-5bac-4e39-8537-fe8c788c1475", // TODO: set to the correct loop UUID for the demo
    thread_id: "b01164e6-c719-4fb1-b2d0-85755e7ebf38",
    user_a: "b8d99c3c-0d3a-4773-a324-a6bc60dee64e",
    user_b: "0dd8b495-6a25-440d-a6e4-d8b7a77bc688",
    bot: "b59042b5-9cee-4c20-ad5d-8a0ad42cb374"
  },

  aivl: {
    loop_id: "", // TODO: set to the AIVL loop UUID to enable digests
    thread_id: "86fe2f0e-a4ac-4ef7-a283-a24fe735d49b",
    // Dedicated AIVL bot:
    bot_id: "c9cf9661-346c-4f9d-a549-66137f29d87e",

    // USE PROFILE IDS (from your table's profile_id column)
    users: {
      "Denis":  "2f5cf6cc-3744-49b6-bf9b-7bd2f1ac8fdb",
      "Ravin":  "830800a2-5072-45a3-b3f3-0cf407251584",
      "Kanags": "21520d4c-3c62-46d1-b056-636ca91481a2",
      "Yanan":  "700cf32f-8f98-41f9-8617-43b52f0581e4",
      "Jason":  "ab32f236-a990-4586-a4b6-d32eddcfa754",
      "Arvind": "3c421916-4f3e-49cd-8458-223e85d6bd1d"
    }
  }
};

// Application State
let currentPage = 'home'
let isLoading = false;

// DOM references (initialized on DOMContentLoaded)
let sidebar, sidebarToggle, toast, toastMessage;

// Navigation Initialization
document.addEventListener('DOMContentLoaded', function() {
  // Cache DOM elements
  sidebar = document.getElementById('sidebar');
  sidebarToggle = document.getElementById('sidebarToggle');
  toast = document.getElementById('toast');
  toastMessage = document.getElementById('toast-message');

  initializeNavigation();
  initializeMobileToggle();
  initializeDemoHandlers();
  initializeAIVLHandlers();

  // Navigate to default page
  navigateToPage('home');
});

// Navigation Functions
function initializeNavigation() {
  const navLinks = document.querySelectorAll('.nav-link');
  navLinks.forEach(link => {
    link.addEventListener('click', function(e) {
      e.preventDefault();
      const page = this.getAttribute('data-page');
      if (page) navigateToPage(page);

      // Close sidebar on mobile after navigation
      if (window.innerWidth <= 768) {
        sidebar.classList.remove('open');
      }
    });
  });
}

function navigateToPage(page) {
  currentPage = page;

  // Update active link
  document.querySelectorAll('.nav-link').forEach(link => {
    link.classList.toggle('active', link.getAttribute('data-page') === page);
  });

  // Show the selected page
  document.querySelectorAll('.page').forEach(p => {
    p.classList.toggle('active', p.id === `${page}-page`);
  });

  // When navigating to demo, auto-refresh all panels
  if (page === 'demo') {
    refreshDemoMessages('user_a');
    refreshDemoMessages('user_b');
    // Also render the current digest in the Bot panel (preview)
    refreshBotDigestPreview();
  }

  // When navigating to AIVL, reset UI
  if (page === 'aivl') {
    resetAIVLState();
  }
}

function initializeMobileToggle() {
  sidebarToggle.addEventListener('click', function() {
    sidebar.classList.toggle('open');
  });

  // Close sidebar when clicking outside on mobile
  document.addEventListener('click', function(e) {
    if (window.innerWidth <= 768 &&
        !sidebar.contains(e.target) &&
        !sidebarToggle.contains(e.target) &&
        sidebar.classList.contains('open')) {
      sidebar.classList.remove('open');
    }
  });
}

// function initializeDemoHandlers() {
//   // Send buttons for A/B and Bot (demo uses same handler; Bot send is disabled in HTML)
//   document.querySelectorAll('.send-btn').forEach(btn => {
//     btn.addEventListener('click', function() {
//       const userType = this.getAttribute('data-user') || (this.id === 'aivl-send' ? 'aivl' : null);
//       if (userType && ['user_a', 'user_b'].includes(userType)) {
//         sendDemoMessage(userType);
//       }
//     });
//   });

  function initializeDemoHandlers() {
  // Send buttons for A/B and Bot
  document.querySelectorAll('.send-btn').forEach(btn => {
    btn.addEventListener('click', async function() {
      const who = this.getAttribute('data-user'); // 'user_a' | 'user_b' | 'bot'
      if (!who) return;

      if (who === 'user_a' || who === 'user_b') {
        sendDemoMessage(who);
        return;
      }

      if (who === 'bot') {
        // choose which viewer to summarise FOR (exclude their own posts).
        // For the demo, let’s use User A as the “viewer”; you can switch to B if needed.
        // const forProfileId = CONFIG.original_demo.user_a;
        const activeViewer = document.querySelector('.chat-panel.user-a.active') ? 'user_a' : 'user_b';
        const forProfileId = CONFIG.original_demo[activeViewer];
        const loopId = CONFIG.original_demo.loop_id;
        const threadId = CONFIG.original_demo.thread_id;

        try {
          setLoading(true);
          const { digest_text } = await botPostDigest(loopId, threadId, forProfileId);

          // Update the Bot panel immediately
          const botContainer = document.getElementById('bot-messages');
          displayDigest(botContainer, digest_text || 'No new updates.');

          // Refresh A/B panels so the newly posted bot message appears in the thread
          await Promise.all([
            refreshDemoMessages('user_a'),
            refreshDemoMessages('user_b')
          ]);

          showToast('Loop Bot posted an update');
        } catch (e) {
          console.error(e);
          showToast(e.message || 'Bot digest failed');
        } finally {
          setLoading(false);
        }
      }
    });
  });

  // (keep your existing refresh-btn and Enter-key handlers as-is)
}

  // "How's my Loop?" buttons — Users refresh messages; Bot shows digest
  document.querySelectorAll('.refresh-btn').forEach(btn => {
    btn.addEventListener('click', async function() {
      const userType = this.getAttribute('data-user');
      if (!userType) return;

      if (['user_a', 'user_b'].includes(userType)) {
        refreshDemoMessages(userType);
      } else if (userType === 'bot') {
        await refreshBotDigestPreview();
      }
    });
  });

  // Enter key handlers for demo inputs
  document.querySelectorAll('.chat-input').forEach(input => {
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const containerId = this.id;
        const userType = containerId.includes('user-a') ? 'user_a' :
                         containerId.includes('user-b') ? 'user_b' : null;
        if (userType) sendDemoMessage(userType);
      }
    });
  });


async function refreshBotDigestPreview() {
  try {
    setLoading(true);
    const loopId = CONFIG.original_demo.loop_id;
    const forProfile = CONFIG.original_demo.user_a; // preview digest as seen by User A
    if (!loopId || !forProfile) {
      showToast('Loop ID not configured for demo digest');
      return;
    }
    const data = await fetchFeed(loopId, forProfile, true);
    const botContainer = document.getElementById('bot-messages');
    displayDigest(botContainer, data.digest_text);
  } catch (e) {
    console.warn('Digest fetch failed:', e);
    showToast(e.message || 'Failed to fetch digest');
  } finally {
    setLoading(false);
  }
}

function initializeAIVLHandlers() {
  const userTiles = document.querySelectorAll('.user-tile');
  const backBtn = document.querySelector('.back-btn');
  const refreshBtn = document.getElementById('aivl-refresh');
  const sendBtn = document.getElementById('aivl-send');

  userTiles.forEach(tile => {
    tile.addEventListener('click', () => selectAIVLUser(tile.getAttribute('data-user')));
  });

  backBtn.addEventListener('click', () => showUserSelection());

  refreshBtn.addEventListener('click', () => {
    refreshAIVLMessages();
  });

  sendBtn.addEventListener('click', () => sendAIVLMessage());

  // Enter to send in AIVL
  const aivlInput = document.getElementById('aivl-input');
  aivlInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendAIVLMessage();
    }
  });
}

let currentAIVLUser = null;

function selectAIVLUser(userName) {
  currentAIVLUser = userName;
  document.getElementById('current-user-name').textContent = userName;
  showUserChat();
  refreshAIVLMessages();
}

function showUserSelection() {
  document.getElementById('user-selection').classList.remove('hidden');
  document.getElementById('user-chat').classList.add('hidden');
  currentAIVLUser = null;
}

function resetAIVLState() {
  showUserSelection();
  document.getElementById('aivl-messages').innerHTML = '';
  document.getElementById('aivl-input').value = '';
}

function showUserChat() {
  document.getElementById('user-selection').classList.add('hidden');
  document.getElementById('user-chat').classList.remove('hidden');
}

async function sendDemoMessage(userType) {
  const inputId = `${userType.replace('_', '-')}-input`;
  const input = document.getElementById(inputId);
  const message = input.value.trim();

  if (!message) {
    showToast('Please enter a message');
    return;
  }

  if (isLoading) return;

  const userId = CONFIG.original_demo[userType];
  const threadId = CONFIG.original_demo.thread_id;

  try {
    setLoading(true);

    const response = await fetch(`${CONFIG.baseURL}/api/send_message`, {
      method: 'POST',
      mode: 'cors',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        thread_id: threadId,
        user_id: userId,
        content: message
      })
    });

    if (response.ok) {
      input.value = '';
      showToast('Message sent!');
      // Refresh that user's message list quickly
      setTimeout(() => refreshDemoMessages(userType), 300);
      // Also refresh the Loop Bot digest preview (does not advance last_seen_at)
      try {
        const feed = await fetchFeed(CONFIG.original_demo.loop_id, CONFIG.original_demo.user_a, true);
        const botC = document.getElementById('bot-messages');
        displayDigest(botC, feed.digest_text);
      } catch (e) {
        console.warn('Digest refresh failed:', e);
      }
    } else {
      const err = await safeJson(response);
      throw new Error(err?.detail || 'Failed to send message');
    }
  } catch (error) {
    console.error('Error sending message:', error);
    showToast(error.message || 'Error sending message');
  } finally {
    setLoading(false);
  }
}

async function refreshDemoMessages(userType) {
  const userId = CONFIG.original_demo[userType];
  const threadId = CONFIG.original_demo.thread_id;
  const messagesContainer = document.getElementById(`${userType.replace('_', '-')}-messages`);

  try {
    setLoading(true);

    const response = await fetch(`${CONFIG.baseURL}/api/get_messages?thread_id=${threadId}&user_id=${userId}`, {
      mode: 'cors'
    });

    if (response.ok) {
      const data = await response.json();
      displayMessages(messagesContainer, data.messages || [], userId);
    } else {
      const err = await safeJson(response);
      throw new Error(err?.detail || 'Failed to fetch messages');
    }
  } catch (error) {
    console.error('Error fetching messages:', error);
    showToast(error.message || 'Error loading messages');
  } finally {
    setLoading(false);
  }
}

async function sendAIVLMessage() {
  const input = document.getElementById('aivl-input');
  const message = input.value.trim();

  if (!currentAIVLUser) {
    showToast('Please select a user first');
    return;
  }

  if (!message) {
    showToast('Please enter a message');
    return;
  }

  if (isLoading) return;

  const userId = CONFIG.aivl.users[currentAIVLUser];
  const threadId = CONFIG.aivl.thread_id;

  try {
    setLoading(true);

    const response = await fetch(`${CONFIG.baseURL}/api/send_message`, {
      method: 'POST',
      mode: 'cors',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        thread_id: threadId,
        user_id: userId,
        content: message
      })
    });

    if (response.ok) {
      input.value = '';
      showToast('Message sent!');
      setTimeout(() => refreshAIVLMessages(), 500);
    } else {
      const err = await safeJson(response);
      throw new Error(err?.detail || 'Failed to send message');
    }
  } catch (error) {
    console.error('Error sending message:', error);
    showToast(error.message || 'Error sending message');
  } finally {
    setLoading(false);
  }
}

async function refreshAIVLMessages() {
  if (!currentAIVLUser) return;

  const userId = CONFIG.aivl.users[currentAIVLUser];
  const threadId = CONFIG.aivl.thread_id;
  const messagesContainer = document.getElementById('aivl-messages');

  try {
    setLoading(true);

    if (CONFIG.aivl.loop_id) {
      // Use digest when loop_id is configured
      const feed = await fetchFeed(CONFIG.aivl.loop_id, userId, true);
      displayDigest(messagesContainer, feed.digest_text);
    } else {
      // Fallback to raw message list if loop_id not set
      const response = await fetch(`${CONFIG.baseURL}/api/get_messages?thread_id=${threadId}&user_id=${userId}`, {
        mode: 'cors'
      });

      if (response.ok) {
        const data = await response.json();
        displayMessages(messagesContainer, data.messages || [], userId);
      } else {
        const err = await safeJson(response);
        throw new Error(err?.detail || 'Failed to fetch messages');
      }
    }
  } catch (error) {
    console.error('Error fetching messages:', error);
    showToast(error.message || 'Error loading messages');
  } finally {
    setLoading(false);
  }
}

// --- FEED (digest) helpers ---
// async function fetchFeed(loopId, forProfileId, preview = false) {
//   if (!loopId || !forProfileId) throw new Error('Missing loopId or forProfileId');
//   const url = `${CONFIG.baseURL}/api/feed?loop_id=${loopId}&for_profile_id=${forProfileId}` + (preview ? '&preview=true' : '');
//   const resp = await fetch(url, { mode: 'cors' });
//   if (!resp.ok) {
//     const err = await safeJson(resp);
//     throw new Error((err && err.detail) ? err.detail : `Feed failed (${resp.status})`);
//   }
//   return await resp.json();
// }

async function fetchFeed(loopId, forProfileId, preview=true) {
  const url = `${CONFIG.baseURL}/api/feed?loop_id=${loopId}&for_profile_id=${forProfileId}&preview=${preview}`;
  const res = await fetch(url, { method: 'GET' });
  if (!res.ok) throw new Error('Not Found');
  return await res.json(); // has { digest_text, items_count, ... }
}

// wherever you render the Bot panel:
async function refreshBotDigestPreview() {
  try {
    const data = await fetchFeed(DEMO_LOOP_ID, DEMO_USER_A_ID /* or B */, true);
    renderBotBubble(data.digest_text || 'No new updates.');
  } catch (e) {
    renderBotBubble('Load Failed');
    console.error('Digest fetch failed:', e);
  }
}

function displayDigest(container, digestText) {
  container.innerHTML = '';
  const bubble = document.createElement('div');
  bubble.className = 'message bot';
  bubble.innerHTML = `
    <div class="message-bubble">
      <div class="message-author">Loop Bot</div>
      <div class="message-text">${escapeHtml(digestText || 'No new updates.')}</div>
    </div>`;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

// Message Display Functions
function displayMessages(container, messages, currentUserId) {
  container.innerHTML = '';

  if (messages.length === 0) {
    const emptyMessage = document.createElement('div');
    emptyMessage.className = 'empty-state';
    emptyMessage.textContent = 'No messages yet.';
    container.appendChild(emptyMessage);
    return;
  }

  messages.forEach(msg => {
    const role = msg.role || (msg.created_by === currentUserId ? 'user' : 'assistant');
    const isUser = msg.created_by === currentUserId || role === 'user';
    const isAssistant = !isUser;

    // Decode the simple cipher prefix for display
    const raw = msg.content_ciphertext || '';
    const text = raw.startsWith('cipher:') ? raw.slice(7) : raw;

    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${isUser ? 'user' : 'bot'}`;

    const timestampText = msg.created_at ? new Date(msg.created_at).toLocaleString() : '';

    messageDiv.innerHTML = `
      <div class="message-bubble">
        <div class="message-author">${isUser ? 'You' : 'Loop Bot'}</div>
        <div class="message-text">${escapeHtml(text)}</div>
      </div>
      ${timestampText ? `<div class="message-time">${timestampText}</div>` : ''}
    `;

    container.appendChild(messageDiv);
  });

  // Scroll to bottom
  container.scrollTop = container.scrollHeight;
}

// Utility Functions
function showToast(message, duration = 3000) {
  toastMessage.textContent = message;
  toast.classList.remove('hidden');
  toast.classList.add('show');

  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.classList.add('hidden'), 300);
  }, duration);
}

function setLoading(loading) {
  isLoading = loading;

  document.querySelectorAll('.send-btn, .refresh-btn, .back-btn').forEach(btn => {
    btn.disabled = loading;
    btn.classList.toggle('loading', loading);
  });

  const spinners = document.querySelectorAll('.spinner');
  spinners.forEach(spinner => spinner.style.display = loading ? 'inline-block' : 'none');
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

async function safeJson(response) {
  try { return await response.json(); } catch { return null; }
}

// Handle window resize for responsive behavior
window.addEventListener('resize', function() {
  if (window.innerWidth > 768) {
    sidebar.classList.remove('open');
  }
});

// async function botPostDigest(loopId, threadId, forProfileId) {
//   const res = await fetch(`${CONFIG.baseURL}/api/bot_post_digest`, {
//     method: 'POST',
//     headers: {'Content-Type': 'application/json'},
//     body: JSON.stringify({ loop_id: loopId, thread_id: threadId, for_profile_id: forProfileId })
//   });
//   if (!res.ok) throw new Error(await res.text());
//   return await res.json(); // { ok, message, digest_text }
// }

// on Bot “Send” click:
async function onBotSendClick() {
  try {
    const { digest_text } = await botPostDigest(DEMO_LOOP_ID, DEMO_THREAD_ID, ACTIVE_USER_ID);
    renderBotBubble(digest_text);
    await refreshDemoMessages(); // shows the bot message in the thread
  } catch (e) {
    renderBotBubble('Bot send failed');
    console.error(e);
  }
}

async function botPostDigest(loopId, threadId, forProfileId) {
  const res = await fetch(`${CONFIG.baseURL}/api/bot_post_digest`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ loop_id: loopId, thread_id: threadId, for_profile_id: forProfileId })
  });
  if (!res.ok) {
    // try to surface server error detail if present
    let detail = 'Bot digest failed';
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch {}
    throw new Error(detail);
  }
  return await res.json(); // { ok, message, digest_text }
}