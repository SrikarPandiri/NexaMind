/* =========================================================================
   NexaMind — front-end application logic
   Handles: conversation list, chat streaming simulation, quick tools,
   markdown + code rendering, theme, search, export, and keyboard shortcuts.
   ========================================================================= */

(() => {
  "use strict";

  // ---- DOM references -----------------------------------------------------
  const sidebar = document.getElementById("sidebar");
  const sidebarScrim = document.getElementById("sidebarScrim");
  const mobileSidebarToggle = document.getElementById("mobileSidebarToggle");
  const collapseSidebarBtn = document.getElementById("collapseSidebarBtn");

  const conversationList = document.getElementById("conversationList");
  const newChatBtn = document.getElementById("newChatBtn");
  const searchInput = document.getElementById("searchInput");
  const searchClearBtn = document.getElementById("searchClearBtn");

  const welcomeScreen = document.getElementById("welcomeScreen");
  const chatStream = document.getElementById("chatStream");
  const conversationTitle = document.getElementById("conversationTitle");

  const messageInput = document.getElementById("messageInput");
  const sendBtn = document.getElementById("sendBtn");
  const charCounter = document.getElementById("charCounter");
  const messageTemplate = document.getElementById("messageTemplate");

  const sidebarTools = document.getElementById("sidebarTools");
  const activeToolBanner = document.getElementById("activeToolBanner");
  const activeToolLabel = document.getElementById("activeToolLabel");
  const clearToolBtn = document.getElementById("clearToolBtn");

  const exportBtn = document.getElementById("exportBtn");
  const clearBtn = document.getElementById("clearBtn");
  const themeToggleBtn = document.getElementById("themeToggleBtn");
  const themeLabel = document.getElementById("themeLabel");
  const commandPopover = document.getElementById("commandPopover");
  const charProgress = document.getElementById("charProgress");
  const toastRegion = document.getElementById("toastRegion");
  const confirmDialog = document.getElementById("confirmDialog");
  const settingsDialog = document.getElementById("settingsDialog");
  const username = document.body.dataset.username || "User";

  const TOOL_NAMES = {
    explain: "Explain", summarize: "Summarize", rewrite: "Rewrite",
    ideas: "Generate Ideas", code: "Generate Code", debug: "Debug Code", study: "Study Assistant",
  };

  // ---- State ---------------------------------------------------------------
  let state = {
    conversationId: null,
    conversations: [],
    activeTool: null,
    isSending: false,
  };

  if (window.marked) {
    marked.setOptions({ breaks: true, gfm: true });
  }

  // =========================================================================
  // Theme
  // =========================================================================
  function initTheme() {
    const saved = localStorage_safe_get("nexamind-theme") || "dark";
    setTheme(saved);
  }
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    themeLabel.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    themeToggleBtn.dataset.tooltip = theme === "dark" ? "Light mode" : "Dark mode";
    localStorage_safe_set("nexamind-theme", theme);
  }
  themeToggleBtn.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    setTheme(current === "dark" ? "light" : "dark");
  });

  // Small wrapper in case localStorage is unavailable (e.g. privacy mode)
  function localStorage_safe_get(key) { try { return localStorage.getItem(key); } catch { return null; } }
  function localStorage_safe_set(key, val) { try { localStorage.setItem(key, val); } catch { /* ignore */ } }

  // =========================================================================
  // Sidebar (mobile + collapse)
  // =========================================================================
  function isMobileLayout() { return window.matchMedia("(max-width: 920px)").matches; }
  function setSidebarCollapsed(collapsed) {
    document.querySelector(".app-shell").classList.toggle("is-collapsed", collapsed);
    collapseSidebarBtn.setAttribute("aria-expanded", String(!collapsed));
    collapseSidebarBtn.dataset.tooltip = collapsed ? "Expand sidebar" : "Collapse sidebar";
    collapseSidebarBtn.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    localStorage_safe_set("nexamind-sidebar-collapsed", String(collapsed));
  }
  function toggleSidebar() {
    if (isMobileLayout()) {
      const isOpen = sidebar.classList.toggle("is-open");
      sidebarScrim.classList.toggle("is-open", isOpen);
      mobileSidebarToggle.setAttribute("aria-expanded", String(isOpen));
      document.body.classList.toggle("drawer-open", isOpen);
      return;
    }
    setSidebarCollapsed(!document.querySelector(".app-shell").classList.contains("is-collapsed"));
  }
  setSidebarCollapsed(localStorage_safe_get("nexamind-sidebar-collapsed") === "true");
  mobileSidebarToggle.addEventListener("click", () => {
    toggleSidebar();
  });
  sidebarScrim.addEventListener("click", closeMobileSidebar);
  function closeMobileSidebar() {
    sidebar.classList.remove("is-open");
    sidebarScrim.classList.remove("is-open");
    mobileSidebarToggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("drawer-open");
  }
  collapseSidebarBtn.addEventListener("click", toggleSidebar);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (sidebar.classList.contains("is-open")) closeMobileSidebar();
      document.getElementById("overflowMenu").hidden = true;
    }
  });
  window.addEventListener("resize", () => {
    if (!isMobileLayout()) closeMobileSidebar();
  });

  // =========================================================================
  // Conversations
  // =========================================================================
  async function loadConversations(query) {
    const url = query ? `/api/conversations?q=${encodeURIComponent(query)}` : "/api/conversations";
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error("Failed to load conversations");
      state.conversations = await res.json();
      renderConversationList();
    } catch (err) {
      conversationList.innerHTML = `<div class="conversation-empty">Couldn't load conversations.</div>`;
    }
  }

  function renderConversationList() {
    if (!state.conversations.length) {
      conversationList.innerHTML = `<div class="conversation-empty">No conversations yet — start one below.</div>`;
      return;
    }
    conversationList.innerHTML = "";
    const pinned = state.conversations.filter((c) => isPinned(c.id));
    if (pinned.length) appendConversationGroup("Pinned", pinned);
    const groups = { Today: [], Yesterday: [], "Previous 7 days": [], Older: [] };
    state.conversations.filter((c) => !isPinned(c.id)).forEach((c) => groups[recencyGroup(c.updated_at || c.created_at)].push(c));
    Object.entries(groups).forEach(([label, items]) => { if (items.length) appendConversationGroup(label, items); });
  }

  function isPinned(id) { return localStorage_safe_get("nexamind-pins")?.split(",").includes(String(id)); }
  function togglePinned(id) {
    const pins = new Set((localStorage_safe_get("nexamind-pins") || "").split(",").filter(Boolean));
    pins.has(String(id)) ? pins.delete(String(id)) : pins.add(String(id));
    localStorage_safe_set("nexamind-pins", [...pins].join(","));
    renderConversationList();
  }
  function recencyGroup(iso) {
    const date = new Date(iso || 0); const now = new Date();
    const day = (value) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
    const delta = Math.floor((day(now) - day(date)) / 86400000);
    return delta <= 0 ? "Today" : delta === 1 ? "Yesterday" : delta <= 7 ? "Previous 7 days" : "Older";
  }
  function appendConversationGroup(label, conversations) {
    const heading = document.createElement("div"); heading.className = "conversation-group-label"; heading.textContent = label;
    conversationList.appendChild(heading);
    conversations.forEach((c) => {
      const item = document.createElement("div");
      item.className = "conversation-item" + (c.id === state.conversationId ? " is-active" : "");
      item.innerHTML = `
        <span class="conv-title">${escapeHtml(c.title)}</span>
        <button class="conv-pin ${isPinned(c.id) ? "is-pinned" : ""}" title="${isPinned(c.id) ? "Unpin" : "Pin conversation"}" aria-label="${isPinned(c.id) ? "Unpin" : "Pin conversation"}">★</button>
        <button class="conv-delete" title="Delete conversation">
          <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6zM19 4h-3.5l-1-1h-5l-1 1H5v2h14z"/></svg>
        </button>`;
      item.addEventListener("click", (e) => {
        if (e.target.closest(".conv-delete,.conv-pin")) return;
        openConversation(c.id, c.title);
        closeMobileSidebar();
      });
      item.querySelector(".conv-pin").addEventListener("click", (e) => { e.stopPropagation(); togglePinned(c.id); });
      item.querySelector(".conv-delete").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!await askConfirm(`Delete "${c.title}"? This can't be undone.`)) return;
        await fetch(`/api/conversations/${c.id}`, { method: "DELETE" });
        if (state.conversationId === c.id) startNewConversation();
        loadConversations(searchInput.value.trim());
      });
      conversationList.appendChild(item);
    });
  }

  async function openConversation(id, title) {
    state.conversationId = id;
    conversationTitle.textContent = title || "Conversation";
    showChatView();
    chatStream.innerHTML = "";
    renderConversationList();

    try {
      const res = await fetch(`/api/conversations/${id}/messages`);
      if (!res.ok) throw new Error();
      const messages = await res.json();
      messages.forEach((m) => appendMessage(m.role, m.content, m.created_at));
      scrollToBottom();
    } catch {
      appendSystemNote("Couldn't load this conversation's messages.", true);
    }
  }

  function startNewConversation() {
    state.conversationId = null;
    conversationTitle.textContent = "New conversation";
    chatStream.innerHTML = "";
    showWelcomeView();
    renderConversationList();
    messageInput.value = "";
    updateCharCounter();
    closeMobileSidebar();
    messageInput.focus();
  }
  newChatBtn.addEventListener("click", startNewConversation);

  let searchDebounce;
  searchInput.addEventListener("input", () => {
    searchClearBtn.hidden = !searchInput.value;
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => loadConversations(searchInput.value.trim()), 300);
  });
  searchClearBtn.addEventListener("click", () => { searchInput.value = ""; searchClearBtn.hidden = true; loadConversations(); searchInput.focus(); });
  function focusConversationSearch() {
    if (!isMobileLayout() && document.querySelector(".app-shell").classList.contains("is-collapsed")) setSidebarCollapsed(false);
    searchInput.focus();
  }
  document.getElementById("sidebarSearch").addEventListener("click", focusConversationSearch);
  document.getElementById("sidebarSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focusConversationSearch(); }
  });
  const userPill = document.querySelector(".user-pill");
  userPill.addEventListener("click", (event) => {
    if (!event.target.closest("button,a")) settingsDialog.hidden = false;
  });
  document.getElementById("profileBtn").addEventListener("click", () => { settingsDialog.hidden = false; });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") { event.preventDefault(); startNewConversation(); }
  });

  // =========================================================================
  // View toggling
  // =========================================================================
  function showWelcomeView() {
    welcomeScreen.hidden = false;
    chatStream.hidden = true;
  }
  function showChatView() {
    welcomeScreen.hidden = true;
    chatStream.hidden = false;
  }

  // =========================================================================
  // Quick tools
  // =========================================================================
  sidebarTools.addEventListener("click", (e) => {
    const chip = e.target.closest(".tool-chip");
    if (!chip) return;
    const tool = chip.dataset.tool;
    if (state.activeTool === tool) {
      clearActiveTool();
    } else {
      setActiveTool(tool);
    }
    messageInput.focus();
  });
  clearToolBtn.addEventListener("click", clearActiveTool);

  function setActiveTool(tool) {
    state.activeTool = tool;
    activeToolBanner.hidden = false;
    activeToolLabel.textContent = `${TOOL_NAMES[tool]} mode is on — your next message will use this tool.`;
    document.querySelectorAll(".tool-chip").forEach((c) => c.classList.toggle("is-active", c.dataset.tool === tool));
  }
  function clearActiveTool() {
    state.activeTool = null;
    activeToolBanner.hidden = true;
    document.querySelectorAll(".tool-chip").forEach((c) => c.classList.remove("is-active"));
  }

  // Welcome screen suggestion cards
  document.querySelectorAll(".suggestion-card").forEach((card) => {
    card.addEventListener("click", () => {
      if (card.dataset.tool) setActiveTool(card.dataset.tool);
      messageInput.value = card.dataset.prompt || "";
      updateCharCounter();
      autoGrow();
      messageInput.focus();
      messageInput.selectionStart = messageInput.selectionEnd = messageInput.value.length;
    });
  });

  // =========================================================================
  // Composer
  // =========================================================================
  function autoGrow() {
    messageInput.style.height = "auto";
    messageInput.style.height = Math.min(messageInput.scrollHeight, 200) + "px";
  }
  function updateCharCounter() {
    const len = messageInput.value.length;
    charCounter.textContent = `${len} / 8000`;
    const ratio = Math.min(len / 8000, 1);
    charProgress.hidden = ratio < 0.8;
    charProgress.style.setProperty("--progress", `${ratio * 100}%`);
    sendBtn.disabled = state.isSending || len === 0 || len > 8000;
  }
  messageInput.addEventListener("input", () => {
    autoGrow(); updateCharCounter();
    const startsCommand = messageInput.value === "/" || (messageInput.value.startsWith("/") && !messageInput.value.includes(" "));
    commandPopover.hidden = !startsCommand;
    if (startsCommand) renderCommandPopover(messageInput.value.slice(1));
  });
  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  sendBtn.addEventListener("click", sendMessage);

  function renderCommandPopover(filter) {
    const tools = Object.entries(TOOL_NAMES).filter(([key, name]) => `${key} ${name}`.toLowerCase().includes(filter.toLowerCase()));
    commandPopover.innerHTML = tools.map(([key, name]) => `<button data-tool="${key}"><span>${name}</span><kbd>/${key}</kbd></button>`).join("");
    commandPopover.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
      setActiveTool(button.dataset.tool); messageInput.value = ""; commandPopover.hidden = true; updateCharCounter(); messageInput.focus();
    }));
  }

  // =========================================================================
  // Sending messages / rendering
  // =========================================================================
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function renderMarkdown(text) {
    let html = window.marked && window.DOMPurify
      ? DOMPurify.sanitize(marked.parse(text))
      : escapeHtml(text).replace(/\n/g, "<br>");
    const wrapper = document.createElement("div");
    wrapper.innerHTML = html;
    wrapper.querySelectorAll("pre code").forEach((block) => {
      if (window.hljs) hljs.highlightElement(block);
      const pre = block.parentElement;
      const wrap = document.createElement("div");
      wrap.className = "code-block-wrap";
      const languageClass = [...block.classList].find((name) => name.startsWith("language-"));
      wrap.dataset.language = languageClass ? languageClass.slice(9) : "code";
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      const copyBtn = document.createElement("button");
      copyBtn.className = "code-copy-btn";
      copyBtn.textContent = "Copy code";
      copyBtn.title = "Copy code";
      copyBtn.setAttribute("aria-label", "Copy code");
      copyBtn.addEventListener("click", async () => {
        if (await copyText(block.textContent)) {
          copyBtn.textContent = "Copied!";
          copyBtn.setAttribute("aria-label", "Copied!");
          setTimeout(() => {
            copyBtn.textContent = "Copy code";
            copyBtn.setAttribute("aria-label", "Copy code");
          }, 1500);
        }
      });
      wrap.appendChild(copyBtn);
    });
    return wrapper.innerHTML;
  }

  function formatTime(iso) {
    if (!iso) return "Just now";
    const value = String(iso).trim();
    const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
    const d = new Date(hasTimezone ? value : `${value}Z`);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function relativeTime(iso) {
    const date = new Date(iso || 0); const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "just now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`; return `${Math.floor(seconds / 86400)}d ago`;
  }

  function avatarInitial() { return username.trim().slice(0, 1).toUpperCase() || "U"; }
  function avatarColor() {
    let hash = 0; for (const character of username) hash = character.charCodeAt(0) + ((hash << 5) - hash);
    return ["#F0B15C", "#4FD6C0", "#E78A9B", "#8BA7F0"][Math.abs(hash) % 4];
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const input = document.createElement("textarea");
        input.value = text;
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        input.select();
        if (!document.execCommand("copy")) throw new Error("Copy failed");
        input.remove();
      }
      return true;
    } catch {
      return false;
    }
  }

  function appendMessage(role, content, createdAt, { allowRegen = false } = {}) {
    const node = messageTemplate.content.cloneNode(true);
    const messageEl = node.querySelector(".message");
    messageEl.classList.add(role === "user" ? "role-user" : "role-assistant");
    node.querySelector(".message-sender").textContent = role === "user" ? "You" : "NexaMind";
    const timeEl = node.querySelector(".message-time"); timeEl.textContent = formatTime(createdAt); timeEl.title = relativeTime(createdAt);
    const avatar = node.querySelector(".message-avatar");
    avatar.textContent = role === "user" ? avatarInitial() : "";
    if (role === "user") avatar.style.background = avatarColor();
    node.querySelector(".message-content").innerHTML = renderMarkdown(content);

    if (content) {
      const copyBtn = node.querySelector(".copy-btn");
      copyBtn.addEventListener("click", () => {
        copyText(content).then((copied) => {
          if (!copied) return;
          copyBtn.style.color = "var(--accent-teal)";
          copyBtn.setAttribute("aria-label", "Copied!");
          setTimeout(() => {
            copyBtn.style.color = "";
            copyBtn.setAttribute("aria-label", "Copy response");
          }, 1200);
        });
      });
    }

    const helpfulBtn = node.querySelector(".helpful-btn");
    if (role === "assistant") {
      helpfulBtn.addEventListener("click", () => { helpfulBtn.classList.toggle("is-selected"); helpfulBtn.textContent = helpfulBtn.classList.contains("is-selected") ? "♥" : "♡"; });
    } else { helpfulBtn.remove(); }
    const editBtn = node.querySelector(".edit-btn");
    if (role === "user") {
      editBtn.hidden = false;
      editBtn.addEventListener("click", () => { messageInput.value = content; updateCharCounter(); autoGrow(); messageInput.focus(); });
    } else { editBtn.remove(); }

    const regenBtn = node.querySelector(".regen-btn");
    if (role === "assistant" && allowRegen) {
      regenBtn.hidden = false;
      regenBtn.addEventListener("click", regenerateLast);
    }

    chatStream.appendChild(node);
    return chatStream.lastElementChild;
  }

  function appendTypingIndicator() {
    const el = document.createElement("div");
    el.className = "message role-assistant";
    el.id = "typingIndicator";
    el.innerHTML = `
      <div class="message-avatar"></div>
      <div class="message-body">
        <div class="message-meta"><span class="message-sender">NexaMind</span></div>
        <div class="message-content"><div class="typing-indicator"></div></div>
      </div>`;
    chatStream.appendChild(el);
    scrollToBottom();
    return el;
  }

  function appendSystemNote(text, isError) {
    const el = document.createElement("div");
    el.className = "system-note" + (isError ? " is-error" : "");
    el.textContent = text;
    chatStream.appendChild(el);
    scrollToBottom();
  }

  function scrollToBottom() {
    chatStream.scrollTop = chatStream.scrollHeight;
  }

  /**
   * Reads a Server-Sent Events stream from a fetch Response body and calls
   * the matching handler for each "event: ... / data: ..." block. EventSource
   * can't be used here because it only supports GET requests, and our chat
   * calls need a POST body.
   */
  async function consumeSSE(response, handlers) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);

        let eventName = "message";
        let dataStr = "";
        rawEvent.split("\n").forEach((line) => {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
        });
        if (!dataStr) continue;

        let data;
        try {
          data = JSON.parse(dataStr);
        } catch {
          continue;
        }
        const handler = handlers[eventName];
        if (handler) handler(data);
      }
    }
  }

  /**
   * Creates a live "typing" message bubble that grows as SSE "chunk" events
   * arrive, and returns helper functions to update and finalize it.
   */
  function createStreamingBubble() {
    const el = appendMessage("assistant", "", new Date().toISOString(), { allowRegen: false });
    const contentEl = el.querySelector(".message-content");
    contentEl.innerHTML = `<div class="typing-indicator"></div>`;
    sendBtn.classList.add("is-streaming");

    let rawText = "";
    let hasContent = false;

    return {
      element: el,
      appendChunk(chunk) {
        if (!hasContent) {
          hasContent = true;
          contentEl.innerHTML = "";
        }
        rawText += chunk;
        contentEl.innerHTML = renderMarkdown(rawText);
        scrollToBottom();
      },
      getText() {
        return rawText;
      },
      finalize(allowRegen) {
        if (!hasContent) {
          el.remove();
          return;
        }
        contentEl.innerHTML = renderMarkdown(rawText);
        if (allowRegen) {
          const regenBtn = el.querySelector(".regen-btn");
          regenBtn.hidden = false;
          regenBtn.addEventListener("click", regenerateLast);
        }
        const copyBtn = el.querySelector(".copy-btn");
        copyBtn.addEventListener("click", () => {
          copyText(rawText).then((copied) => {
            if (!copied) return;
            copyBtn.style.color = "var(--accent-teal)";
            copyBtn.setAttribute("aria-label", "Copied!");
            setTimeout(() => {
              copyBtn.style.color = "";
              copyBtn.setAttribute("aria-label", "Copy response");
            }, 1200);
          });
        });
      },
      fail() {
        sendBtn.classList.remove("is-streaming");
        el.remove();
      },
    };
  }

  async function sendMessage() {
    const text = messageInput.value.trim();
    if (!text || state.isSending) return;

    state.isSending = true;
    sendBtn.disabled = true;

    showChatView();
    appendMessage("user", text, new Date().toISOString());
    scrollToBottom();

    const usedTool = state.activeTool;
    messageInput.value = "";
    autoGrow();
    updateCharCounter();
    clearActiveTool();

    const bubble = createStreamingBubble();
    scrollToBottom();

    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: state.conversationId, tool: usedTool }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        bubble.fail();
        appendSystemNote(data.error || "Something went wrong. Please try again.", true);
        return;
      }

      let streamError = null;
      await consumeSSE(res, {
        meta: (data) => {
          state.conversationId = data.conversation_id;
          conversationTitle.textContent = data.title;
        },
        chunk: (data) => bubble.appendChunk(data.text),
        error: (data) => { streamError = data.error; },
        done: () => {},
      });

      if (streamError) {
        bubble.fail();
        appendSystemNote(streamError, true);
        return;
      }

      bubble.finalize(true);
      scrollToBottom();
      loadConversations(searchInput.value.trim());
    } catch (err) {
      bubble.fail();
      appendSystemNote("We couldn't reach the server. Check your connection and try again.", true);
    } finally {
      sendBtn.classList.remove("is-streaming");
      state.isSending = false;
      updateCharCounter();
    }
  }

  async function regenerateLast() {
    if (!state.conversationId || state.isSending) return;
    state.isSending = true;
    updateCharCounter();

    const assistantMessages = [...chatStream.querySelectorAll(".message.role-assistant")];
    const lastMsg = assistantMessages[assistantMessages.length - 1];
    if (lastMsg) lastMsg.remove();

    const bubble = createStreamingBubble();
    scrollToBottom();

    try {
      const res = await fetch("/api/regenerate/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: state.conversationId }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        bubble.fail();
        appendSystemNote(data.error || "Couldn't regenerate that response.", true);
        return;
      }

      let streamError = null;

      await consumeSSE(res, {
        chunk: (data) => bubble.appendChunk(data.text),
        error: (data) => { streamError = data.error; },
      });

      if (streamError) {
        bubble.fail();
        appendSystemNote(streamError, true);
        return;
      }

      bubble.finalize(true);
      scrollToBottom();
    } catch {
      bubble.fail();
      appendSystemNote("We couldn't reach the server. Please try again.", true);
    } finally {
      state.isSending = false;
      updateCharCounter();
    }
  }

  // =========================================================================
  // Header actions: export + clear
  // =========================================================================
  exportBtn.addEventListener("click", () => {
    if (!state.conversationId) {
      toast("Start a conversation first, then you can export it.", true);
      return;
    }
    const messages = [...chatStream.querySelectorAll(".message")].map((m) => {
      const sender = m.querySelector(".message-sender").textContent;
      const content = m.querySelector(".message-content").innerText;
      return `${sender}:\n${content}\n`;
    });
    const blob = new Blob([messages.join("\n---\n\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(conversationTitle.textContent || "nexamind-conversation").replace(/\s+/g, "_")}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  });

  clearBtn.addEventListener("click", async () => {
    if (!state.conversationId) {
      startNewConversation();
      return;
    }
    if (!await askConfirm("Clear this conversation? This deletes it permanently.")) return;
    await fetch(`/api/conversations/${state.conversationId}`, { method: "DELETE" });
    startNewConversation();
    loadConversations();
  });

  function toast(message, isError = false) {
    const item = document.createElement("div"); item.className = `toast ${isError ? "is-error" : ""}`; item.textContent = message;
    toastRegion.appendChild(item); setTimeout(() => item.remove(), 3200);
  }
  function askConfirm(message) {
    return new Promise((resolve) => {
      document.getElementById("confirmMessage").textContent = message; confirmDialog.hidden = false;
      const finish = (result) => { confirmDialog.hidden = true; cancel.removeEventListener("click", onCancel); accept.removeEventListener("click", onAccept); resolve(result); };
      const cancel = document.getElementById("confirmCancel"); const accept = document.getElementById("confirmAccept");
      const onCancel = () => finish(false); const onAccept = () => finish(true); cancel.addEventListener("click", onCancel); accept.addEventListener("click", onAccept);
    });
  }
  document.getElementById("settingsBtn").addEventListener("click", () => { settingsDialog.hidden = false; });
  document.getElementById("settingsClose").addEventListener("click", () => { settingsDialog.hidden = true; });
  document.getElementById("mobileBackBtn").addEventListener("click", () => { sidebar.classList.add("is-open"); sidebarScrim.classList.add("is-open"); });
  document.getElementById("overflowBtn").addEventListener("click", () => {
    const menu = document.getElementById("overflowMenu"); menu.innerHTML = `<button>Export conversation</button><button>Clear conversation</button>`; menu.hidden = !menu.hidden;
    menu.querySelectorAll("button")[0].onclick = () => exportBtn.click(); menu.querySelectorAll("button")[1].onclick = () => clearBtn.click();
  });

  // =========================================================================
  // Init
  // =========================================================================
  initTheme();
  loadConversations();
  showWelcomeView();
  updateCharCounter();
})();
