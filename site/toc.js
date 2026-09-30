// Highlights, in the "On this page" column, the section being read.
(() => {
  const toc = document.querySelector(".toc");
  if (!toc) return;
  const links = new Map(
    [...toc.querySelectorAll("a[href^='#']")].map((a) => [decodeURIComponent(a.hash.slice(1)), a]),
  );
  const headings = [...document.querySelectorAll("article.doc h2[id], article.doc h3[id]")].filter((h) =>
    links.has(h.id),
  );
  if (!headings.length) return;
  let active = null;

  function update() {
    // The section whose heading last went under the sticky navigation bar; the last one at
    // the bottom of the page, where short final sections never reach the top.
    const line = document.querySelector(".nav").offsetHeight + 24;
    let current = headings[0];
    for (const h of headings) {
      if (h.getBoundingClientRect().top > line) break;
      current = h;
    }
    if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) current = headings.at(-1);
    const link = links.get(current.id);
    if (link === active) return;
    active?.classList.remove("active");
    toc.querySelectorAll(".parent").forEach((a) => a.classList.remove("parent"));
    link.classList.add("active");
    // A sub-section also marks its section.
    link.parentElement.parentElement.closest("li")?.querySelector(":scope > a")?.classList.add("parent");
    active = link;
    // Keep it visible in the column, which scrolls on its own when taller than the screen.
    const box = toc.getBoundingClientRect(), item = link.getBoundingClientRect();
    if (item.top < box.top + 30 || item.bottom > box.bottom - 30) {
      toc.scrollTop += item.top - box.top - box.height / 3;
    }
  }

  let queued = false;
  addEventListener("scroll", () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(() => { queued = false; update(); });
    }
  }, { passive: true });
  addEventListener("resize", update);
  update();
})();
