async (page) => {
  const checks = [];
  const assert = (value, name) => {
    if (!value) throw Error(name);
    checks.push(name);
  };
  await page.goto("http://127.0.0.1:8876/");
  await page.waitForFunction(() => !!S.settings);
  await page.setViewportSize({ width: 900, height: 640 });
  await page.locator("#source").selectOption("loopback");
  await page.route("**/api/session/start", (r) =>
    r.fulfill({
      json: {
        status: { state: "running", id: "20261027-ui-demo", speaking: false },
      },
    }),
  );
  await page.route("**/api/session/stop", (r) =>
    r.fulfill({
      json: { status: { state: "stopping", id: "20261027-ui-demo" } },
    }),
  );
  await page.locator("#startBtn").click();
  await page.locator("#stopBtn").waitFor({ state: "visible" });
  assert(await page.locator("#source").isDisabled(), "실행 중 입력 변경 방지");
  await page.evaluate(() =>
    handleEvent({
      type: "segment",
      segment: {
        id: 900,
        start: 0,
        end: 6,
        text: "화면 상태 확인용 문장입니다.",
        speaker: "시험 화자",
        speaker_source: "none",
      },
    }),
  );
  assert(
    (await page.locator("#transcript .seg").count()) === 1,
    "실시간 문장 수신 표시",
  );
  assert(
    (await page.locator("#transcript .empty-state").count()) === 0,
    "첫 문장 뒤 빈 안내 제거",
  );
  await page.evaluate(() =>
    handleEvent({ type: "partial", text: "다음 발언을 받아 적는 중입니다." }),
  );
  assert(
    (await page.locator("#partial").innerText()).includes("다음 발언"),
    "인식 중 자막 표시",
  );
  await page.locator("#stopBtn").click();
  await page.locator("#tab-archive").waitFor({ state: "visible" });
  assert(true, "마친 뒤 기록 화면 이동");
  assert(await page.locator("#summaryBtn").isDisabled(), "마무리 중 요약 방지");
  await page.evaluate(() =>
    applyStatus({ state: "finished", id: "20261027-ui-demo" }),
  );
  await page.waitForFunction(
    () => !document.querySelector("#summaryBtn").disabled,
  );
  assert(true, "마무리 뒤 요약 가능");
  await page.locator("[data-tab=live]").click();
  assert(
    (await page.locator("#startBtn").isVisible()) &&
      (await page.locator("#source").isEnabled()),
    "다음 회의 준비 상태 복구",
  );
  await page.locator("[data-tab=settings]").click();
  await page.locator("[data-setting=recognition]").click();
  await page.locator("#setThreads").fill("-1");
  await page.waitForFunction(() =>
    document.querySelector("#saveMsg").textContent.includes("아직 저장되지"),
  );
  assert(true, "잘못된 설정 저장 방지");
  await page.locator("#setThreads").fill("2");
  await page.waitForFunction(() =>
    document.querySelector("#saveMsg").textContent.includes("저장했습니다"),
  );
  assert(true, "설정 입력 수정 후 저장 복구");
  await page.locator("[data-tab=live]").click();
  await page.locator("[data-tool=speaker]").click();
  await page.route("**/api/face/screen?*", (r) =>
    r.fulfill({ status: 500, json: { detail: "검증용 캡처 오류" } }),
  );
  await page.locator("#pickRegion").click();
  await page.locator(".region-error").waitFor();
  assert(
    await page.locator("#regionApply").isDisabled(),
    "영역 캡처 실패 안내 및 적용 방지",
  );
  await page.keyboard.press("Escape");
  assert(await page.locator("#regionModal").isHidden(), "영역 창 키보드 닫기");
  await page.unroute("**/api/face/screen?*");
  await page.unroute("**/api/session/start");
  await page.unroute("**/api/session/stop");
  return {
    checks,
    note: "시작·중지·인식 이벤트·캡처 오류는 통제된 응답으로 검증. 실제 음성인식 또는 AI 실행 없음.",
  };
}
