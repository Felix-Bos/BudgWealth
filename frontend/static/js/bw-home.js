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
    var dateLabel = new Intl.DateTimeFormat((document.documentElement.lang === 'en' ? 'en-GB' : 'fr-FR'), { day: 'numeric', month: 'short', year: 'numeric' });

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
                        label: gettext('Patrimoine'),
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
                        label: gettext('Versé'),
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
                        bar(gettext('Revenus'), num(history.income), p.income),
                        bar(gettext('Charges'), num(history.expense), p.expense),
                        bar(gettext('Épargne'), num(history.savings), p.savings)
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

    // ---------- Trésorerie : solde prévu ----------
    var cashCanvas = document.getElementById('homeCashChart');
    if (cashCanvas && data.cash) {
        var lang = document.documentElement.lang === 'en' ? 'en-GB' : 'fr-FR';
        var dayFormat = new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'short' });
        var cashDate = function (label) { var d = label.split('-'); return new Date(+d[0], +d[1] - 1, +d[2]); };
        charts.create(cashCanvas, function (p) {
            var scales = charts.scales(p);
            scales.y.beginAtZero = false;
            scales.y.ticks.maxTicksLimit = 4;
            scales.x.ticks.maxTicksLimit = 5;
            scales.x.ticks.callback = function (value, index) { return dayFormat.format(cashDate(data.cash.labels[index])); };
            return {
                type: 'line',
                data: {
                    labels: data.cash.labels,
                    datasets: [
                        {
                            label: gettext('Solde prévu'),
                            data: data.cash.values,
                            borderColor: p.balance,
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.1,
                            fill: { target: { value: 0 }, above: charts.alpha(p.balance, 0.12), below: charts.alpha(p.expense, 0.25) }
                        },
                        {
                            label: gettext('Marge de sécurité'),
                            data: data.cash.labels.map(function () { return data.cash.buffer; }),
                            borderColor: p.expense,
                            borderDash: [4, 4],
                            borderWidth: 1,
                            pointRadius: 0,
                            fill: false
                        }
                    ]
                },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                title: function (items) { return dayFormat.format(cashDate(data.cash.labels[items[0].dataIndex])); },
                                label: charts.euroLabel
                            }
                        }
                    },
                    scales: scales
                }
            };
        });
    }
})();
