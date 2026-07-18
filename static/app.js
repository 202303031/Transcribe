const $ = (id) => document.getElementById(id);

const form = $("form");
const goBtn = $("go");
const formError = $("formError");
const progress = $("progress");
const results = $("results");
const askForm = $("askForm");
const askThread = $("askThread");
const askInput = $("askInput");

let currentJobId = null;

const KEYS = "yt-transcriber-keys";

/* ------------------------------------------------------------- keys */

function loadKeys() {
  try {
    return JSON.parse(localStorage.getItem(KEYS)) || {};
  } catch {
    return {};
  }
}

function paintKeyDot() {
  const { openai } = loadKeys();
  $("keyDot").classList.toggle("set", Boolean(openai));
}

$("keysBtn").addEventListener("click", () => {
  const keys = loadKeys();
  $("openaiKey").value = keys.openai || "";
  $("sarvamKey").value = keys.sarvam || "";
  $("keysModal").showModal();
});

$("keysModal").addEventListener("close", () => {
  if ($("keysModal").returnValue !== "save") return;
  localStorage.setItem(
    KEYS,
    JSON.stringify({ openai: $("openaiKey").value.trim(), sarvam: $("sarvamKey").value.trim() })
  );
  paintKeyDot();
});

paintKeyDot();

/* --------------------------------------------------------- progress */

function setStep(name, status, detail) {
  const step = document.querySelector(`.step[data-step="${name}"]`);
  if (!step) return;
  step.classList.remove("running", "done", "error");
  step.classList.add(status);
  if (detail) step.querySelector(".detail").textContent = detail;
}

function failCurrentStep(message) {
  const running = document.querySelector(".step.running");
  if (running) {
    running.classList.remove("running");
    running.classList.add("error");
    running.querySelector(".detail").textContent = message;
  }
}

function resetUI() {
  formError.hidden = true;
  results.hidden = true;
  $("rawPanel").hidden = true;
  $("refinedPanel").hidden = true;
  $("askPanel").hidden = true;
  askThread.innerHTML = "";
  currentJobId = null;
  $("rawText").textContent = "";
  $("refinedText").textContent = "";
  $("videoTitle").textContent = "Working…";
  $("videoMeta").textContent = "";
  document.querySelectorAll(".step").forEach((step) => {
    step.classList.remove("running", "done", "error");
    step.querySelector(".detail").textContent = "Waiting";
  });
  progress.hidden = false;
}

function setBusy(busy) {
  goBtn.disabled = busy;
  goBtn.classList.toggle("busy", busy);
  goBtn.querySelector(".btn-label").textContent = busy ? "Transcribing…" : "Transcribe";
}

function showError(message) {
  formError.textContent = message;
  formError.hidden = false;
}

/* ------------------------------------------------------------- run */

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const keys = loadKeys();
  const language = form.querySelector("input[name=language]:checked").value;

  if (!keys.openai) {
    showError("Add your OpenAI API key first — it powers the refinement pass.");
    return $("keysModal").showModal();
  }
  if (language === "hinglish" && !keys.sarvam) {
    showError("Hinglish transcription runs on Sarvam. Add a Sarvam API key.");
    return $("keysModal").showModal();
  }

  setBusy(true);
  resetUI();
  progress.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const engine = language === "english" ? "OpenAI Whisper" : "Sarvam Saaras v3";
  $("rawEngine").textContent = `Straight from ${engine}`;
  $("refineEngine").textContent = `Rewritten by ${$("refineModel").value}`;

  let jobId;
  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: $("url").value.trim(),
        language,
        openai_key: keys.openai,
        sarvam_key: keys.sarvam || "",
        sarvam_mode: $("sarvamMode").value,
        transcribe_model: $("transcribeModel").value,
        refine_model: $("refineModel").value,
        diarize: $("diarize").checked,
      }),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    jobId = (await res.json()).job_id;
  } catch (err) {
    setBusy(false);
    progress.hidden = true;
    return showError(err.message);
  }

  const events = new EventSource(`/api/jobs/${jobId}/events`);

  events.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);

    switch (msg.type) {
      case "meta":
        $("videoTitle").textContent = msg.title;
        if (msg.duration) $("videoMeta").textContent = formatDuration(msg.duration);
        break;

      case "stage":
        setStep(msg.stage, msg.status, msg.detail);
        break;

      case "transcript":
        results.hidden = false;
        $("rawPanel").hidden = false;
        $("rawText").textContent = msg.text;
        break;

      case "refined":
        $("refinedPanel").hidden = false;
        $("refinedText").textContent = msg.text;
        break;

      case "indexed":
        currentJobId = jobId;
        $("askPanel").hidden = false;
        $("askThread").innerHTML = "";
        break;

      case "error":
        failCurrentStep("Failed");
        showError(msg.message);
        events.close();
        setBusy(false);
        break;

      case "done":
        events.close();
        setBusy(false);
        $("refinedPanel").scrollIntoView({ behavior: "smooth", block: "start" });
        break;
    }
  });

  events.onerror = () => {
    events.close();
    setBusy(false);
    failCurrentStep("Connection lost");
    showError("Lost connection to the server. Is it still running?");
  };
});

/* ----------------------------------------------------------- ask/RAG */

askForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = askInput.value.trim();
  if (!question || !currentJobId) return;

  const qEl = document.createElement("div");
  qEl.className = "ask-q";
  qEl.textContent = question;
  askThread.appendChild(qEl);

  const aEl = document.createElement("div");
  aEl.className = "ask-a pending";
  aEl.textContent = "Thinking…";
  askThread.appendChild(aEl);
  aEl.scrollIntoView({ behavior: "smooth", block: "end" });

  askInput.value = "";
  askInput.disabled = true;

  try {
    const res = await fetch(`/api/jobs/${currentJobId}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    const { answer } = await res.json();
    aEl.classList.remove("pending");
    aEl.textContent = answer;
  } catch (err) {
    aEl.classList.remove("pending");
    aEl.classList.add("failed");
    aEl.textContent = err.message;
  } finally {
    askInput.disabled = false;
    askInput.focus();
  }
});

/* ---------------------------------------------------- copy/download */

document.addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    await navigator.clipboard.writeText($(copy.dataset.copy).textContent);
    const original = copy.textContent;
    copy.textContent = "Copied";
    setTimeout(() => (copy.textContent = original), 1400);
    return;
  }

  const dl = e.target.closest("[data-download]");
  if (dl) {
    const blob = new Blob([$(dl.dataset.download).textContent], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = dl.dataset.name;
    a.click();
    URL.revokeObjectURL(a.href);
  }
});

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : m;
  return `${h ? h + ":" : ""}${mm}:${String(s).padStart(2, "0")}`;
}