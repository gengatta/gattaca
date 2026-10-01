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

  try {
    if (!localStorage.getItem("vion-cookie-ok")) {
      const bar = document.createElement("div");
      bar.className = "cookie-bar";
      bar.innerHTML =
        '<p>Этот сайт использует cookies. Мы запоминаем ваши действия и предпочтения, чтобы сделать использование сайта удобнее. Нажмите «ОК», если соглашаетесь с условиями их обработки. Вы всегда можете запретить обработку cookies через браузер.</p>' +
        '<button class="btn btn-primary" type="button">ОК</button>';
      document.body.appendChild(bar);
      bar.querySelector("button").addEventListener("click", () => {
        try { localStorage.setItem("vion-cookie-ok", "1"); } catch (e) {}
        bar.remove();
      });
    }
  } catch (e) {
    /* localStorage unavailable — skip the banner rather than break the page */
  }
});
