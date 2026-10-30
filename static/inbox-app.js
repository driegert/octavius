(function() {
  const $ = (id) => document.getElementById(id);
  const esc = OctaviusApp.escapeHtml;

  const shell = $('shell');
  const scrim = $('scrim');
  const searchInput = $('search');
  const statusChips = $('status-filter');
  const typeFilter = $('type-filter');
  const listNote = $('list-note');
  const itemsEl = $('items');
  const pager = $('pagination');
  const prevBtn = $('prev-btn');
  const nextBtn = $('next-btn');
  const pageWhere = $('page-where');
  const connState = $('conn-state');
  const stageTitle = $('stage-title');
  const stageBody = $('stage-body');
  const emptyState = $('empty-state');
  const itemView = $('item-view');
  const itemKicker = $('item-kicker');
  const itemTitle = $('item-title');
  const itemMeta = $('item-meta');
  const itemActions = $('item-actions');
  const itemNotice = $('item-notice');
  const itemContent = $('item-content');
  const chatEl = $('chat');
  const chatLog = $('chat-log');
  const chatInvite = $('chat-invite');
  const chatOpenBtn = $('chat-open');
  const chatTurns = $('chat-turns');
  const chatReset = $('chat-reset');
  const chatForm = $('chat-form');
  const chatInput = $('chat-input');
  const chatSend = $('chat-send');

  const PAGE_SIZE = 30;
  const NARROW = '(max-width: 860px)';
  const STACKED = '(max-width: 1279px)';
  const TYPE_LABELS = {
    note: 'Note',
    search_summary: 'Search summary',
    article: 'Article',
    email_draft: 'Email draft',
  };

  let offset = 0;
  let status = 'pending';
  let debounceTimer = null;
  let listSeq = 0;
  let detailSeq = 0;
  let items = [];
  let selectedId = null;
  let selectedItem = null;

  // ---------- Markdown (item content is untrusted: web pages, emails) ----------

  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
    try {
      marked.use({
        renderer: {
          // Raw HTML is shown as text, never injected.
          html(token) {
            const raw = typeof token === 'string' ? token : (token && (token.raw || token.text)) || '';
            return esc(raw);
          },
          // Remote images in saved emails/pages are tracking pixels as often as
          // not: show a link to the image instead of loading it.
          image(token) {
            const href = (token && token.href) || '#';
            const label = (token && token.text) || 'image';
            return `<a href="${esc(href)}">[image: ${esc(label)}]</a>`;
          },
        },
        // Only web and mail links survive.
        walkTokens(token) {
          if ((token.type === 'link' || token.type === 'image') && !/^(https?:|mailto:)/i.test(token.href || '')) {
            token.href = '#';
          }
        },
      });
    } catch (_err) {
    }
  }

  function renderMarkdown(text) {
    if (typeof marked !== 'undefined' && text) {
      try {
        return marked.parse(text);
      } catch (_err) {
      }
    }
    return esc(text || '').replace(/\n/g, '<br>');
  }

  // Links in rendered content open in a new tab and carry no referrer.
  function externalLinks(root) {
    root.querySelectorAll('a[href]').forEach((a) => {
      if (a.getAttribute('href') === '#') {
        a.removeAttribute('href');
        return;
      }
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    });
  }

  function isWebUrl(url) {
    return /^https?:\/\//i.test(url || '');
  }

  // ---------- Dates ----------

  function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const diff = Date.now() - d;
    if (diff < 86400000) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (diff < 604800000) return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function formatFullDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function typeLabel(type) {
    return TYPE_LABELS[type] || String(type || 'item').replace(/_/g, ' ');
  }

  // CSS class from a server value: only the known types get a colour.
  function typeClass(type) {
    return Object.prototype.hasOwnProperty.call(TYPE_LABELS, type) ? type : '';
  }

  // ---------- List ----------

  function setListNote(text, tone) {
    listNote.textContent = text || '';
    if (tone) listNote.dataset.tone = tone; else delete listNote.dataset.tone;
  }

  async function loadItems(opts) {
    const { reselect = true } = opts || {};
    const q = searchInput.value.trim();
    const type = typeFilter.value;
    const seq = ++listSeq;

    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (type) params.set('type', type);
    if (status) params.set('status', status);
    params.set('limit', PAGE_SIZE);
    params.set('offset', q ? 0 : offset);

    itemsEl.setAttribute('aria-busy', 'true');
    try {
      const resp = await fetch(`/api/inbox?${params}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (seq !== listSeq) return;
      let rows = Array.isArray(data.items) ? data.items : [];

      if (q) {
        // Search ranks across every type and status (except dismissed, which
        // the server never searches), so apply the visible filters here.
        const all = rows.length;
        rows = rows.filter((it) => (!type || it.item_type === type) && (!status || it.status === status));
        const hidden = all - rows.length;
        let note = `${rows.length} ${rows.length === 1 ? 'match' : 'matches'}, best first`;
        if (hidden) note += `; ${hidden} more outside these filters`;
        if (status === 'dismissed') note = 'Search never looks in dismissed items.';
        setListNote(note);
        pager.hidden = true;
      } else {
        setListNote('');
        pager.hidden = offset === 0 && rows.length < PAGE_SIZE;
        prevBtn.disabled = offset === 0;
        nextBtn.disabled = rows.length < PAGE_SIZE;
        const first = rows.length ? offset + 1 : offset;
        pageWhere.textContent = rows.length ? `${first}–${offset + rows.length}` : '';
      }

      items = rows;
      renderList();
      if (reselect && selectedId != null) markCurrent();
    } catch (_err) {
      if (seq !== listSeq) return;
      items = [];
      itemsEl.innerHTML = '';
      setListNote('Could not load the inbox.', 'error');
      pager.hidden = true;
    } finally {
      if (seq === listSeq) itemsEl.removeAttribute('aria-busy');
    }
  }

  function emptyListText() {
    if (searchInput.value.trim()) return 'Nothing matches that search.';
    if (status === 'pending') return typeFilter.value ? 'Nothing pending of this type.' : 'Nothing pending. All caught up.';
    if (status === 'done') return 'Nothing marked done yet.';
    if (status === 'dismissed') return 'Nothing dismissed.';
    return 'The inbox is empty.';
  }

  // Search snippets are raw markdown chunks: drop the markup characters.
  function plainSnippet(text) {
    return String(text)
      .replace(/(^|\n)\s*(#{1,6}|>|[-*+]|\d+\.)\s+/g, '$1')
      .replace(/\*\*|__|`+|!?\[([^\]]*)\]\([^)]*\)/g, (m, label) => label || '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function renderList() {
    if (!items.length) {
      itemsEl.innerHTML = `<p class="list-empty">${esc(emptyListText())}</p>`;
      return;
    }
    const searching = !!searchInput.value.trim();
    itemsEl.innerHTML = items.map((it) => {
      const id = Number(it.id);
      const statusTag = it.status && it.status !== 'pending'
        ? `<span class="sep"></span><span class="status-tag ${it.status === 'done' ? 'done' : 'dismissed'}">${esc(it.status === 'done' ? 'Done' : it.status === 'dismissed' ? 'Dismissed' : it.status)}</span>`
        : '';
      const snippet = searching && it.content
        ? `<span class="row-snippet">${esc(plainSnippet(it.content))}</span>`
        : '';
      return `<button class="row" role="listitem" data-id="${id}" aria-current="${id === selectedId}">
        <span class="row-title">${esc(it.title || 'Untitled')}</span>
        ${snippet}
        <span class="row-meta">
          <span class="type-tag ${typeClass(it.item_type)}">${esc(typeLabel(it.item_type))}</span>
          <span class="sep"></span><span>${esc(formatDate(it.created_at))}</span>${statusTag}
        </span>
      </button>`;
    }).join('');
  }

  function markCurrent() {
    itemsEl.querySelectorAll('.row').forEach((row) => {
      row.setAttribute('aria-current', String(Number(row.dataset.id) === selectedId));
    });
  }

  itemsEl.addEventListener('click', (e) => {
    const row = e.target.closest('.row');
    if (row) selectItem(Number(row.dataset.id));
  });

  // ---------- The selected item ----------

  function setUrlItem(id) {
    try {
      const url = new URL(location.href);
      if (id == null) url.searchParams.delete('item'); else url.searchParams.set('item', id);
      history.replaceState(null, '', url);
    } catch (_err) {
    }
  }

  function showNotice(text, tone) {
    itemNotice.textContent = text || '';
    itemNotice.hidden = !text;
    if (tone) itemNotice.dataset.tone = tone; else delete itemNotice.dataset.tone;
  }

  async function selectItem(id, opts) {
    const { closeDrawer = true, scrollRow = true } = opts || {};
    if (!Number.isFinite(id)) return;
    const seq = ++detailSeq;
    selectedId = id;
    markCurrent();
    setUrlItem(id);
    if (closeDrawer) closeRail();
    if (scrollRow) {
      const row = itemsEl.querySelector(`.row[data-id="${id}"]`);
      if (row) row.scrollIntoView({ block: 'nearest' });
    }

    // Show what the list already knows while the full text loads.
    const known = items.find((it) => Number(it.id) === id);
    emptyState.hidden = true;
    itemView.hidden = false;
    showNotice('');
    if (known) renderHeader(known);
    itemContent.className = 'prose loading';
    itemContent.textContent = 'Loading…';
    renderChat();
    resetScroll();

    try {
      const resp = await fetch(`/api/inbox/${id}`);
      const data = await resp.json().catch(() => ({}));
      if (seq !== detailSeq) return;
      if (!resp.ok || !data.item) throw new Error(resp.status === 404 ? 'This item no longer exists.' : 'Could not load this item.');
      selectedItem = data.item;
      renderHeader(selectedItem);
      itemContent.className = 'prose';
      itemContent.innerHTML = renderMarkdown(selectedItem.content || '');
      externalLinks(itemContent);
    } catch (err) {
      if (seq !== detailSeq) return;
      selectedItem = null;
      if (!known) {
        itemKicker.textContent = '';
        itemTitle.textContent = 'Unavailable';
        itemMeta.innerHTML = '';
        itemActions.innerHTML = '';
        stageTitle.textContent = 'Inbox';
      }
      itemContent.className = 'prose';
      itemContent.textContent = '';
      showNotice(err.message || 'Could not load this item.', 'error');
    }
  }

  function resetScroll() {
    $('page').scrollTop = 0;
    stageBody.scrollTop = 0;
  }

  function renderHeader(item) {
    const id = Number(item.id);
    const st = item.status || 'pending';
    const parts = [
      `<span class="type-tag ${typeClass(item.item_type)}">${esc(typeLabel(item.item_type))}</span>`,
      `<span>Saved ${esc(formatFullDate(item.created_at))}</span>`,
    ];
    if (st !== 'pending') {
      parts.push(`<span class="status-tag ${st === 'done' ? 'done' : 'dismissed'}">${esc(st === 'done' ? 'Done' : st === 'dismissed' ? 'Dismissed' : st)}</span>`);
    }
    itemKicker.innerHTML = parts.join('<span class="sep"></span>');
    itemTitle.textContent = item.title || 'Untitled';
    stageTitle.textContent = item.title || 'Inbox';
    document.title = `${item.title || 'Item'} · Octavius Inbox`;

    const meta = [];
    if (item.source_url) {
      const src = String(item.source_url);
      meta.push(isWebUrl(src)
        ? `<span><span class="k">Source</span><a href="${esc(src)}" target="_blank" rel="noopener noreferrer">${esc(src)}</a></span>`
        : `<span><span class="k">Source</span>${esc(src)}</span>`);
    }
    const md = item.metadata && typeof item.metadata === 'object' ? item.metadata : null;
    if (md && md.to) meta.push(`<span><span class="k">To</span>${esc(md.to)}</span>`);
    if (md && md.subject) meta.push(`<span><span class="k">Subject</span>${esc(md.subject)}</span>`);
    if (item.conversation_id) meta.push(`<span><span class="k">From</span>conversation #${esc(item.conversation_id)}</span>`);
    itemMeta.innerHTML = meta.join('');

    const actions = [];
    if (st === 'pending') {
      actions.push(`<button class="btn done" data-act="done">Mark done <kbd>e</kbd></button>`);
      actions.push(`<button class="btn" data-act="dismissed">Dismiss</button>`);
    } else {
      actions.push(`<button class="btn" data-act="pending">Restore to pending</button>`);
    }
    if (item.item_type === 'article') {
      actions.push(`<a class="btn" href="/reader?inbox_id=${id}">Read aloud</a>`);
    }
    actions.push('<span class="spacer"></span>');
    actions.push(`<button class="btn danger" data-act="delete">Delete</button>`);
    itemActions.innerHTML = actions.join('');
  }

  itemActions.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn || selectedId == null) return;
    const act = btn.dataset.act;
    if (act === 'delete') deleteItem(selectedId);
    else updateStatus(selectedId, act);
  });

  function clearSelection() {
    detailSeq++;
    selectedId = null;
    selectedItem = null;
    markCurrent();
    setUrlItem(null);
    itemView.hidden = true;
    emptyState.hidden = false;
    stageTitle.textContent = 'Inbox';
    document.title = 'Octavius Inbox';
    renderChat();
  }

  // After an item leaves the current view, open its neighbour.
  async function reloadAfterChange(id) {
    const oldIndex = items.findIndex((it) => Number(it.id) === id);
    await loadItems({ reselect: false });
    if (items.some((it) => Number(it.id) === id)) {
      selectItem(id, { closeDrawer: false });
      return;
    }
    if (!items.length) { clearSelection(); return; }
    const next = items[Math.min(Math.max(oldIndex, 0), items.length - 1)];
    selectItem(Number(next.id), { closeDrawer: false });
  }

  async function updateStatus(id, newStatus) {
    itemActions.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      const resp = await fetch(`/api/inbox/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${resp.status}`);
      }
      await reloadAfterChange(id);
    } catch (err) {
      itemActions.querySelectorAll('button').forEach((b) => { b.disabled = false; });
      showNotice(`Could not update the item: ${err.message}`, 'error');
    }
  }

  async function deleteItem(id) {
    if (!confirm('Permanently delete this item?')) return;
    try {
      const resp = await fetch(`/api/inbox/${id}`, { method: 'DELETE' });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${resp.status}`);
      }
      chats.delete(id);
      await reloadAfterChange(id);
    } catch (err) {
      showNotice(`Could not delete: ${err.message}`, 'error');
    }
  }

  // ---------- Item chat ----------
  // One state per item, kept for the life of the page. `requested` means Dave
  // opened the chat (item_chat_load starts a server-side history session, so
  // it is never sent just because an item was selected).

  const chats = new Map();
  let ws = null;
  let socketController = null;

  function chatState(id) {
    if (!chats.has(id)) chats.set(id, { requested: false, loaded: false, busy: false, status: '', messages: [], rendered: 0 });
    return chats.get(id);
  }

  function socketOpen() {
    return ws && ws.readyState === WebSocket.OPEN;
  }

  function send(payload) {
    if (!socketOpen()) return false;
    ws.send(JSON.stringify(payload));
    return true;
  }

  function openChat(focus) {
    if (selectedId == null) return;
    const st = chatState(selectedId);
    if (!st.requested) {
      st.requested = true;
      st.loaded = false;
      st.status = '';
      send({ type: 'item_chat_load', item_id: selectedId });
    }
    renderChat();
    if (focus) {
      chatInput.focus({ preventScroll: true });
      // Stacked layouts put the chat under the item: bring it into view.
      if (window.matchMedia(STACKED).matches) chatEl.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function renderChat() {
    if (selectedId == null) {
      chatEl.hidden = true;
      stageBody.classList.remove('with-chat');
      return;
    }
    chatEl.hidden = false;
    stageBody.classList.add('with-chat');
    const st = chatState(selectedId);

    chatInvite.hidden = st.requested;
    chatForm.hidden = !st.requested;
    chatReset.hidden = !(st.requested && st.loaded && st.messages.length);

    const html = [];
    st.messages.forEach((m, i) => {
      const enter = i >= st.rendered ? ' turn-enter' : '';
      if (m.role === 'assistant') {
        html.push(`<div class="turn assistant${enter}">${renderMarkdown(m.content)}</div>`);
      } else if (m.role === 'user') {
        html.push(`<div class="turn user${enter}">${esc(m.content)}</div>`);
      } else {
        html.push(`<div class="turn note${m.error ? ' error' : ''}${enter}">${esc(m.content)}</div>`);
      }
    });
    if (st.requested && !st.loaded && !st.status) {
      html.push(`<div class="turn note">${socketOpen() ? 'Opening the conversation…' : 'Waiting for the connection…'}</div>`);
    }
    if (st.busy) {
      html.push(`<div class="turn pending">${esc(st.status || 'Thinking…')}</div>`);
    } else if (st.status) {
      html.push(`<div class="turn note error">${esc(st.status)}</div>`);
    }
    if (st.requested && st.loaded && !st.messages.length && !st.busy && !st.status) {
      html.push('<p class="chat-empty">Ask anything about this item.</p>');
    }
    st.rendered = st.messages.length;
    chatTurns.innerHTML = html.join('');
    externalLinks(chatTurns);
    updateComposer();
  }

  function scrollChatToEnd() {
    const last = chatTurns.lastElementChild;
    if (!last) return;
    if (chatLog.scrollHeight > chatLog.clientHeight + 1) chatLog.scrollTop = chatLog.scrollHeight;
    else last.scrollIntoView({ block: 'nearest' });
  }

  function updateComposer() {
    const st = selectedId != null ? chatState(selectedId) : null;
    const ready = !!(st && st.loaded && !st.busy && socketOpen());
    chatInput.disabled = !(st && st.loaded && !st.busy);
    chatSend.disabled = !ready || !chatInput.value.trim();
    chatInput.placeholder = st && st.busy ? 'Octavius is answering…' : 'Ask about this item';
  }

  function autosize() {
    chatInput.style.height = 'auto';
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 144)}px`;
  }

  function sendChat() {
    if (selectedId == null) return;
    const st = chatState(selectedId);
    const text = chatInput.value.trim();
    if (!text || !st.loaded || st.busy) return;
    if (!send({ type: 'item_chat', item_id: selectedId, text })) return;
    st.messages.push({ role: 'user', content: text });
    st.busy = true;
    st.status = 'Thinking…';
    chatInput.value = '';
    autosize();
    renderChat();
    scrollChatToEnd();
  }

  chatOpenBtn.addEventListener('click', () => openChat(true));
  chatForm.addEventListener('submit', (e) => { e.preventDefault(); sendChat(); });
  chatInput.addEventListener('input', () => { autosize(); updateComposer(); });
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendChat();
    }
  });
  chatReset.addEventListener('click', () => {
    if (selectedId == null) return;
    const st = chatState(selectedId);
    if (st.busy) return;
    if (send({ type: 'item_chat_reset', item_id: selectedId })) {
      st.requested = true;
      st.loaded = false;
      st.messages = [];
      st.rendered = 0;
      st.status = '';
      renderChat();
    }
  });

  function onSocketMessage(msg) {
    if (!msg || msg.item_id == null) return;
    const id = Number(msg.item_id);
    const st = chatState(id);

    if (msg.type === 'item_chat_loaded') {
      st.requested = true;
      st.loaded = true;
      st.busy = false;
      st.status = '';
      st.messages = (msg.messages || []).filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
        .map((m) => ({ role: m.role, content: String(m.content || '') }));
      st.rendered = st.messages.length;  // history arrives without animation
    } else if (msg.type === 'item_chat_status') {
      st.status = String(msg.text || '');
    } else if (msg.type === 'item_chat_response') {
      st.busy = false;
      st.status = '';
      st.messages.push({ role: 'assistant', content: String(msg.text || '') });
    } else {
      return;
    }
    if (id === selectedId) {
      renderChat();
      scrollChatToEnd();
      // The input was disabled while waiting, which dropped its focus: give it
      // back unless Dave has since moved somewhere else.
      const active = document.activeElement;
      if (msg.type === 'item_chat_response' && (!active || active === document.body || chatEl.contains(active))) {
        chatInput.focus({ preventScroll: true });
      }
    }
  }

  function connectWS() {
    socketController = OctaviusApp.createWebSocket({
      heartbeat: true,
      onOpen(_evt, socket) {
        ws = socket;
        connState.dataset.state = 'open';
        connState.textContent = 'Connected';
        // A new socket is a new server session: every item conversation has to
        // be reopened before it will take a message.
        for (const [id, st] of chats) {
          if (!st.requested) continue;
          st.loaded = false;
          if (id === selectedId) send({ type: 'item_chat_load', item_id: id });
          else st.requested = false;
        }
        renderChat();
      },
      onMessage(evt) {
        ws = socketController.getSocket();
        if (typeof evt.data !== 'string') return;
        let msg;
        try { msg = JSON.parse(evt.data); } catch (_err) { return; }
        onSocketMessage(msg);
      },
      onClose() {
        connState.dataset.state = 'closed';
        connState.textContent = 'Reconnecting';
        for (const st of chats.values()) {
          if (st.busy) {
            st.busy = false;
            st.status = '';
            st.messages.push({ role: 'note', error: true, content: 'The connection dropped before the reply arrived.' });
          }
        }
        renderChat();
      },
    });
    ws = socketController.connect();
  }

  // ---------- Filters, search, paging ----------

  searchInput.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => { offset = 0; loadItems(); }, 300);
  });

  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      if (searchInput.value) {
        searchInput.value = '';
        clearTimeout(debounceTimer);
        offset = 0;
        loadItems();
      } else {
        searchInput.blur();
      }
    } else if (e.key === 'Enter' || e.key === 'ArrowDown') {
      e.preventDefault();
      clearTimeout(debounceTimer);
      const go = () => { if (items.length) { searchInput.blur(); selectItem(Number(items[0].id), { closeDrawer: e.key === 'Enter' }); } };
      if (e.key === 'Enter') { offset = 0; loadItems().then(go); } else go();
    }
  });

  statusChips.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    status = chip.dataset.status;
    statusChips.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-checked', String(c === chip)));
    offset = 0;
    loadItems();
  });

  typeFilter.addEventListener('change', () => { offset = 0; loadItems(); });

  prevBtn.addEventListener('click', () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadItems();
    itemsEl.scrollTop = 0;
  });
  nextBtn.addEventListener('click', () => {
    offset += PAGE_SIZE;
    loadItems();
    itemsEl.scrollTop = 0;
  });

  // ---------- Drawer (narrow screens) ----------

  function openRail() {
    shell.classList.add('rail-open');
    scrim.hidden = false;
  }
  function closeRail() {
    shell.classList.remove('rail-open');
    scrim.hidden = true;
  }
  $('rail-open').addEventListener('click', openRail);
  $('rail-close').addEventListener('click', closeRail);
  $('empty-open').addEventListener('click', openRail);
  scrim.addEventListener('click', closeRail);

  // ---------- Keyboard ----------

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable ||
      (tag === 'INPUT' && !['checkbox', 'radio', 'range', 'button'].includes(el.type));
  }

  function step(dir) {
    if (!items.length) return;
    const idx = items.findIndex((it) => Number(it.id) === selectedId);
    let next;
    if (idx === -1) next = dir > 0 ? 0 : items.length - 1;
    else next = Math.min(Math.max(idx + dir, 0), items.length - 1);
    if (next === idx) return;
    selectItem(Number(items[next].id), { closeDrawer: false });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (shell.classList.contains('rail-open')) { closeRail(); return; }
      const active = document.activeElement;
      if (isTypingTarget(active)) { active.blur(); return; }
      if (selectedId != null) clearSelection();
      return;
    }
    if (isTypingTarget(document.activeElement)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.key === '/') {
      event.preventDefault();
      if (window.matchMedia(NARROW).matches) openRail();
      searchInput.focus();
      searchInput.select();
      return;
    }
    if (event.key === 'j' || event.key === 'ArrowDown') { event.preventDefault(); step(1); return; }
    if (event.key === 'k' || event.key === 'ArrowUp') { event.preventDefault(); step(-1); return; }
    if (event.key === 'c' && selectedId != null) { event.preventDefault(); openChat(true); return; }
    if (event.key === 'e' && selectedItem && selectedItem.status === 'pending' && Number(selectedItem.id) === selectedId) {
      event.preventDefault();
      updateStatus(selectedId, 'done');
    }
  });

  // ---------- Boot ----------

  const bootItem = parseInt(new URLSearchParams(location.search).get('item'), 10);
  connectWS();
  loadItems().then(() => {
    if (Number.isFinite(bootItem)) selectItem(bootItem);
    else if (window.matchMedia(NARROW).matches) openRail();
  });
})();
