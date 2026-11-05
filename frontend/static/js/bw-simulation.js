/*
 * Simulation du PEA : éventail des 100 scénarios (trajectoires fines,
 * bandes 50 % et 90 %, médiane, capital investi), sans ou avec inflation.
 */
(function () {
    var dataEl = document.getElementById('simulation-data');
    var charts = window.BW && window.BW.charts;
    var canvas = document.getElementById('simulationChart');
    if (!dataEl || !charts || !canvas) return;

    var data = JSON.parse(dataEl.textContent);
    var mode = 'nominal';
    var lang = document.documentElement.lang === 'en' ? 'en-GB' : 'fr-FR';
    var yearFormat = new Intl.DateTimeFormat(lang, { year: 'numeric' });
    var monthFormat = new Intl.DateTimeFormat(lang, { month: 'short', year: 'numeric' });

    function labelDate(label) {
        var parts = label.split('-');
        return new Date(+parts[0], +parts[1] - 1, 1);
    }

    function band(label, values, color, fill) {
        return {
            label: label,
            data: values,
            borderColor: 'transparent',
            backgroundColor: color,
            pointRadius: 0,
            borderWidth: 0,
            fill: fill,
            order: 2
        };
    }

    var handle = charts.create(canvas, function (p) {
        var series = data[mode];
        var paths = series.paths.map(function (path) {
            return {
                label: '',
                data: path,
                borderColor: charts.alpha(p.savings, 0.13),
                borderWidth: 1,
                pointRadius: 0,
                pointHoverRadius: 0,
                fill: false,
                tension: 0,
                order: 3
            };
        });
        var datasets = paths.concat([
            band(gettext('Très favorable (95 %)'), series.p95, charts.alpha(p.savings, 0.12), '+1'),
            band(gettext('Défavorable (5 %)'), series.p5, 'transparent', false),
            band(gettext('Favorable (75 %)'), series.p75, charts.alpha(p.savings, 0.22), '+1'),
            band(gettext('Prudent (25 %)'), series.p25, 'transparent', false),
            {
                label: gettext('Médiane'),
                data: series.p50,
                borderColor: p.savings,
                borderWidth: 2.5,
                pointRadius: 0,
                fill: false,
                tension: 0.2,
                order: 1
            },
            {
                label: gettext('Capital investi'),
                data: data.invested,
                borderColor: p.budget,
                borderDash: [6, 4],
                borderWidth: 1.5,
                pointRadius: 0,
                fill: false,
                tension: 0,
                order: 0
            }
        ]);

        var scales = charts.scales(p);
        scales.y.beginAtZero = false;
        // Axe borné un peu au-dessus de la bande 95 % : un scénario extrême
        // écraserait sinon tout l'éventail
        scales.y.min = 0;
        var top = Math.max.apply(null, series.p95.concat(data.invested)) * 1.25;
        var unit = Math.pow(10, Math.floor(Math.log10(top))) / 2;
        scales.y.max = Math.ceil(top / unit) * unit;
        scales.x.ticks.autoSkip = false;
        scales.x.ticks.callback = function (value, index) {
            var label = data.labels[index];
            // Un repère par an (ou tous les 5 ans au-delà de 20 ans)
            var step = data.labels.length > 241 ? 60 : 12;
            return index % step === 0 ? yearFormat.format(labelDate(label)) : null;
        };

        return {
            type: 'line',
            data: { labels: data.labels, datasets: datasets },
            options: {
                animation: false,
                normalized: true,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { filter: function (item) { return item.text !== ''; } }
                    },
                    tooltip: {
                        filter: function (item) { return item.dataset.label !== ''; },
                        itemSort: function (a, b) { return b.parsed.y - a.parsed.y; },
                        callbacks: {
                            title: function (items) { return monthFormat.format(labelDate(data.labels[items[0].dataIndex])); },
                            label: charts.euroLabel
                        }
                    }
                },
                scales: scales
            }
        };
    });

    // ---------- Lignes verticales repères (capital investi, médiane) ----------
    var markersPlugin = {
        id: 'simMarkers',
        afterDatasetsDraw: function (chart, args, options) {
            var lines = (options && options.lines) || [];
            var x = chart.scales.x;
            var area = chart.chartArea;
            var ctx = chart.ctx;
            lines.forEach(function (line, i) {
                var px = x.getPixelForValue(line.value);
                if (px < area.left - 1 || px > area.right + 1) return;
                ctx.save();
                ctx.strokeStyle = line.color;
                ctx.lineWidth = 1.5;
                ctx.setLineDash(line.dash || []);
                ctx.beginPath();
                ctx.moveTo(px, area.top);
                ctx.lineTo(px, area.bottom);
                ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = line.color;
                ctx.font = '600 11px ' + Chart.defaults.font.family;
                var text = line.label;
                var width = ctx.measureText(text).width;
                var tx = Math.min(Math.max(px - width / 2, area.left), area.right - width);
                ctx.fillText(text, tx, area.top - 6 - i * 13);
                ctx.restore();
            });
        }
    };

    function percentile(sorted, q) {
        if (!sorted.length) return 0;
        var pos = (sorted.length - 1) * q;
        var lo = Math.floor(pos);
        var hi = Math.ceil(pos);
        return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
    }

    function niceStep(range, count) {
        var raw = range / count;
        var power = Math.pow(10, Math.floor(Math.log10(raw)));
        var steps = [1, 2, 2.5, 5, 10];
        for (var i = 0; i < steps.length; i++) {
            if (steps[i] * power >= raw) return steps[i] * power;
        }
        return 10 * power;
    }

    function compactEuro(value) {
        var abs = Math.abs(value);
        if (abs >= 1e6) return (value / 1e6).toLocaleString(lang, { maximumFractionDigits: 1 }) + ' M€';
        if (abs >= 1e4) return Math.round(value / 1e3).toLocaleString(lang) + ' k€';
        return charts.money.format(value) + ' €';
    }

    // ---------- Histogramme des valeurs finales ----------
    var histogramCanvas = document.getElementById('simHistogramChart');
    var histogram = histogramCanvas && charts.create(histogramCanvas, function (p) {
        var series = data[mode];
        var invested = data.invested[data.invested.length - 1];
        var finals = series.paths.map(function (path) { return path[path.length - 1]; })
            .sort(function (a, b) { return a - b; });
        // Bornes : 2e et 98e centiles, les extrêmes tombent dans les cases du bord
        var low = percentile(finals, 0.02);
        var high = percentile(finals, 0.98);
        var step = niceStep(Math.max(high - low, 1), 14);
        var start = Math.floor(low / step) * step;
        var count = Math.max(1, Math.ceil((high - start) / step));
        var bins = [];
        for (var i = 0; i < count; i++) bins.push({ x: start + (i + 0.5) * step, y: 0, from: start + i * step });
        finals.forEach(function (value) {
            var index = Math.min(count - 1, Math.max(0, Math.floor((value - start) / step)));
            bins[index].y += 1;
        });
        var median = series.p50[series.p50.length - 1];

        return {
            type: 'bar',
            data: {
                datasets: [{
                    label: gettext('Scénarios'),
                    data: bins,
                    backgroundColor: bins.map(function (bin) {
                        return bin.x < invested ? charts.alpha(p.expense, 0.75) : charts.alpha(p.savings, 0.75);
                    }),
                    hoverBackgroundColor: bins.map(function (bin) { return bin.x < invested ? p.expense : p.savings; }),
                    borderRadius: 4,
                    barPercentage: 1,
                    categoryPercentage: 0.92
                }]
            },
            options: {
                layout: { padding: { top: 30 } },
                plugins: {
                    legend: { display: false },
                    simMarkers: {
                        lines: [
                            { value: invested, color: p.budget, dash: [5, 4], label: gettext('Investi') },
                            { value: median, color: p.text, label: gettext('Médiane') }
                        ]
                    },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                var bin = items[0].raw;
                                return compactEuro(bin.from) + ' – ' + compactEuro(bin.from + step);
                            },
                            label: function (ctx) {
                                return ' ' + ctx.raw.y + ' ' + (ctx.raw.y > 1 ? gettext('scénarios') : gettext('scénario'));
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'linear',
                        offset: false,
                        min: start,
                        max: start + count * step,
                        grid: { display: false },
                        border: { display: false },
                        ticks: { color: p.muted, maxTicksLimit: 6, callback: compactEuro }
                    },
                    y: {
                        beginAtZero: true,
                        grid: { color: p.grid },
                        border: { display: false },
                        ticks: { color: p.muted, precision: 0 },
                        title: { display: true, text: gettext('scénarios'), color: p.muted }
                    }
                }
            },
            plugins: [markersPlugin]
        };
    });

    // ---------- Du pire au meilleur : percentiles sans / avec inflation ----------
    var percentileCanvas = document.getElementById('simPercentileChart');
    var percentileLabels = [gettext('Défavorable (5 %)'), gettext('Prudent (25 %)'), gettext('Médian'), gettext('Favorable (75 %)'), gettext('Très favorable (95 %)')];
    var percentileKeys = ['p5', 'p25', 'p50', 'p75', 'p95'];
    if (percentileCanvas) charts.create(percentileCanvas, function (p) {
        function last(kind) {
            return percentileKeys.map(function (key) { var values = data[kind][key]; return values[values.length - 1]; });
        }
        var invested = data.invested[data.invested.length - 1];
        return {
            type: 'bar',
            data: {
                labels: percentileLabels,
                datasets: [
                    { label: gettext('Sans inflation'), data: last('nominal'), backgroundColor: p.savings, borderRadius: 5, barPercentage: 0.8, categoryPercentage: 0.75 },
                    { label: gettext('Avec inflation'), data: last('real'), backgroundColor: charts.alpha(p.savings, 0.38), borderRadius: 5, barPercentage: 0.8, categoryPercentage: 0.75 }
                ]
            },
            options: {
                indexAxis: 'y',
                layout: { padding: { top: 18 } },
                plugins: {
                    legend: { position: 'bottom' },
                    simMarkers: { lines: [{ value: invested, color: p.budget, dash: [5, 4], label: gettext('Investi') }] },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return ' ' + ctx.dataset.label + ' : ' + charts.money.format(ctx.parsed.x) + ' €';
                            },
                            afterLabel: function (ctx) {
                                var gain = invested ? (ctx.parsed.x / invested - 1) * 100 : 0;
                                return '   ' + (gain >= 0 ? '+' : '') + gain.toLocaleString(lang, { maximumFractionDigits: 0 }) + ' % ' + gettext('vs investi');
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        beginAtZero: true,
                        grid: { color: p.grid },
                        border: { display: false },
                        ticks: { color: p.muted, maxTicksLimit: 6, callback: compactEuro }
                    },
                    y: { grid: { display: false }, border: { display: false }, ticks: { color: p.text } }
                }
            },
            plugins: [markersPlugin]
        };
    });

    // ---------- Rendement / risque de chaque ligne ----------
    var riskCanvas = document.getElementById('simRiskChart');
    if (riskCanvas && data.assets && data.assets.length) {
        charts.create(riskCanvas, function (p) {
            var datasets = data.assets.map(function (asset, i) {
                var color = p.cats[i % p.cats.length];
                return {
                    label: asset.key,
                    data: [{ x: asset.volatility, y: asset.drift, r: 6 + Math.sqrt(asset.weight) * 2.2, asset: asset }],
                    backgroundColor: charts.alpha(color, 0.55),
                    borderColor: color,
                    borderWidth: 1.5
                };
            });
            if (data.market) {
                datasets.push({
                    label: data.market.name,
                    data: [{ x: data.market.volatility, y: data.market.drift, r: 7, asset: data.market }],
                    pointStyle: 'rectRot',
                    backgroundColor: 'transparent',
                    borderColor: p.text,
                    borderWidth: 2
                });
            }
            var pct = function (value) { return value.toLocaleString(lang, { maximumFractionDigits: 1 }) + ' %'; };
            return {
                type: 'bubble',
                data: { datasets: datasets },
                options: {
                    layout: { padding: 8 },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                title: function (items) { return items[0].raw.asset.name; },
                                label: function (ctx) {
                                    var a = ctx.raw.asset;
                                    var lines = [
                                        ' ' + gettext('Rendement') + ' : ' + pct(a.drift) + ' ' + gettext('/an'),
                                        ' ' + gettext('Volatilité') + ' : ' + pct(a.volatility)
                                    ];
                                    if (a.weight !== undefined) lines.push(' ' + gettext('Poids') + ' : ' + pct(a.weight));
                                    return lines;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            beginAtZero: true,
                            grace: '10%',
                            grid: { color: p.grid },
                            border: { display: false },
                            ticks: { color: p.muted, callback: pct },
                            title: { display: true, text: gettext('Volatilité (risque)'), color: p.muted }
                        },
                        y: {
                            grace: '15%',
                            grid: { color: p.grid },
                            border: { display: false },
                            ticks: { color: p.muted, callback: pct },
                            title: { display: true, text: gettext('Rendement annuel'), color: p.muted }
                        }
                    }
                }
            };
        });
    }

    // ---------- Éventail de l'inflation ----------
    var inflationCanvas = document.getElementById('simInflationChart');
    if (inflationCanvas && data.inflation) {
        charts.create(inflationCanvas, function (p) {
            var infl = data.inflation;
            var color = p.expense;
            var target = data.labels.map(function () { return data.inflation_target; });
            var scales = charts.scales(p, { percent: true });
            scales.y.beginAtZero = false;
            scales.y.ticks.callback = function (value) { return value.toLocaleString(lang, { maximumFractionDigits: 1 }) + ' %'; };
            scales.x.ticks.autoSkip = false;
            scales.x.ticks.callback = function (value, index) {
                var step = data.labels.length > 241 ? 60 : 12;
                return index % step === 0 ? yearFormat.format(labelDate(data.labels[index])) : null;
            };
            return {
                type: 'line',
                data: {
                    labels: data.labels,
                    datasets: [
                        band(gettext('Très haute (95 %)'), infl.p95, charts.alpha(color, 0.1), '+1'),
                        band(gettext('Très basse (5 %)'), infl.p5, 'transparent', false),
                        band(gettext('Haute (75 %)'), infl.p75, charts.alpha(color, 0.2), '+1'),
                        band(gettext('Basse (25 %)'), infl.p25, 'transparent', false),
                        { label: gettext('Médiane'), data: infl.p50, borderColor: color, borderWidth: 2.5, pointRadius: 0, fill: false, tension: 0.2, order: 1 },
                        { label: gettext('Moyenne de long terme'), data: target, borderColor: p.muted, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, fill: false, order: 0 }
                    ]
                },
                options: {
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { position: 'bottom', labels: { filter: function (item) { return item.datasetIndex >= 4; } } },
                        tooltip: {
                            itemSort: function (a, b) { return b.parsed.y - a.parsed.y; },
                            callbacks: {
                                title: function (items) { return monthFormat.format(labelDate(data.labels[items[0].dataIndex])); },
                                label: function (ctx) { return ' ' + ctx.dataset.label + ' : ' + ctx.parsed.y.toLocaleString(lang, { maximumFractionDigits: 1 }) + ' %'; }
                            }
                        }
                    },
                    scales: scales
                }
            };
        });
    }

    document.querySelectorAll('.js-sim-mode').forEach(function (button) {
        button.addEventListener('click', function () {
            mode = button.getAttribute('data-mode');
            document.querySelectorAll('.js-sim-mode').forEach(function (other) {
                var active = other === button;
                other.classList.toggle('btn-primary', active);
                other.classList.toggle('btn-outline-secondary', !active);
                other.setAttribute('aria-pressed', active ? 'true' : 'false');
            });
            handle.render();
            if (histogram) histogram.render();
        });
    });
})();
