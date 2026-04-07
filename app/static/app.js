const state = {
  conversations: [],
  activeConversationId: null,
  selectedFiles: [],
};

const els = {
  conversationList: document.getElementById('conversationList'),
  conversationTitle: document.getElementById('conversationTitle'),
  conversationMeta: document.getElementById('conversationMeta'),
  messageList: document.getElementById('messageList'),
  chatForm: document.getElementById('chatForm'),
  messageInput: document.getElementById('messageInput'),
  imageInput: document.getElementById('imageInput'),
  previewBar: document.getElementById('previewBar'),
  memoryBtn: document.getElementById('memoryBtn'),
  memoryDialog: document.getElementById('memoryDialog'),
  memoryContent: document.getElementById('memoryContent'),
  closeMemoryBtn: document.getElementById('closeMemoryBtn'),
  newChatBtn: document.getElementById('newChatBtn'),
  refreshBtn: document.getElementById('refreshBtn'),
  sendBtn: document.getElementById('sendBtn'),
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  const contentType = response.headers.get('content-type') || '';
  return contentType.includes('application/json') ? response.json() : response.text();
}

function formatDate(value) {
  if (!value) return '';
  return new Date(value).toLocaleString('vi-VN');
}

function trimText(text, max = 72) {
  if (!text) return 'Chưa có nội dung';
  return text.length > max ? text.slice(0, max) + '…' : text;
}

function renderConversationList() {
  els.conversationList.innerHTML = '';
  for (const convo of state.conversations) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'conversation-item' + (convo.conversation_id === state.activeConversationId ? ' active' : '');
    item.innerHTML = `
      <h3>${convo.title || 'Cuộc hội thoại mới'}</h3>
      <p>${trimText(convo.last_message || 'Chưa có tin nhắn')}</p>
      <div class="meta">
        <span>${convo.turn_count} turns</span>
        <span>${formatDate(convo.updated_at || convo.created_at)}</span>
      </div>
    `;
    item.addEventListener('click', () => loadConversation(convo.conversation_id));
    els.conversationList.appendChild(item);
  }
}

function renderMessages(detail) {
  els.messageList.classList.remove('empty-state');
  els.messageList.innerHTML = '';

  if (!detail.messages.length) {
    els.messageList.classList.add('empty-state');
    els.messageList.innerHTML = '<div><h3>Chưa có tin nhắn</h3><p>Hãy gửi câu hỏi đầu tiên của bạn.</p></div>';
    return;
  }

  for (const msg of detail.messages) {
    const div = document.createElement('article');
    div.className = `message ${msg.role}`;
    const imagesHtml = (msg.images || []).map((img) => `
      <div>
        <img src="${img.url}" alt="${img.short_caption || 'image'}" />
        <div class="image-caption">${img.short_caption || img.image_type || 'image'}</div>
      </div>
    `).join('');

    div.innerHTML = `
      <div class="message-header">
        <strong>${msg.role === 'user' ? 'Bạn' : 'Assistant'}</strong>
        <span>${formatDate(msg.created_at)}</span>
      </div>
      <div>${msg.text || ''}</div>
      ${imagesHtml ? `<div class="message-images">${imagesHtml}</div>` : ''}
    `;
    els.messageList.appendChild(div);
  }
  els.messageList.scrollTop = els.messageList.scrollHeight;
}

async function loadConversations() {
  const data = await api('/conversations');
  state.conversations = data.conversations || [];
  renderConversationList();

  if (!state.activeConversationId && state.conversations.length) {
    await loadConversation(state.conversations[0].conversation_id);
  }
}

async function loadConversation(conversationId) {
  const detail = await api(`/conversations/${conversationId}`);
  state.activeConversationId = detail.conversation_id;
  els.conversationTitle.textContent = detail.title || 'Cuộc hội thoại';
  els.conversationMeta.textContent = `Tạo lúc ${formatDate(detail.created_at)}`;
  renderConversationList();
  renderMessages(detail);
}

function renderPreview() {
  if (!state.selectedFiles.length) {
    els.previewBar.classList.add('hidden');
    els.previewBar.innerHTML = '';
    return;
  }
  els.previewBar.classList.remove('hidden');
  els.previewBar.innerHTML = '';
  state.selectedFiles.forEach((file, index) => {
    const wrap = document.createElement('div');
    wrap.className = 'preview-item';
    const img = document.createElement('img');
    img.src = URL.createObjectURL(file);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '×';
    btn.addEventListener('click', () => {
      state.selectedFiles.splice(index, 1);
      renderPreview();
    });
    wrap.appendChild(img);
    wrap.appendChild(btn);
    els.previewBar.appendChild(wrap);
  });
}

async function createConversation(title = 'Multimodal Context Demo') {
  const formData = new FormData();
  formData.append('title', title);
  const data = await api('/conversations', { method: 'POST', body: formData });
  await loadConversations();
  await loadConversation(data.conversation_id);
}

async function sendMessage(event) {
  event.preventDefault();
  const text = els.messageInput.value.trim();
  if (!text && !state.selectedFiles.length) return;
  if (!state.activeConversationId) {
    await createConversation(text ? trimText(text, 40) : 'Cuộc hội thoại mới');
  }

  const formData = new FormData();
  if (text) formData.append('text', text);
  for (const file of state.selectedFiles) formData.append('images', file);

  els.sendBtn.disabled = true;
  try {
    await api(`/conversations/${state.activeConversationId}/chat`, {
      method: 'POST',
      body: formData,
    });
    els.messageInput.value = '';
    state.selectedFiles = [];
    renderPreview();
    await loadConversations();
    await loadConversation(state.activeConversationId);
  } catch (error) {
    const div = document.createElement('article');
    div.className = 'message assistant error-text';
    div.textContent = `Lỗi: ${error.message}`;
    els.messageList.appendChild(div);
  } finally {
    els.sendBtn.disabled = false;
  }
}

async function showMemory() {
  if (!state.activeConversationId) return;
  try {
    const data = await api(`/conversations/${state.activeConversationId}/memory`);
    els.memoryContent.textContent = JSON.stringify(data, null, 2);
    els.memoryDialog.showModal();
  } catch (error) {
    els.memoryContent.textContent = error.message;
    els.memoryDialog.showModal();
  }
}

els.chatForm.addEventListener('submit', sendMessage);
els.imageInput.addEventListener('change', (event) => {
  const files = Array.from(event.target.files || []);
  state.selectedFiles.push(...files);
  event.target.value = '';
  renderPreview();
});
els.memoryBtn.addEventListener('click', showMemory);
els.closeMemoryBtn.addEventListener('click', () => els.memoryDialog.close());
els.newChatBtn.addEventListener('click', async () => {
  await createConversation();
});
els.refreshBtn.addEventListener('click', async () => {
  await loadConversations();
  if (state.activeConversationId) await loadConversation(state.activeConversationId);
});

loadConversations().catch((error) => {
  els.messageList.classList.remove('empty-state');
  els.messageList.innerHTML = `<div class="error-text">Không tải được dữ liệu: ${error.message}</div>`;
});
