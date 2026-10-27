/* 화면 공통 동작. 네트워크·음성인식은 app.js의 기존 경로를 사용합니다. */
function uiDialog({
  title,
  message = "",
  value,
  multiline = false,
  confirmLabel = "저장",
  danger = false,
}) {
  const dialog = document.getElementById("actionDialog");
  if (dialog.open) return Promise.resolve(null);
  const input = document.getElementById(
    multiline ? "actionText" : "actionInput",
  );
  const field = document.getElementById("actionField");
  const ok = document.getElementById("actionOk");
  document.getElementById("actionTitle").textContent = title;
  document.getElementById("actionMessage").textContent = message;
  document.getElementById("actionLabel").textContent = title;
  field.classList.toggle("hidden", value === undefined);
  document.getElementById("actionInput").classList.toggle("hidden", multiline);
  document.getElementById("actionText").classList.toggle("hidden", !multiline);
  input.value = value ?? "";
  ok.textContent = confirmLabel;
  ok.className = danger ? "danger" : "primary";
  ok.disabled = value !== undefined && !input.value.trim();
  input.oninput = () => {
    ok.disabled = !input.value.trim();
  };
  const previous = document.activeElement;
  dialog.returnValue = "cancel";
  dialog.showModal();
  if (value !== undefined) {
    input.focus();
    input.select();
  } else dialog.querySelector('[value="cancel"]').focus();
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => {
        const answer =
          dialog.returnValue === "ok"
            ? value === undefined
              ? true
              : input.value.trim()
            : null;
        if (previous?.isConnected) previous.focus();
        resolve(answer);
      },
      { once: true },
    );
  });
}
function uiConfirm(message, title = "계속 진행할까요?") {
  return uiDialog({
    title,
    message,
    confirmLabel: /지우|삭제/.test(message) ? "삭제" : "확인",
    danger: /지우|삭제|초기화/.test(message),
  });
}
function uiPrompt(title, value = "") {
  return uiDialog({ title, value });
}

function paintEmptyTranscript(container) {
  if (!container || container.querySelector(".seg")) return;
  const live = container.id === "transcript";
  const running = live && S.running;
  const selected = !live && S.archive.id;
  const title = running
    ? "회의의 첫 문장을 기다리고 있어요."
    : live
      ? "첫 문장부터, 차곡차곡."
      : selected
        ? "아직 기록된 문장이 없습니다."
        : "다시 펼쳐볼 회의를 선택하세요.";
  const detail = running
    ? "소리가 들어오면 인식한 문장이 여기에 쌓입니다."
    : live
      ? "위에서 소리를 고르고 받아쓰기를 시작하세요.<br>확정된 문장은 자동으로 저장됩니다."
      : selected
        ? "받아쓰기 상태를 확인하거나 다른 회의를 선택해 주세요."
        : "왼쪽에서 회의를 고르면 녹취록과 요약을 볼 수 있습니다.<br>문장을 눌러 녹음을 들으며 검수하세요.";
  container.innerHTML = `<div class="empty-state"><span class="empty-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 3h10l4 4v14H5zM14 3v5h5M9 12h6m-6 4h4"/></svg></span><b>${title}</b><p>${detail}</p><span class="empty-foot">${live ? "음성인식은 이 PC에서 처리합니다" : "회의 기록 · 녹음 · AI 요약을 한곳에서"}</span></div>`;
}

function paintWorkspaceState() {
  document
    .querySelectorAll("#source, #device, #fileInput, #fileUrl, #startSec")
    .forEach((el) => {
      el.disabled = !!S.running || !!S.uploading;
    });
  const button = document.getElementById("summaryBtn");
  button.disabled =
    !S.archive.id ||
    !!S.summaryJob?.id ||
    (S.running && S.archive.id === S.sessionId);
  document.querySelectorAll("[data-aexport]").forEach((b) => {
    b.disabled = !S.archive.id;
  });
  document.querySelectorAll("[data-export]").forEach((b) => {
    b.disabled = !S.sessionId;
  });
  document
    .querySelector('.spk-bar[data-scope="archive"]')
    .classList.toggle("hidden", !S.archive.id);
  paintEmptyTranscript(document.getElementById("transcript"));
  paintEmptyTranscript(document.getElementById("archTranscript"));
}

document.addEventListener("DOMContentLoaded", () => {
  const tabNames = {
    live: "받아쓰기",
    archive: "기록·요약",
    glossary: "용어집",
    settings: "설정",
  };
  document.querySelectorAll(".tab").forEach((button) => {
    button.setAttribute("aria-label", tabNames[button.dataset.tab]);
    button.title = tabNames[button.dataset.tab];
    button.addEventListener("click", () => {
      document.getElementById("workspaceTitle").textContent =
        tabNames[button.dataset.tab];
      document.querySelectorAll(".tab").forEach((b) => {
        if (b === button) b.setAttribute("aria-current", "page");
        else b.removeAttribute("aria-current");
      });
      document.querySelector(".skip-link").href = "#tab-" + button.dataset.tab;
    });
  });
  document
    .querySelectorAll("[data-go]")
    .forEach((button) =>
      button.addEventListener("click", () => showTab(button.dataset.go)),
    );
  const toolButtons = [...document.querySelectorAll("[data-tool]")];
  function selectTool(button) {
    toolButtons.forEach((b) => {
      b.setAttribute("aria-selected", String(b === button));
      b.tabIndex = b === button ? 0 : -1;
    });
    document.querySelectorAll("[data-tool-panel]").forEach((panel) => {
      panel.classList.toggle(
        "hidden",
        panel.dataset.toolPanel !== button.dataset.tool,
      );
    });
  }
  toolButtons.forEach((button, index) => {
    button.tabIndex = index ? -1 : 0;
    button.addEventListener("click", () => selectTool(button));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const i =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? toolButtons.length - 1
            : (index +
                (event.key === "ArrowRight" ? 1 : -1) +
                toolButtons.length) %
              toolButtons.length;
      selectTool(toolButtons[i]);
      toolButtons[i].focus();
    });
  });
  document.querySelectorAll("[data-setting]").forEach((button) =>
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-setting]").forEach((b) => {
        b.classList.toggle("active", b === button);
        b.setAttribute("aria-pressed", String(b === button));
      });
      document
        .querySelectorAll("[data-setting-panel]")
        .forEach((panel) =>
          panel.classList.toggle(
            "hidden",
            panel.dataset.settingPanel !== button.dataset.setting,
          ),
        );
    }),
  );
  document
    .getElementById("keyGoSettings")
    .addEventListener("click", () =>
      document.querySelector('[data-setting="ai"]').click(),
    );
  document
    .getElementById("helpBtn")
    .addEventListener("click", () =>
      document.getElementById("helpModal").classList.remove("hidden"),
    );
  document
    .getElementById("helpClose")
    .addEventListener("click", () =>
      document.getElementById("helpModal").classList.add("hidden"),
    );
  document.querySelectorAll(".export-menu").forEach((menu) => {
    menu.addEventListener("toggle", () => {
      if (menu.open)
        document.querySelectorAll(".export-menu").forEach((other) => {
          if (other !== menu) other.open = false;
        });
    });
    menu.addEventListener("click", (event) => {
      if (event.target.closest("button")) menu.open = false;
    });
  });
  document.addEventListener("click", (event) =>
    document.querySelectorAll(".export-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    }),
  );
  // 앱 대화상자: 키보드가 배경 화면으로 빠지지 않게 하고, 닫으면 원래 버튼으로 돌아갑니다.
  const closers = {
    speakerModal: "speakerCancel",
    regionModal: "regionCancel",
    keyModal: "keyClose",
    helpModal: "helpClose",
  };
  let previousFocus = document.activeElement;
  document.addEventListener("focusin", (event) => {
    if (!event.target.closest(".modal,dialog")) previousFocus = event.target;
  });
  document.querySelectorAll(".modal").forEach((modal) => {
    new MutationObserver(() => {
      const open = !modal.classList.contains("hidden");
      document
        .querySelectorAll("body > header, body > main, .workspace-bar")
        .forEach((el) => {
          el.inert = !!document.querySelector(".modal:not(.hidden)");
        });
      if (open) {
        modal._returnFocus = previousFocus;
        if (!modal.contains(document.activeElement))
          modal
            .querySelector("input:not([disabled]),button:not([disabled])")
            ?.focus();
      } else if (modal._returnFocus?.isConnected) modal._returnFocus.focus();
    }).observe(modal, { attributes: true, attributeFilter: ["class"] });
    modal.addEventListener("click", (event) => {
      if (event.target === modal)
        document.getElementById(closers[modal.id])?.click();
    });
  });
  document.addEventListener("keydown", (event) => {
    if (document.getElementById("actionDialog").open) return;
    const modal = document.querySelector(".modal:not(.hidden)");
    if (modal) {
      if (event.key === "Escape") {
        event.preventDefault();
        document.getElementById(closers[modal.id])?.click();
      }
      if (event.key === "Tab") {
        const focusable = [
          ...modal.querySelectorAll(
            'button,input,select,textarea,[tabindex="0"]',
          ),
        ].filter((el) => !el.disabled && el.getClientRects().length);
        const first = focusable[0],
          last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
      return;
    }
    if (event.key === "Escape")
      document.querySelectorAll(".export-menu[open]").forEach((menu) => {
        menu.open = false;
        menu.querySelector("summary").focus();
      });
    if (
      event.key === "?" &&
      !event.target.closest('input,textarea,select,[contenteditable="true"]')
    )
      document.getElementById("helpBtn").click();
  });
  // placeholder만 있던 입력에도 보조기기가 읽을 수 있는 이름을 제공합니다.
  document.querySelectorAll("input,textarea,select").forEach((el) => {
    if (
      !el.getAttribute("aria-label") &&
      !el.closest("label") &&
      !document.querySelector(`label[for="${el.id}"]`)
    )
      el.setAttribute(
        "aria-label",
        el.getAttribute("placeholder") || el.getAttribute("title") || el.id,
      );
  });
  document
    .getElementById("sessionSearch")
    .addEventListener("input", paintSessions);
  paintWorkspaceState();
});
