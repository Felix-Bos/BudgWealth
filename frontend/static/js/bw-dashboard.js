/*
 * Dashboard Budget : tendances sur 12 mois (graphiques interactifs,
 * redessinés automatiquement en mode jour / nuit).
 */
(function () {
    var dataEl = document.getElementById('dashboard-data');
    var charts = window.BW && window.BW.charts;
    if (!dataEl || !charts) return;

    var data = JSON.parse(dataEl.textContent);
    var history = data.history;
    var num = function (values) { return values.map(Number); };

    function bar(label, values, color, extra) {
        var dataset = {
            label: label,
            data: values,
            backgroundColor: color,
            borderRadius: { topLeft: 5, topRight: 5 },
            borderSkipped: false,
            maxBarThickness: 22
        };
        for (var key in extra || {}) dataset[key] = extra[key];
        return dataset;
    }

    // ---------- Évolution mensuelle (clic = page du mois) ----------
    var historyCanvas = document.getElementById('historyChart');
    if (historyCanvas) {
        charts.create(historyCanvas, function (p) {
            return {
                type: 'bar',
                data: {
                    labels: history.labels,
                    datasets: [
                        bar('Revenus', num(history.income), p.income),
                        bar('Charges', num(history.expense), p.expense),
                        bar('Épargne', num(history.savings), p.savings),
                        {
                            type: 'line',
                            label: 'Solde (revenus − charges)',
                            data: num(history.balance),
                            borderColor: p.balance,
                            backgroundColor: p.balance,
                            borderWidth: 2.5,
                            tension: 0.3,
                            pointRadius: 3,
                            pointHoverRadius: 6
                        }
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
                        // Mode "index" : tous les éléments du mois survolé
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

    // ---------- Taux d'épargne ----------
    var rateCanvas = document.getElementById('savingsRateChart');
    if (rateCanvas) {
        charts.create(rateCanvas, function (p) {
            var datasets = [
                {
                    label: "Taux d'épargne",
                    data: history.savingsRate,
                    borderColor: p.savings,
                    backgroundColor: charts.alpha(p.savings, 0.15),
                    fill: true,
                    tension: 0.35,
                    borderWidth: 2.5,
                    pointRadius: 3,
                    pointHoverRadius: 6,
                    pointBackgroundColor: p.savings
                }
            ];
            if (data.savingsTargetRate !== null && data.savingsTargetRate !== undefined) {
                datasets.push({
                    label: 'Objectif',
                    data: history.labels.map(function () { return data.savingsTargetRate; }),
                    borderColor: p.budget,
                    borderDash: [6, 5],
                    borderWidth: 1.5,
                    pointRadius: 0,
                    pointHoverRadius: 0
                });
            }
            return {
                type: 'line',
                data: { labels: history.labels, datasets: datasets },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: function (ctx) { return ' ' + ctx.dataset.label + ' : ' + ctx.parsed.y + ' %'; }
                            }
                        }
                    },
                    scales: charts.scales(p, { percent: true })
                }
            };
        });
    }

    // ---------- Réussite du budget ----------
    var budgetCanvas = document.getElementById('budgetChart');
    if (budgetCanvas && data.trend.labels.length) {
        var trend = data.trend;
        charts.create(budgetCanvas, function (p) {
            var budget = num(trend.budget);
            var spent = num(trend.spent);
            return {
                type: 'bar',
                data: {
                    labels: trend.labels,
                    datasets: [
                        bar('Dépensé', spent, spent.map(function (value, i) {
                            return value > budget[i] ? p.expense : charts.alpha(p.balance, 0.85);
                        }), { order: 2 }),
                        bar('Épargné', num(trend.savings), p.savings, { order: 2 }),
                        {
                            type: 'line',
                            label: 'Budget',
                            data: budget,
                            borderColor: p.text,
                            borderDash: [6, 4],
                            borderWidth: 1.5,
                            pointRadius: 0,
                            stepped: 'middle',
                            order: 1
                        },
                        {
                            type: 'line',
                            label: 'Objectif épargne',
                            data: num(trend.savingsTarget),
                            borderColor: p.savings,
                            borderDash: [2, 3],
                            borderWidth: 2,
                            pointRadius: 0,
                            stepped: 'middle',
                            order: 1
                        }
                    ]
                },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: { callbacks: { label: charts.euroLabel } }
                    },
                    scales: charts.scales(p)
                }
            };
        });
    }

    // ---------- Charges par catégorie (empilées) ----------
    var categoriesCanvas = document.getElementById('categoriesChart');
    if (categoriesCanvas) {
        charts.create(categoriesCanvas, function (p) {
            return {
                type: 'bar',
                data: {
                    labels: history.labels,
                    datasets: data.categories.map(function (serie, i) {
                        return bar(serie.name, num(serie.values), p.cats[i % p.cats.length], {
                            borderRadius: 3,
                            maxBarThickness: 34
                        });
                    })
                },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            itemSort: function (a, b) { return b.parsed.y - a.parsed.y; },
                            filter: function (item) { return item.parsed.y > 0; },
                            callbacks: {
                                label: charts.euroLabel,
                                footer: function (items) {
                                    var total = items.reduce(function (sum, item) { return sum + item.parsed.y; }, 0);
                                    return 'Total : ' + charts.money2.format(total) + ' €';
                                }
                            }
                        }
                    },
                    scales: charts.scales(p, { stacked: true })
                }
            };
        });
    }

    // ---------- Abonnements ----------
    var subsCanvas = document.getElementById('subscriptionsChart');
    if (subsCanvas) {
        charts.create(subsCanvas, function (p) {
            return {
                type: 'line',
                data: {
                    labels: data.subscriptions.labels,
                    datasets: [{
                        label: 'Abonnements',
                        data: num(data.subscriptions.totals),
                        borderColor: p.cats[4],
                        backgroundColor: charts.alpha(p.cats[4], 0.15),
                        fill: true,
                        tension: 0.3,
                        borderWidth: 2.5,
                        pointRadius: 3,
                        pointHoverRadius: 6
                    }]
                },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function (ctx) {
                                    var count = data.subscriptions.counts[ctx.dataIndex];
                                    return ' ' + charts.money2.format(ctx.parsed.y) + ' € · ' + count + ' abonnement' + (count > 1 ? 's' : '');
                                }
                            }
                        }
                    },
                    scales: charts.scales(p)
                }
            };
        });
    }
})();
