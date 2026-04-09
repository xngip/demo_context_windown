const state = {
  conversations: [],
  activeConversationId: null,
  selectedFiles: [],
  backgroundPollTimer: null,
  backgroundPollAttempts: 0,
  composerMode: 'chat',
};

const els = {
  conversationList: document.getElementById('conversationList'),
  conversationTitle: document.getElementById('conversationTitle'),
  conversationMeta: document.getElementById('conversationMeta'),
  messageList: document.getElementById('messageList'),
  chatForm: document.getElementById('chatForm'),
  messageInput: document.getElementById('messageInput'),
  fileInput: document.getElementById('fileInput'),
  previewBar: document.getElementById('previewBar'),
  deleteChatBtn: document.getElementById('deleteChatBtn'),
  memoryBtn: document.getElementById('memoryBtn'),
  memoryDialog: document.getElementById('memoryDialog'),
  memoryContent: document.getElementById('memoryContent'),
  closeMemoryBtn: document.getElementById('closeMemoryBtn'),
  newChatBtn: document.getElementById('newChatBtn'),
  refreshBtn: document.getElementById('refreshBtn'),
  sendBtn: document.getElementById('sendBtn'),
  statusBar: document.getElementById('statusBar'),
  modeSwitch: document.getElementById('modeSwitch'),
  composerModeHint: document.getElementById('composerModeHint'),
  composerHint: document.getElementById('composerHint'),
};

class Typewriter {
  constructor(element, speed = 15) {
    this.element = element;
    this.queue = '';
    this.isTyping = false;
    this.speed = speed;
    this.onFinish = null;
    this.isDoneSignaled = false;
  }

  add(text) {
    this.queue += text;
    if (!this.isTyping) {
      this.type();
    }
  }

  signalDone(callback) {
    this.isDoneSignaled = true;
    if (!this.isTyping && this.queue.length === 0) {
      if (callback) callback();
    } else {
      this.onFinish = callback;
    }
  }

  type() {
    if (this.queue.length > 0) {
      this.isTyping = true;
      const chatContainer = document.getElementById('messageList');
      let isAtBottom = false;
      if (chatContainer) {
        isAtBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight <= 10;
      }

      this.currentText = (this.currentText || '') + this.queue[0];
      this.element.innerHTML = parseMarkdown(this.currentText);
      renderMath(this.element);
      this.queue = this.queue.slice(1);

      if (chatContainer && isAtBottom) {
        chatContainer.scrollTop = chatContainer.scrollHeight;
      }

      let delay = this.speed;
      const lastChar = this.currentText.slice(-1);
      if (['.', '!', '?', '\n'].includes(lastChar)) delay += 20;
      else if ([',', ';'].includes(lastChar)) delay += 10;

      setTimeout(() => this.type(), delay);
    } else {
      this.isTyping = false;
      if (this.isDoneSignaled && this.onFinish) {
        this.onFinish();
      }
    }
  }
}

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

function setStatus(text = '', type = 'info') {
  if (!text) {
    els.statusBar.classList.add('hidden');
    els.statusBar.textContent = '';
    delete els.statusBar.dataset.type;
    return;
  }
  els.statusBar.classList.remove('hidden');
  els.statusBar.textContent = text;
  els.statusBar.dataset.type = type;
}

function escapeHtml(text) {
  return (text || '').replace(/[&<>]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch]));
}

function parseMarkdown(text) {
  if (typeof window.marked !== 'undefined') {
    if (typeof window.marked.parse === 'function') {
      return window.marked.parse(text || '', { breaks: true, gfm: true });
    }
    return window.marked(text || '', { breaks: true, gfm: true });
  }
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*([^\*]+)\*/g, '<em>$1</em>');
  return html;
}

function renderMath(element) {
  if (!element || typeof window.renderMathInElement !== 'function') return;
  try {
    window.renderMathInElement(element, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true },
      ],
      throwOnError: false,
    });
  } catch (e) {
    // silently ignore KaTeX errors
  }
}

function setComposerMode(mode) {
  state.composerMode = mode;
  const buttons = els.modeSwitch?.querySelectorAll('.mode-btn') || [];
  buttons.forEach((btn) => btn.classList.toggle('active', btn.dataset.mode === mode));

  if (mode === 'image') {
    els.messageInput.placeholder = 'Mô tả ảnh cần tạo, hoặc nhập ví dụ: sửa ảnh 2 thành poster tối giản, giữ khuôn mặt gốc';
    els.composerModeHint.textContent = 'Chế độ Nano Banana 2: tạo ảnh mới hoặc chỉnh ảnh từ ảnh vừa upload hay ảnh đã có trong lịch sử hội thoại.';
    els.composerHint.textContent = 'Ở chế độ này chỉ nhận ảnh tham chiếu. Ví dụ: “sửa ảnh 3”, “lấy ảnh gốc thứ 2 rồi đổi nền”.';
    els.fileInput.accept = 'image/*';
  } else {
    els.messageInput.placeholder = 'Nhập câu hỏi... ví dụ: tóm tắt ảnh này, sửa ảnh này thành phong cách anime, hoặc tạo ảnh poster theo mô tả';
    els.composerModeHint.textContent = 'Chế độ chat thường để hỏi đáp, OCR, tóm tắt, retrieval và debug context.';
    els.composerHint.textContent = 'Enter để gửi và Shift Enter để xuống dòng';
    els.fileInput.accept = 'image/*,.pdf,.txt,.docx,.csv';
  }
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

function renderImageAttachment(img) {
  const sourceBadge = img.source_kind === 'assistant_generated'
    ? '<span class="image-badge generated">Nano Banana 2</span>'
    : '<span class="image-badge uploaded">User upload</span>';
  const editBadge = img.edit_generation_index ? `<span class="image-badge generated">Bản #${img.edit_generation_index}</span>` : '';
  const caption = img.display_label || img.short_caption || img.image_type || 'image';
  const processingHint = img.processing_status === 'pending' ? ' · đang phân tích' : '';

  return `
    <div class="message-image-card">
      <img src="${img.url}" alt="${caption}" />
      <div class="image-caption">${caption}${processingHint}</div>
      <div class="image-meta-row">
        ${sourceBadge}
        ${editBadge}
      </div>
    </div>
  `;
}

function renderMessages(detail) {
  els.messageList.classList.remove('empty-state');
  els.messageList.innerHTML = '';

  if (!detail.messages.length) {
    els.messageList.classList.add('empty-state');
    els.messageList.innerHTML = '<div><h3>Chưa có tin nhắn</h3><p>Hãy gửi câu hỏi đầu tiên của bạn.</p></div>';
    return;
  }

  let hasPending = false;
  for (const msg of detail.messages) {
    const meta = msg.metadata || {};
    if (meta.background_enrichment_pending) hasPending = true;

    const div = document.createElement('article');
    div.className = `message ${msg.role}`;
    const imagesHtml = (msg.images || []).map(renderImageAttachment).join('');

    const debugInputs = Array.isArray(meta.model_input_images) ? meta.model_input_images : [];
    const debugHtml = debugInputs.length ? `
      <details class="debug-block">
        <summary>Ảnh được nạp vào model (${debugInputs.length})</summary>
        <div class="debug-list">
          ${debugInputs.map((item, idx) => `
            <div class="debug-item">
              <strong>#${idx + 1}</strong>
              <span>ID: ${item.image_id || 'n/a'}</span>
              <span>Nguồn: ${item.source_kind || 'unknown'}</span>
              <span>Lý do: ${item.reason || 'n/a'}</span>
              ${item.edit_generation_index ? `<span>Bản: ${item.edit_generation_index}</span>` : ''}
              ${item.resolution_type ? `<span>Resolve: ${item.resolution_type}</span>` : ''}
            </div>
          `).join('')}
        </div>
      </details>
    ` : '';

    const docsHtml = (msg.documents || []).map((doc) => `
      <div class="document-attachment">
        <span class="doc-icon">📄</span>
        <div class="doc-info">
            <span class="doc-name">${doc.file_name}</span>
            <span class="doc-status">${doc.processing_status === 'pending' ? 'đang phân tích...' : 'đã lưu ngữ cảnh'}</span>
        </div>
      </div>
    `).join('');

    div.innerHTML = `
      <div class="message-header">
        <strong>${msg.role === 'user' ? 'Bạn' : 'Assistant'}</strong>
      </div>
      <div class="message-text">${parseMarkdown(msg.text || '')}</div>
      ${imagesHtml ? `<div class="message-images">${imagesHtml}</div>` : ''}
      ${docsHtml ? `<div class="message-documents">${docsHtml}</div>` : ''}
      ${debugHtml}
    `;
    const textEl = div.querySelector('.message-text');
    if (textEl) renderMath(textEl);
    els.messageList.appendChild(div);
  }
  els.messageList.scrollTop = els.messageList.scrollHeight;

  if (hasPending) {
    setStatus('Ảnh và bộ nhớ đang được enrich song song ở nền. Câu trả lời đã trả ra trước để giảm thời gian chờ.', 'info');
    scheduleBackgroundRefresh();
  } else {
    clearBackgroundRefresh();
    setStatus('');
  }
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

    if (file.type.startsWith('image/')) {
      const img = document.createElement('img');
      img.src = URL.createObjectURL(file);
      wrap.appendChild(img);
    } else {
      const docIcon = document.createElement('div');
      docIcon.className = 'preview-doc-icon';
      docIcon.textContent = '📄 ' + trimText(file.name, 15);
      wrap.appendChild(docIcon);
    }

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '×';
    btn.addEventListener('click', () => {
      state.selectedFiles.splice(index, 1);
      renderPreview();
    });
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

function appendOptimisticUserMessage(text, files) {
  els.messageList.classList.remove('empty-state');
  const user = document.createElement('article');
  user.className = 'message user';

  const imgFiles = files.filter((f) => f.type.startsWith('image/'));
  const docFiles = files.filter((f) => !f.type.startsWith('image/'));

  const previewImages = imgFiles.map((file) => `
    <div class="message-image-card">
      <img src="${URL.createObjectURL(file)}" alt="preview" />
      <div class="image-caption">${file.name}</div>
      <div class="image-meta-row"><span class="image-badge uploaded">current upload</span></div>
    </div>
  `).join('');

  const previewDocs = docFiles.map((file) => `
      <div class="document-attachment">
        <span class="doc-icon">📄</span>
        <div class="doc-info">
            <span class="doc-name">${file.name}</span>
        </div>
      </div>
  `).join('');

  const modeLine = state.composerMode === 'image'
    ? '<div class="meta-chips"><span class="chip ok">Nano Banana 2 mode</span></div>'
    : '';

  user.innerHTML = `
    <div class="message-header">
      <strong>Bạn</strong>
    </div>
    <div class="message-text">${parseMarkdown(text || '')}</div>
    ${modeLine}
    ${previewImages ? `<div class="message-images">${previewImages}</div>` : ''}
    ${previewDocs ? `<div class="message-documents">${previewDocs}</div>` : ''}
  `;
  els.messageList.appendChild(user);
  els.messageList.scrollTop = els.messageList.scrollHeight;
}

function createStreamingAssistantBubble(label = 'Assistant') {
  const pending = document.createElement('article');
  pending.className = 'message assistant pending streaming';
  pending.id = 'pendingAssistant';
  pending.innerHTML = `
    <div class="message-header">
      <strong>${label}</strong>
    </div>
    <div class="message-text"></div>
  `;
  els.messageList.appendChild(pending);
  els.messageList.scrollTop = els.messageList.scrollHeight;
  return pending;
}

// Exponential backoff delays (ms): check lần 1 sau 8s, lần 2 sau 20s, lần 3 sau 35s
const BG_REFRESH_DELAYS = [8000, 20000, 35000];

function scheduleBackgroundRefresh() {
  if (state.backgroundPollTimer || !state.activeConversationId) return;
  if (state.backgroundPollAttempts >= BG_REFRESH_DELAYS.length) return; // đã hết lượt

  const delay = BG_REFRESH_DELAYS[state.backgroundPollAttempts];
  state.backgroundPollTimer = setTimeout(async () => {
    state.backgroundPollTimer = null;
    if (!state.activeConversationId || els.sendBtn.disabled) return;

    try {
      const detail = await api(`/conversations/${state.activeConversationId}`);
      const stillPending = (detail.messages || []).some(
        (m) => (m.metadata || {}).background_enrichment_pending
      );
      if (!stillPending) {
        renderMessages(detail);
        setStatus('');
        state.backgroundPollAttempts = 0;
      } else {
        state.backgroundPollAttempts += 1;
        scheduleBackgroundRefresh(); // thử lần tiếp theo với delay lớn hơn
      }
    } catch {
      state.backgroundPollAttempts = 0;
    }
  }, delay);
}

function clearBackgroundRefresh() {
  if (state.backgroundPollTimer) {
    clearTimeout(state.backgroundPollTimer);
    state.backgroundPollTimer = null;
  }
  state.backgroundPollAttempts = 0;
}

function parseSseEvent(block) {
  const lines = block.split('\n');
  let event = 'message';
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
  }
  const rawData = dataLines.join('\n');
  let data = {};
  if (rawData) {
    try {
      data = JSON.parse(rawData);
    } catch {
      data = { text: rawData };
    }
  }
  return { event, data };
}

async function streamChat(formData, bubble) {
  const response = await fetch(`/conversations/${state.activeConversationId}/chat/stream`, {
    method: 'POST',
    body: formData,
    headers: { Accept: 'text/event-stream' },
  });

  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finalResult = null;
  const textEl = bubble.querySelector('.message-text');

  const typewriter = new Typewriter(textEl, 2);

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1 || (idx = buffer.indexOf('\r\n\r\n')) !== -1) {
      const block = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + (buffer.substring(idx, idx + 4) === '\r\n\r\n' ? 4 : 2));
      if (!block) continue;
      const parsed = parseSseEvent(block);

      if (parsed.event === 'meta') {
        setStatus('Đang stream câu trả lời...', 'info');
      } else if (parsed.event === 'token') {
        typewriter.add(parsed.data.text || '');
      } else if (parsed.event === 'done') {
        finalResult = parsed.data;
      } else if (parsed.event === 'error') {
        throw new Error(parsed.data.message || 'Streaming error');
      }
    }
  }

  return new Promise((resolve) => {
    typewriter.signalDone(() => {
      resolve(finalResult || {});
    });
  });
}

async function runImageMode(formData, bubble) {
  const textEl = bubble.querySelector('.message-text');
  textEl.innerHTML = parseMarkdown('Đang gửi yêu cầu sang Nano Banana 2...');
  setStatus('Nano Banana 2 đang tạo hoặc chỉnh ảnh...', 'info');
  const result = await api(`/conversations/${state.activeConversationId}/images/generate`, {
    method: 'POST',
    body: formData,
  });
  const message = result?.answer || 'Đã nhận kết quả ảnh.';
  textEl.innerHTML = parseMarkdown(message);
  renderMath(textEl);
  return result;
}

async function sendMessage(event) {
  event.preventDefault();
  const text = els.messageInput.value.trim();
  if (!text && !state.selectedFiles.length) return;
  if (!state.activeConversationId) {
    await createConversation(text ? trimText(text, 40) : 'Cuộc hội thoại mới');
  }

  const selectedFiles = [...state.selectedFiles];
  const formData = new FormData();
  if (text) formData.append('text', text);
  for (const file of selectedFiles) formData.append('files', file);

  els.sendBtn.disabled = true;
  appendOptimisticUserMessage(text, selectedFiles);
  const bubble = createStreamingAssistantBubble(state.composerMode === 'image' ? 'Nano Banana 2' : 'Assistant');

  els.messageInput.value = '';
  state.selectedFiles = [];
  renderPreview();

  try {
    let result;
    if (state.composerMode === 'image') {
      result = await runImageMode(formData, bubble);
    } else {
      result = await streamChat(formData, bubble);
    }

    await loadConversations();
    await loadConversation(state.activeConversationId);

    const latency = result?.latency_ms || 0;
    const pending = result?.background_enrichment_started ? ' Memory tiếp tục enrich ở nền.' : '';
    if (state.composerMode === 'image') {
      setStatus(`Nano Banana 2 đã xử lý xong trong khoảng ${latency} ms.${pending}`, 'info');
    } else {
      setStatus(`Đã stream xong trong khoảng ${latency} ms.${pending}`, 'info');
    }
    if (result?.background_enrichment_started) scheduleBackgroundRefresh();
  } catch (error) {
    bubble.classList.remove('pending', 'streaming');
    bubble.classList.add('error-text');
    bubble.querySelector('.message-text').textContent = `Lỗi: ${error.message}`;
    setStatus('Có lỗi khi xử lý yêu cầu.', 'error');
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
els.messageInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});
els.fileInput.addEventListener('change', (event) => {
  let files = Array.from(event.target.files || []);
  if (state.composerMode === 'image') {
    const invalidFiles = files.filter((file) => !file.type.startsWith('image/'));
    if (invalidFiles.length) {
      alert('Ở chế độ Nano Banana 2, bạn chỉ có thể upload ảnh tham chiếu.');
    }
    files = files.filter((file) => file.type.startsWith('image/'));
  }

  const availableSlots = 10 - state.selectedFiles.length;
  if (files.length > availableSlots) {
    alert('Bạn chỉ được gửi tối đa 10 tệp 1 lần.');
    state.selectedFiles.push(...files.slice(0, Math.max(0, availableSlots)));
  } else {
    state.selectedFiles.push(...files);
  }
  event.target.value = '';
  renderPreview();
});
els.memoryBtn.addEventListener('click', showMemory);
els.deleteChatBtn.addEventListener('click', async () => {
  if (!state.activeConversationId) return;
  if (!confirm('Bạn có chắc muốn xóa cuộc hội thoại này? Dữ liệu không thể khôi phục.')) return;
  try {
    const oldBtnText = els.deleteChatBtn.textContent;
    els.deleteChatBtn.textContent = 'Đang xóa...';
    els.deleteChatBtn.disabled = true;
    await api(`/conversations/${state.activeConversationId}`, { method: 'DELETE' });
    state.activeConversationId = null;
    await loadConversations();
    els.conversationTitle.textContent = 'Chọn một cuộc hội thoại';
    els.conversationMeta.textContent = 'Tạo chat mới hoặc chọn lịch sử bên trái';
    els.messageList.classList.add('empty-state');
    els.messageList.innerHTML = '<div><h3>Chưa có tin nhắn</h3><p>Hãy bắt đầu bằng một câu hỏi hoặc tải ảnh lên để demo quản lý ngữ cảnh đa phương thức.</p></div>';
    els.deleteChatBtn.textContent = oldBtnText;
    els.deleteChatBtn.disabled = false;
  } catch (err) {
    alert('Lỗi khi xóa: ' + err.message);
    els.deleteChatBtn.textContent = 'Xóa Chat';
    els.deleteChatBtn.disabled = false;
  }
});
els.closeMemoryBtn.addEventListener('click', () => els.memoryDialog.close());
els.newChatBtn.addEventListener('click', async () => {
  await createConversation();
});
els.refreshBtn.addEventListener('click', async () => {
  await loadConversations();
  if (state.activeConversationId) await loadConversation(state.activeConversationId);
});

els.modeSwitch?.querySelectorAll('.mode-btn').forEach((btn) => {
  btn.addEventListener('click', () => setComposerMode(btn.dataset.mode));
});

setComposerMode('chat');
loadConversations().catch((error) => {
  els.messageList.classList.remove('empty-state');
  els.messageList.innerHTML = `<div class="error-text">Không tải được dữ liệu: ${error.message}</div>`;
});
