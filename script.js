// ===== XYNEZ — interactions =====

// Adres backendu API. Lokalnie: "" (ten sam serwer).
// Na GitHub Pages: podmień na adres deployu, np. "https://xynez-api.onrender.com"
const API_BASE = "";

document.getElementById("year").textContent = new Date().getFullYear();

// ===== Scroll progress + nav =====
const nav = document.getElementById("nav");
const toTop = document.getElementById("toTop");
const progress = document.getElementById("progress");

function onScroll() {
  const y = window.scrollY;
  const max = document.documentElement.scrollHeight - innerHeight;
  progress.style.width = (max > 0 ? (y / max) * 100 : 0) + "%";
  toTop.classList.toggle("show", y > 600);
}
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

toTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));

// ===== Mobile menu =====
const burger = document.getElementById("burger");
const mobileMenu = document.getElementById("mobileMenu");

function closeMenu() {
  mobileMenu.classList.remove("open");
  burger.classList.remove("open");
  burger.setAttribute("aria-expanded", "false");
  document.body.style.overflow = "";
}
burger.addEventListener("click", () => {
  const open = mobileMenu.classList.toggle("open");
  burger.classList.toggle("open", open);
  burger.setAttribute("aria-expanded", open);
  document.body.style.overflow = open ? "hidden" : "";
});
mobileMenu.querySelectorAll("a").forEach((a) => a.addEventListener("click", closeMenu));

// ===== Reveal on scroll =====
const revealObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("visible");
        revealObserver.unobserve(entry.target);
      }
    });
  },
  { threshold: 0.1, rootMargin: "0px 0px -40px 0px" }
);
document.querySelectorAll(".reveal").forEach((el) => revealObserver.observe(el));

// ===== Contact form =====
const form = document.getElementById("contactForm");
const submitBtn = document.getElementById("submitBtn");
const formSuccess = document.getElementById("formSuccess");
const successNumber = document.getElementById("successNumber");

function genNumber() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let s = "";
  for (let i = 0; i < 4; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return `XY-${new Date().getFullYear()}-${s}`;
}

// FormSubmit — wysyła zgłoszenia na mail. Przy pierwszej wysyłce
// na adres trafi mail aktywacyjny — trzeba go potwierdzić raz.
const FORM_ENDPOINT = "https://formsubmit.co/ajax/yeetovadev@proton.me";
const CONTACT_EMAIL = "yeetovadev@proton.me";

const validators = {
  name: (v) => (v.trim().length >= 3 ? "" : "Podaj imię i nazwisko (min. 3 znaki)."),
  phone: (v) => {
    const digits = v.replace(/\D/g, "");
    return digits.length >= 9 ? "" : "Podaj poprawny numer telefonu.";
  },
  email: (v) =>
    !v || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) ? "" : "Podaj poprawny adres e-mail.",
  device: (v) => (v ? "" : "Wybierz rodzaj urządzenia."),
  message: (v) => (v.trim().length >= 10 ? "" : "Opisz problem nieco dokładniej (min. 10 znaków)."),
  consent: (v) => (v ? "" : "Zaznacz wymaganą zgodę."),
};

function setError(name, msg) {
  const el = form.elements[name];
  if (!el) return;
  const errorEl = form.querySelector(`[data-error-for="${name}"]`);
  if (errorEl) errorEl.textContent = msg;

  let wrapper;
  if (el.type === "checkbox") {
    wrapper = el.closest(".form__consent");
  } else {
    wrapper = el.closest(".field");
  }
  if (wrapper) wrapper.classList.toggle("invalid", !!msg);
  el.setAttribute("aria-invalid", msg ? "true" : "false");
}

const touched = new Set();
form.querySelectorAll("input, select, textarea").forEach((el) => {
  el.addEventListener("input", () => {
    if (touched.has(el.name) && el.name !== "_honey") {
      const value = el.type === "checkbox" ? el.checked : el.value;
      setError(el.name, validators[el.name] ? validators[el.name](value) : "");
    }
  });
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  formSuccess.hidden = true;

  // honeypot — boty wypełniają ukryte pole
  if (form.elements._honey.value) return;

  let valid = true;
  for (const [name, validate] of Object.entries(validators)) {
    const el = form.elements[name];
    if (!el) continue;
    touched.add(name);
    const value = el.type === "checkbox" ? el.checked : el.value;
    const msg = validate(value);
    setError(name, msg);
    if (msg) valid = false;
  }
  if (!valid) {
    const firstBad = form.querySelector(".invalid input:not([type=checkbox]), .invalid select, .invalid textarea");
    firstBad?.focus();
    return;
  }

  const data = Object.fromEntries(new FormData(form).entries());
  data.zgloszenie = genNumber();
  data.consent = form.elements.consent.checked ? "tak" : "";
  delete data._honey;

  submitBtn.classList.add("loading");
  submitBtn.disabled = true;

  try {
    if (FORM_ENDPOINT) {
      const res = await fetch(FORM_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(data),
      });
      if (!res.ok) throw new Error("send failed");
    } else {
      await new Promise((r) => setTimeout(r, 900));
      const subject = encodeURIComponent(`Zgłoszenie XYNEZ ${data.zgloszenie} — ${data.name}`);
      const body = encodeURIComponent(
        `Zgłoszenie: ${data.zgloszenie}\nImię: ${data.name}\nTel: ${data.phone}\nEmail: ${data.email || "-"}\nUrządzenie: ${data.device} ${data.brand || ""}\n\n${data.message}`
      );
      window.location.href = `mailto:${CONTACT_EMAIL}?subject=${subject}&body=${body}`;
    }

    // Zarejestruj zgłoszenie w backendzie (do statusów) — nie blokuje formularza
    fetch(API_BASE + "/api/reports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).catch(() => {});

    successNumber.textContent = "Numer zgłoszenia: " + data.zgloszenie;
    formSuccess.hidden = false;
    form.reset();
    formSuccess.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch {
    alert("Nie udało się wysłać zgłoszenia. Napisz na " + CONTACT_EMAIL + ".");
  } finally {
    submitBtn.classList.remove("loading");
    submitBtn.disabled = false;
  }
});

// ===== Sprawdzanie statusu zgłoszenia =====
const statusCheck = document.getElementById("statusCheck");
const statusForm = document.getElementById("statusForm");
const statusResult = document.getElementById("statusResult");

// Jeśli backend nie odpowiada (np. strona na GitHub Pages bez API) — ukryj widget
fetch(API_BASE + "/api/health")
  .then((r) => r.json())
  .then((d) => { if (!d.ok) statusCheck.style.display = "none"; })
  .catch(() => { statusCheck.style.display = "none"; });

const STATUS_STEPS = [
  ["przyjęte", "Przyjęte"],
  ["w_trakcie", "W trakcie"],
  ["gotowe", "Gotowe"],
  ["odebrane", "Odebrane"],
];

statusForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const nr = document.getElementById("statusNr").value.trim();
  const phone = document.getElementById("statusPhone").value.trim();
  if (!nr || !phone) return;

  statusResult.hidden = false;
  statusResult.innerHTML = '<p class="status-result__meta">Sprawdzanie…</p>';

  try {
    const res = await fetch(`${API_BASE}/api/status?nr=${encodeURIComponent(nr)}&phone=${encodeURIComponent(phone)}`);
    const data = await res.json();

    if (!data.ok) {
      statusResult.innerHTML = `<p class="status-result__error">${data.error || "Nie znaleziono zgłoszenia."}</p>`;
      return;
    }

    const steps = STATUS_STEPS.map(([key, label], i) => {
      const cls = i < data.step ? "done" : i === data.step ? "current" : "";
      return `<li class="status-step ${cls}">${label}</li>`;
    }).join("");

    statusResult.innerHTML = `
      <ul class="status-steps">${steps}</ul>
      <p class="status-result__meta">
        Zgłoszenie ${data.nr} · status: <strong>${data.label}</strong>
        ${data.updated ? " · aktualizacja: " + data.updated : ""}
      </p>`;
  } catch {
    statusResult.innerHTML = '<p class="status-result__error">Serwer niedostępny. Spróbuj ponownie.</p>';
  }
});
