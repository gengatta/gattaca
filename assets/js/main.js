// VION — базовые интерактивные элементы сайта.
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => {
      links.classList.toggle("open");
    });
    links.querySelectorAll("a").forEach((a) =>
      a.addEventListener("click", () => links.classList.remove("open"))
    );
  }

  const filterButtons = document.querySelectorAll(".filter-btn");
  const entries = document.querySelectorAll(".entry-card");
  if (filterButtons.length && entries.length) {
    filterButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        filterButtons.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const cat = btn.dataset.filter;
        entries.forEach((card) => {
          const show = cat === "все" || card.dataset.cat === cat;
          card.style.display = show ? "" : "none";
        });
      });
    });
  }

  const yearEl = document.querySelector("[data-year]");
  if (yearEl) yearEl.textContent = new Date().getFullYear();
});
