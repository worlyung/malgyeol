async (page) => {
  const checks = [],
    errors = [],
    failures = [];
  const assert = (ok, label) => {
    if (!ok) throw new Error(label);
    checks.push(label);
  };
  const base = "http://127.0.0.1:8876";
  const output = "C:/Users/Admin/Desktop/livecaption-v1.0.7/docs";
  page.on("pageerror", (e) => errors.push(e.message));
  // 실제 AI 호출과 화면 캡처를 실행하지 않고 화면 처리만 검증합니다.
  await page.route("**/api/sessions/*/summary", (route) =>
    route.fulfill({ json: { ok: true } }),
  );
  await page.route("**/api/material", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          json: { terms: ["검증자료 용어"], added: ["검증자료 용어"] },
        })
      : route.continue(),
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base);
  await page.waitForFunction(() => !!S.settings);
  assert(
    (await page.locator("#transcript .empty-state").count()) === 1,
    "첫 실행 안내",
  );
  await page.locator("[data-tool=speaker]").click();
  assert(await page.locator("#faceToggle").isVisible(), "화자 도구 전환");
  await page.locator("[data-tool=notes]").click();
  assert(
    (await page.locator("#memo").isVisible()) &&
      (await page.locator("#kwInput").isVisible()),
    "메모·키워드 도구 전환",
  );
  await page.locator("[data-tool=prepare]").click();
  await page.locator("#helpBtn").click();
  assert(await page.locator("#helpModal").isVisible(), "도움말");
  await page.keyboard.press("Escape");
  assert(await page.locator("#helpModal").isHidden(), "도움말 Escape 닫기");
  assert(
    await page
      .locator("#helpBtn")
      .evaluate((el) => el === document.activeElement),
    "도움말 포커스 복원",
  );
  await page.locator("#source").selectOption("file");
  await page.locator("#startBtn").click();
  assert(
    (await page.locator("#toast").innerText()).includes("파일을 고르거나"),
    "파일 미선택 안내",
  );
  assert(await page.locator("#startBtn").isEnabled(), "시작 검증 후 버튼 복구");
  await page.locator("#fileUrl").fill("invalid-address");
  await page.locator("#startBtn").click();
  assert(
    (await page.locator("#toast").innerText()).includes("http"),
    "잘못된 영상 주소 안내",
  );
  await page.locator("#fileUrl").fill("");
  await page.route("**/api/upload", (route) =>
    route.fulfill({ status: 500, json: { detail: "검증용 업로드 실패" } }),
  );
  await page.locator("#fileInput").setInputFiles({
    name: "ui-test.wav",
    mimeType: "audio/wav",
    buffer: Buffer.from("test"),
  });
  await page.locator("#startBtn").click();
  await page.waitForFunction(() => !S.uploading);
  assert(
    (await page.locator("#startBtn").isEnabled()) &&
      (await page.locator("#toast").innerText()).includes("업로드 실패"),
    "업로드 실패 복구",
  );
  await page.locator("#fileInput").setInputFiles([]);
  await page.unroute("**/api/upload");
  await page.locator("#source").selectOption("loopback");
  await page.screenshot({ path: output + "/ui-live.png" });

  await page.locator("[data-tab=archive]").click();
  await page.locator(".sess").first().waitFor();
  await page.locator("#sessionSearch").fill("지역축제");
  assert((await page.locator(".sess").count()) === 1, "회의 제목 검색");
  await page.locator("#sessionSearch").fill("없는회의");
  assert(
    (await page.locator(".session-empty").innerText()).includes(
      "검색 결과가 없습니다",
    ),
    "검색 결과 없음",
  );
  await page.locator("#sessionSearch").fill("");
  await page.locator(".sess").first().click();
  await page.waitForFunction(
    () => document.querySelectorAll("#archTranscript .seg").length === 6,
  );
  const sid = await page.evaluate(() => S.archive.id);
  await page.locator("#summaryFold").click();
  await page.locator("#summaryFold").click();
  if (await page.locator("#summaryBox").isVisible())
    await page.locator("#summaryFold").click();
  await page.locator("#archTranscript .seg [data-act=edit]").first().click();
  await page
    .locator("#actionText")
    .fill("UI 검증: 문화예술 행사 운영 계획을 점검하겠습니다.");
  await page.locator("#actionOk").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#archTranscript .tx")
      .textContent.startsWith("UI 검증:"),
  );
  const record = await (
    await page.request.get(base + "/api/sessions/" + sid)
  ).json();
  assert(record.segments[0].text.startsWith("UI 검증:"), "문장 수정 실제 저장");
  await page.locator("#archTranscript .seg [data-act=edit]").first().click();
  await page.locator("#actionText").fill("취소될 내용");
  await page.keyboard.press("Escape");
  assert(
    (await page.locator("#archTranscript .tx").first().innerText()).startsWith(
      "UI 검증:",
    ),
    "문장 수정 취소",
  );
  await page.route("**/api/sessions/*/segments/*", (route) =>
    route.fulfill({ status: 500, json: { detail: "검증용 저장 실패" } }),
  );
  await page.locator("#archTranscript .seg [data-act=edit]").first().click();
  await page.locator("#actionText").fill("실패할 수정");
  await page.locator("#actionOk").click();
  await page.waitForTimeout(150);
  assert(
    (await page.locator("#archTranscript .tx").first().innerText()).startsWith(
      "UI 검증:",
    ),
    "수정 저장 실패 시 원문 보존",
  );
  await page.unroute("**/api/sessions/*/segments/*");
  await page.locator("#archTranscript .seg [data-act=speaker]").first().click();
  assert(await page.locator("#speakerModal").isVisible(), "화자 지정 대화상자");
  await page.keyboard.press("Escape");
  assert(await page.locator("#speakerModal").isHidden(), "화자 지정 취소");
  await page.keyboard.press("Control+f");
  await page.keyboard.type("문화");
  await page.waitForTimeout(250);
  assert(
    (await page.locator("#tab-archive .find-count").innerText()).includes("/"),
    "녹취록 검색 결과",
  );
  await page.keyboard.press("Escape");
  await page.locator("#meetInfo summary").click();
  await page.locator("#miPlace").fill("UI 검증 회의실");
  await page.locator("#miPlace").press("Tab");
  await page.waitForTimeout(150);
  const saved = await (
    await page.request.get(base + "/api/sessions/" + sid)
  ).json();
  assert(saved.meta.info.place === "UI 검증 회의실", "회의 정보 저장");
  await page.locator("#meetInfo summary").click();
  await page.locator("#plPlay").click();
  await page.waitForTimeout(250);
  assert(await page.evaluate(() => !player.paused), "검수 재생기 재생");
  await page.locator("#plPlay").click();
  await page.locator("#plSpeed").selectOption("0.75");
  assert(
    await page.evaluate(() => player.playbackRate === 0.75),
    "재생 속도 변경",
  );
  await page.locator("#plSpeed").selectOption("1.5");
  await page.locator("#tab-archive .export-menu summary").click();
  const downloadPromise = page.waitForEvent("download");
  await page.locator("[data-aexport=docx]").click();
  const download = await downloadPromise;
  assert(
    download.suggestedFilename().endsWith(".docx"),
    "Word 문서 실제 내려받기",
  );
  assert((await download.failure()) === null, "Word 내려받기 성공");
  for (const format of [
    "txt",
    "srt",
    "md",
    "json",
    "summary_md",
    "summary_docx",
  ]) {
    const response = await page.request.get(
      base + "/api/sessions/" + sid + "/export?format=" + format,
    );
    assert(
      response.ok() && (await response.body()).length > 0,
      "내보내기 응답 " + format,
    );
  }
  await page.locator("#summaryFold").click();
  assert(await page.locator("#summaryBox").isVisible(), "요약 펼치기");
  await page.locator("#summaryBig").click();
  assert(await page.locator("#archTranscript").isHidden(), "요약 크게 보기");
  await page.locator("#summaryBig").click();
  await page.locator("#summaryFold").click();
  await page.screenshot({ path: output + "/ui-archive.png" });
  await page.locator(".fold-add").click();
  await page.locator("#actionInput").fill("UI 확인 폴더");
  await page.locator("#actionOk").click();
  await page.waitForFunction(() => S.folders.includes("UI 확인 폴더"));
  assert(true, "폴더 생성 실제 저장");
  const folder = page.locator(".fold-row").filter({ hasText: "UI 확인 폴더" });
  await folder.locator("[data-act=delete]").click();
  await page.keyboard.press("Escape");
  assert((await folder.count()) === 1, "폴더 삭제 취소");
  await folder.locator("[data-act=delete]").click();
  await page.locator("#actionOk").click();
  await page.waitForFunction(() => !S.folders.includes("UI 확인 폴더"));
  assert(true, "시험용 폴더 삭제");

  await page.locator("[data-tab=glossary]").click();
  await page.locator("#bulkTerms").fill("검증용어, 접근성");
  await page.locator("#bulkAdd").click();
  await page.waitForFunction(() =>
    S.glossary.terms.some((t) => t.text === "검증용어"),
  );
  assert(true, "용어 등록 실제 저장");
  await page.locator("#glossFind").fill("검증용어");
  await page.waitForTimeout(250);
  assert((await page.locator(".gf-row").count()) > 0, "등록 용어 검색");
  await page.locator("#repFrom").fill("검증옹어");
  await page.locator("#repTo").fill("검증용어");
  await page.locator("#repAdd").click();
  await page.waitForFunction(() =>
    S.glossary.replacements.some((r) => r.from === "검증옹어"),
  );
  assert(true, "교정 규칙 실제 저장");
  await page.screenshot({ path: output + "/ui-glossary.png" });
  await page.locator("[data-tab=settings]").click();
  for (const name of ["ai", "recognition", "speakers", "people"]) {
    await page.locator("[data-setting=" + name + "]").click();
    assert(
      await page
        .locator("[data-setting-panel=" + name + "]")
        .first()
        .isVisible(),
      "설정 분류 " + name,
    );
  }
  await page.locator("#memberName").fill("시험사용자");
  await page.locator("#memberTitle").fill("검증 담당");
  await page.locator("#addMember").click();
  await page.waitForFunction(() =>
    S.members.some((m) => m.name === "시험사용자"),
  );
  assert(true, "인물 등록 실제 저장");
  await page.locator("[data-setting=recognition]").click();
  await page.locator("#setThreads").fill("2");
  await page.locator("#setThreads").press("Tab");
  await page.waitForTimeout(650);
  assert(
    (await (await page.request.get(base + "/api/state")).json()).settings.asr
      .cpu_threads === 2,
    "설정 자동 저장",
  );
  await page.locator("[data-setting=ai]").click();
  await page.screenshot({ path: output + "/ui-settings.png" });

  for (const [width, height] of [
    [1440, 1000],
    [1280, 800],
    [900, 640],
    [390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    for (const tab of ["live", "archive", "glossary", "settings"]) {
      await page.locator("[data-tab=" + tab + "]").click();
      const bounds = await page.evaluate(() => ({
        width: innerWidth,
        scroll: document.documentElement.scrollWidth,
        offscreen: [
          ...document.querySelectorAll(
            ".tabpane.active input,.tabpane.active button,.tabpane.active select",
          ),
        ]
          .filter(
            (el) =>
              el.getClientRects().length &&
              !el.closest("details:not([open]) .export-options"),
          )
          .filter((el) => {
            const r = el.getBoundingClientRect();
            return r.left < -2 || r.right > innerWidth + 2;
          })
          .map((el) => el.id || el.textContent.slice(0, 20)),
      }));
      if (bounds.scroll > width || bounds.offscreen.length)
        failures.push({ width, height, tab, ...bounds });
    }
    if (width === 900) {
      await page.locator("[data-tab=live]").click();
      await page.screenshot({ path: output + "/ui-compact.png" });
    }
  }
  await page.locator("[data-tab=live]").click();
  await page.screenshot({ path: output + "/ui-mobile.png", fullPage: true });
  assert(!failures.length, "4개 창 크기 × 4개 화면 가로 잘림 없음");
  assert(!errors.length, "브라우저 스크립트 오류 없음");
  return { checks, failures, errors };
}
