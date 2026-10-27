/* 회의 받아쓰기 - 화면 동작 */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const S = {
  sessionId: null,
  running: false,
  segments: [],
  members: [],
  glossary: { terms: [], replacements: [], meeting_note: "" },
  settings: null,
  archive: { id: null, segments: [], modes: null },
  liveModes: null,                 // 받아쓰기 탭의 얼굴/목소리/맥락 스위치
  audio: new Audio(),
  playingSeg: null,
};

const api = async (url, opts = {}) => {
  const res = await fetch(url, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* 본문 없음 */ }
    throw new Error(msg);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
};

let toastTimer;
function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

// 없어진 칸이 있어도 멈추지 않도록, 있을 때만 글자를 바꿉니다
function setText(sel, text) { const el = $(sel); if (el) el.textContent = text; }

const hms = (t) => {
  t = Math.max(0, t | 0);
  const h = (t / 3600) | 0, m = ((t % 3600) / 60) | 0, s = t % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
};

/* ───────────────── 탭 ───────────────── */
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "archive") loadSessions().catch(e => toast("회의 목록을 불러오지 못했습니다: " + e.message, true));
}));

/* ───────────────── 웹소켓 ───────────────── */
function connect() {
  // 서버가 잠시 꺼져 있어도 계속 다시 붙습니다. (프로그램을 갱신하고 다시 켤 때)
  let ws;
  try {
    ws = new WebSocket(`ws://${location.host}/ws`);
  } catch (e) {
    setTimeout(connect, 1500);
    return;
  }
  ws.onopen = async () => {
    $("#conn").className = "badge on";
    $("#conn").textContent = "연결됨";
    if (!S.settings) await init();          // 처음 접속이 실패했다면 이때 다시 불러옵니다
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* 이미 닫힘 */ } };
  ws.onclose = () => {
    $("#conn").className = "badge off";
    $("#conn").textContent = "연결 끊김";
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
}

function handleEvent(e) {
  switch (e.type) {
    case "segment":
      window.orbPulse?.(.4);
      S.segments.push(e.segment);
      appendSegment(e.segment);
      $("#segCount").textContent = `${S.segments.length}문장`;
      break;
    case "segments_update":
      // 화자 스위치를 바꾸거나, 사회자 지명을 AI가 뒤늦게 알려 주면 이미 올라온 문장을 고칩니다
      applySegmentUpdates(e.segments || [], e.session_id);
      break;
    case "keyword":
      onKeyword(e);
      break;
    case "partial":
      if (e.text) window.orbPulse?.(.22);
      showPartial(e.text);
      if ($("#autoscroll").checked) scrollBottom();
      break;
    case "status":
      applyStatus(e.status);
      break;
    case "face":
      $("#faceNow").textContent = e.name ? `${e.name} (${(e.score * 100) | 0}%)` : "미등록 인물";
      break;
    case "log":
      toast(e.message);
      break;
    case "summary_progress":
      S.summaryJob.message = e.message;
      if (e.session_id === S.archive.id) paintSummaryJob();
      break;
    case "summary_delta":
      S.summaryJob.text = e.text;
      if (e.session_id === S.archive.id) paintSummaryJob();
      break;
    case "summary_done":
      endSummaryJob();
      if (!e.session_id || e.session_id === S.archive.id) {
        $("#summaryStatus").textContent = "요약을 마쳤습니다.";
        showSummary(e.summary);
      }
      loadSessions().catch(() => {});
      break;
    case "memo_progress":
      toast(e.message);
      break;
    case "memo_done":
      toast(`메모·회의 자료를 반영했습니다: 문장 ${e.edits}곳, 화자 이름 ${e.renames}개`);
      if (S.archive.id === e.session_id) openSession(e.session_id).catch(() => {});
      break;
    case "memo_error":
      if (!$("#memo").value.trim()) $("#memo").value = e.memo || "";
      toast("메모 반영 실패: " + e.error, true);
      break;
    case "summary_error":
      endSummaryJob();
      if (!e.session_id || e.session_id === S.archive.id) {
        $("#summaryStatus").textContent = "요약 실패: " + e.error;
        showSummary(S.summaryBefore || "");
      }
      break;
  }
}

function applySegmentUpdates(list, sessionId) {
  list.forEach((up) => {
    if (sessionId === S.sessionId) {
      const live = S.segments.find((s) => s.id === up.id);
      if (live) Object.assign(live, up);
      const row = $("#transcript").querySelector(`.seg[data-id="${up.id}"]`);
      if (row) paintSpeaker(row, up.speaker, up.speaker_source, up.cands);
    }
    if (sessionId === S.archive.id) {
      const arch = S.archive.segments.find((s) => s.id === up.id);
      if (arch) Object.assign(arch, up);
      const row = $("#archTranscript").querySelector(`.seg[data-id="${up.id}"]`);
      if (row) paintSpeaker(row, up.speaker, up.speaker_source, up.cands);
    }
  });
}

/* ───────────────── 화자 구분 스위치 (얼굴 / 목소리 / 맥락) ─────────────────
   켜 둔 방법만으로 화자를 다시 정해 녹취록에 바로 반영합니다. 녹취 중·후 모두 됩니다.
   받아쓰기 탭의 스위치는 다음 회의의 기본값도 함께 바꿉니다. */
function defaultModes() {
  const sp = S.settings?.speaker || {};
  return { face: sp.face_enabled !== false, voice: sp.voice_enabled !== false,
           context: sp.context_enabled !== false };
}

function paintModes(scope) {
  const modes = (scope === "live" ? S.liveModes : S.archive.modes) || defaultModes();
  $$(`.spk-bar[data-scope="${scope}"] [data-mode]`).forEach((b) => {
    b.setAttribute("aria-pressed", String(!!modes[b.dataset.mode]));
  });
}

// want: 켜기(true)/끄기(false)를 정해서 부를 때 씁니다. 빼면 지금 상태를 뒤집습니다.
// fromWatch: 화면 감시 스위치에서 불렀을 때 — 감시는 이미 바뀌었으니 다시 건드리지 않습니다.
async function toggleMode(scope, mode, { want, fromWatch = false } = {}) {
  const sid = scope === "live" ? S.sessionId : S.archive.id;
  if (scope === "archive" && !sid) return toast("먼저 회의를 고르세요", true);
  const cur = (scope === "live" ? S.liveModes : S.archive.modes) || defaultModes();
  const next = want === undefined ? !cur[mode] : want;
  if (!!cur[mode] === next) return;
  const modes = { ...cur, [mode]: next };
  const label = { face: "얼굴", voice: "목소리", context: "맥락" }[mode];
  if (scope === "live") {
    S.liveModes = modes;
    // '얼굴'을 켜면 화면 감시도 함께 켭니다 (감시가 꺼져 있으면 얼굴을 알아볼 수 없으므로)
    // '얼굴'을 끄면 화면 감시도 함께 끕니다 (CPU·화면 캡처를 쓸 이유가 없으므로)
    if (mode === "face" && !fromWatch && $("#faceToggle").checked !== modes.face) {
      $("#faceToggle").checked = modes.face;
      toggleFace(modes.face);
    }
    // 다음 회의의 기본값으로도 저장합니다 (설정 탭의 같은 항목)
    const box = { face: "#setFace", voice: "#setVoice", context: "#setContext" }[mode];
    if ($(box)) { $(box).checked = modes[mode]; saveSettingsNow(); }
  } else {
    S.archive.modes = modes;
  }
  paintModes(scope);
  if (!sid) return toast(`${label} 인식을 ${modes[mode] ? "켰" : "껐"}습니다 (다음 회의부터)`);
  try {
    const r = await api(`/api/sessions/${sid}/speaker-modes`, { method: "POST", body: modes });
    if (scope === "live") S.liveModes = r.modes; else S.archive.modes = r.modes;
    paintModes(scope);
    applySegmentUpdates(r.segments || [], sid);
    const n = (r.segments || []).length;
    let msg = `${label} 인식을 ${modes[mode] ? "켰" : "껐"}습니다 · ${n}문장의 화자가 바뀌었습니다`;
    if (r.job) msg += " · 앞부분을 채우는 중…";
    toast(msg);
    (r.notes || []).forEach((m) => setTimeout(() => toast(m, true), 1800));
  } catch (e) { toast(e.message, true); }
}

$$(".spk-bar [data-mode]").forEach((b) => b.addEventListener("click", () => {
  toggleMode(b.closest(".spk-bar").dataset.scope, b.dataset.mode);
}));

function showTab(name) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (btn && getComputedStyle(btn).display !== "none") btn.click();
}

// 받아쓰기를 마치고(멈춤·파일 끝) 기록·요약으로 넘어갈 때 부릅니다.
async function goToArchive(sessionId) {
  clearLiveTitle();
  await loadSessions();
  if (sessionId) await openSession(sessionId);
  showTab("archive");
}

// 끝난 회의의 제목이 다음 받아쓰기에 남지 않도록 제목 칸을 비웁니다. (기억해 둔 화면 설정에서도)
function clearLiveTitle() {
  clearTimeout(liveTitleTimer);
  liveTitleSent = "";
  $("#title").value = "";
  saveUiPrefs();
}

function applyStatus(st) {
  if (!st) return;
  const wasRunning = S.running;
  // 모델을 불러오는 '준비 중'에도 중지 단추를 보여 줍니다
  S.running = st.state === "starting" || st.state === "running" || st.state === "stopping";
  S.stopping = st.state === "stopping";       // 중지를 눌러 마지막 문장을 정리하는 중
  // 받아쓰기가 끝나면(직접 중지했든, 파일이 끝났든) 바로 기록·요약으로 넘어갑니다
  if (wasRunning && st.state === "finished" && st.id) {
    showPartial("");
    clearTimeout(memoTimer);
    $("#memo").value = "";
    if (S.stopRequested) {
      // 직접 멈췄을 때는 이미 기록·요약으로 넘어가 있습니다. 보고 있으면 최신으로 다시 불러옵니다.
      if (S.archive.id === st.id) openSession(st.id).catch(() => {}); else loadSessions().catch(() => {});
    } else {
      goToArchive(st.id).catch(() => {});              // 파일이 끝나 저절로 멈춘 경우
    }
    S.stopRequested = false;
  }
  if (st.id) S.sessionId = st.id;
  if (st.speaker_modes) { S.liveModes = st.speaker_modes; paintModes("live"); }
  $("#startBtn").classList.toggle("hidden", S.running);
  $("#startBtn").disabled = S.running || !!S.uploading;
  $("#stopBtn").classList.toggle("hidden", !S.running);
  const marks = [];
  if (st.state === "starting") marks.push("준비 중… (음성인식 모델을 불러오는 중)");
  if (st.state === "running") marks.push(st.speaking ? "말하는 중" : "듣는 중");
  if (st.state === "error") marks.push("오류: " + (st.error || ""));
  if (st.state === "finished") marks.push("완료");
  if (st.state === "stopping") marks.push("마지막 문장을 저장하는 중…");
  if (!marks.length) marks.push("시작 전");
  $("#status").textContent = marks.join("  ");
  $("#status").dataset.state = st.state === "running" && st.speaking ? "speaking" : (st.state || "");
  // 시작 단추(마이크 고리)가 녹음 중·말하는 중에 따라 빛나도록 알려 줍니다
  document.body.classList.toggle("is-running", S.running);
  document.body.classList.toggle("is-speaking", st.state === "running" && !!st.speaking);
  $("#stopBtn").disabled = S.stopping;
  $("#stopBtn span").textContent = S.stopping ? "저장하는 중…" : st.state === "starting" ? "준비 취소" : "마치고 저장";
  paintWorkspaceState();
  if (st.face) {
    const on = !!st.face.running;
    if (!on && $("#faceToggle").checked) clearFacePreview();
    $("#faceToggle").checked = on;
    $("#captureFaces").disabled = !on;
    $("#faceErr").textContent = st.face.error || "";
    if (st.face.current) {
      $("#faceNow").textContent = st.face.current.name
        ? `${st.face.current.name} (${(st.face.current.score * 100) | 0}%)` : "미등록 인물";
    }
  }
}

/* ───────────────── 받아쓰기 화면 ───────────────── */
// 화자마다 색을 줍니다. 서로 확연히 다른 색을, 대비가 큰 순서대로 늘어놓았습니다.
// 녹취록에 처음 나오는 순서대로, 바로 앞·뒤 화자와 겹치지 않고 가장 덜 쓴 색을 고릅니다.
// 그래서 화자가 10명 이하면 모두 다른 색이고, 그보다 많아도 붙어 있는 화자끼리는 다른 색입니다.
const SPEAKER_COLORS = [
  { fg: "#2355d6", bg: "#e2eaff" },   // 파랑
  { fg: "#b95207", bg: "#ffe8d4" },   // 주황
  { fg: "#12804b", bg: "#d8f2e4" },   // 초록
  { fg: "#b8286f", bg: "#ffe0ef" },   // 자홍
  { fg: "#6538d0", bg: "#ebe3ff" },   // 보라
  { fg: "#067a82", bg: "#d3f1f2" },   // 청록
  { fg: "#8a5d00", bg: "#fbeec4" },   // 황토
  { fg: "#b3261e", bg: "#ffe0dc" },   // 다홍
  { fg: "#3b4a63", bg: "#e1e6ee" },   // 먹색
  { fg: "#547410", bg: "#e8f2cf" },   // 올리브
];
const UNKNOWN_COLOR = { fg: "#56628c", bg: "#e8ecf5" };
const colorBook = new WeakMap();     // 녹취록 칸 → { map: 이름→색 번호, uses: 색별 사용 수 }

function resetColors(container) { colorBook.delete(container); }

function neighbourSpeaker(row, dir) {
  const name = row.dataset.speaker;
  for (let el = row[dir]; el; el = el[dir]) {
    if (el.dataset.speaker && el.dataset.speaker !== name) return el.dataset.speaker;
    if (el.dataset.speaker === name) continue;
  }
  return null;
}

function speakerColor(row) {
  const name = row.dataset.speaker;
  if (!name || name === "화자 미상") return UNKNOWN_COLOR;
  const box = row.closest(".transcript");
  if (!box) return SPEAKER_COLORS[0];
  let book = colorBook.get(box);
  if (!book) {
    book = { map: new Map(), uses: SPEAKER_COLORS.map(() => 0) };
    colorBook.set(box, book);
  }
  if (!book.map.has(name)) {
    const avoid = new Set(["previousElementSibling", "nextElementSibling"]
      .map((d) => neighbourSpeaker(row, d))
      .filter((n) => n && book.map.has(n))
      .map((n) => book.map.get(n)));
    let pick = -1;
    SPEAKER_COLORS.forEach((_, i) => {
      if (avoid.has(i)) return;
      if (pick < 0 || book.uses[i] < book.uses[pick]) pick = i;
    });
    if (pick < 0) pick = 0;
    book.map.set(name, pick);
    book.uses[pick] += 1;
  }
  return SPEAKER_COLORS[book.map.get(name)];
}

function applyColor(row) {
  const c = speakerColor(row);
  row.style.setProperty("--sp-fg", c.fg);
  row.style.setProperty("--sp-bg", c.bg);
}

// 화자를 어떻게 알아냈는지 알려 주는 표시
const SOURCE_MARK = {
  face: { mark: "얼굴", title: "화면 얼굴로 인식" },
  context: { mark: "맥락", title: "사회자의 지명을 보고 AI가 지정" },
  voice: { mark: "목소리", title: "목소리로 인식" },
  manual: { mark: "직접", title: "직접 지정" },
  none: { mark: "", title: "알 수 없음" },
};

function segmentNode(seg, sessionId) {
  const el = document.createElement("div");
  el.className = "seg";
  el.dataset.id = seg.id;
  el.dataset.start = seg.start;
  el.dataset.end = seg.end;

  const fixed = (seg.corrections || []).length
    ? ` title="교정: ${escapeHtml(seg.corrections.map((c) => `${c.from}→${c.to}`).join(", "))}"` : "";

  el.innerHTML = `
    <div class="av"></div>
    <div class="body">
      <div class="sp"></div>
      <div class="tx"${fixed}>${highlightHtml(seg.text)}</div>
    </div>
    <div class="aside">
      <div class="t">${hms(seg.start)}</div>
      <div class="tools">
        <button class="mini" data-act="speaker">화자</button>
        <button class="mini" data-act="edit">수정</button>
      </div>
    </div>`;
  paintSpeaker(el, seg.speaker, seg.speaker_source || "none", seg.cands);
  el._seg = seg;
  el.classList.toggle("kw-hit", hasKeyword(seg.text));

  el.addEventListener("click", (ev) => {
    if (ev.target.closest(".tools")) return;
    if (ev.target.isContentEditable) return;
    // 화자 이름·머리글자를 누르면 화자 바꾸기 (나머지 자리는 그 위치의 녹음 재생)
    if (ev.target.closest(".sp, .av")) return changeSpeaker(el, seg, sessionId);
    playSegment(sessionId || S.sessionId, seg, el);
  });
  el.querySelector('[data-act="edit"]').addEventListener("click", () => startEdit(el, seg, sessionId));
  el.querySelector('[data-act="speaker"]').addEventListener("click", () => changeSpeaker(el, seg, sessionId));
  el.tabIndex = 0;
  el.setAttribute("aria-label", `${hms(seg.start)} ${seg.speaker || "화자 미상"}. ${seg.text}`);
  el.addEventListener("keydown", ev => {
    if (ev.target === el && ev.key === "Enter") { ev.preventDefault(); playSegment(sessionId || S.sessionId, seg, el); }
  });
  return el;
}

const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// 한 번의 인식에서 문장이 여러 개 확정되면 한꺼번에 '우르르' 붙지 않고, 차례로 흘러들어오게 합니다.
// (기다리게 하는 것이 아니라 1초 안에 모두 나타나도록 짧은 간격만 둡니다)
let segQueue = [], segTimer = null;
function appendSegment(seg) {
  segQueue.push(seg);
  if (!segTimer) flushSegments();
}
function flushSegments() {
  const seg = segQueue.shift();
  if (!seg) { segTimer = null; return; }
  const row = segmentNode(seg, S.sessionId);
  $("#transcript .empty-state")?.remove();
  row.classList.add("seg-new");
  applyColor($("#transcript").appendChild(row));
  if ($("#autoscroll").checked) scrollBottom();
  segTimer = setTimeout(flushSegments, segQueue.length ? Math.min(220, 900 / segQueue.length) : 0);
}

function showPartial(text) {
  const box = $("#partial");
  if (text) {
    // 말이 길어지면 칸이 함께 늘어나고, 최대 높이를 넘으면 맨 끝(지금 말)을 따라 내려갑니다
    box.innerHTML = `<span class="live-dot"></span>${highlightHtml(text)}`;
    box.scrollTop = box.scrollHeight;
    return;
  }
  // 절전 모드는 미리보기를 만들지 않습니다. 왜 비어 있는지 알려 줍니다.
  if (S.running && S.settings?.asr?.performance === "low") {
    box.innerHTML = '<span class="note">절전 모드 — 말이 끝나면 문장으로 올라옵니다. '
                  + '(설정에서 성능 모드를 바꾸면 여기에 실시간으로 보입니다)</span>';
  } else {
    box.textContent = "";
  }
}

function scrollBottom() {
  const t = $("#transcript");
  t.scrollTop = t.scrollHeight;
}

function playSegment(sessionId, seg, el) {
  if (!sessionId) return;
  // 받아쓰는 동안 재생하면 지금 녹음되는 소리와 섞여 버립니다.
  if (S.running) {
    toast(S.stopping
      ? "마지막 문장을 정리하는 중입니다. 마무리가 끝나면 바로 들을 수 있습니다."
      : "받아쓰는 중에는 들을 수 없습니다. 받아쓰기를 마친 뒤 기록·요약 탭에서 들으세요.");
    return;
  }
  // 받아쓰기 탭이면 이 회의 녹음을 재생기에 걸어 둡니다.
  if (el.closest("#transcript") && S.player?.id !== sessionId) {
    movePlayerTo("livePlayerSlot");
    loadAudio(sessionId, { has_audio: true }, S.segments);
  }
  if (!player.src) return toast("녹음 파일이 없습니다", true);
  S.loopSegment = $("#plLoop").checked ? seg : null;
  playFrom(seg.start);
}

async function startEdit(el, seg, sessionId) {
  const tx = el.querySelector(".tx");
  const text = await uiDialog({title:"문장 수정",message:`${hms(seg.start)} · ${seg.speaker || "화자 미상"}`,value:seg.text,multiline:true,confirmLabel:"수정 저장"});
  if (text == null || text === seg.text) return;
  try {
    await api(`/api/sessions/${sessionId || S.sessionId}/segments/${seg.id}`, {method:"PATCH",body:{text}});
    seg.text = text;
    tx.innerHTML = highlightHtml(text);
    el.setAttribute("aria-label",`${hms(seg.start)} ${seg.speaker || "화자 미상"}. ${text}`);
    el.classList.toggle("kw-hit",hasKeyword(text));
    toast("문장을 저장했습니다");
  } catch (e) { toast("저장하지 못했습니다. 원문을 유지합니다: " + e.message, true); }
}

async function changeSpeaker(el, seg, sessionId) {
  const sid = sessionId || S.sessionId;
  const name = await askSpeaker(seg.speaker, seg.speaker_source === "manual");
  if (!name) return;
  if (name === CLEAR_ONE || name === CLEAR_ALL) {
    // 잘못 붙은 이름 지우기: '화자 N'(이름 모를 목소리 번호)으로 돌아갑니다
    try {
      const r = await api(`/api/sessions/${sid}/segments/${seg.id}/speaker/clear`, {
        method: "POST", body: { all: name === CLEAR_ALL },
      });
      applySegmentUpdates(r.segments || [], sid);
      const mine = (r.segments || []).find((s) => s.id === seg.id);
      if (mine) { Object.assign(seg, mine); paintSpeaker(el, mine.speaker, mine.speaker_source, mine.cands); }
      toast(`'${r.name}' 이름을 ${r.cleared}문장에서 지웠습니다.`
            + (r.forgot_voices ? " · 잘못 배운 목소리도 잊게 했습니다" : ""));
    } catch (e) { toast(e.message, true); }
    return;
  }
  if (name === UNSET) {
    // 직접 지정 취소: 원래 알아본 화자로 되돌립니다
    try {
      const r = await api(`/api/sessions/${sid}/segments/${seg.id}/speaker`, { method: "DELETE" });
      const up = r.segment;
      Object.assign(seg, up);
      paintSpeaker(el, up.speaker, up.speaker_source, up.cands);
      applySegmentUpdates(r.others || [], sid);          // 함께 바뀌었던 다른 문장도 원래대로
      const n = (r.others || []).length;
      toast(`직접 지정을 취소했습니다 → ${up.speaker}`
            + (n ? ` · 함께 바뀐 ${n}문장도 되돌림` : "")
            + (r.unlearned ? " · 그때 배운 목소리도 지움" : ""));
    } catch (e) { toast(e.message, true); }
    return;
  }

  // 서버를 기다리지 않고 바로 화면에 반영합니다. (실패하면 되돌립니다)
  // 이름 뒤 ' 모두'는 같은 화자로 잡힌 문장 전체를 바꾸라는 뜻이라 그 문장들도 함께 칠합니다.
  // '화자 1'처럼 번호만 붙은 화자는 한 사람이라 '모두'를 안 붙여도 같은 번호 문장 전체를 바꿉니다.
  const all = name.endsWith(" 모두") || /^화자 \d+$/.test(seg.speaker || "");
  const shown = all ? name.slice(0, -3).trim() : name;
  const list = sid === S.sessionId ? S.segments : S.archive.segments;
  const targets = [seg, ...(all && seg.speaker && seg.speaker !== "화자 미상" && seg.speaker !== shown
    ? (list || []).filter((s) => s.id !== seg.id && s.speaker === seg.speaker) : [])];
  const before = targets.map((s) => ({ id: s.id, speaker: s.speaker, speaker_source: s.speaker_source, cands: s.cands }));
  applySegmentUpdates(targets.map((s) => ({ id: s.id, speaker: shown, speaker_source: "manual" })), sid);
  Object.assign(seg, { speaker: shown, speaker_source: "manual" });
  paintSpeaker(el, shown, "manual", seg.cands);

  try {
    // 이 목소리를 그 사람의 것으로 학습시킵니다. (서버가 뒤에서 하고, 같은 목소리 문장은 끝나는 대로 바뀝니다)
    // 이후 같은 목소리가 나오면 자동으로 같은 이름이 붙습니다.
    const r = await api(`/api/sessions/${sid}/segments/${seg.id}/speaker`, {
      method: "POST", body: { name, learn: true },
    });
    applySegmentUpdates(r.applied || [], sid);
    loadMembers();
    const extra = (r.applied || []).length - 1;
    toast(extra > 0
      ? `${shown} 으로 바꿨습니다. 같은 화자 ${extra}문장도 함께 바꿨습니다.`
      : `${shown} 으로 바꿨습니다.` + (r.learning ? " 같은 목소리를 찾아 함께 바꾸는 중입니다." : ""));
  } catch (e) {
    applySegmentUpdates(before, sid);
    const mine = before[0];
    Object.assign(seg, mine);
    paintSpeaker(el, mine.speaker, mine.speaker_source, mine.cands);
    toast(e.message, true);
  }
}

// 방법별 판단을 알려 주는 설명 (표시 위에 마우스를 올리면 보입니다)
function evidenceTitle(base, cands) {
  const names = { face: "얼굴", voice: "목소리", context: "맥락" };
  const parts = Object.entries(cands || {})
    .filter(([m, c]) => names[m] && c && c.name)
    .map(([m, c]) => `${names[m]}: ${c.name}${m !== "context" && c.score ? ` (${Math.round(c.score * 100)}%)` : ""}`);
  if (!parts.length) return base;
  const who = new Set(Object.values(cands).filter((c) => c && c.id).map((c) => c.id));
  return `${base}\n${parts.join(" · ")}` + (who.size > 1 ? "\n방법마다 다르게 보여, 확신이 가장 큰 쪽을 골랐습니다." : "");
}

function paintSpeaker(row, name, source, cands) {
  const sp = row.querySelector(".sp");
  if (!sp) return;
  const key = SOURCE_MARK[source] ? source : "manual";
  const src = SOURCE_MARK[key];
  const ids = new Set(Object.values(cands || {}).filter((c) => c && c.id).map((c) => c.id));
  const split = ids.size > 1 && key !== "manual";
  sp.innerHTML = `<span class="name">${escapeHtml(name || "화자 미상")}</span>`
               + `<span class="src src-${key}${split ? " split" : ""}" title="${escapeHtml(evidenceTitle(src.title, cands))}">`
               + `${src.mark}${split ? " ≠" : ""}</span>`;
  // 화자 타일에는 이름 첫 글자를 보여 줍니다
  const av = row.querySelector(".av");
  // '화자 3'처럼 이름이 아직 없으면 번호를 보여 줍니다
  const num = /^화자\s*(\d+)/.exec(name || "");
  if (av) av.textContent = num ? num[1] : (name && name !== "화자 미상") ? [...name][0] : "?";
  row.dataset.speaker = name || "";
  if (row.isConnected) applyColor(row);     // 아직 화면에 붙기 전이면 붙인 뒤 칠합니다
}

// 화자 고르기 (브라우저 prompt 대신 직접 만든 창 — 등록된 사람을 바로 고를 수 있습니다)
const UNSET = Symbol("unset");
const CLEAR_ONE = Symbol("clear-one");
const CLEAR_ALL = Symbol("clear-all");
function askSpeaker(current, isManual = false) {
  return new Promise((resolve) => {
    $("#speakerUnset").classList.toggle("hidden", !isManual);
    $("#speakerUnset").onclick = () => close(UNSET);
    // 이름이 붙은 문장에만 '이름 지우기'를 보여 줍니다
    const named = !!current && !/^화자/.test(current);
    $("#speakerClearRow").classList.toggle("hidden", !named);
    $("#speakerClearAll").textContent = `이 회의의 '${current || ""}' 모두 지우기`;
    $("#speakerClearOne").onclick = () => close(CLEAR_ONE);
    $("#speakerClearAll").onclick = () => close(CLEAR_ALL);
    const modal = $("#speakerModal");
    const input = $("#speakerName");
    const list = $("#speakerList");
    input.value = current && !current.startsWith("화자") ? current : "";
    list.innerHTML = S.members.map((m) =>
      `<button class="mini" data-name="${escapeHtml(m.name + (m.title ? " " + m.title : ""))}">${escapeHtml(m.name)}</button>`
    ).join("") || '<span class="hint">등록된 사람이 없습니다. 이름을 입력하세요.</span>';
    modal.classList.remove("hidden");
    input.focus();

    const close = (value) => {
      modal.classList.add("hidden");
      list.replaceChildren();
      resolve(value);
    };
    list.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => close(b.dataset.name)));
    $("#speakerOk").onclick = () => close(input.value.trim());
    $("#speakerCancel").onclick = () => close(null);
    input.onkeydown = (e) => {
      if (e.key === "Enter") close(input.value.trim());
      if (e.key === "Escape") close(null);
    };
  });
}

function renderTranscript(container, segments, sessionId) {
  container.innerHTML = "";
  resetColors(container);
  segments.forEach((s) => applyColor(container.appendChild(segmentNode(s, sessionId))));
  paintEmptyTranscript(container);
}

/* ───────────────── 녹취록 검색 ─────────────────
   글자를 바꾸지 않고 브라우저의 강조 기능(CSS Highlight)으로만 칠하므로,
   키워드 강조·문장 고치기와 부딪히지 않습니다. 새 문장이 들어오거나 고치면 저절로 다시 찾습니다. */
const finders = [];
const canHighlight = typeof CSS !== "undefined" && CSS.highlights && typeof Highlight === "function";

function paintFinds() {
  if (!canHighlight) return;
  const hits = [], cur = [];
  finders.forEach((f) => f.ranges.forEach((r, i) => (i === f.cur ? cur : hits).push(r)));
  CSS.highlights.set("find-hit", new Highlight(...hits));
  CSS.highlights.set("find-cur", new Highlight(...cur));
}

// 한 문장(.tx) 안에서 검색어가 나오는 자리마다 Range 를 만듭니다. 키워드 <mark> 로 글자가 쪼개져 있어도 찾습니다.
function findRanges(list, q) {
  const out = [];
  list.querySelectorAll(".seg .tx").forEach((tx) => {
    const nodes = [];
    let full = "";
    const walker = document.createTreeWalker(tx, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) { nodes.push([n, full.length]); full += n.data; }
    const hay = full.toLowerCase();
    const at = (pos) => {
      let k = nodes.length - 1;
      while (k > 0 && nodes[k][1] > pos) k--;
      return [nodes[k][0], pos - nodes[k][1]];
    };
    for (let i = hay.indexOf(q); i !== -1; i = hay.indexOf(q, i + q.length)) {
      const r = document.createRange();
      r.setStart(...at(i));
      r.setEnd(...at(i + q.length));
      out.push(r);
    }
  });
  return out;
}

$$(".find").forEach((box) => {
  const list = $(box.dataset.find);
  const input = box.querySelector("input");
  const count = box.querySelector(".find-count");
  const f = { ranges: [], cur: -1 };
  finders.push(f);

  const rowOf = (r) => r.startContainer.parentElement?.closest(".seg");
  const show = (scroll) => {
    list.querySelectorAll(".seg.find-cur").forEach((el) => el.classList.remove("find-cur"));
    const r = f.ranges[f.cur];
    if (r) {
      const row = rowOf(r);
      row?.classList.add("find-cur");
      if (scroll) row?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    const q = input.value.trim();
    count.textContent = !q ? "" : f.ranges.length ? `${f.cur + 1}/${f.ranges.length}` : "없음";
    count.classList.toggle("none", !!q && !f.ranges.length);
    box.querySelectorAll(".find-btn").forEach((b) => { b.disabled = f.ranges.length < 2; });
    paintFinds();
  };
  // keep: 새 문장이 들어와 다시 찾을 때는 보던 자리를 지키고 화면을 옮기지 않습니다.
  const run = (keep) => {
    const q = input.value.trim().toLowerCase();
    f.ranges = q ? findRanges(list, q) : [];
    f.cur = !f.ranges.length ? -1 : keep && f.cur >= 0 ? Math.min(f.cur, f.ranges.length - 1) : 0;
    show(!keep);
  };
  const go = (d) => {
    if (!f.ranges.length) return;
    f.cur = (f.cur + d + f.ranges.length) % f.ranges.length;
    show(true);
  };

  let typing = null;
  input.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => run(false), 150); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      clearTimeout(typing);
      if (input.value.trim() && !f.ranges.length) run(false); else go(e.shiftKey ? -1 : 1);
    } else if (e.key === "Escape" && input.value) {
      e.preventDefault();
      input.value = "";
      run(false);
    }
  });
  box.querySelectorAll(".find-btn").forEach((b) => b.addEventListener("click", () => go(+b.dataset.dir)));

  let again = null;
  new MutationObserver(() => {
    if (!input.value.trim()) return;
    clearTimeout(again);
    again = setTimeout(() => run(true), 200);
  }).observe(list, { childList: true, subtree: true, characterData: true });
  show(false);
});

// Ctrl+F: 지금 보고 있는 탭의 녹취록 검색 칸으로 갑니다.
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "f") return;
  if (document.querySelector('.modal:not(.hidden), dialog[open]')) return;
  const input = document.querySelector(".tabpane.active .find input");
  if (!input) return;
  e.preventDefault();
  input.focus();
  input.select();
});

/* ───────────────── 시작/중지 ───────────────── */
// 소리 칸에 따라 보일 칸. PC 소리는 스피커 볼륨과 상관없이 PC의 모든 소리를 받으므로 장치를 고르지 않습니다.
// (볼륨을 0으로 하거나 음소거해도 받아 적습니다. 장치 칸은 마이크일 때만)
function updateSourceFields() {
  const src = $("#source").value;
  $("#filePick").classList.toggle("hidden", src !== "file");
  $("#deviceField").classList.toggle("hidden", src !== "microphone");
  $("#sourceHint").textContent = {loopback:"PC에서 재생되는 모든 소리를 받아씁니다. 중계 탭의 소리는 켜 두세요.",microphone:"사용할 마이크를 선택하세요. 주변의 대화도 함께 녹음됩니다.",file:"음성·영상 파일 또는 직접 재생 가능한 영상 주소 중 하나를 입력하세요."}[src];
}
$("#source").addEventListener("change", () => { updateSourceFields(); loadDevices(); });

async function loadDevices() {
  const kind = $("#source").value;
  if (kind === "file") return;
  try {
    const { devices } = await api("/api/devices");
    const list = devices.filter((d) => d.kind === kind);
    // 지난번에 쓰던 장치가 그대로 있으면 그걸 다시 고릅니다
    const saved = S.settings?.audio?.device_index;
    const keep = list.some((d) => d.index === saved) ? saved : null;
    $("#device").innerHTML = list.map((d) => {
      const selected = keep != null ? d.index === keep : d.default;
      return `<option value="${d.index}" ${selected ? "selected" : ""}>` +
             `${d.name}${d.default ? " (기본)" : ""}</option>`;
    }).join("") || `<option value="">장치 없음</option>`;
  } catch (e) { toast(e.message, true); }
}

$("#startBtn").addEventListener("click", async () => {
  if (S.running || S.uploading) return;
  const body = { title: $("#title").value, source: $("#source").value };
  liveTitleSent = body.title.trim();
  $("#startBtn").disabled = true;
  let starting = false;
  try {
  if (body.source === "file") {
    const url = $("#fileUrl").value.trim();
    const f = $("#fileInput").files[0];
    if (url && f) { toast("파일과 주소가 모두 있습니다. 사용할 소리 하나만 남겨 주세요.",true); return; }
    const start = Number($("#startSec").value || 0);
    if (!Number.isFinite(start) || start < 0) { toast("시작 위치는 0 이상의 초 단위 숫자로 입력해 주세요.",true); return; }
    if (url) {
      if (!/^https?:\/\//i.test(url)) { toast("http:// 또는 https://로 시작하는 영상 주소를 입력해 주세요.",true); $("#fileUrl").focus(); return; }
      body.url = url;
      body.start_sec = start;
    } else if (f) {
      S.uploading = true;
      paintWorkspaceState();
      $("#startBtn span").textContent = "파일 준비 중…";
      toast("파일 올리는 중...");
      const fd = new FormData(); fd.append("file", f);
      const response = await fetch("/api/upload", { method: "POST", body: fd });
      const up = await response.json();
      if (!response.ok || !up.path) throw new Error(up.detail || "파일을 올리지 못했습니다. 다시 선택해 주세요.");
      body.file_path = up.path;
      body.start_sec = start;
    } else {
      return toast("파일을 고르거나 주소를 넣으세요", true);
    }
  } else {
    if (body.source === "microphone" && !$("#device").value) { toast("사용할 마이크가 없습니다. 연결 상태를 확인해 주세요.",true); return; }
    body.device_index = parseInt($("#device").value, 10);
  }
  starting = true;
  applyStatus({ state: "starting" });       // 서버 응답을 기다리지 않고 바로 중지할 수 있게
    S.segments = [];
    segQueue = []; clearTimeout(segTimer); segTimer = null;   // 이전 회의에서 남은 문장은 버립니다
    $("#transcript").innerHTML = "";
    paintEmptyTranscript($("#transcript"));
    resetColors($("#transcript"));
    $("#segCount").textContent = "0문장";
    const r = await api("/api/session/start", { method: "POST", body });
    applyStatus(r.status);
    toast(r.cancelled ? "시작을 취소했습니다" : "받아쓰기를 시작했습니다");
  } catch (e) {
    if (starting) applyStatus({ state: "error", error:e.message });
    toast(e.message, true);
  } finally {
    S.uploading = false;
    $("#startBtn span").textContent = "받아쓰기 시작";
    $("#startBtn").disabled = false;
    paintWorkspaceState();
  }
});

// 받아쓰는 중에 제목을 바꾸면 지금 회의의 제목도 바꿉니다. (기록·요약과 내보내기 파일 이름에 그대로 쓰입니다)
let liveTitleTimer = null, liveTitleSent = "";
async function pushLiveTitle() {
  clearTimeout(liveTitleTimer);
  const title = $("#title").value.trim();
  if (!S.running || !S.sessionId || !title || title === liveTitleSent) return;
  liveTitleSent = title;
  try { await api(`/api/sessions/${S.sessionId}`, { method: "PATCH", body: { title } }); }
  catch (e) { liveTitleSent = ""; toast("제목을 바꾸지 못했습니다: " + e.message, true); }
}
$("#title").addEventListener("input", () => { clearTimeout(liveTitleTimer); liveTitleTimer = setTimeout(pushLiveTitle, 600); });
$("#title").addEventListener("change", pushLiveTitle);

$("#stopBtn").addEventListener("click", async () => {
  $("#stopBtn").disabled = true;
  await pushLiveTitle();                    // 마지막으로 고친 제목을 먼저 반영합니다
  clearTimeout(memoTimer);                  // 방금 친 메모도 빠지지 않게
  await api("/api/memo", { method: "PUT", body: { memo: $("#memo").value } }).catch(() => {});
  $("#status").textContent = "마무리 중... (마지막 문장을 정리하고 있습니다)";
  S.stopRequested = true;
  try {
    const r = await api("/api/session/stop", { method: "POST" });
    if (r.cancelling) {
      // 아직 모델을 불러오는 중이면, 준비가 끝나는 즉시 멈춥니다
      $("#status").textContent = "취소하는 중… (모델 준비가 끝나면 바로 멈춥니다)";
      $("#stopBtn").disabled = false;
      return;
    }
    const id = r.status?.id || S.sessionId;
    applyStatus(r.status);
    showPartial("");
    // 마지막 문장 정리를 기다리지 않고 바로 넘어갑니다. 정리가 끝나면 녹취록을 다시 불러옵니다.
    await goToArchive(id);
    if (r.status?.state === "stopping" && S.running) {
      $("#summaryStatus").textContent = "마지막 문장을 정리하는 중입니다. 끝나면 녹취록이 저절로 채워집니다.";
    }
    toast("받아쓰기를 멈췄습니다.");
  } catch (e) { S.stopRequested = false; toast(e.message, true); }
  $("#stopBtn").disabled = !!S.stopping;
});

$$("[data-export]").forEach((b) => b.addEventListener("click", () => {
  if (!S.sessionId) return toast("먼저 받아쓰기를 하세요", true);
  location.href = `/api/sessions/${S.sessionId}/export?format=${b.dataset.export}`;
}));

/* ───────────────── 화면 속 발언자 (얼굴) ───────────────── */
async function toggleFace(on) {
  try {
    const r = await api("/api/face/watch", { method: "POST", body: { on } });
    $("#faceErr").textContent = r.error || "";
    $("#captureFaces").disabled = !r.running;
    if (on && !r.running) toast("화면 감시를 켜지 못했습니다: " + (r.error || ""), true);
    if (!on) { $("#faceCandidates").innerHTML = ""; clearFacePreview(); }
    return !!r.running;
  } catch (e) { toast(e.message, true); return null; }
}
// 화면 감시를 켜면 화자 구분의 '얼굴'도 켜고, 끄면 '얼굴'도 끕니다.
// (감시를 켜지 못했으면 '얼굴'은 그대로 둡니다)
// 감시를 끄면 마지막 화면을 남기지 않고 처음처럼 빈 칸으로 돌립니다.
function clearFacePreview() {
  $("#facePreview").removeAttribute("src");
  $("#faceNow").textContent = "대기 중";
}
$("#faceToggle").addEventListener("change", async (e) => {
  const on = e.target.checked;
  const running = await toggleFace(on);
  if (running === null || running !== on) return;
  toggleMode("live", "face", { want: on, fromWatch: true });
});

setInterval(() => {
  if ($("#faceToggle").checked) $("#facePreview").src = "/api/face/preview.jpg?t=" + Date.now();
}, 1000);

// 지금 감시 중인 화면에 보이는 얼굴을 잡아와 이름을 붙입니다.
$("#captureFaces").addEventListener("click", captureFaces);

async function captureFaces() {
  const box = $("#faceCandidates");
  box.innerHTML = '<div class="hint">화면을 보는 중...</div>';
  try {
    const { faces } = await api("/api/face/candidates");
    if (!faces.length) {
      box.innerHTML = '<div class="hint">얼굴을 찾지 못했습니다. 발언자가 크게 보일 때 다시 눌러 보세요.</div>';
      return;
    }
    box.innerHTML = "";
    faces.forEach((f) => {
      const card = document.createElement("div");
      card.className = "face-card";
      card.innerHTML = `
        <img src="${f.image || ""}" alt="얼굴">
        <div class="name">${f.name ? `이미 등록됨: <b>${escapeHtml(f.name)}</b>` : "새 얼굴"}</div>
        <input type="text" placeholder="이름 (예: 김철수)" value="${f.name ? escapeHtml(f.name) : ""}">
        <input type="text" placeholder="직함 (예: 의원, 의장, 국장)">
        <button class="mini primary">이 얼굴 등록</button>
        <button class="mini" data-act="cancel">취소</button>`;
      const [input, titleInput] = card.querySelectorAll("input");
      const save = async () => {
        const name = input.value.trim();
        if (!name) return toast("이름을 넣으세요", true);
        try {
          const title = titleInput.value.trim();
          const { member } = await api("/api/members", { method: "POST", body: { name, title } });
          await api(`/api/members/${member.id}/face/from-screen`, {
            method: "POST", body: { rank: f.rank },
          });
          toast(`${name} 얼굴을 등록했습니다`);
          card.remove();
          loadMembers();
        } catch (e) { toast(e.message, true); }
      };
      card.querySelector("button").addEventListener("click", save);
      card.querySelector('[data-act="cancel"]').addEventListener("click", () => card.remove());
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
      box.appendChild(card);
    });
  } catch (e) {
    box.innerHTML = `<div class="err">${escapeHtml(e.message)}</div>`;
  }
}

/* ───────────────── 용어집 ───────────────── */
function renderGlossary() {
  // 용어·바꾸기 규칙은 수백~수천 개라 화면에 뿌리지 않습니다. ('보기'로 전체 파일 확인, 고치기·지우기는 찾아서)
  $("#termSummary").textContent =
    `용어 ${S.glossary.terms.length}개 · 바꾸기 규칙 ${S.glossary.replacements.length}개`;
  renderGlossFound();
}

// 찾아서 고치기·지우기: 낱말이 들어간 바꾸기 규칙과 용어를 보여 주고, 고치거나 지우면 바로 저장합니다.
const GLOSS_FOUND_MAX = 30;
function renderGlossFound() {
  const box = $("#glossFound");
  const q = ($("#glossFind").value || "").trim();
  box.innerHTML = "";
  if (!q) return;
  const reps = S.glossary.replacements.filter((r) => (r.from || "").includes(q) || (r.to || "").includes(q));
  const terms = S.glossary.terms.filter((t) => (t.text || "").includes(q));
  const head = document.createElement("div");
  head.className = "gf-head";
  head.textContent = `바꾸기 규칙 ${reps.length}개 · 용어 ${terms.length}개` +
    (reps.length + terms.length > GLOSS_FOUND_MAX ? ` (앞 ${GLOSS_FOUND_MAX}개만 보입니다. 더 좁혀 찾아 주세요)` : "");
  box.appendChild(head);
  const rows = [...reps.map((r) => ({ r })), ...terms.map((t) => ({ t }))].slice(0, GLOSS_FOUND_MAX);
  rows.forEach(({ r, t }) => {
    const row = document.createElement("div");
    row.className = "gf-row";
    if (r) {
      row.innerHTML = `<span class="gf-kind">규칙</span>
        <input type="text" aria-label="찾을 말"><span class="gf-arrow">→</span><input type="text" aria-label="바꿀 말">
        <button class="mini">저장</button><button class="mini danger-ghost">삭제</button>`;
      const [a, b] = row.querySelectorAll("input");
      a.value = r.from || ""; b.value = r.to || "";
      const [save, del] = row.querySelectorAll("button");
      save.addEventListener("click", () => editReplacement(r, a.value.trim(), b.value.trim()));
      del.addEventListener("click", () => saveGlossaryChange(() => {
        S.glossary.replacements = S.glossary.replacements.filter((x) => x !== r);
      }, `규칙을 지웠습니다: ${r.from} → ${r.to}`));
    } else {
      row.innerHTML = `<span class="gf-kind">용어</span><span class="gf-text"></span><span class="gf-cat"></span>
        <span class="spacer"></span><button class="mini danger-ghost">삭제</button>`;
      row.querySelector(".gf-text").textContent = t.text;
      row.querySelector(".gf-cat").textContent = t.category || "";
      row.querySelector("button").addEventListener("click", () => saveGlossaryChange(() => {
        S.glossary.terms = S.glossary.terms.filter((x) => x !== t);
      }, `용어를 지웠습니다: ${t.text}`));
    }
    box.appendChild(row);
  });
}

function editReplacement(rule, from, to) {
  if (!from || !to) return toast("찾을 말과 바꿀 말을 모두 적어 주세요", true);
  if (from === to) return toast("찾을 말과 바꿀 말이 같습니다", true);
  saveGlossaryChange(() => {
    // 고친 '찾을 말'이 다른 규칙과 같으면 하나로 합칩니다
    S.glossary.replacements = S.glossary.replacements.filter((x) => x === rule || x.from !== from);
    rule.from = from; rule.to = to;
  }, `규칙을 고쳤습니다: ${from} → ${to}`);
}

// 용어집을 바꾸고 바로 저장합니다. 저장하지 못하면 서버의 용어집으로 되돌립니다.
async function saveGlossaryChange(change, message) {
  change();
  try {
    S.glossary = await api("/api/glossary", { method: "PUT", body: S.glossary });
    toast(message);
  } catch (e) {
    toast(e.message, true);
    try { S.glossary = await api("/api/glossary"); } catch (e2) { /* 연결 문제 */ }
  }
  renderGlossary();
}

let glossFindTimer = null;
$("#glossFind").addEventListener("input", () => {
  clearTimeout(glossFindTimer);
  glossFindTimer = setTimeout(renderGlossFound, 200);
});

$("#viewTerms").addEventListener("click", () => {
  window.open("/api/glossary/terms.txt", "_blank");
});

// 바꾸기 규칙 넣기: 같은 '찾을 말'이 있으면 '바꿀 말'만 바꾸고, 없으면 더한 뒤 바로 저장합니다.
async function addReplacement() {
  const from = $("#repFrom").value.trim(), to = $("#repTo").value.trim();
  if (!from || !to) return toast("찾을 말과 바꿀 말을 모두 적어 주세요", true);
  if (from === to) return toast("찾을 말과 바꿀 말이 같습니다", true);
  const reps = S.glossary.replacements;
  const same = reps.find((r) => r.from === from);
  const before = same ? same.to : null;
  if (same) same.to = to; else reps.push({ from, to });
  try {
    S.glossary = await api("/api/glossary", { method: "PUT", body: S.glossary });
    renderGlossary();
    $("#repFrom").value = ""; $("#repTo").value = "";
    $("#repFrom").focus();
    toast(same ? `규칙을 고쳤습니다: ${from} → ${to} (전에는 ${before})` : `규칙을 추가했습니다: ${from} → ${to}`);
  } catch (e) {
    if (same) same.to = before; else reps.pop();      // 저장하지 못했으면 되돌립니다
    toast(e.message, true);
  }
}
$("#repAdd").addEventListener("click", addReplacement);
["#repFrom", "#repTo"].forEach((sel) => $(sel).addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addReplacement(); }
}));

$("#bulkAdd").addEventListener("click", async () => {
  const text = $("#bulkTerms").value.trim();
  if (!text) return;
  try {
    const r = await api("/api/glossary/bulk", {
      method: "POST",
      body: { text, category: $("#bulkCategory").value },
    });
    S.glossary = r.glossary;
    $("#bulkTerms").value = "";
    renderGlossary();
    toast(`${r.added}개를 추가했습니다`);
  } catch (e) { toast(e.message, true); }
});

/* ───────────────── 정정 메모 ───────────────── */
let memoTimer = null;
$("#memo").addEventListener("input", () => {
  clearTimeout(memoTimer);
  memoTimer = setTimeout(() => api("/api/memo", { method: "PUT", body: { memo: $("#memo").value } }).catch(() => {}), 400);
});
// 받아쓰기가 끝나 메모가 서버로 넘어가면 칸을 비웁니다 (실패하면 memo_error 로 되돌아옴)
api("/api/memo").then((r) => { $("#memo").value = r.memo || ""; }).catch(() => {});

/* ───────────────── 회의 자료 ───────────────── */
function paintMaterial(terms) {
  $("#materialCount").textContent = terms.length ? `(${terms.length})` : "";
  $("#materialClear").classList.toggle("hidden", !terms.length);
  const box = $("#materialTerms");
  box.innerHTML = "";
  terms.forEach((t) => {
    const chip = document.createElement("span");
    chip.className = "kw-chip";
    chip.textContent = t;
    box.appendChild(chip);
  });
}
api("/api/material").then((r) => paintMaterial(r.terms || [])).catch(() => {});

$("#materialInput").addEventListener("change", async (ev) => {
  const files = [...ev.target.files];
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const hint = $("#materialHint");
  const before = hint.textContent;
  hint.textContent = "AI가 자료에서 이름·용어를 뽑는 중… (1분쯤 걸릴 수 있습니다)";
  try {
    const res = await fetch("/api/material", { method: "POST", body: fd });
    const r = await res.json();
    if (!res.ok) throw new Error(r.detail || res.statusText);
    paintMaterial(r.terms);
    toast(`회의 자료에서 ${r.added.length}개를 뽑아 용어집에 넣었습니다`);
  } catch (e) { toast(e.message, true); }
  hint.textContent = before;
  ev.target.value = "";
});

$("#materialClear").addEventListener("click", async () => {
  const r = await api("/api/material", { method: "DELETE" });
  paintMaterial(r.terms);
  toast("회의 자료 목록을 비웠습니다 (용어집의 '회의자료' 낱말은 용어집 탭에서 지울 수 있습니다)");
});

$("#quickAdd").addEventListener("click", async () => {
  const text = $("#quickTerm").value.trim();
  if (!text) return;
  try {
    const r = await api("/api/glossary/bulk", { method: "POST", body: { text, category: "용어" } });
    S.glossary = r.glossary;
    $("#quickTerm").value = "";
    renderGlossary();
    toast("추가했습니다");
  } catch (e) { toast("용어를 추가하지 못했습니다: " + e.message, true); }
});

/* ───────────────── 등록된 사람 ───────────────── */
async function loadMembers() {
  const { members } = await api("/api/members");
  S.members = members;
  $("#memberCount").textContent = `(${members.length})`;
  const box = $("#memberList");
  box.innerHTML = "";
  members.forEach((m) => {
    const el = document.createElement("div");
    el.className = "member";
    const photo = m.photos?.length ? `/api/members/${m.id}/photo/${m.photos[m.photos.length - 1]}` : "";
    el.innerHTML = `
      <div class="top">
        ${photo ? `<img src="${photo}" alt="">` : ""}
        <div>
          <div><b>${escapeHtml(m.name)}</b> ${escapeHtml(m.title || "")}</div>
          <div class="meta">얼굴 ${m.face_count} · 목소리 ${m.voice_count}</div>
        </div>
      </div>
      <div class="acts">
        <label class="mini" style="cursor:pointer">사진 등록<input type="file" accept="image/*" class="hidden"></label>
        <button class="mini" data-act="voice">목소리 초기화</button>
        <button class="mini" data-act="del">삭제</button>
      </div>`;
    // 사진이 없거나 못 불러오면 깨진 그림 대신 이름 첫 글자를 보여 줍니다.
    const ph = document.createElement("div");
    ph.className = "ph";
    ph.textContent = (m.name || "?").trim().charAt(0) || "?";
    ph.title = "등록된 사진이 없습니다";
    const img = el.querySelector(".top img");
    if (img) img.addEventListener("error", () => img.replaceWith(ph), { once: true });
    else el.querySelector(".top").prepend(ph);
    el.querySelector('input[type="file"]').addEventListener("change", async (ev) => {
      const f = ev.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      const res = await fetch(`/api/members/${m.id}/face/upload`, { method: "POST", body: fd });
      if (res.ok) { toast("사진에서 얼굴을 등록했습니다"); loadMembers(); }
      else toast((await res.json()).detail, true);
    });
    el.querySelector('[data-act="voice"]').addEventListener("click", async () => {
      await api(`/api/members/${m.id}/voice/clear`, { method: "POST" });
      toast("목소리 정보를 지웠습니다"); loadMembers();
    });
    el.querySelector('[data-act="del"]').addEventListener("click", async () => {
      if (!await uiConfirm(`${m.name} 을(를) 명부에서 지울까요?`, "등록된 사람 삭제")) return;
      try { await api(`/api/members/${m.id}`, { method: "DELETE" }); await loadMembers(); }
      catch (e) { toast(e.message, true); }
    });
    box.appendChild(el);
  });
}

$("#clearAllVoices").addEventListener("click", async () => {
  if (!await uiConfirm("학습된 목소리(성문)를 모두 지울까요? 얼굴로 다시 확인되면 새로 학습합니다.", "학습된 목소리 초기화")) return;
  try {
    const r = await api("/api/members/voices/clear-all", { method: "POST" });
    toast(`${r.cleared}명의 목소리를 초기화했습니다`);
    loadMembers();
  } catch (e) { toast(e.message, true); }
});

$("#addMember").addEventListener("click", async () => {
  const name = $("#memberName").value.trim();
  if (!name) return toast("이름을 넣으세요", true);
  try {
    await api("/api/members", { method: "POST", body: { name, title: $("#memberTitle").value.trim() } });
    $("#memberName").value = "";
    await loadMembers();
    S.glossary = await api("/api/glossary");
    renderGlossary();
    toast("추가했습니다");
  } catch (e) { toast(e.message, true); }
});


/* ───────────────── 녹음 재생기 (기록·요약) ─────────────────
   회의 전체 녹음을 하나로 불러와서, 문장을 누르면 그 시각으로 건너뜁니다.
   받아쓴 내용을 귀로 확인하는 것이 녹취록 검수의 핵심이라 이 부분을 따로 만들었습니다. */
const player = new Audio();
player.preload = "metadata";

function fmtTime(sec) {
  if (!isFinite(sec)) return "0:00";
  return hms(Math.floor(sec));
}

// 재생기는 하나만 두고 지금 보고 있는 탭으로 옮겨 붙입니다.
function movePlayerTo(slotId) {
  const slot = document.getElementById(slotId);
  if (!slot) return;
  slot.appendChild($("#player"));
  slot.appendChild($("#playerHint"));
}

function loadAudio(sessionId, meta, segments) {
  S.player = { id: sessionId, segments: segments || [] };
  const has = meta?.has_audio !== false;
  $("#player").classList.toggle("hidden", !has);
  $("#playerHint").classList.toggle("hidden", !has);
  if (!has) {
    player.removeAttribute("src");
    return;
  }
  if (player.dataset.session !== sessionId) {
    player.src = `/api/sessions/${sessionId}/audio/full`;
    player.dataset.session = sessionId;
  }
  player.playbackRate = parseFloat($("#plSpeed").value) || 1.5;
  $("#plDownload").href = `/api/sessions/${sessionId}/audio/full`;
  $("#plDownload").setAttribute("download", `${sessionId}.wav`);
  S.audioDuration = meta?.audio_duration || 0;
  updatePlayerUI();
}

function playFrom(seconds) {
  if (!player.src) return;
  player.currentTime = Math.max(0, seconds);
  player.play().catch((e) => toast("재생할 수 없습니다: " + e.message, true));
}

function togglePlay() {
  if (!player.src) return;
  if (player.paused) player.play().catch(() => {});
  else player.pause();
}

function currentSegment() {
  const t = player.currentTime;
  return (S.player?.segments || []).find((s) => t >= s.start - 0.05 && t <= s.end + 0.25);
}

function updatePlayerUI() {
  const total = player.duration && isFinite(player.duration) ? player.duration : S.audioDuration;
  $("#plPlay").textContent = player.paused ? "▶" : "❚❚";
  $("#plTime").textContent = `${fmtTime(player.currentTime)} / ${fmtTime(total)}`;
  if (total > 0) $("#plSeek").value = Math.round((player.currentTime / total) * 1000);
}

function highlightPlaying() {
  const seg = currentSegment();
  const box = $("#player").closest(".tabpane")?.querySelector(".transcript") || $("#archTranscript");
  box.querySelectorAll(".seg.playing").forEach((el) => el.classList.remove("playing"));
  if (!seg) return;
  const el = box.querySelector(`.seg[data-id="${seg.id}"]`);
  if (!el) return;
  el.classList.add("playing");
  if ($("#plFollow").checked) {
    const r = el.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (r.top < b.top || r.bottom > b.bottom) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

player.addEventListener("timeupdate", () => {
  updatePlayerUI();
  highlightPlaying();
  if ($("#plLoop").checked) {            // 한 문장 반복
    const seg = S.loopSegment;
    if (seg && player.currentTime > seg.end) playFrom(seg.start);
  }
});
player.addEventListener("play", updatePlayerUI);
player.addEventListener("pause", updatePlayerUI);
player.addEventListener("loadedmetadata", updatePlayerUI);
player.addEventListener("error", () => toast("녹음 파일을 읽지 못했습니다", true));

$("#plPlay").addEventListener("click", togglePlay);
$("#plSeek").addEventListener("input", (e) => {
  const total = player.duration && isFinite(player.duration) ? player.duration : S.audioDuration;
  if (total > 0) player.currentTime = (e.target.value / 1000) * total;
});
$("#plSpeed").addEventListener("change", (e) => { player.playbackRate = parseFloat(e.target.value); });
$("#plLoop").addEventListener("change", (e) => { S.loopSegment = e.target.checked ? currentSegment() : null; });

// 속기 검수용 단축키 (기록·요약 탭에서만, 글을 고치는 중에는 동작하지 않습니다)
document.addEventListener("keydown", (e) => {
  const tab = document.querySelector(".tabpane.active")?.id;
  if (document.querySelector('.modal:not(.hidden), dialog[open]')) return;
  if (!["tab-archive", "tab-live"].includes(tab) || !player.src) return;
  const el = document.activeElement;
  if (el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "SUMMARY"].includes(el.tagName) || el.getAttribute("role") === "tab")) return;

  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft" && e.shiftKey) { e.preventDefault(); jumpSegment(-1); }
  else if (e.key === "ArrowRight" && e.shiftKey) { e.preventDefault(); jumpSegment(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); playerSeek(-3); }
  else if (e.key === "ArrowRight") { e.preventDefault(); playerSeek(3); }
});

function playerSeek(delta) {
  player.currentTime = Math.max(0, player.currentTime + delta);
}

function jumpSegment(dir) {
  const segs = S.player?.segments || [];
  if (!segs.length) return;
  const cur = currentSegment();
  let idx = cur ? segs.indexOf(cur) + dir : 0;
  idx = Math.max(0, Math.min(segs.length - 1, idx));
  playFrom(segs[idx].start);
}

/* ───────────────── 기록·요약 ───────────────── */
/* 폴더: '' 는 전체. 고른 폴더는 다음에 열어도 그대로 */
S.folder = (() => { try { return localStorage.getItem("lt_folder") || ""; } catch (e) { return ""; } })();
S.folders = [];

function paintFolders(sessions) {
  const box = $("#folderList");
  box.innerHTML = "";
  if (S.folder && !S.folders.includes(S.folder)) S.folder = "";
  const row = (name, label, count) => {
    const el = document.createElement("div");
    el.className = "fold-row" + (name === S.folder ? " sel" : "");
    el.innerHTML = `<span class="nm"></span><span class="n">${count}</span>`;
    el.querySelector(".nm").textContent = label;
    if (name) {
      const tools = document.createElement("span");
      tools.className = "sess-tools";
      tools.innerHTML = '<button class="mini" data-act="rename">이름</button>'
                      + '<button class="mini danger-ghost" data-act="delete">삭제</button>';
      tools.querySelector('[data-act="rename"]').addEventListener("click", () => renameFolder(name));
      tools.querySelector('[data-act="delete"]').addEventListener("click", () => deleteFolder(name));
      el.appendChild(tools);
    }
    el.addEventListener("click", (ev) => {
      if (ev.target.closest(".sess-tools")) return;
      S.folder = name;
      try { localStorage.setItem("lt_folder", name); } catch (e) { /* 저장 불가 */ }
      loadSessions().catch(e => toast(e.message,true));
    });
    el.tabIndex = 0;
    el.setAttribute("aria-label",label);
    el.addEventListener("keydown",ev => { if (ev.target === el && ["Enter"," "].includes(ev.key)) { ev.preventDefault(); el.click(); } });
    box.appendChild(el);
  };
  row("", "전체 회의", sessions.length);
  S.folders.forEach((f) => row(f, f, sessions.filter((s) => s.folder === f).length));
  const add = document.createElement("button");
  add.className = "mini fold-add";
  add.textContent = "+ 새 폴더";
  add.addEventListener("click", addFolder);
  box.appendChild(add);
}

async function addFolder() {
  const name = (await uiPrompt("새 폴더 이름") || "").trim();
  if (!name) return;
  try {
    await api("/api/folders", { method: "POST", body: { name } });
    await loadSessions();
  } catch (e) { toast(e.message, true); }
}

async function renameFolder(old) {
  const name = (await uiPrompt("폴더 이름", old) || "").trim();
  if (!name || name === old) return;
  try {
    await api("/api/folders", { method: "PATCH", body: { old, new: name } });
    if (S.folder === old) S.folder = name;
    await loadSessions();
    if (S.archive.id) openSession(S.archive.id).catch(() => {});
  } catch (e) { toast(e.message, true); }
}

async function deleteFolder(name) {
  if (!await uiConfirm(`'${name}' 폴더를 지울까요?\n안의 회의는 지워지지 않고 '전체 회의'에 남습니다.`, "폴더 삭제")) return;
  try {
    await api("/api/folders", { method: "PATCH", body: { old: name, new: "" } });
    if (S.folder === name) S.folder = "";
    await loadSessions();
    if (S.archive.id) openSession(S.archive.id).catch(() => {});
  } catch (e) { toast(e.message, true); }
}

async function loadSessions() {
  const { sessions, folders } = await api("/api/sessions");
  S.sessions = sessions;
  S.folders = folders || [];
  paintFolders(sessions);
  paintFolderSelect();
  paintSessions();
}

function paintSessions() {
  const sessions = S.sessions || [];
  const query = $("#sessionSearch").value.trim().toLocaleLowerCase();
  const filtered = sessions.filter(s => (!S.folder || s.folder === S.folder) && (!query || (s.title || s.id).toLocaleLowerCase().includes(query)));
  $("#sessionCount").textContent = String(filtered.length);
  const box = $("#sessionList");
  box.innerHTML = "";
  if (!filtered.length) {
    const empty = document.createElement("p");
    empty.className = "session-empty";
    empty.textContent = query ? "검색 결과가 없습니다. 다른 제목으로 찾아보세요." : S.folder ? "이 폴더에 저장된 회의가 없습니다." : "아직 저장된 회의가 없습니다. 받아쓰기를 마치면 여기에 나타납니다.";
    box.appendChild(empty);
  }
  filtered.forEach((s) => {
    const el = document.createElement("div");
    el.className = "sess" + (s.id === S.archive.id ? " sel" : "");
    const date = new Date((s.created || 0) * 1000);
    el.innerHTML = `<div class="sess-top"><div class="sess-name"></div>
        <div class="sess-tools">
          <button class="mini" data-act="rename" title="이름 바꾸기">이름</button>
          <button class="mini danger-ghost" data-act="delete" title="지우기">삭제</button>
        </div></div>
      <div class="m">${date.toLocaleString("ko-KR")} · ${hms(s.duration || 0)} · ${s.segments || 0}문장${s.has_summary ? " · 요약✓" : ""}</div>`;
    el.querySelector(".sess-name").textContent = s.title || s.id;
    el.addEventListener("click", (ev) => {
      if (ev.target.closest(".sess-tools")) return;
      openSession(s.id).catch(e => toast("회의를 열지 못했습니다: " + e.message, true));
    });
    el.querySelector('[data-act="rename"]').addEventListener("click", () => renameSession(s));
    el.querySelector('[data-act="delete"]').addEventListener("click", () => deleteSession(s));
    el.tabIndex = 0;
    el.setAttribute("aria-label",s.title || s.id);
    el.setAttribute("aria-current",String(s.id === S.archive.id));
    el.addEventListener("keydown",ev => { if (ev.target === el && ["Enter"," "].includes(ev.key)) { ev.preventDefault(); el.click(); } });
    box.appendChild(el);
  });
}

async function renameSession(s) {
  const title = await uiPrompt("회의 이름", s.title || "");
  if (title == null || !title.trim() || title.trim() === s.title) return;
  try {
    await api(`/api/sessions/${s.id}`, { method: "PATCH", body: { title: title.trim() } });
    if (S.archive.id === s.id) {
      $("#archTitle").textContent = $("#miTitle").value = S.archive.meta.title = title.trim();
    }
    await loadSessions();
    toast("이름을 바꿨습니다");
  } catch (e) { toast(e.message, true); }
}

async function deleteSession(s) {
  const name = s.title || s.id;
  if (stillWriting(s.id)) return toast("받아쓰는 중이거나 마지막 문장을 정리하는 중인 회의는 지울 수 없습니다. 끝난 뒤 지워 주세요.", true);
  if (!await uiConfirm(`'${name}' 회의를 지울까요?\n녹취록·녹음·요약이 모두 지워지고 되돌릴 수 없습니다.`, "회의 삭제")) return;
  try {
    await api(`/api/sessions/${s.id}`, { method: "DELETE" });
    if (S.archive.id === s.id) {
      S.archive = { id: null, segments: [] };
      $("#meetInfo").classList.add("hidden");
      $("#archTitle").textContent = "회의를 선택하세요";
      $("#archTranscript").innerHTML = "";
      showSummary("");
      $("#summaryStatus").textContent = "";
      try { S.audio.pause(); } catch (e) { /* 재생 중 아님 */ }
      $("#player").classList.add("hidden");
      $("#playerHint").classList.add("hidden");
      paintWorkspaceState();
    }
    await loadSessions();
    toast("지웠습니다");
  } catch (e) { toast(e.message, true); }
}

let openingSession = 0;
async function openSession(id) {
  const request = ++openingSession;
  const r = await api(`/api/sessions/${id}`);
  if (request !== openingSession) return;
  S.archive = { id, segments: r.segments, modes: r.meta.speaker_modes || null };
  paintModes("archive");
  S.loopSegment = null;
  $("#archTitle").textContent = r.meta.title || id;
  paintMeetInfo(r.meta, r.segments);
  renderTranscript($("#archTranscript"), r.segments, id);
  movePlayerTo("archivePlayerSlot");
  loadAudio(id, r.meta, r.segments);
  showSummary(r.summary || "");
  $("#summaryStatus").textContent = "";
  $("#summaryBtn").disabled = !!S.summaryJob.id;
  if (S.summaryJob.id === id) paintSummaryJob();     // 요약 중이던 회의로 돌아오면 이어서 보여 줍니다
  paintWorkspaceState();
  loadSessions();
}

/* 회의 정보: 폴더·회의명·일시·장소·인원·참석자. 모르면 비워 두고, AI 요약의 '회의 개요'에 들어갑니다 */
const MEET_FIELDS = { miDate: "date", miPlace: "place", miCount: "count", miAttendees: "attendees" };

function paintFolderSelect() {
  const sel = $("#miFolder");
  const cur = S.archive.meta?.folder || "";
  sel.innerHTML = '<option value="">(폴더 없음)</option>'
    + S.folders.map((f) => `<option>${escapeHtml(f)}</option>`).join("")
    + '<option value="__new">+ 새 폴더…</option>';
  sel.value = cur;
}

function paintMeetInfo(meta, segments) {
  S.archive.meta = meta;
  $("#meetInfo").classList.remove("hidden");
  paintFolderSelect();
  const info = meta.info || {};
  $("#miTitle").value = meta.title || "";
  Object.entries(MEET_FIELDS).forEach(([id, k]) => { $("#" + id).value = info[k] || ""; });
  // 비어 있을 때 참고만 되도록 회색 글씨로 보여 줍니다 (저장되지는 않음)
  $("#miDate").placeholder = meta.created ? new Date(meta.created * 1000).toLocaleString("ko-KR") : "";
  const who = [...new Set((segments || []).map((s) => s.speaker).filter((n) => n && n !== "화자 미상"))];
  $("#miCount").placeholder = who.length ? `녹취록 화자 ${who.length}명` : "";
}

async function saveMeetInfo() {
  const id = S.archive.id;
  if (!id) return;
  const info = {};
  Object.entries(MEET_FIELDS).forEach(([el, k]) => { info[k] = $("#" + el).value.trim(); });
  try {
    await api(`/api/sessions/${id}`, { method: "PATCH", body: { info } });
    S.archive.meta.info = info;
  } catch (e) { toast(e.message, true); }
}
Object.keys(MEET_FIELDS).forEach((id) => $("#" + id).addEventListener("change", saveMeetInfo));

$("#miTitle").addEventListener("change", async () => {
  const title = $("#miTitle").value.trim();
  if (!S.archive.id || !title || title === S.archive.meta.title) return;
  try {
    await api(`/api/sessions/${S.archive.id}`, { method: "PATCH", body: { title } });
    S.archive.meta.title = title;
    $("#archTitle").textContent = title;
    loadSessions();
  } catch (e) { toast(e.message, true); }
});

$("#miFolder").addEventListener("change", async () => {
  const sel = $("#miFolder");
  let folder = sel.value;
  if (folder === "__new") {
    folder = (await uiPrompt("새 폴더 이름") || "").trim();
    if (!folder) { sel.value = S.archive.meta.folder || ""; return; }
  }
  try {
    await api(`/api/sessions/${S.archive.id}`, { method: "PATCH", body: { folder } });
    S.archive.meta.folder = folder;
    await loadSessions();
    toast(folder ? `'${folder}' 폴더로 옮겼습니다` : "폴더에서 뺐습니다");
  } catch (e) { toast(e.message, true); }
});

// AI 요약은 마크다운으로 옵니다. **굵게**, 제목, 목록이 그대로 보이도록 바꿔 줍니다.
function renderMarkdown(md) {
  const inline = (s) => escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");

  const out = [];
  let list = null;                       // "ul" | "ol" | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const raw of md.split("\n")) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);

    if (heading) {
      closeList();
      const level = Math.min(4, heading[1].length + 1);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else if (/^\s*(---+|===+|\*\*\*+)\s*$/.test(line)) {
      closeList();
      out.push("<hr>");
    } else if (/^\s*>/.test(line)) {
      closeList();
      out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`);
    } else if (bullet) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(bullet[1])}</li>`);
    } else if (numbered) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(numbered[2])}</li>`);
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join("\n");
}

/* AI 요약이 진행되는 동안: 받은 글을 한 글자씩 보여 주고, 첫 글자가 오기 전에는 '요약하는 중'과 걸린 시간을 보여 줍니다 */
S.summaryJob = { id: null, text: "", message: "", started: 0, timer: null };

function startSummaryJob(id) {
  clearInterval(S.summaryJob.timer);
  S.summaryJob = { id, text: "", message: "AI가 회의 내용을 읽고 있습니다", started: Date.now(), timer: null };
  S.summaryJob.timer = setInterval(() => { if (S.archive.id === S.summaryJob.id) paintSummaryJob(); }, 1000);
  paintSummaryJob();
}

function endSummaryJob() {
  clearInterval(S.summaryJob.timer);
  S.summaryJob.id = null;
  paintWorkspaceState();
}

function paintSummaryJob() {
  const job = S.summaryJob;
  if (!job.id || job.id !== S.archive.id) return;
  const sec = Math.floor((Date.now() - job.started) / 1000);
  $("#summaryStatus").textContent = `${job.message} · ${hms(sec)}`;
  const box = $("#summaryBox");
  box.classList.remove("hidden");               // 요약하는 동안은 접혀 있어도 펼쳐 보여 줍니다
  $("#summaryBar").classList.remove("hidden");
  $("#summaryFold").textContent = "접기";
  if (job.text) {
    box.innerHTML = renderMarkdown(job.text) + '<span class="type-caret" aria-hidden="true"></span>';
    box.scrollTop = box.scrollHeight;
  } else {
    box.innerHTML = `<div class="summary-wait"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>`
                  + `<b>요약하는 중입니다</b><span>녹취록이 길면 1~2분쯤 걸립니다. 첫 글자가 나오면 이어서 써 내려갑니다.</span></div>`;
  }
  $("#summaryBtn").disabled = true;
}

function showSummary(text) {
  S.summaryShown = text || "";
  const box = $("#summaryBox");
  if (!text) {
    box.classList.add("hidden"); box.textContent = "";
    $("#summaryBar").classList.add("hidden");
    $(".archive-pane").classList.remove("summary-big");      // 요약 없는 회의에서 빈 화면이 되지 않게
    $("#summaryBig").textContent = "크게 보기";
    return;
  }
  box.innerHTML = renderMarkdown(text);
  $("#summaryBar").classList.remove("hidden");
  setSummaryFolded(summaryFoldedPref());
}

// 요약 크게 보기: 녹취록을 잠시 감추고 요약만 창 가득 봅니다
$("#summaryBig").addEventListener("click", () => {
  const big = $(".archive-pane").classList.toggle("summary-big");
  $("#summaryBig").textContent = big ? "녹취록과 함께 보기" : "크게 보기";
  $("#summaryBig").setAttribute("aria-pressed", String(big));
  if (big) setSummaryFolded(false);
});

// AI 요약 접기·펼치기 (한 번 고르면 다음 회의에서도 그대로)
function summaryFoldedPref() {
  try { return localStorage.getItem("lt_summary_folded") !== "0"; } catch (e) { return true; }
}
function setSummaryFolded(folded) {
  $("#summaryBox").classList.toggle("hidden", folded);
  $("#summaryFold").textContent = folded ? "펼치기" : "접기";
  $("#summaryFold").setAttribute("aria-expanded", String(!folded));
}
$("#summaryFold").addEventListener("click", () => {
  const folded = !$("#summaryBox").classList.contains("hidden");
  setSummaryFolded(folded);
  try { localStorage.setItem("lt_summary_folded", folded ? "1" : "0"); } catch (e) { /* 저장 불가 */ }
});

// 받아쓰기 탭 오른쪽 칸들도 하나씩 접을 수 있게 합니다. (기본은 모두 펼침, 고른 상태는 기억)
function loadSideFolds() {
  try { return JSON.parse(localStorage.getItem("lt_side_folded") || "{}"); } catch (e) { return {}; }
}
$$(".side > .card").forEach((card) => {
  const head = card.querySelector(".card-head");
  const key = head.querySelector("b")?.firstChild?.textContent.trim() || "";
  const btn = document.createElement("button");
  btn.className = "mini fold";
  const paint = (folded) => {
    card.classList.toggle("folded", folded);
    btn.textContent = folded ? "펼치기" : "접기";
    btn.setAttribute("aria-expanded", String(!folded));
  };
  paint(!!loadSideFolds()[key]);
  btn.addEventListener("click", () => {
    const folded = !card.classList.contains("folded");
    paint(folded);
    const all = loadSideFolds();
    all[key] = folded;
    try { localStorage.setItem("lt_side_folded", JSON.stringify(all)); } catch (e) { /* 저장 불가 */ }
  });
  head.appendChild(btn);
});

$("#refreshSessions").addEventListener("click", () => loadSessions().catch(e => toast(e.message,true)));
$$("[data-aexport]").forEach((b) => b.addEventListener("click", () => {
  if (!S.archive.id) return toast("회의를 고르세요", true);
  location.href = `/api/sessions/${S.archive.id}/export?format=${b.dataset.aexport}`;
}));

// 받아쓰는 중이거나 마지막 문장을 정리하는 중인 회의 (지우거나 요약하면 녹취가 빠지거나 파일이 깨질 수 있음)
function stillWriting(id) { return S.running && !!id && id === S.sessionId; }

$("#summaryBtn").addEventListener("click", async () => {
  if (!S.archive.id) return toast("회의를 고르세요", true);
  if (stillWriting(S.archive.id)) return toast("받아쓰기가 끝난 뒤에 요약할 수 있습니다. 마지막 문장을 정리하는 중이면 잠시 기다려 주세요.", true);
  if (!hasAiConnection()) {
    if (["claude_cli", "codex_cli"].includes($("#setProvider").value))
      return toast("AI 연결이 없습니다. Claude Code 또는 Codex 로그인을 확인하세요.", true);
    return showKeyGuide("AI 요약을 만들려면 선택한 제공자의 API 키가 필요합니다.");
  }
  $("#summaryBtn").disabled = true;
  S.summaryBefore = $("#summaryBox").classList.contains("hidden") ? "" : S.summaryShown || "";
  startSummaryJob(S.archive.id);
  try {
    await api(`/api/sessions/${S.archive.id}/summary`, { method: "POST" });
  } catch (e) {
    endSummaryJob();
    showSummary(S.summaryBefore || "");
    if (/API 키/.test(e.message)) showKeyGuide(e.message);
    else toast(e.message, true);
    $("#summaryStatus").textContent = "";
    $("#summaryBtn").disabled = false;
  }
});

/* ───────────────── 설정 ───────────────── */
const PERF_HINT = {
  balanced: "인식이 끝나는 대로 계속 갱신 (빠른 PC는 2초마다) · 다른 프로그램에 CPU 양보",
  low: "CPU 최소 · 미리보기 없이 말이 끊길 때 문장으로",
};

function fillSettings(cfg) {
  S.settings = cfg;
  paintKeyState();
  fillAlerts(cfg.alerts || {});
  paintRegionLabel(cfg.speaker?.face_region);
  if (!S.running) { S.liveModes = null; }
  setTimeout(() => { paintModes("live"); paintModes("archive"); });
  const a = cfg.asr, sp = cfg.speaker, g = cfg.glossary, su = cfg.summary;
  $("#setWhisperModel").value = a.model === "large-v3-turbo" ? "" : (a.model || "");
  $("#setDeviceType").value = a.device;
  $("#setCompute").value = a.compute_type; $("#setLang").value = a.language;
  $("#setSilence").value = a.silence_finalize_sec;
  $("#setBeam").value = a.beam_size;
  $("#setPerf").value = a.performance === "low" ? "low" : "balanced";    // 예전 '빠름'은 '보통'으로
  $("#setThreads").value = a.cpu_threads ?? 0;
  $("#setLowPrio").checked = a.low_priority !== false;
  $("#setFace").checked = sp.face_enabled; $("#setFaceTh").value = sp.face_threshold;
  $("#setVoice").checked = sp.voice_enabled; $("#setVoiceTh").value = sp.voice_threshold;
  $("#setAutoEnroll").checked = sp.voice_autoenroll;
  $("#setContext").checked = sp.context_enabled !== false;
  const r = sp.face_region || [];
  $("#setCorrect").checked = g.correction_enabled; $("#setCorrectTh").value = g.correction_threshold;
  $("#setProvider").value = su.provider; $("#setSumModel").value = su.model || "";
  paintProvider();
  paintKeyState();
  $("#setKey").value = su.api_key || "";
  loadMonitors(sp.face_monitor);
  $("#perfHint").textContent = PERF_HINT[$("#setPerf").value] || "";
  settingsReady = true;          // 화면을 채우는 동안에는 저장하지 않습니다
}

async function loadMonitors(sel) {
  try {
    const { monitors } = await api("/api/monitors");
    $("#setMonitor").innerHTML = monitors.map((m) =>
      `<option value="${m.index}" ${m.index === sel ? "selected" : ""}>${m.label} (${m.width}×${m.height})</option>`).join("");
  } catch (e) { /* 모니터 정보 없음 */ }
}

// 설정은 건드리는 즉시 저장합니다. ('저장' 단추 없음)
const SETTING_IDS = ["setPerf", "setWhisperModel", "setDeviceType", "setCompute", "setLang",
                     "setSilence", "setBeam", "setThreads", "setLowPrio", "setFace", "setMonitor",
                     "setFaceTh", "setVoice", "setVoiceTh", "setAutoEnroll", "setContext", "setCorrect", "setCorrectTh", "setProvider",
                     "setKey", "setSumModel"];

function collectSettings() {
  const num = (id) => parseFloat($(id).value);
  return {
    asr: {
      model: $("#setWhisperModel").value.trim() || "large-v3-turbo",
      device: $("#setDeviceType").value,
      compute_type: $("#setCompute").value, language: $("#setLang").value,
      silence_finalize_sec: num("#setSilence"),
      beam_size: parseInt($("#setBeam").value, 10), performance: $("#setPerf").value,
      cpu_threads: parseInt($("#setThreads").value || "0", 10),
      low_priority: $("#setLowPrio").checked,
    },
    speaker: {
      face_enabled: $("#setFace").checked, face_threshold: num("#setFaceTh"),
      face_monitor: parseInt($("#setMonitor").value || "1", 10),
      face_region: S.settings?.speaker?.face_region ?? null,    // 받아쓰기 탭 '드래그로 지정'에서 정합니다
      voice_enabled: $("#setVoice").checked, voice_threshold: num("#setVoiceTh"),
      voice_autoenroll: $("#setAutoEnroll").checked,
      context_enabled: $("#setContext").checked,
    },
    glossary: {
      correction_enabled: $("#setCorrect").checked, correction_threshold: num("#setCorrectTh"),
    },
    summary: {
      provider: $("#setProvider").value, model: $("#setSumModel").value.trim(),
      api_key: $("#setKey").value,
    },
  };
}

// 제공자마다 고를 수 있는 모델 (모델 칸을 누르면 목록이 나옵니다. 직접 입력도 됩니다)
const PROVIDER_MODELS = {
  claude_cli: { key: "", models: [] },
  codex_cli: { key: "", models: [] },
  gemini: { key: "Gemini API 키", models: ["gemini-3.1-flash-lite"] },
  anthropic: { key: "sk-ant-...", models: ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"] },
  openai: { key: "sk-...", models: ["gpt-4.1", "gpt-4.1-mini"] },
  clova: {
    key: "Clova Studio GOV API 키",
    models: [
      ["HCX-GOV-THINK-V1-32B", "HCX-GOV-THINK 32B (기본 · 가장 빠름)"],
      ["openai/gpt-oss-120b", "GPT-OSS 120B"],
      ["K-Exaone-236B", "K-Exaone 236B"],
      ["LLM42-Gemma4", "LLM42-Gemma4 31B"],
      ["NVIDIA-Nemotron-3-Ultra-550B-A55B", "Nemotron-3-Ultra 550B"],
    ],
  },
};

function paintProvider() {
  const p = PROVIDER_MODELS[$("#setProvider").value] || PROVIDER_MODELS.gemini;
  const login = ["claude_cli", "codex_cli"].includes($("#setProvider").value);
  $("#setKeyRow").classList.toggle("hidden", login);
  $("#openKeyGuide").classList.toggle("hidden", login);
  const list = p.models.map((m) => (Array.isArray(m) ? m : [m, ""]));
  $("#sumModels").innerHTML = list.map(([id, label]) =>
    `<option value="${escapeHtml(id)}"${label ? ` label="${escapeHtml(label)}"` : ""}></option>`).join("");
  $("#setSumModel").placeholder = login ? "비우면 로그인 도구의 기본 모델" : list[0][0];
  if (!$("#setKey").value) $("#setKey").placeholder = p.key;
}

// 제공자를 바꾸면 이전 제공자의 모델 이름은 맞지 않으므로 비웁니다.
$("#setProvider").addEventListener("change", () => {
  $("#setSumModel").value = "";
  paintProvider();
  paintKeyState();
});

let settingsTimer;
let settingsReady = false;

async function saveSettingsNow() {
  if (!settingsReady) return;
  const msg = $("#saveMsg");
  const invalid = SETTING_IDS.map(id => document.getElementById(id)).find(el => el &&
    (!el.checkValidity() || (el.type === "number" && el.id !== "setThreads" && !el.value.trim())));
  if (invalid) { msg.textContent = "입력 범위를 확인해 주세요. 아직 저장되지 않았습니다."; return; }
  msg.textContent = "설정을 저장하고 있습니다…";
  try {
    const cfg = await api("/api/settings", { method: "POST", body: collectSettings() });
    S.settings = cfg;
    paintKeyState();
    msg.textContent = "저장했습니다 · " + new Date().toLocaleTimeString("ko-KR");
    $("#perfHint").textContent = PERF_HINT[$("#setPerf").value] || "";
  } catch (e) { msg.textContent = "저장하지 못했습니다. 연결을 확인한 뒤 다시 변경해 주세요."; toast("설정을 저장하지 못했습니다: " + e.message, true); }
}

SETTING_IDS.forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  const ev = el.tagName === "INPUT" && el.type !== "checkbox" ? "input" : "change";
  el.addEventListener(ev, () => {
    clearTimeout(settingsTimer);
    settingsTimer = setTimeout(saveSettingsNow, el.type === "password" ? 1200 : 400);
  });
});

/* ───────────────── 새 버전 알림 ─────────────────
   GitLab 패키지 저장소의 최신 버전 정보(latest.json)를 확인합니다.
   인터넷이 막힌 PC에서는 아무것도 하지 않습니다. */
async function checkUpdate() {
  try {
    const u = await api("/api/update");
    if (!u.update_available) return;
    if (localStorage.getItem("lt_skip_version") === u.latest) return;   // 넘어가기 누른 버전
    S.update = u;
    $("#updateText").innerHTML =
      `새 버전 <b>${escapeHtml(u.latest)}</b> 이 나왔습니다. (지금 ${escapeHtml(u.current)})` +
      (u.notes ? " — " + escapeHtml(u.notes.split(/\r?\n/)[0].slice(0, 80)) : "");
    $("#updateBar").classList.remove("hidden");
  } catch (e) { /* 확인 실패는 조용히 넘어갑니다 */ }
}

$("#updateGet").addEventListener("click", async () => {
  const u = S.update;
  if (!u) return;
  if (S.running && !await uiConfirm("받아쓰기가 진행 중입니다. 그래도 새 버전을 받으시겠습니까?")) return;
  // 설치 파일이 있으면 그것을, 없으면 릴리스 페이지를 엽니다.
  window.open(u.download?.url || u.page, "_blank");
  toast("내려받은 설치 파일을 실행하면 프로그램만 새로 덮어씁니다. 기록과 설정은 그대로입니다.");
});

$("#updateSkip").addEventListener("click", () => {
  if (S.update?.latest) localStorage.setItem("lt_skip_version", S.update.latest);
  $("#updateBar").classList.add("hidden");
});

/* ───────────────── 화면 설정 기억하기 ─────────────────
   서버에 저장할 만한 값은 아니지만 매번 다시 고르기 번거로운 것들입니다. */
const UI_KEYS = ["title", "source", "fileUrl", "startSec", "autoscroll", "plSpeed", "plFollow",
                 "termCategory", "bulkCategory"];

function saveUiPrefs() {
  const out = {};
  for (const id of UI_KEYS) {
    const el = document.getElementById(id);
    if (el) out[id] = el.type === "checkbox" ? el.checked : el.value;
  }
  try { localStorage.setItem("lt_ui", JSON.stringify(out)); } catch (e) { /* 저장 불가 */ }
}

function restoreUiPrefs() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("lt_ui") || "{}"); } catch (e) { return; }
  for (const [id, value] of Object.entries(saved)) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value;
  }
  updateSourceFields();
  if ($("#plSpeed").value) player.playbackRate = parseFloat($("#plSpeed").value) || 1;
}

UI_KEYS.forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", saveUiPrefs);
});

/* ───────────────── 시작 ───────────────── */
async function init() {
  try {
    const st = await api("/api/state");
    S.glossary = st.glossary;
    S.members = st.members;
    fillSettings(st.settings);
    restoreUiPrefs();
    applyStatus(st.status);
    renderGlossary();
    await loadMembers();
    await loadDevices();
    if (st.status.id) {
      const cur = await api("/api/session/current");
      S.segments = cur.segments;
      renderTranscript($("#transcript"), cur.segments, st.status.id);
      $("#segCount").textContent = `${cur.segments.length}문장`;
    }
    if (st.version) $("#appVersion").textContent = "v" + st.version;
    checkUpdate();
    if (st.voice_error) toast("목소리 화자구분 사용 불가: " + st.voice_error, true);
  } catch (e) {
    S.settings = null;               // 연결되면 다시 시도합니다
    toast("서버에 연결하는 중입니다...", true);
  }
}

connect();
init();

/* ───────────────── AI API 키 안내 ─────────────────
   AI 요약은 외부 AI를 부르므로 키가 필요합니다. 없으면 어디에 넣는지, 어떻게 받는지 알려 줍니다. */
function hasAiConnection() {
  const su = S.settings?.summary || {};
  return su.provider === $("#setProvider").value && !!su.ai_ready;
}

function paintKeyState() {
  const el = $("#keyState");
  if (!el) return;
  const login = ["claude_cli", "codex_cli"].includes($("#setProvider").value);
  const ok = hasAiConnection();
  el.textContent = login
    ? (ok ? "로그인된 AI 도구를 사용할 수 있습니다."
          : "Claude Code 또는 Codex 로그인이 필요합니다."
            + (S.settings?.summary?.ai_reason ? ` (확인 실패 이유: ${S.settings.summary.ai_reason})` : ""))
    : (ok ? "API 키가 연결되어 있습니다." : "API 키가 아직 없습니다.");
  el.className = "hint " + (ok ? "ok" : "missing");
}

function openKeyGuide() { window.open("/static/api-key-guide.html", "_blank"); }

function showKeyGuide(reason) {
  $("#keyReason").textContent = reason;
  $("#keyModal").classList.remove("hidden");
}
$("#keyClose").addEventListener("click", () => $("#keyModal").classList.add("hidden"));
$("#keyGuide").addEventListener("click", openKeyGuide);
$("#openKeyGuide").addEventListener("click", openKeyGuide);
$("#keyGoSettings").addEventListener("click", () => {
  $("#keyModal").classList.add("hidden");
  showTab("settings");
  const key = $("#setKey");
  key.scrollIntoView({ block: "center" });
  key.focus();
});

/* ───────────────── 프로그램을 다시 켜야 하는지 ─────────────────
   화면 파일은 새로고침으로 바뀌지만 서버 코드는 프로그램을 다시 켜야 바뀝니다. */
async function checkRestart() {
  try {
    const r = await api("/api/restart-needed");
    $("#restartBar").classList.toggle("hidden", !r.restart_needed);
  } catch (e) {
    // 옛 서버에는 이 확인 기능이 없습니다 = 다시 켜야 하는 상태
    if (/not found/i.test(e.message)) $("#restartBar").classList.remove("hidden");
  }
}
checkRestart();
setInterval(checkRestart, 120000);

/* ───────────────── 관심 키워드 ─────────────────
   담당 사업명·부서명 같은 키워드가 발언에 나오면 알려 주고, 녹취록에 형광펜으로 표시합니다.
   찾는 일은 서버가 하고(창이 가려져 있어도 Windows 알림), 화면은 표시와 목록을 맡습니다. */
S.alerts = { keywords: [], os_notify: true };
let kwRegex = null;

function buildKwRegex() {
  // 음성인식은 띄어쓰기가 들쭉날쭉하므로 글자 사이 띄어쓰기를 무시하고 찾습니다
  const parts = S.alerts.keywords
    .map((k) => [...k.replace(/\s+/g, "")].map((ch) => ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s*"))
    .filter(Boolean)
    .sort((x, y) => y.length - x.length);
  kwRegex = parts.length ? new RegExp(parts.join("|"), "gi") : null;
}

function hasKeyword(text) {
  if (!kwRegex || !text) return false;
  kwRegex.lastIndex = 0;
  return kwRegex.test(text);
}

function highlightHtml(text) {
  text = text || "";
  if (!kwRegex) return escapeHtml(text);
  let out = "", last = 0;
  kwRegex.lastIndex = 0;
  for (const m of text.matchAll(kwRegex)) {
    if (!m[0]) continue;
    out += escapeHtml(text.slice(last, m.index)) + `<mark class="kw">${escapeHtml(m[0])}</mark>`;
    last = m.index + m[0].length;
  }
  return out + escapeHtml(text.slice(last));
}

function fillAlerts(a) {
  S.alerts = { keywords: [...(a.keywords || [])], os_notify: a.os_notify !== false };
  $("#kwNotify").checked = S.alerts.os_notify;
  buildKwRegex();
  paintKeywords();
}

function paintKeywords() {
  const box = $("#kwChips");
  box.innerHTML = "";
  S.alerts.keywords.forEach((k, i) => {
    const chip = document.createElement("span");
    chip.className = "kw-chip";
    chip.innerHTML = `<span></span><button title="지우기" aria-label="${escapeHtml(k)} 지우기">×</button>`;
    chip.firstChild.textContent = k;
    chip.querySelector("button").addEventListener("click", () => {
      S.alerts.keywords.splice(i, 1);
      saveAlerts();
    });
    box.appendChild(chip);
  });
  $("#kwCount").textContent = S.alerts.keywords.length ? `${S.alerts.keywords.length}개` : "";
}

// 키워드가 바뀌면 이미 올라온 문장의 형광펜도 다시 칠합니다
function rehighlight() {
  $$(".transcript .seg").forEach((row) => {
    if (!row._seg) return;
    const tx = row.querySelector(".tx");
    if (tx && tx.contentEditable !== "true") tx.innerHTML = highlightHtml(row._seg.text);
    row.classList.toggle("kw-hit", hasKeyword(row._seg.text));
  });
}

async function saveAlerts() {
  S.alerts.os_notify = $("#kwNotify").checked;
  buildKwRegex();
  paintKeywords();
  rehighlight();
  try {
    const cfg = await api("/api/settings", { method: "POST", body: { alerts: S.alerts } });
    S.settings = cfg;
  } catch (e) { toast("키워드를 저장하지 못했습니다: " + e.message, true); }
}

function addKeywords() {
  const raw = $("#kwInput").value;
  const words = raw.split(/[,\n]/).map((w) => w.trim()).filter(Boolean);
  if (!words.length) return;
  words.forEach((w) => { if (!S.alerts.keywords.includes(w)) S.alerts.keywords.push(w); });
  $("#kwInput").value = "";
  saveAlerts();
}
$("#kwAdd").addEventListener("click", addKeywords);
$("#kwInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addKeywords(); } });
$("#kwNotify").addEventListener("change", saveAlerts);

function onKeyword(e) {
  const who = e.speaker && e.speaker !== "화자 미상" ? e.speaker : "발언";
  const el = $("#toast");
  el.textContent = `관심 키워드 ‘${e.keywords.join(", ")}’ — ${who}`;
  el.className = "toast kw";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);

  const item = document.createElement("button");
  item.className = "kw-hit-item";
  item.innerHTML = `<span class="t">${hms(e.t || 0)}</span><span class="who"></span><span class="say"></span>`;
  item.querySelector(".who").textContent = `${who} · ${e.keywords.join(", ")}`;
  item.querySelector(".say").innerHTML = highlightHtml(e.text || "");
  item.addEventListener("click", () => {
    const row = $("#transcript").querySelector(`.seg[data-id="${e.segment_id}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
  });
  $("#kwHits").prepend(item);
  while ($("#kwHits").children.length > 30) $("#kwHits").lastChild.remove();
}

/* ───────────────── 화면 감시 영역: 드래그로 고르기 ───────────────── */
function paintRegionLabel(r) {
  $("#regionLabel").textContent = Array.isArray(r) && r.length === 4
    ? `${r[2]}×${r[3]} (${r[0]}, ${r[1]})` : "모니터 전체";
}

async function saveRegion(region) {
  const monitor = parseInt($("#setMonitor").value || "1", 10);
  try {
    const cfg = await api("/api/settings", { method: "POST", body: { speaker: { face_region: region, face_monitor: monitor } } });
    S.settings = cfg;
    paintRegionLabel(cfg.speaker.face_region);
    toast(region ? "이 영역만 감시합니다" : "모니터 전체를 감시합니다");
  } catch (e) { toast(e.message, true); }
}

const region = { shot: null, start: null, rect: null };

async function takeShot() {
  const stage = $("#regionStage");
  stage.classList.add("loading");
  $("#regionStage .region-error")?.remove();
  $("#regionShot").classList.remove("hidden");
  $("#regionShot").removeAttribute("src");
  $("#regionRect").classList.add("hidden");
  $("#regionApply").disabled = true;
  $("#regionSize").textContent = "화면을 찍는 중…";
  region.rect = null;
  try {
    const monitor = parseInt($("#setMonitor").value || "1", 10);
    region.shot = await api(`/api/face/screen?monitor=${monitor}`);
    $("#regionShot").src = region.shot.image;
    $("#regionSize").textContent = `${region.shot.width}×${region.shot.height}`;
    // 지금 쓰는 영역이 이 모니터 안이면 미리 그려 둡니다
    const r = S.settings?.speaker?.face_region;
    $("#regionShot").onload = () => {
      stage.classList.remove("loading");
      if (Array.isArray(r) && r.length === 4) showRect(toStage(r));
    };
  } catch (e) {
    stage.classList.remove("loading");
    $("#regionSize").textContent = "";
    const msg = /not found/i.test(e.message)
      ? "화면을 찍는 기능이 아직 켜지지 않았습니다. 프로그램을 껐다가 다시 켠 뒤 해 주세요."
      : `화면을 찍지 못했습니다: ${e.message}`;
    showRegionError(msg);
  }
}

function showRegionError(msg) {
  $("#regionShot").classList.add("hidden");
  let box = $("#regionStage .region-error");
  if (!box) {
    box = document.createElement("div");
    box.className = "region-error";
    $("#regionStage").appendChild(box);
  }
  box.textContent = msg;
}

// 화면(실제 픽셀) ↔ 창 속 그림 좌표
function stageScale() {
  const img = $("#regionShot");
  return { sx: region.shot.width / img.clientWidth, sy: region.shot.height / img.clientHeight };
}
function toStage(r) {
  const { sx, sy } = stageScale();
  return { x: (r[0] - region.shot.left) / sx, y: (r[1] - region.shot.top) / sy, w: r[2] / sx, h: r[3] / sy };
}
function toScreen(b) {
  const { sx, sy } = stageScale();
  return [Math.round(region.shot.left + b.x * sx), Math.round(region.shot.top + b.y * sy),
          Math.round(b.w * sx), Math.round(b.h * sy)];
}

function showRect(b) {
  const img = $("#regionShot");
  const x = Math.max(0, Math.min(b.x, img.clientWidth)), y = Math.max(0, Math.min(b.y, img.clientHeight));
  const w = Math.max(0, Math.min(b.w, img.clientWidth - x)), h = Math.max(0, Math.min(b.h, img.clientHeight - y));
  if (w < 2 || h < 2) return;
  region.rect = { x, y, w, h };
  Object.assign($("#regionRect").style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
  $("#regionRect").classList.remove("hidden");
  const r = toScreen(region.rect);
  $("#regionSize").textContent = `${r[2]}×${r[3]} 영역`;
  $("#regionApply").disabled = r[2] < 40 || r[3] < 40;       // 너무 작으면 얼굴을 찾을 수 없습니다
}

const stage = $("#regionStage");
stage.addEventListener("pointerdown", (e) => {
  if (!region.shot || !$("#regionShot").complete) return;
  const box = $("#regionShot").getBoundingClientRect();
  region.start = { x: e.clientX - box.left, y: e.clientY - box.top };
  stage.setPointerCapture(e.pointerId);
});
stage.addEventListener("pointermove", (e) => {
  if (!region.start) return;
  const box = $("#regionShot").getBoundingClientRect();
  const x = e.clientX - box.left, y = e.clientY - box.top;
  showRect({ x: Math.min(x, region.start.x), y: Math.min(y, region.start.y),
             w: Math.abs(x - region.start.x), h: Math.abs(y - region.start.y) });
});
stage.addEventListener("pointerup", () => { region.start = null; });

$("#pickRegion").addEventListener("click", () => {
  $("#regionModal").classList.remove("hidden");
  takeShot();
});
$("#regionRetake").addEventListener("click", takeShot);
$("#regionCancel").addEventListener("click", () => $("#regionModal").classList.add("hidden"));
$("#regionApply").addEventListener("click", () => {
  if (!region.rect) return;
  const r = toScreen(region.rect);                 // 창을 닫으면 그림 크기가 0이 되므로 먼저 바꿉니다
  $("#regionModal").classList.add("hidden");
  saveRegion(r);
});
$("#clearRegion").addEventListener("click", () => saveRegion(null));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#regionModal").classList.contains("hidden")) $("#regionModal").classList.add("hidden");
});

/* 성능 모드는 받아쓰는 도중에 바꿔도 다음 인식부터 바로 적용됩니다 (서버가 적용) */
$("#setPerf").addEventListener("change", () => {
  if (S.running) toast("성능 모드를 바꿨습니다. 받아쓰는 중에도 바로 적용됩니다.");
});

