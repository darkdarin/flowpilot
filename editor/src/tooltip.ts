// Tooltips for every `title` in the editor: dark, and quicker than the browser's own. While the
// pointer is over an element its `title` moves to `data-fp-tip` (so the browser shows nothing) and
// goes back when the pointer leaves; Vue keeps owning the attribute.

const DELAY_MS = 250; // the first tooltip
const WARM_MS = 600; // moving to the next element within this shows its tooltip at once
const GAP = 6;
const MARGIN = 8;

let tip: HTMLDivElement | null = null;
let target: HTMLElement | null = null;
let timer: number | undefined;
let hiddenAt = 0;

function element(): HTMLDivElement {
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "fp-tooltip";
    tip.setAttribute("role", "tooltip");
    document.body.append(tip);
  }
  return tip;
}

function place(el: HTMLElement) {
  const box = element();
  const at = el.getBoundingClientRect();
  const { width, height } = box.getBoundingClientRect();
  let top = at.bottom + GAP;
  if (top + height > window.innerHeight - MARGIN) top = at.top - GAP - height; // no room below
  const left = Math.min(
    Math.max(MARGIN, at.left + at.width / 2 - width / 2),
    window.innerWidth - MARGIN - width,
  );
  box.style.transform = `translate(${Math.round(left)}px, ${Math.round(Math.max(MARGIN, top))}px)`;
}

function show(el: HTMLElement) {
  const text = el.dataset.fpTip;
  if (!text || !el.isConnected) return;
  const box = element();
  box.textContent = text;
  box.classList.add("on");
  place(el);
}

function release(el: HTMLElement) {
  // give the title back, unless Vue has set a new one meanwhile
  if (el.dataset.fpTip !== undefined && !el.hasAttribute("title")) el.setAttribute("title", el.dataset.fpTip);
  delete el.dataset.fpTip;
}

/** Hide the tooltip; the element keeps it away from the browser until the pointer leaves. */
function conceal() {
  window.clearTimeout(timer);
  if (tip?.classList.contains("on")) hiddenAt = performance.now();
  tip?.classList.remove("on");
}

function hide() {
  conceal();
  if (target) release(target);
  target = null;
}

function take(el: HTMLElement): string | undefined {
  const text = el.getAttribute("title")?.trim();
  el.removeAttribute("title");
  if (text) el.dataset.fpTip = text;
  return text || el.dataset.fpTip;
}

function onOver(event: PointerEvent) {
  if (event.pointerType === "touch") return;
  const el = (event.target as Element | null)?.closest<HTMLElement>("[title], [data-fp-tip]");
  if (el && el === target) {
    take(el); // Vue gave it a new title meanwhile (e.g. Play became Stop)
    if (tip?.classList.contains("on")) show(el);
    return;
  }
  if (!el) return;
  hide();
  if (!take(el)) return;
  target = el;
  const warm = performance.now() - hiddenAt < WARM_MS;
  timer = window.setTimeout(() => show(el), warm ? 0 : DELAY_MS);
}

function onOut(event: PointerEvent) {
  if (!target) return;
  const to = event.relatedTarget as Node | null;
  if (to && target.contains(to)) return; // still inside
  hide();
}

export function installTooltips() {
  document.addEventListener("pointerover", onOver, true);
  document.addEventListener("pointerout", onOut, true);
  for (const name of ["pointerdown", "keydown", "wheel"] as const) {
    document.addEventListener(name, conceal, true);
  }
  window.addEventListener("scroll", conceal, true);
  window.addEventListener("blur", hide);
}
