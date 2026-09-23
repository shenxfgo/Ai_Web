import { chromium } from "playwright";

const url = "http://127.0.0.1:5173/";
const out = process.argv[2] ?? ".";
const errors = [];

const box = (el) => {
  const b = el.getBoundingClientRect();
  return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) };
};

const browser = await chromium.launch();

// 桌面：默认态 / 收起态
const desk = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
desk.on("console", (m) => m.type() === "error" && errors.push(`desktop: ${m.text()}`));
desk.on("pageerror", (e) => errors.push(`desktop: ${e.message}`));
await desk.goto(url, { waitUntil: "networkidle" });
await desk.screenshot({ path: `${out}/ui-desktop.png`, fullPage: true });
console.log(
  "desktop",
  await desk.evaluate(() => ({
    headings: [...document.querySelectorAll("h1,h2,h3")].map((h) => `${h.tagName}:${h.textContent.trim()}`),
    crumb: document.querySelector(".top__crumb")?.textContent.replace(/\s+/g, " ").trim(),
    nav: (() => { const b = document.querySelector(".nav").getBoundingClientRect(); return { x: Math.round(b.x), width: Math.round(b.width) }; })(),
    overflowX: document.documentElement.scrollWidth > window.innerWidth,
    activeNav: document.querySelector(".nav__item--active")?.textContent.trim(),
  })),
);
await desk.locator(".nav__toggle").click();
await desk.waitForTimeout(400);
console.log(
  "collapsed",
  await desk.evaluate(() => ({
    navWidth: Math.round(document.querySelector(".nav").getBoundingClientRect().width),
    labelsVisible: [...document.querySelectorAll(".nav__item span")].some(
      (s) => s.offsetParent !== null && s.textContent.trim(),
    ),
  })),
);
await desk.screenshot({ path: `${out}/ui-collapsed.png`, fullPage: true });
await desk.close();

// 窄屏：抽屉开合
const mob = await browser.newPage({ viewport: { width: 880, height: 720 }, deviceScaleFactor: 2 });
mob.on("pageerror", (e) => errors.push(`mobile: ${e.message}`));
await mob.goto(url, { waitUntil: "networkidle" });
const hiddenX = await mob.evaluate(
  () => document.querySelector(".nav").getBoundingClientRect().right,
);
await mob.locator(".top__burger").click();
await mob.waitForTimeout(400);
await mob.screenshot({ path: `${out}/ui-mobile-drawer.png` });
const opened = await mob.evaluate(
  () => document.querySelector(".nav").getBoundingClientRect().left === 0,
);
await mob.locator(".nav-scrim").click({ position: { x: 700, y: 400 } });
await mob.waitForTimeout(400);
const closedAgain = await mob.evaluate(
  () => document.querySelector(".nav").getBoundingClientRect().right <= 0,
);
console.log("mobile", { hiddenX, opened, closedAgain });
await mob.close();

await browser.close();
console.log(errors.length ? `ERRORS:\n${errors.join("\n")}` : "no console errors");
