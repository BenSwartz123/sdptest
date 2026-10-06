/* RAT dashboard helpers: hand-rolled SVG charts, status polling, table utilities.
   No external dependencies so the app works fully offline. */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var PALETTE = ["#4f7cff", "#22a06b", "#e8a33d", "#d3574b", "#8e6bd9", "#3aa6b9",
                 "#c464a8", "#7a8b3f", "#5b8def", "#b0743b"];

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(NS, tag);
    for (var k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function title(node, text) {
    node.appendChild(svg("title")).textContent = text;
    return node;
  }

  function fmt(n) { return (n || 0).toLocaleString(); }

  function compact(n) {
    var a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  function parseJSON(node, attr, fallback) {
    try { return JSON.parse(node.getAttribute(attr) || ""); }
    catch (e) { return fallback; }
  }

  function fmtDate(ts) {
    var d = new Date(ts * 1000);
    return d.toISOString().slice(0, 10);
  }

  /* ------------------------------------------------------------------ */
  /* Line chart: churn + growth per week (+ commits in tooltip)          */
  /* ------------------------------------------------------------------ */
  function lineChart(host, points) {
    host.innerHTML = "";
    if (!points.length) { host.appendChild(el("p", "empty", "No activity in this commit set.")); return; }
    var W = 900, H = 260, padL = 56, padR = 16, padT = 14, padB = 30;
    var iw = W - padL - padR, ih = H - padT - padB;
    var maxY = 0, minY = 0;
    points.forEach(function (p) {
      maxY = Math.max(maxY, p.churn, p.growth, p.add, p.rem);
      minY = Math.min(minY, p.growth);
    });
    if (maxY === 0 && minY === 0) maxY = 1;
    var span = maxY - minY || 1;
    function x(i) { return padL + (points.length === 1 ? iw / 2 : (iw * i) / (points.length - 1)); }
    function y(v) { return padT + ih - ((v - minY) / span) * ih; }

    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "chart-svg",
                         preserveAspectRatio: "none" });

    // gridlines + y labels
    for (var g = 0; g <= 4; g++) {
      var val = minY + (span * g) / 4;
      var gy = y(val);
      s.appendChild(svg("line", { x1: padL, y1: gy, x2: W - padR, y2: gy, class: "grid" }));
      var lab = svg("text", { x: padL - 8, y: gy + 4, class: "axis", "text-anchor": "end" });
      lab.textContent = compact(Math.round(val));
      s.appendChild(lab);
    }

    function path(key) {
      return points.map(function (p, i) { return (i ? "L" : "M") + x(i) + "," + y(p[key]); }).join(" ");
    }

    // area under churn
    var area = points.map(function (p, i) {
      return (i ? "L" : "M") + x(i) + "," + y(p.churn);
    }).join(" ") + " L" + x(points.length - 1) + "," + y(minY > 0 ? minY : 0) +
      " L" + x(0) + "," + y(minY > 0 ? minY : 0) + " Z";
    s.appendChild(svg("path", { d: area, class: "area-churn" }));
    s.appendChild(svg("path", { d: path("churn"), class: "line-churn" }));
    s.appendChild(svg("path", { d: path("growth"), class: "line-growth" }));

    // hover targets
    var step = iw / Math.max(points.length, 1);
    points.forEach(function (p, i) {
      var r = svg("rect", { x: x(i) - step / 2, y: padT, width: step, height: ih, class: "hover" });
      title(r, fmtDate(p.ts) + "\nchurn " + fmt(p.churn) + "  growth " + fmt(p.growth) +
            "\ncommits " + fmt(p.commits) + "  added +" + fmt(p.add) + "  removed −" + fmt(p.rem));
      s.appendChild(r);
    });

    // x labels (first, middle, last)
    [0, Math.floor((points.length - 1) / 2), points.length - 1].forEach(function (i, k) {
      if (k === 1 && points.length < 3) return;
      var t = svg("text", {
        x: x(i), y: H - 8, class: "axis",
        "text-anchor": k === 0 ? "start" : (k === 2 ? "end" : "middle")
      });
      t.textContent = fmtDate(points[i].ts);
      s.appendChild(t);
    });

    host.appendChild(s);
    var legend = el("div", "legend");
    [["churn", "line-churn"], ["growth", "line-growth"]].forEach(function (pair) {
      var item = el("span", "legend-item");
      item.appendChild(el("span", "swatch " + pair[1]));
      item.appendChild(document.createTextNode(pair[0]));
      legend.appendChild(item);
    });
    host.appendChild(legend);
  }

  /* ------------------------------------------------------------------ */
  /* Horizontal bar chart (top files by churn)                           */
  /* ------------------------------------------------------------------ */
  function barChart(host, rows, labelKey, valueKey, base) {
    host.innerHTML = "";
    if (!rows.length) { host.appendChild(el("p", "empty", "Nothing to show.")); return; }
    var max = rows.reduce(function (m, r) { return Math.max(m, r[valueKey] || 0); }, 0) || 1;
    var table = el("div", "bars");
    rows.forEach(function (r) {
      var row = el("div", "bar-row");
      var label = el("span", "bar-label mono", r[labelKey]);
      label.title = r[labelKey];
      row.appendChild(label);
      var track = el("div", "bar-track");
      var fill = el("div", "bar-fill");
      fill.style.width = Math.max(2, Math.round(100 * (r[valueKey] || 0) / max)) + "%";
      fill.title = "churn " + fmt(r[valueKey]) + "  (+" + fmt(r.addl) + " / −" + fmt(r.dell) + ", " + fmt(r.mods) + " mods)";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el("span", "bar-value", fmt(r[valueKey])));
      if (base && r.path) {
        var link = el("a", "bar-link", "open");
        link.href = base.replace("__PATH__", r.path.split("/").map(encodeURIComponent).join("/"));
        row.appendChild(link);
      }
      table.appendChild(row);
    });
    host.appendChild(table);
  }

  /* ------------------------------------------------------------------ */
  /* Donut: share of churn per author                                    */
  /* ------------------------------------------------------------------ */
  function donutChart(host, rows, totalOverride) {
    host.innerHTML = "";
    var data = rows.filter(function (r) { return (r.churn || 0) > 0; });
    if (!data.length) { host.appendChild(el("p", "empty", "No churn in this commit set.")); return; }
    var rowsTotal = data.reduce(function (s, r) { return s + r.churn; }, 0) || 1;
    // Prefer the server-computed ownership (true share of the full churn);
    // fall back to the share within the supplied rows.
    function share(r) {
      var f = (typeof r.ownership === "number") ? r.ownership : (r.churn || 0) / rowsTotal;
      return Math.max(0, Math.min(1, f));
    }
    if (data.length > 8) {
      var top = data.slice(0, 7);
      var topShare = top.reduce(function (s, r) { return s + share(r); }, 0);
      var restChurn = data.slice(7).reduce(function (s, r) { return s + r.churn; }, 0);
      data = top.concat([{ name: "others", churn: restChurn,
                           ownership: Math.max(0, 1 - topShare) }]);
    }
    var W = 220, H = 220, cx = W / 2, cy = H / 2, R = 92, r0 = 52;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "chart-svg donut" });
    var angle = -Math.PI / 2;
    data.forEach(function (d, i) {
      var frac = share(d);
      var a2 = angle + frac * Math.PI * 2;
      var large = frac > 0.5 ? 1 : 0;
      function pt(rad, a) { return (cx + rad * Math.cos(a)) + "," + (cy + rad * Math.sin(a)); }
      var dpath = "M" + pt(R, angle) + " A" + R + "," + R + " 0 " + large + " 1 " + pt(R, a2) +
                  " L" + pt(r0, a2) + " A" + r0 + "," + r0 + " 0 " + large + " 0 " + pt(r0, angle) + " Z";
      var seg = svg("path", { d: dpath, fill: PALETTE[i % PALETTE.length], class: "donut-seg" });
      title(seg, d.name + "\nchurn " + fmt(d.churn) + "  (" + (100 * frac).toFixed(1) + "%)");
      s.appendChild(seg);
      angle = a2;
    });
    var center = svg("text", { x: cx, y: cy + 5, class: "donut-total", "text-anchor": "middle" });
    center.textContent = compact(totalOverride || rowsTotal);
    s.appendChild(center);
    host.appendChild(s);
    var legend = el("div", "legend vertical");
    data.forEach(function (d, i) {
      var item = el("span", "legend-item");
      var sw = el("span", "swatch dot");
      sw.style.background = PALETTE[i % PALETTE.length];
      item.appendChild(sw);
      item.appendChild(document.createTextNode(d.name + " · " + (100 * share(d)).toFixed(1) + "%"));
      legend.appendChild(item);
    });
    host.appendChild(legend);
  }

  /* ------------------------------------------------------------------ */
  /* Status page polling                                                 */
  /* ------------------------------------------------------------------ */
  function pollStatus(card) {
    var url = card.getAttribute("data-status-url");
    var overview = card.getAttribute("data-overview-url");
    function tick() {
      fetch(url, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === "ready") {
            var st = document.getElementById("status-stage");
            if (st) st.textContent = "Indexed " + (d.commits || 0).toLocaleString() + " commits — opening dashboard…";
            location.href = overview;
            return;
          }
          if (d.status === "error") { location.reload(); return; }
          var stage = document.getElementById("status-stage");
          if (stage) stage.textContent = d.stage || "Working…";
          var pct = d.total > 0 ? Math.round((100 * d.done) / d.total) : 0;
          var fill = document.getElementById("progress-fill");
          if (fill) fill.style.width = pct + "%";
          var txt = document.getElementById("progress-text");
          if (txt) txt.textContent = fmt(d.done) + " / " + fmt(d.total) + " commits";
          setTimeout(tick, 1500);
        })
        .catch(function () { setTimeout(tick, 3000); });
    }
    tick();
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                                */
  /* ------------------------------------------------------------------ */
  document.addEventListener("DOMContentLoaded", function () {
    // Explain-on-hover controls (.tip labels/cards and help chips) show their
    // text instantly: move it from title into data-tip, which the stylesheet
    // renders as a CSS tooltip (native title tooltips are slow or suppressed
    // in some browsers). aria-label keeps screen-reader access.
    document.querySelectorAll(".tip[title], a.chip[title]").forEach(function (node) {
      var text = node.getAttribute("title");
      node.setAttribute("data-tip", text);
      node.setAttribute("aria-label", text);
      node.removeAttribute("title");
    });

    var activity = document.getElementById("chart-activity");
    if (activity) lineChart(activity, parseJSON(activity, "data-points", []));

    var authors = document.getElementById("chart-authors");
    if (authors) {
      donutChart(authors, parseJSON(authors, "data-rows", []),
                 parseFloat(authors.getAttribute("data-total")) || 0);
    }

    var topfiles = document.getElementById("chart-topfiles");
    if (topfiles) {
      barChart(topfiles, parseJSON(topfiles, "data-rows", []), "path", "churn",
               topfiles.getAttribute("data-base"));
    }

    var statusCard = document.getElementById("status-card");
    if (statusCard && statusCard.getAttribute("data-status") !== "error") pollStatus(statusCard);

    document.querySelectorAll("[data-check-all]").forEach(function (box) {
      box.addEventListener("change", function () {
        var name = box.getAttribute("data-check-all");
        document.querySelectorAll('input[name="' + name + '"]').forEach(function (cb) {
          cb.checked = box.checked;
        });
      });
    });
  });
})();
