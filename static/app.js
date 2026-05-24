let currentSession = null;
let blocks = [];
const selected = new Set();

const loadForm = document.querySelector("#load-form");
const pathInput = document.querySelector("#path-input");
const fileInput = document.querySelector("#file-input");
const blockList = document.querySelector("#block-list");
const readerPreview = document.querySelector("#reader-preview");
const originalPreview = document.querySelector("#original-preview");
const previewPane = document.querySelector(".preview-pane");
const generateButton = document.querySelector("#generate");
const selectAllButton = document.querySelector("#select-all");
const selectNoneButton = document.querySelector("#select-none");
const outputDir = document.querySelector("#output-dir");
const filename = document.querySelector("#filename");
const result = document.querySelector("#result");
const statusLine = document.querySelector("#status");
let pathLoadTimer = null;
let loadRequestId = 0;

async function refreshStatus() {
  const response = await fetch("/api/status");
  const status = await response.json();
  statusLine.textContent = status.pdf_available
    ? `Ready. JavaScript will be ignored and removed during cleanup. PDF engine: ${status.pdf_engine}.`
    : "Ready to preview. PDF generation needs WeasyPrint installed correctly.";
}

function setResult(message, isError = false) {
  result.textContent = message;
  result.classList.toggle("error", isError);
}

function pdfNameFromTitle(title) {
  return `${title.replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim() || "document"}.pdf`;
}

function renderBlocks() {
  blockList.innerHTML = "";
  readerPreview.innerHTML = "";

  if (!blocks.length) {
    blockList.className = "block-list empty";
    blockList.textContent = "No readable blocks were found.";
    return;
  }

  blockList.className = "block-list";
  const article = document.createElement("article");
  readerPreview.append(article);

  blocks.forEach((block, index) => {
    selected.add(block.id);

    const row = document.createElement("label");
    row.className = "block-row";
    row.dataset.blockId = block.id;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.addEventListener("change", () => toggleBlock(block.id, checkbox.checked));

    const label = document.createElement("div");
    label.className = "block-label";
    label.innerHTML = `<span>${index + 1}. ${block.tag}</span>${escapeHtml(block.label)}`;
    row.append(checkbox, label);
    blockList.append(row);

    const wrapper = document.createElement("section");
    wrapper.className = "reader-block";
    wrapper.dataset.blockId = block.id;
    wrapper.innerHTML = block.html;
    wrapper.addEventListener("click", () => {
      const next = !selected.has(block.id);
      checkbox.checked = next;
      toggleBlock(block.id, next);
    });
    article.append(wrapper);
  });
}

function toggleBlock(id, keep) {
  if (keep) {
    selected.add(id);
  } else {
    selected.delete(id);
  }
  document.querySelectorAll(`[data-block-id="${CSS.escape(id)}"]`).forEach((element) => {
    element.classList.toggle("removed", !keep && element.classList.contains("reader-block"));
    if (element.classList.contains("block-row")) {
      element.style.opacity = keep ? "1" : "0.55";
    }
  });
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  }[char]));
}

function resetLoadedDocument() {
  currentSession = null;
  blocks = [];
  selected.clear();
  blockList.className = "block-list empty";
  blockList.textContent = "Load an HTML file to begin.";
  readerPreview.innerHTML = "";
  originalPreview.removeAttribute("src");
  generateButton.disabled = true;
  selectAllButton.disabled = true;
  selectNoneButton.disabled = true;
}

function looksLikeHtmlPath(path) {
  return /\.(html?|HTML?)$/.test(path.trim());
}

async function loadDocument(source) {
  const requestId = ++loadRequestId;
  setResult("");
  statusLine.textContent = "Loading and cleaning the page...";
  selected.clear();
  generateButton.disabled = true;
  selectAllButton.disabled = true;
  selectNoneButton.disabled = true;

  const form = new FormData();
  if (source === "file" && fileInput.files[0]) {
    form.append("file", fileInput.files[0]);
  } else if (source === "path" && pathInput.value.trim()) {
    form.append("path", pathInput.value.trim());
  } else {
    resetLoadedDocument();
    statusLine.textContent = "Ready.";
    return;
  }

  const response = await fetch("/api/load", { method: "POST", body: form });
  const data = await response.json();
  if (requestId !== loadRequestId) return;
  if (!response.ok) {
    statusLine.textContent = "Ready.";
    setResult(data.error || "Could not load that HTML file.", true);
    resetLoadedDocument();
    return;
  }

  currentSession = data.session_id;
  blocks = data.blocks;
  filename.value = pdfNameFromTitle(data.title);
  originalPreview.src = data.original_url;
  renderBlocks();
  generateButton.disabled = false;
  selectAllButton.disabled = false;
  selectNoneButton.disabled = false;
  statusLine.textContent = `Loaded ${blocks.length} candidate content blocks from ${data.source_path}`;
}

function schedulePathLoad() {
  window.clearTimeout(pathLoadTimer);
  if (!pathInput.value.trim()) {
    ++loadRequestId;
    resetLoadedDocument();
    refreshStatus();
    return;
  }
  if (!looksLikeHtmlPath(pathInput.value)) return;
  pathLoadTimer = window.setTimeout(() => loadDocument("path"), 450);
}

loadForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (looksLikeHtmlPath(pathInput.value)) {
    window.clearTimeout(pathLoadTimer);
    loadDocument("path");
  }
});

pathInput.addEventListener("input", () => {
  if (fileInput.value) fileInput.value = "";
  schedulePathLoad();
});

pathInput.addEventListener("change", () => {
  if (fileInput.value) fileInput.value = "";
  if (looksLikeHtmlPath(pathInput.value)) {
    window.clearTimeout(pathLoadTimer);
    loadDocument("path");
  }
});

fileInput.addEventListener("change", () => {
  window.clearTimeout(pathLoadTimer);
  if (fileInput.files[0]) {
    pathInput.value = "";
    loadDocument("file");
  }
});

generateButton.addEventListener("click", async () => {
  if (!currentSession) return;
  setResult("Generating PDF...");
  const response = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: currentSession,
      selected_ids: Array.from(selected),
      output_dir: outputDir.value,
      filename: filename.value,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    setResult(data.error || "PDF generation failed.", true);
    return;
  }
  setResult(`Saved ${data.output_path}`);
});

selectAllButton.addEventListener("click", () => {
  blocks.forEach((block) => {
    const checkbox = document.querySelector(`.block-row[data-block-id="${CSS.escape(block.id)}"] input`);
    if (checkbox) checkbox.checked = true;
    toggleBlock(block.id, true);
  });
});

selectNoneButton.addEventListener("click", () => {
  blocks.forEach((block) => {
    const checkbox = document.querySelector(`.block-row[data-block-id="${CSS.escape(block.id)}"] input`);
    if (checkbox) checkbox.checked = false;
    toggleBlock(block.id, false);
  });
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    previewPane.classList.toggle("show-original", tab.dataset.tab === "original");
  });
});

refreshStatus();
