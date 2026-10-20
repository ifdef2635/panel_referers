(function () {
  // Confirmation for destructive forms (inline handlers are blocked by CSP).
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  document.addEventListener("DOMContentLoaded", function () {
    var el = document.getElementById("chart-data");
    if (!el || !window.Chart) return;
    var data = JSON.parse(el.textContent);
    var css = getComputedStyle(document.documentElement);
    var v = function (name) { return css.getPropertyValue(name).trim(); };
    var palette = [v("--chart-1"), v("--chart-2"), v("--chart-3")];

    Chart.defaults.color = v("--muted");
    Chart.defaults.borderColor = v("--border");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

    var common = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } } },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } },
        y: { beginAtZero: true, ticks: { precision: 0 } },
      },
    };

    var activity = document.querySelector('[data-chart="activity"]');
    if (activity) {
      new Chart(activity, {
        type: "bar",
        data: {
          labels: data.labels,
          datasets: [
            { label: "Регистрации", data: data.registrations, backgroundColor: palette[0], borderRadius: 4, maxBarThickness: 28 },
            { label: "Новые покупатели", data: data.new_buyers, backgroundColor: palette[1], borderRadius: 4, maxBarThickness: 28 },
          ],
        },
        options: common,
      });
    }

    var revenue = document.querySelector('[data-chart="revenue"]');
    if (revenue) {
      var symbols = { RUB: "₽", USD: "$", XTR: "★" };
      var sets = Object.keys(data.revenue).map(function (cur, i) {
        return {
          label: symbols[cur] ? cur + " (" + symbols[cur] + ")" : cur,
          data: data.revenue[cur],
          borderColor: palette[i % palette.length],
          backgroundColor: palette[i % palette.length],
          tension: 0.3,
          cubicInterpolationMode: "monotone",
          pointRadius: data.labels.length > 40 ? 0 : 3,
          yAxisID: i === 0 ? "y" : "y" + i,
        };
      });
      var scales = { x: common.scales.x, y: { beginAtZero: true } };
      sets.slice(1).forEach(function (_, i) {
        scales["y" + (i + 1)] = { beginAtZero: true, position: "right", grid: { display: false } };
      });
      new Chart(revenue, {
        type: "line",
        data: { labels: data.labels, datasets: sets },
        options: Object.assign({}, common, { scales: scales }),
      });
    }
  });
})();
