/*
 * Accueil : évolution du patrimoine et 6 derniers mois (redessinés
 * automatiquement en mode jour / nuit).
 */
(function () {
    var dataEl = document.getElementById('home-data');
    var charts = window.BW && window.BW.charts;
    if (!dataEl || !charts) return;

    var data = JSON.parse(dataEl.textContent);
    var num = function (values) { return values.map(Number); };
    var dateLabel = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });

    // ---------- Patrimoine : courbe claire sur la carte colorée ----------
    var wealthCanvas = document.getElementById('wealthChart');
    if (wealthCanvas) {
        var wealth = data.wealth;
        var totals = wealth.balances.map(function (value, index) {
            return Number(value) + Number(wealth.securities[index]);
        });
        var labels = wealth.dates.map(function (iso) { return dateLabel.format(new Date(iso)); });

        charts.create(wealthCanvas, function (p) {
            return {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Patrimoine',
                        data: totals,
                        borderColor: 'rgba(255,255,255,0.95)',
                        borderWidth: 2.5,
                        tension: 0.35,
                        pointRadius: 0,
                        pointHoverRadius: 5,
                        pointHoverBackgroundColor: '#fff',
                        fill: true,
                        backgroundColor: function (context) {
                            var area = context.chart.chartArea;
                            if (!area) return 'rgba(255,255,255,0.1)';
                            var gradient = context.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
                            gradient.addColorStop(0, 'rgba(255,255,255,0.28)');
                            gradient.addColorStop(1, 'rgba(255,255,255,0)');
                            return gradient;
                        }
                    }]
                },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: charts.euroLabel } }
                    },
                    scales: {
                        x: { display: false },
                        y: { display: false, beginAtZero: false }
                    }
                }
            };
        });
    }

    // ---------- Sans historique de soldes : versements vers l'épargne ----------
    var flowCanvas = document.getElementById('wealthFlowChart');
    if (flowCanvas) {
        charts.create(flowCanvas, function () {
            return {
                type: 'bar',
                data: {
                    labels: data.history.labels,
                    datasets: [{
                        label: 'Versé',
                        data: num(data.history.savings),
                        backgroundColor: 'rgba(255,255,255,0.32)',
                        hoverBackgroundColor: 'rgba(255,255,255,0.9)',
                        borderRadius: 6,
                        borderSkipped: false,
                        maxBarThickness: 34
                    }]
                },
                options: {
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: charts.euroLabel } }
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            border: { display: false },
                            ticks: { color: 'rgba(255,255,255,0.7)', font: { size: 11 }, maxRotation: 0, autoSkipPadding: 8 }
                        },
                        y: { display: false, beginAtZero: true }
                    }
                }
            };
        });
    }

    // ---------- 6 derniers mois (clic = page du mois) ----------
    var historyCanvas = document.getElementById('homeHistoryChart');
    if (historyCanvas) {
        var history = data.history;

        function bar(label, values, color) {
            return {
                label: label,
                data: values,
                backgroundColor: color,
                borderRadius: 6,
                borderSkipped: false,
                maxBarThickness: 18,
                categoryPercentage: 0.7,
                barPercentage: 0.9
            };
        }

        charts.create(historyCanvas, function (p) {
            return {
                type: 'bar',
                data: {
                    labels: history.labels,
                    datasets: [
                        bar('Revenus', num(history.income), p.income),
                        bar('Charges', num(history.expense), p.expense),
                        bar('Épargne', num(history.savings), p.savings)
                    ]
                },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: { callbacks: { label: charts.euroLabel } }
                    },
                    scales: charts.scales(p),
                    onClick: function (event, elements) {
                        if (!elements.length) return;
                        var month = history.months[elements[0].index];
                        window.location = data.monthUrl + month[0] + '/' + month[1] + '/';
                    },
                    onHover: function (event, elements) {
                        event.native.target.style.cursor = elements.length ? 'pointer' : 'default';
                    }
                }
            };
        });
    }
})();
