"use strict";
document.getElementById("show-token").addEventListener("click", function () {
  const input = document.getElementById("id_token");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  this.textContent = show ? "Hide" : "Show";
  this.setAttribute("aria-pressed", String(show));
});
