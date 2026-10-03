import { expect, request, test, type Page, type WebSocketRoute } from "@playwright/test";
import { easyCopy } from "../lib/easyCopy";

// Seeding is through the Config API as a superadmin. E2E_EMAIL and E2E_PASSWORD
// are required (no defaults); E2E_CONFIG_URL is optional.
// Each run creates two fresh accounts with a random suffix, so reruns never
// collide: A has one speech-recognition, one AI and one voice setup ({1,1,1}),
// B has two of each ({2,2,2}). Nothing else in the database is read or changed.
const CONFIG_URL = process.env.E2E_CONFIG_URL ?? "http://localhost:8010";
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
if (!EMAIL || !PASSWORD) throw new Error("Set E2E_EMAIL and E2E_PASSWORD to a Config superadmin login.");
const TOKEN_KEY = "yuviz_access_token";
const ACTIVE_TENANT_KEY = "yuviz.activeTenantId";

// Same list as BANNED in services/config/tests/test_agent_templates.py.
const BANNED = /\b(agent|prompt|llm|stt|tts|provider|workflow|configuration|engine|latency|model)s?\b/gi;

const VOICE_JOB = "Appointment booking";
const CHAT_JOB = "Questions and answers";
const CHAT_REPLY = "Sure, I can help with that.";

const suffix = Math.random().toString(36).slice(2, 8);
let token = "";
let tenantA = "";
let tenantB = "";
let nameCounter = 0;
const uniqueName = (prefix: string) => `${prefix} ${suffix}${++nameCounter}`;

test.beforeAll(async () => {
  const api = await request.newContext({ baseURL: CONFIG_URL });
  const login = await api.post("/auth/login", { data: { email: EMAIL, password: PASSWORD } });
  expect(login.ok()).toBeTruthy();
  token = (await login.json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };

  const seed = async (slug: string, count: number) => {
    const created = await api.post("/tenants", { headers, data: { name: `E2E ${slug}`, slug } });
    expect(created.ok()).toBeTruthy();
    const { id } = await created.json();
    const names = count === 1 ? ["Main"] : ["Front", "Back"];
    for (const n of names) {
      const configs = [
        { role: "llm", engine: "openai", model: "gpt-4o-mini", api_key: "sk-e2e-not-a-real-key", name: `${n} AI` },
        { role: "stt", engine: "whisper", model: "base", name: `${n} Ears` },
        { role: "tts", engine: "macos", voice: "Samantha", name: `${n} Voice` },
      ];
      for (const data of configs) {
        const res = await api.post(`/tenants/${id}/providers`, { headers, data });
        expect(res.ok()).toBeTruthy();
      }
    }
    return slug;
  };
  tenantA = await seed(`e2e-a-${suffix}`, 1);
  tenantB = await seed(`e2e-b-${suffix}`, 2);
  await api.dispose();
});

async function openEasy(page: Page, tenantSlug: string) {
  await page.addInitScript(
    ([t, slug, tokenKey, tenantKey]) => {
      localStorage.setItem(tokenKey, t);
      localStorage.setItem(tenantKey, slug);
    },
    [token, tenantSlug, TOKEN_KEY, ACTIVE_TENANT_KEY],
  );
  await page.goto("/agents/new");
  await page.locator("button[aria-pressed]:not([disabled])").first().waitFor();
}

// The Easy flow container: the parent of the step tabs. Everything the Easy
// flow renders is inside it.
const flow = (page: Page) => page.locator(".tabs").locator("xpath=..");

async function scan(page: Page, label: string) {
  const text = await flow(page).innerText();
  expect(text.length, `${label}: scanned text is empty`).toBeGreaterThan(0);
  expect(text, `${label}: scope is the Easy flow`).toContain(easyCopy.stepTitles[0]);
  expect(text.match(BANNED) ?? [], `${label}: banned words`).toEqual([]);
}

// Labels (or placeholders) of every enabled form field in the flow.
async function controls(page: Page): Promise<string[]> {
  const found = await flow(page)
    .locator("input, textarea, select")
    .evaluateAll((els) =>
      els
        .filter((e) => !(e as HTMLInputElement).disabled && (e as HTMLInputElement).type !== "hidden")
        .map((e) => {
          const input = e as HTMLInputElement;
          return (input.labels?.[0]?.childNodes[0]?.textContent ?? input.placeholder).trim();
        }),
    );
  return found.sort();
}

async function expectStep(page: Page, index: number) {
  await expect(page.locator(".tab.active")).toHaveText(`${index + 1}. ${easyCopy.stepTitles[index]}`);
}

const next = (page: Page) => page.getByRole("button", { name: easyCopy.continue, exact: true }).click();

async function pickJob(page: Page, label: string) {
  await page.locator("button[aria-pressed]", { hasText: label }).click();
}

async function fillBusiness(page: Page, name: string, business = "Acme Dental") {
  await page.getByLabel(easyCopy.nameLabel, { exact: true }).fill(name);
  await page.getByLabel(easyCopy.businessNameLabel).fill(business);
  await page.getByLabel(easyCopy.businessFactsLabel).fill("Open 9 to 5.");
}

// Job, business and speaking steps, ending on the Test step with the agent created.
async function createThrough(page: Page, job: string, name: string) {
  await pickJob(page, job);
  await next(page);
  await fillBusiness(page, name);
  await next(page);
  await expectStep(page, 2);
  await next(page);
  await expectStep(page, 3);
}

async function chatTurn(page: Page, text: string) {
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await page.getByPlaceholder(easyCopy.chatPlaceholder).fill(text);
  await page.getByRole("button", { name: easyCopy.send }).click();
}

test("the six titles appear in order, and the active step follows", async ({ page }) => {
  await openEasy(page, tenantA);
  const expected = [
    "1. What should my AI receptionist do?",
    "2. Tell it about my business",
    "3. Choose how it speaks",
    "4. Test it",
    "5. Fix anything that's wrong",
    "6. Put it to work",
  ];
  expect(easyCopy.stepTitles.map((t, i) => `${i + 1}. ${t}`)).toEqual(expected);
  expect(await page.locator(".tabs .tab").allInnerTexts()).toEqual(expected);
  await expectStep(page, 0);
  await pickJob(page, CHAT_JOB);
  await next(page);
  await expectStep(page, 1);
  await fillBusiness(page, uniqueName("Titles"));
  await next(page);
  await expectStep(page, 2);
  await next(page);
  await expectStep(page, 3);
  await chatTurn(page, "What are your hours?");
  await page.getByText(CHAT_REPLY).waitFor();
  await next(page);
  await expectStep(page, 4);
  await next(page);
  await expectStep(page, 5);
  expect(await page.locator(".tabs .tab").allInnerTexts()).toEqual(expected);
});

test("one of each setup: full chat journey, control set and banned-word scan on every step", async ({ page }) => {
  await openEasy(page, tenantA);
  expect(await controls(page)).toEqual([]);
  await scan(page, "step 1");
  await pickJob(page, CHAT_JOB);
  await page.getByText(easyCopy.whatItDoes).waitFor();
  await scan(page, "step 1 selected");
  await next(page);

  // Step 2, with both required-field errors.
  expect(await controls(page)).toEqual(
    [easyCopy.nameLabel, easyCopy.businessNameLabel, easyCopy.businessFactsLabel].sort(),
  );
  await next(page);
  await expect(page.getByText(easyCopy.nameRequired)).toBeVisible();
  await scan(page, "name required");
  await page.getByLabel(easyCopy.nameLabel, { exact: true }).fill(uniqueName("Journey"));
  await next(page);
  await expect(page.getByText(easyCopy.businessNameRequired)).toBeVisible();
  await scan(page, "business name required");
  await page.getByLabel(easyCopy.businessNameLabel).fill("Acme {{secret}}");
  await next(page);

  // Step 3: one setup of each, so Language is the only control. The server
  // refuses the double braces and Easy shows its own text.
  await expectStep(page, 2);
  expect(await controls(page)).toEqual([easyCopy.languageLabel]);
  await scan(page, "step 3");
  await next(page);
  await expect(page.getByText(easyCopy.doubleBraces)).toBeVisible();
  await scan(page, "double braces refused");
  await page.getByRole("button", { name: easyCopy.back }).click();
  await page.getByLabel(easyCopy.businessNameLabel).fill("Acme");
  await next(page);
  await next(page);

  // Step 4, chat variant.
  await expectStep(page, 3);
  expect(await controls(page)).toEqual([]);
  await scan(page, "step 4 before start");
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await expect(page.getByPlaceholder(easyCopy.chatPlaceholder)).toBeEnabled();
  expect(await controls(page)).toEqual([easyCopy.chatPlaceholder]);
  await page.getByPlaceholder(easyCopy.chatPlaceholder).fill("What are your hours?");
  await page.getByRole("button", { name: easyCopy.send }).click();
  await page.getByText(CHAT_REPLY).waitFor();
  await scan(page, "step 4 after a turn");
  await next(page);

  // Step 5: Fix, Discard, Accept, Undo.
  await expectStep(page, 4);
  expect(await controls(page)).toEqual([easyCopy.problemLabel]);
  await scan(page, "step 5");
  await page.getByLabel(easyCopy.problemLabel).fill("It repeated itself.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await page.getByRole("button", { name: easyCopy.acceptFix }).waitFor();
  await expect(page.getByText(easyCopy.beforeLabel, { exact: true })).toBeVisible();
  await expect(page.getByText(easyCopy.afterLabel, { exact: true })).toBeVisible();
  expect(await controls(page)).toEqual([]);
  await scan(page, "fix proposal");
  await page.getByRole("button", { name: easyCopy.discardFix }).click();
  await expect(page.getByRole("button", { name: easyCopy.undoFix })).toHaveCount(0);
  await page.getByLabel(easyCopy.problemLabel).fill("It repeated itself.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await page.getByRole("button", { name: easyCopy.acceptFix }).click();
  await page.getByText(easyCopy.fixAccepted).waitFor();
  await scan(page, "fix accepted");
  await page.getByRole("button", { name: easyCopy.undoFix }).click();
  await page.getByText(easyCopy.fixUndone).waitFor();
  await scan(page, "fix undone");
  await next(page);

  // Step 6.
  await expectStep(page, 5);
  expect(await controls(page)).toEqual([]);
  await scan(page, "step 6");
  await page.getByRole("button", { name: easyCopy.putToWork, exact: true }).click();
  await page.getByText(easyCopy.liveTitle).waitFor();
  await expect(page.getByRole("link", { name: easyCopy.passedOnCallsLink })).toBeVisible();
  await scan(page, "live confirmation");
});

test("two of each setup: the dropdown set, and Continue waits for every choice", async ({ page }) => {
  await openEasy(page, tenantB);
  await pickJob(page, VOICE_JOB);
  await next(page);
  await fillBusiness(page, uniqueName("Pair"));
  await next(page);
  await expectStep(page, 2);

  expect(await controls(page)).toEqual(
    [easyCopy.languageLabel, easyCopy.aiServiceLabel, easyCopy.speechRecognitionLabel, easyCopy.voiceLabel].sort(),
  );
  await scan(page, "step 3 with choices");
  const optionsOf = (label: string) => page.getByLabel(label, { exact: true }).locator("option").allInnerTexts();
  expect(await optionsOf(easyCopy.aiServiceLabel)).toEqual([easyCopy.chooseOne, "Back AI", "Front AI"]);
  expect(await optionsOf(easyCopy.speechRecognitionLabel)).toEqual([easyCopy.chooseOne, "Back Ears", "Front Ears"]);
  expect(await optionsOf(easyCopy.voiceLabel)).toEqual([easyCopy.chooseOne, "Back Voice", "Front Voice"]);

  const cont = page.getByRole("button", { name: easyCopy.continue, exact: true });
  await expect(cont).toBeDisabled();
  await page.getByLabel(easyCopy.aiServiceLabel, { exact: true }).selectOption({ label: "Front AI" });
  await page.getByLabel(easyCopy.speechRecognitionLabel, { exact: true }).selectOption({ label: "Front Ears" });
  await expect(cont).toBeDisabled();
  await page.getByLabel(easyCopy.voiceLabel, { exact: true }).selectOption({ label: "Front Voice" });
  await expect(cont).toBeEnabled();
  await next(page);
  await expectStep(page, 3);
  await scan(page, "step 4 voice");
});

test("one-job chat test failures show only Easy text", async ({ page }) => {
  await openEasy(page, tenantA);
  await pickJob(page, CHAT_JOB);
  await next(page);
  await fillBusiness(page, uniqueName("Failures"));
  await next(page);
  await next(page);
  await expectStep(page, 3);

  // A mint failure, then a good start.
  let mintFails = 1;
  await page.route("**/test-sessions", (route) =>
    mintFails-- > 0 ? route.fulfill({ status: 502, json: { detail: "vendor said no" } }) : route.fallback(),
  );
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await expect(page.getByText(easyCopy.aiUnavailable)).toBeVisible();
  await scan(page, "mint 502");
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await page.getByPlaceholder(easyCopy.chatPlaceholder).waitFor();

  // Chat turns: 429 then 502, then a real turn so Fix has a session.
  const failures = [
    { status: 429, detail: "slow down", shows: easyCopy.tooManyRequests },
    { status: 502, detail: "upstream exploded", shows: easyCopy.aiUnavailable },
  ];
  let turn = 0;
  await page.route("**/test-chat", (route) =>
    turn < failures.length
      ? route.fulfill({ status: failures[turn].status, json: { detail: failures[turn].detail } })
      : route.fallback(),
  );
  for (const f of failures) {
    await page.getByPlaceholder(easyCopy.chatPlaceholder).fill("Hello?");
    await page.getByRole("button", { name: easyCopy.send }).click();
    await expect(page.locator(".error-banner")).toHaveText(f.shows);
    expect(await page.locator(".error-banner").innerText()).not.toContain(f.detail);
    await scan(page, `chat ${f.status}`);
    turn++;
  }
  await page.getByPlaceholder(easyCopy.chatPlaceholder).fill("Hello?");
  await page.getByRole("button", { name: easyCopy.send }).click();
  await page.getByText(CHAT_REPLY).waitFor();
  await next(page);

  // Revise 502.
  await page.route("**/prompt/revise", (route) => route.fulfill({ status: 502, json: { detail: "ai_unavailable" } }));
  await page.getByLabel(easyCopy.problemLabel).fill("It repeated itself.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await expect(page.locator(".error-banner")).toHaveText(easyCopy.aiUnavailable);
  await scan(page, "revise 502");
});

test("an agent whose instructions were changed by hand shows the not-fixable message and no text box", async ({
  page,
}) => {
  await page.route("**/agents/from-template", async (route) => {
    const res = await route.fetch();
    await route.fulfill({ response: res, json: { ...(await res.json()), prompt_fixable: false } });
  });
  await openEasy(page, tenantA);
  await createThrough(page, CHAT_JOB, uniqueName("Hand edited"));
  await chatTurn(page, "What are your hours?");
  await page.getByText(CHAT_REPLY).waitFor();
  await next(page);
  await expectStep(page, 4);
  await expect(page.getByText(easyCopy.notFixable)).toBeVisible();
  await expect(flow(page).locator("textarea")).toHaveCount(0);
  expect(await controls(page)).toEqual([]);
  await scan(page, "not fixable");
});

test("Advanced opens the existing wizard with its six titles", async ({ page }) => {
  await openEasy(page, tenantA);
  await page.getByRole("button", { name: easyCopy.advancedLink }).click();
  await expect(page.locator(".tabs .tab")).toHaveCount(6);
  expect(await page.locator(".tabs .tab").allInnerTexts()).toEqual([
    "1. Identity",
    "2. Language & Voice",
    "3. Limits",
    "4. Advanced",
    "5. Knowledge & Tools",
    "6. Review",
  ]);
});

test("voice test: every Start mints a fresh one-use credential, sent as the first frame", async ({ page }) => {
  const sockets: WebSocketRoute[] = [];
  const urls: string[] = [];
  const frames: string[][] = [];
  await page.routeWebSocket(/\/webcall/, (ws) => {
    const mine: string[] = [];
    sockets.push(ws);
    urls.push(ws.url());
    frames.push(mine);
    ws.onMessage((m) => mine.push(typeof m === "string" ? m : "<binary>"));
  });
  const minted: string[] = [];
  page.on("response", async (res) => {
    if (res.request().method() === "POST" && res.url().endsWith("/test-sessions") && res.ok()) {
      minted.push((await res.json()).credential);
    }
  });

  await openEasy(page, tenantA);
  await createThrough(page, VOICE_JOB, uniqueName("Voice"));
  await scan(page, "voice step 4");

  const startTalking = page.getByRole("button", { name: easyCopy.startTalking });
  let starts = 0;
  const start = async () => {
    await startTalking.click();
    starts++;
    await expect.poll(() => sockets.length).toBe(starts);
    await expect.poll(() => frames[starts - 1].length).toBeGreaterThan(0);
    await expect.poll(() => minted.length).toBe(starts);
  };
  const fixGatedOff = async () => {
    await next(page);
    await expectStep(page, 4);
    await expect(page.getByText(easyCopy.testFirst)).toBeVisible();
    await expect(flow(page).locator("textarea")).toHaveCount(0);
    await expect(page.getByRole("button", { name: easyCopy.suggestFix })).toHaveCount(0);
    await scan(page, "fix gated");
    await page.getByRole("button", { name: easyCopy.back }).click();
    await expectStep(page, 3);
  };

  // Start 1, then Fix is gated and Back returns to Test.
  await start();
  await fixGatedOff();

  // Stop, then Start.
  await start();
  await page.getByRole("button", { name: easyCopy.stop }).click();
  await expect(startTalking).toBeVisible();
  await start();

  // Forced reconnect: the server drops the socket, and the user starts again.
  await sockets[starts - 1].close();
  await expect(page.getByText(easyCopy.statusEnded)).toBeVisible();
  await scan(page, "call ended");
  await start();

  // A spoken line without the session id keeps Fix gated.
  sockets[starts - 1].send(JSON.stringify({ type: "tts_result", text: "Hello, how can I help?" }));
  await expect(page.getByText("Hello, how can I help?")).toBeVisible();
  await scan(page, "transcript");
  await fixGatedOff();

  // A second call must not inherit the first call's transcript: Fix stays
  // gated until the new session's own first line.
  await start();
  sockets[starts - 1].send(JSON.stringify({ type: "service_ready", session_id: "e2e-session-1" }));
  sockets[starts - 1].send(JSON.stringify({ type: "tts_result", text: "First call line." }));
  await expect(page.getByText("First call line.")).toBeVisible();
  await page.getByRole("button", { name: easyCopy.stop }).click();
  await start();
  sockets[starts - 1].send(JSON.stringify({ type: "service_ready", session_id: "e2e-session-2" }));
  await expect(page.getByText("First call line.")).toHaveCount(0);
  await fixGatedOff();

  // The session id arrives: Fix opens.
  await start();
  sockets[starts - 1].send(JSON.stringify({ type: "service_ready", session_id: "e2e-session" }));
  sockets[starts - 1].send(JSON.stringify({ type: "tts_result", text: "Hello again." }));
  await expect(page.getByText("Hello again.")).toBeVisible();
  await next(page);
  await expectStep(page, 4);
  await expect(page.getByLabel(easyCopy.problemLabel)).toBeVisible();
  await page.route("**/prompt/revise", (route) => route.fulfill({ status: 502, json: { detail: "ai_unavailable" } }));
  await page.getByLabel(easyCopy.problemLabel).fill("It was slow to answer.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await expect(page.locator(".error-banner")).toHaveText(easyCopy.aiUnavailable);
  await scan(page, "voice revise 502");

  // Every Start, one mint, all different, none in a URL.
  expect(starts).toBe(7);
  expect(minted).toHaveLength(starts);
  expect(new Set(minted).size).toBe(starts);
  for (let i = 0; i < starts; i++) {
    expect(urls[i]).toMatch(/\/webcall\?tenant=.+&agent=.+&test=1$/);
    for (const c of minted) expect(urls[i]).not.toContain(c);
    const first = JSON.parse(frames[i][0]);
    expect(first.type).toBe("test_credential");
    expect(first.credential).toBe(minted[i]);
  }
});
