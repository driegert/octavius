(function() {
  const itemsEl = document.getElementById('items');
  const searchInput = document.getElementById('search');
  const typeFilter = document.getElementById('type-filter');
  const statusFilter = document.getElementById('status-filter');
  const prevBtn = document.getElementById('prev-btn');
  const nextBtn = document.getElementById('next-btn');

  const PAGE_SIZE = 30;
  let offset = 0;
  let debounceTimer = null;

  function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const diff = now - d;
    if (diff < 86400000) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    if (diff < 604800000) {
      return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function renderItems(items) {
    if (items.length === 0) {
      itemsEl.innerHTML = '<div class="empty-state">No items found.</div>';
      return;
    }

    itemsEl.innerHTML = '';
    for (const item of items) {
      const card = document.createElement('div');
      card.className = 'card';
      card.dataset.id = item.id;

      let statusHtml = '';
      if (item.status !== 'pending') {
        statusHtml = `<span class="status-badge ${item.status}">${item.status}</span>`;
      }

      card.innerHTML = `
        <div class="card-header">
          <span class="badge ${item.item_type}">${item.item_type.replace('_', ' ')}</span>
          <span class="card-title">${OctaviusApp.escapeHtml(item.title)}</span>
          <span class="card-date">${formatDate(item.created_at)}</span>
          ${statusHtml}
        </div>
        <div class="card-preview">${OctaviusApp.escapeHtml(item.content)}</div>
        <div class="card-full"></div>
        <div class="card-meta"></div>
        <div class="card-actions">
          ${item.status === 'pending' ? `
            <button class="done-btn" onclick="event.stopPropagation(); updateStatus(${item.id}, 'done', this)">Mark done</button>
            <button class="dismiss-btn" onclick="event.stopPropagation(); updateStatus(${item.id}, 'dismissed', this)">Dismiss</button>
          ` : `
            <button onclick="event.stopPropagation(); updateStatus(${item.id}, 'pending', this)">Restore to pending</button>
          `}
          ${item.item_type === 'article' ? `
            <button class="read-btn" onclick="event.stopPropagation(); window.location.href='/reader?inbox_id=${item.id}'">Read aloud</button>
          ` : ''}
          <button class="dismiss-btn" onclick="event.stopPropagation(); deleteItem(${item.id})">Delete</button>
        </div>
        <div class="card-chat">
          <button class="chat-toggle-btn" onclick="event.stopPropagation(); toggleChat(${item.id}, this)">Chat with Octavius</button>
          <div class="chat-panel" id="chat-panel-${item.id}" onclick="event.stopPropagation()">
            <div class="chat-messages" id="chat-msgs-${item.id}"></div>
            <div class="chat-status" id="chat-status-${item.id}"></div>
            <div class="chat-input-row">
              <input type="text" id="chat-input-${item.id}" placeholder="Ask about this item..." onclick="event.stopPropagation()" onkeydown="if(event.key==='Enter'){event.stopPropagation();sendItemChat(${item.id})}">
              <button onclick="event.stopPropagation(); sendItemChat(${item.id})">Send</button>
            </div>
            <button class="chat-reset-btn" onclick="event.stopPropagation(); resetItemChat(${item.id})">Reset chat</button>
          </div>
        </div>
      `;

      card.addEventListener('click', () => toggleExpand(card, item.id));
      itemsEl.appendChild(card);
    }
  }

  async function toggleExpand(card, itemId) {
    if (card.classList.contains('expanded')) {
      card.classList.remove('expanded');
      return;
    }

    document.querySelectorAll('.card.expanded').forEach(c => c.classList.remove('expanded'));

    const fullEl = card.querySelector('.card-full');
    const metaEl = card.querySelector('.card-meta');
    if (!fullEl.dataset.loaded) {
      fullEl.textContent = 'Loading...';
      card.classList.add('expanded');
      try {
        const resp = await fetch(`/api/inbox/${itemId}`);
        const data = await resp.json();
        const item = data.item;
        fullEl.textContent = item.content;
        fullEl.dataset.loaded = '1';

        let metaParts = [];
        if (item.source_url) {
          metaParts.push(`Source: <a href="${OctaviusApp.escapeHtml(item.source_url)}" target="_blank">${OctaviusApp.escapeHtml(item.source_url)}</a>`);
        }
        if (item.metadata) {
          if (item.metadata.to) metaParts.push(`To: ${OctaviusApp.escapeHtml(item.metadata.to)}`);
          if (item.metadata.subject) metaParts.push(`Subject: ${OctaviusApp.escapeHtml(item.metadata.subject)}`);
        }
        if (item.conversation_id) {
          metaParts.push(`Conversation #${item.conversation_id}`);
        }
        metaEl.innerHTML = metaParts.join(' &middot; ');
      } catch {
        fullEl.textContent = 'Failed to load.';
      }
    } else {
      card.classList.add('expanded');
    }
  }

  window.deleteItem = async function(itemId) {
    if (!confirm('Permanently delete this item?')) return;
    try {
      const resp = await fetch(`/api/inbox/${itemId}`, { method: 'DELETE' });
      if (resp.ok) loadItems();
    } catch (e) {
      console.error('Delete failed:', e);
    }
  };

  window.updateStatus = async function(itemId, status, _btn) {
    try {
      const resp = await fetch(`/api/inbox/${itemId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      if (resp.ok) loadItems();
    } catch (e) {
      console.error('Update failed:', e);
    }
  };

  async function loadItems() {
    const q = searchInput.value.trim();
    const type = typeFilter.value;
    const status = statusFilter.value;

    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (type) params.set('type', type);
    if (status) params.set('status', status);
    params.set('limit', PAGE_SIZE);
    params.set('offset', offset);

    itemsEl.innerHTML = '<div class="loading">Loading...</div>';

    try {
      const resp = await fetch(`/api/inbox?${params}`);
      const data = await resp.json();
      renderItems(data.items);
      prevBtn.disabled = offset === 0;
      nextBtn.disabled = data.items.length < PAGE_SIZE;
    } catch {
      itemsEl.innerHTML = '<div class="empty-state">Failed to load items.</div>';
    }
  }

  searchInput.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => { offset = 0; loadItems(); }, 300);
  });

  typeFilter.addEventListener('change', () => { offset = 0; loadItems(); });
  statusFilter.addEventListener('change', () => { offset = 0; loadItems(); });

  prevBtn.addEventListener('click', () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadItems();
  });

  nextBtn.addEventListener('click', () => {
    offset += PAGE_SIZE;
    loadItems();
  });

  loadItems();

  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
  }

  function renderMd(text) {
    if (typeof marked !== 'undefined' && text) return marked.parse(text);
    return OctaviusApp.escapeHtml(text).replace(/\n/g, '<br>');
  }

  let ws = null;
  const chatLoaded = {};

  function connectWS() {
    const socketController = OctaviusApp.createWebSocket({
      onOpen(_evt, socket) {
        ws = socket;
      },
      onMessage(evt) {
        ws = socketController.getSocket();
        if (typeof evt.data !== 'string') return;
        const msg = JSON.parse(evt.data);

        if (msg.type === 'item_chat_loaded') {
          const msgsEl = document.getElementById('chat-msgs-' + msg.item_id);
          const statusEl = document.getElementById('chat-status-' + msg.item_id);
          if (!msgsEl) return;
          msgsEl.innerHTML = '';
          for (const m of (msg.messages || [])) {
            appendChatMsg(msg.item_id, m.role, m.content);
          }
          if (statusEl) statusEl.textContent = '';
        }

        if (msg.type === 'item_chat_status') {
          const statusEl = document.getElementById('chat-status-' + msg.item_id);
          if (statusEl) statusEl.textContent = msg.text;
        }

        if (msg.type === 'item_chat_response') {
          const statusEl = document.getElementById('chat-status-' + msg.item_id);
          if (statusEl) statusEl.textContent = '';
          appendChatMsg(msg.item_id, 'assistant', msg.text);
          const input = document.getElementById('chat-input-' + msg.item_id);
          if (input) {
            input.disabled = false;
            input.focus();
          }
        }
      },
    });
    ws = socketController.connect();
  }

  function appendChatMsg(itemId, role, content) {
    const msgsEl = document.getElementById('chat-msgs-' + itemId);
    if (!msgsEl) return;
    const div = document.createElement('div');
    div.className = 'chat-msg ' + role;
    if (role === 'assistant') div.innerHTML = renderMd(content);
    else div.textContent = content;
    msgsEl.appendChild(div);
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  window.toggleChat = function(itemId, btn) {
    const panel = document.getElementById('chat-panel-' + itemId);
    if (!panel) return;
    const isOpen = panel.classList.toggle('open');
    btn.textContent = isOpen ? 'Hide chat' : 'Chat with Octavius';

    if (isOpen && !chatLoaded[itemId]) {
      chatLoaded[itemId] = true;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'item_chat_load', item_id: itemId }));
      }
    }
  };

  window.sendItemChat = function(itemId) {
    const input = document.getElementById('chat-input-' + itemId);
    if (!input) return;
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    appendChatMsg(itemId, 'user', text);
    input.value = '';
    input.disabled = true;

    ws.send(JSON.stringify({ type: 'item_chat', item_id: itemId, text }));
  };

  window.resetItemChat = function(itemId) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'item_chat_reset', item_id: itemId }));
    chatLoaded[itemId] = true;
  };

  connectWS();
})();
