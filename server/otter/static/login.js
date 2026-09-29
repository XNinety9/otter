"use strict";

const form = document.querySelector("#login-form");
const error = document.querySelector(".error");

// Only follow same-site relative paths after login.
function nextUrl() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  return next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

fetch("/api/auth/status").then((r) => r.json()).then((status) => {
  if (status.user) location.replace(nextUrl());
  document.querySelector(".setup").hidden = status.has_users;
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  error.hidden = true;
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.username.value, password: form.password.value }),
    });
    if (res.ok) return location.replace(nextUrl());
    const { detail } = await res.json().catch(() => ({}));
    error.textContent = typeof detail === "string" ? detail : `Login failed (${res.status})`;
    error.hidden = false;
    form.password.select();
  } finally {
    button.disabled = false;
  }
});
