// Меню на телефоне, тема, горячая клавиша поиска, всплывающие подсказки графиков.
(function () {
  var body = document.body;
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-open-menu]")) body.classList.add("menu-open");
    if (e.target.closest("[data-close-menu]")) body.classList.remove("menu-open");
    var toggle = e.target.closest("[data-theme-toggle]");
    if (toggle) {
      var root = document.documentElement;
      var dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("theme", root.dataset.theme); } catch (err) {}
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") body.classList.remove("menu-open");
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(tag)) {
      var s = document.getElementById("global-search");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });
  // Сообщения сами скрываются через 6 секунд (ошибки — остаются)
  document.querySelectorAll(".toasts .msg:not(.msg-error):not(.msg-warning)").forEach(function (m) {
    setTimeout(function () { m.classList.add("hide"); }, 6000);
  });
  // Подсказка для столбцов графика: data-tip на <g class="bar">
  var tip;
  document.addEventListener("pointerover", function (e) {
    var bar = e.target.closest && e.target.closest("[data-tip]");
    if (!bar) { if (tip) tip.hidden = true; return; }
    if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; document.body.appendChild(tip); }
    tip.innerHTML = bar.getAttribute("data-tip");
    tip.hidden = false;
    var r = bar.getBoundingClientRect();
    tip.style.left = Math.round(r.left + r.width / 2 + window.scrollX) + "px";
    tip.style.top = Math.round(r.top + window.scrollY) + "px";
  });
})();

// Строка таблицы — ссылка; выбор изделий для архива чертежей
(function () {
  document.addEventListener("click", function (e) {
    var row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a, button, input, select, label")) window.location = row.dataset.href;
  });
  var bar = document.querySelector("[data-selection-bar]");
  if (!bar) return;
  function update() {
    var n = document.querySelectorAll("input[name=ids]:checked").length;
    bar.classList.toggle("show", n > 0);
    bar.querySelector("[data-selected-count]").textContent = n;
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches("[data-check-all]")) document.querySelectorAll("input[name=ids]").forEach(function (c) { c.checked = e.target.checked; });
    if (e.target.matches("input[name=ids], [data-check-all]")) update();
  });
})();
