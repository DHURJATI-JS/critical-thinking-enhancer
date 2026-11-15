// Applied before first paint (no inline scripts are allowed by the CSP) to avoid a theme flash.
(function () {
  var theme = "dark";
  try { theme = localStorage.getItem("cte-theme") || "dark"; } catch (e) { /* storage blocked */ }
  var dark = theme === "dark" || (theme === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
})();
