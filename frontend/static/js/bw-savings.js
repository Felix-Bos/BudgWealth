/*
 * Module Épargne : graphiques (évolution, allocation, frais, soldes des
 * livrets), recherche d'un titre sur Yahoo Finance, formulaire d'ajout
 * d'une ligne et actualisation des cours.
 */
(function () {
    var dataEl = document.getElementById('savings-data');
    var data = dataEl ? JSON.parse(dataEl.textContent) : {};
    var charts = window.BW && window.BW.charts;
    var urls = data.urls || {};
    var money2 = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    var dayFormat = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });
    var monthFormat = new Intl.DateTimeFormat('fr-FR', { month: 'short', year: '2-digit' });
    var shortDay = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short' });

    function num(values) { return (values || []).map(Number); }

    function parseDay(iso) {
        var parts = iso.split('-');
        return new Date(+parts[0], +parts[1] - 1, +parts[2]);
    }

    function csrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input) return input.value;
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : '';
    }

    function getJson(url) {
        return fetch(url, {
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        }).then(function (response) { return response.json(); });
    }

    // ============================================================
    // GRAPHIQUES DANS LE TEMPS (avec choix de la période)
    // ============================================================

    var RANGE_MONTHS = { '1M': 1, '6M': 6, '1Y': 12 };

    function rangeStart(dates, range) {
        if (!dates.length || !RANGE_MONTHS[range]) return 0;
        var last = parseDay(dates[dates.length - 1]);
        var cutoff = new Date(last.getFullYear(), last.getMonth() - RANGE_MONTHS[range], last.getDate());
        for (var i = 0; i < dates.length; i++) {
            if (parseDay(dates[i]) >= cutoff) return i;
        }
        return 0;
    }

    function timeChart(canvasId, series, buildDatasets, options) {
        var canvas = document.getElementById(canvasId);
        if (!canvas || !charts || !series || !series.dates || !series.dates.length) return;
        options = options || {};
        var state = { range: 'ALL' };

        var handle = charts.create(canvas, function (p) {
            var start = rangeStart(series.dates, state.range);
            var dates = series.dates.slice(start);
            var span = (parseDay(dates[dates.length - 1]) - parseDay(dates[0])) / 86400000;
            var scales = charts.scales(p, { stacked: !!options.stacked });
            scales.x.ticks.maxTicksLimit = 7;
            scales.x.ticks.callback = function (value) {
                var day = parseDay(this.getLabelForValue(value));
                return span > 120 ? monthFormat.format(day) : shortDay.format(day);
            };
            var slice = function (values) { return num(values).slice(start); };
            return {
                type: 'line',
                data: { labels: dates, datasets: buildDatasets(p, slice) },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    elements: {
                        point: { radius: 0, hoverRadius: 4 },
                        line: { tension: 0.15, borderWidth: 2 }
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                title: function (items) {
                                    return items.length ? dayFormat.format(parseDay(items[0].label)) : '';
                                },
                                label: charts.euroLabel,
                                footer: options.stacked ? function (items) {
                                    var total = items.reduce(function (sum, item) { return sum + item.parsed.y; }, 0);
                                    return 'Total : ' + money2.format(total) + ' €';
                                } : undefined
                            }
                        }
                    },
                    scales: scales
                }
            };
        });

        var group = document.querySelector('[data-range-for="' + canvasId + '"]');
        if (!group) return;
        group.addEventListener('click', function (event) {
            var button = event.target.closest('button[data-range]');
            if (!button) return;
            state.range = button.getAttribute('data-range');
            group.querySelectorAll('button').forEach(function (other) {
                other.classList.toggle('is-active', other === button);
            });
            handle.render();
        });
    }

    function area(label, values, color, extra) {
        var dataset = {
            label: label,
            data: values,
            borderColor: color,
            backgroundColor: charts.alpha(color, 0.22),
            fill: 'origin'
        };
        for (var key in extra || {}) dataset[key] = extra[key];
        return dataset;
    }

    if (charts) {
        // Vue d'ensemble : livrets + titres, empilés
        timeChart('savingsHistoryChart', data.history, function (p, slice) {
            return [
                area('Livrets & comptes', slice(data.history.balances), p.cats[0]),
                area('Titres (PEA, CTO)', slice(data.history.securities), p.savings, { fill: '-1' })
            ];
        }, { stacked: true });

        // Portefeuille : valeur face au montant investi (vert au-dessus, rouge en dessous)
        timeChart('portfolioHistoryChart', data.history, function (p, slice) {
            return [
                {
                    label: 'Valeur',
                    data: slice(data.history.value),
                    borderColor: p.savings,
                    fill: {
                        target: 1,
                        above: charts.alpha(p.income, 0.16),
                        below: charts.alpha(p.expense, 0.16)
                    }
                },
                {
                    label: 'Investi',
                    data: slice(data.history.invested),
                    borderColor: p.budget,
                    borderDash: [6, 4],
                    borderWidth: 1.5,
                    tension: 0,
                    fill: false
                }
            ];
        });

        // Frais cumulés
        timeChart('feesHistoryChart', data.fees, function (p, slice) {
            return [
                area('Courtage', slice(data.fees.brokerage), p.cats[1]),
                area('Frais de compte', slice(data.fees.accountFees), p.cats[3], { fill: '-1' }),
                area('Frais des ETF (estimés)', slice(data.fees.ter), p.cats[5], { fill: '-1' })
            ];
        }, { stacked: true });
    }

    // ============================================================
    // ALLOCATION (anneaux)
    // ============================================================

    function centerText(label) {
        return {
            id: 'centerText',
            afterDraw: function (chart) {
                var meta = chart.getDatasetMeta(0);
                if (!meta.data.length) return;
                var p = charts.palette();
                var x = meta.data[0].x;
                var y = meta.data[0].y;
                var total = chart.data.datasets[0].data.reduce(function (a, b) { return a + b; }, 0);
                var ctx = chart.ctx;
                ctx.save();
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = p.muted;
                ctx.font = '600 12px Inter, sans-serif';
                ctx.fillText(label, x, y - 13);
                ctx.fillStyle = p.text;
                ctx.font = '800 19px "Plus Jakarta Sans", Inter, sans-serif';
                ctx.fillText(charts.money.format(total) + ' €', x, y + 10);
                ctx.restore();
            }
        };
    }

    if (charts) {
        document.querySelectorAll('canvas[data-allocation]').forEach(function (canvas) {
            var items = (data.allocation || {})[canvas.getAttribute('data-allocation')] || [];
            if (!items.length) return;
            charts.create(canvas, function (p) {
                return {
                    type: 'doughnut',
                    data: {
                        labels: items.map(function (item) { return item.name; }),
                        datasets: [{
                            data: items.map(function (item) { return Number(item.value); }),
                            backgroundColor: items.map(function (_, i) { return p.cats[i % p.cats.length]; }),
                            borderColor: p.surface,
                            borderWidth: 2,
                            hoverOffset: 6
                        }]
                    },
                    options: {
                        cutout: '68%',
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                callbacks: {
                                    label: function (ctx) {
                                        var percent = items[ctx.dataIndex].percent.replace('.', ',');
                                        return ' ' + money2.format(ctx.parsed) + ' € · ' + percent + ' %';
                                    }
                                }
                            }
                        }
                    },
                    plugins: [centerText(canvas.getAttribute('data-center') || '')]
                };
            });
        });
    }

    // ============================================================
    // LIVRETS : petite courbe du solde
    // ============================================================

    if (charts) {
        var sparklines = data.sparklines || {};
        document.querySelectorAll('canvas[data-sparkline]').forEach(function (canvas) {
            var serie = sparklines[canvas.getAttribute('data-sparkline')];
            if (!serie || serie.dates.length < 2) return;
            charts.create(canvas, function (p) {
                return {
                    type: 'line',
                    data: {
                        labels: serie.dates,
                        datasets: [{
                            data: num(serie.values),
                            borderColor: p.savings,
                            backgroundColor: charts.alpha(p.savings, 0.14),
                            fill: 'origin',
                            stepped: 'before',
                            borderWidth: 2,
                            pointRadius: 0,
                            pointHoverRadius: 4
                        }]
                    },
                    options: {
                        interaction: { mode: 'index', intersect: false },
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                callbacks: {
                                    title: function (items) { return dayFormat.format(parseDay(items[0].label)); },
                                    label: function (ctx) { return ' ' + money2.format(ctx.parsed.y) + ' €'; }
                                }
                            }
                        },
                        scales: {
                            x: { display: false },
                            y: { display: false, beginAtZero: false, grace: '25%' }
                        }
                    }
                };
            });
        });
    }

    // ============================================================
    // ACTUALISATION DES COURS
    // ============================================================

    (function () {
        var box = document.querySelector('.js-prices');
        if (!box || !urls.refresh) return;
        var label = box.querySelector('.js-prices-label');
        var button = box.querySelector('.js-prices-refresh');
        var original = label.textContent;

        function done(message, isError) {
            box.classList.remove('is-loading');
            box.classList.toggle('is-error', !!isError);
            button.disabled = false;
            label.textContent = message;
        }

        function refresh(force) {
            box.classList.add('is-loading');
            button.disabled = true;
            label.textContent = 'Mise à jour des cours…';
            var body = new FormData();
            if (force) body.append('force', '1');
            fetch(urls.refresh, {
                method: 'POST',
                body: body,
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' }
            })
                .then(function (response) { return response.json(); })
                .then(function (result) {
                    if (result.updated > 0) {
                        window.location.reload();
                        return;
                    }
                    if (result.errors && result.errors.length) {
                        done('Cours non actualisés : ' + result.errors[0], true);
                    } else {
                        done(original, false);
                    }
                })
                .catch(function () { done('Yahoo Finance est injoignable', true); });
        }

        button.addEventListener('click', function () { refresh(true); });
        if (box.getAttribute('data-stale') === '1') refresh(false);
    })();

    // ============================================================
    // AJOUT D'UNE LIGNE : recherche du titre, cours du jour, total
    // ============================================================

    var KIND_LABELS = {
        BUY: "Ajouter l'achat",
        SELL: 'Ajouter la vente',
        DIVIDEND: 'Ajouter le dividende',
        FEE: 'Ajouter les frais'
    };

    function setupOperationForm(form) {
        var fields = {
            symbol: form.querySelector('[name="symbol"]'),
            name: form.querySelector('[name="instrument_name"]'),
            isin: form.querySelector('[name="isin"]'),
            exchange: form.querySelector('[name="exchange"]'),
            quoteType: form.querySelector('[name="quote_type"]')
        };
        var search = form.querySelector('.js-instrument-search');
        var query = form.querySelector('.js-instrument-query');
        var results = form.querySelector('.js-instrument-results');
        var picked = form.querySelector('.js-instrument-picked');
        var status = form.querySelector('.js-instrument-status');
        var heldBox = form.querySelector('.js-held');
        var dateInput = form.querySelector('[name="date"]');
        var quantityInput = form.querySelector('[name="quantity"]');
        var priceInput = form.querySelector('[name="unit_price"]');
        var feesInput = form.querySelector('[name="fees"]');
        var amountInput = form.querySelector('[name="amount"]');
        var hint = form.querySelector('.js-price-hint');
        var total = form.querySelector('.js-op-total');
        var submit = form.querySelector('.js-op-submit');
        var kindInputs = form.querySelectorAll('input[name="kind"]');

        var items = [];
        var activeIndex = -1;
        var searchTimer = null;
        var lastQuery = '';
        var priceTimer = null;
        var priceKey = '';

        function kind() {
            var checked = form.querySelector('input[name="kind"]:checked');
            return checked ? checked.value : 'BUY';
        }

        function setStatus(message, isError) {
            status.hidden = !message;
            status.textContent = message || '';
            status.classList.toggle('is-error', !!isError);
        }

        // ---------- Type d'opération ----------
        function renderKind() {
            var current = kind();
            form.setAttribute('data-kind', current);
            form.querySelectorAll('[data-show-for]').forEach(function (el) {
                el.hidden = el.getAttribute('data-show-for').split(' ').indexOf(current) === -1;
            });
            kindInputs.forEach(function (input) {
                input.closest('label').classList.toggle('is-active', input.checked);
            });
            submit.textContent = KIND_LABELS[current];
            updateTotal();
            suggestPrice();
        }

        // ---------- Recherche ----------
        function closeResults() {
            results.hidden = true;
            results.innerHTML = '';
            query.setAttribute('aria-expanded', 'false');
            activeIndex = -1;
        }

        function highlight(index) {
            var options = results.querySelectorAll('li');
            activeIndex = (index + options.length) % options.length;
            options.forEach(function (option, i) {
                option.classList.toggle('is-active', i === activeIndex);
                option.setAttribute('aria-selected', i === activeIndex ? 'true' : 'false');
            });
            query.setAttribute('aria-activedescendant', options[activeIndex].id);
        }

        function renderResults() {
            results.innerHTML = '';
            items.forEach(function (item, i) {
                var option = document.createElement('li');
                option.id = 'instrument-option-' + i;
                option.setAttribute('role', 'option');
                var name = document.createElement('strong');
                name.textContent = item.name;
                var meta = document.createElement('small');
                meta.textContent = [item.symbol, item.exchange, item.type_label].filter(Boolean).join(' · ');
                option.appendChild(name);
                option.appendChild(meta);
                option.addEventListener('mousedown', function (event) {
                    event.preventDefault();
                    pick(item);
                });
                results.appendChild(option);
            });
            results.hidden = false;
            query.setAttribute('aria-expanded', 'true');
            activeIndex = -1;
        }

        function runSearch(text) {
            lastQuery = text;
            setStatus('Recherche sur Yahoo Finance…');
            getJson(urls.search + '?q=' + encodeURIComponent(text))
                .then(function (payload) {
                    if (text !== lastQuery) return;
                    if (payload.error) {
                        setStatus(payload.error, true);
                        closeResults();
                        return;
                    }
                    items = payload.results || [];
                    if (!items.length) {
                        setStatus('Aucun titre trouvé pour « ' + text + ' ». Essaie l\'ISIN ou le ticker Yahoo (ex. CW8.PA).', true);
                        closeResults();
                        return;
                    }
                    setStatus('');
                    renderResults();
                })
                .catch(function () {
                    if (text === lastQuery) setStatus('Yahoo Finance ne répond pas. Réessaie dans un instant.', true);
                });
        }

        function pick(item) {
            fields.symbol.value = item.symbol;
            fields.name.value = item.name;
            fields.isin.value = item.isin || '';
            fields.exchange.value = item.exchange || '';
            fields.quoteType.value = item.quote_type || '';
            picked.querySelector('.js-picked-name').textContent = item.name;
            picked.querySelector('.js-picked-meta').textContent =
                [item.symbol, item.isin, item.exchange, item.type_label || item.quote_type].filter(Boolean).join(' · ');
            picked.hidden = false;
            search.hidden = true;
            if (heldBox) heldBox.hidden = true;
            closeResults();
            setStatus('');
            suggestPrice();
            var next = kind() === 'DIVIDEND' ? amountInput : quantityInput;
            if (next) next.focus();
        }

        function clearPick() {
            Object.keys(fields).forEach(function (key) { fields[key].value = ''; });
            picked.hidden = true;
            search.hidden = false;
            if (heldBox) heldBox.hidden = !heldBox.querySelector('button');
            hint.hidden = true;
            priceKey = '';
            query.value = '';
            query.focus();
        }

        query.addEventListener('input', function () {
            clearTimeout(searchTimer);
            var text = query.value.trim();
            if (text.length < 2) {
                closeResults();
                setStatus('');
                return;
            }
            searchTimer = setTimeout(function () { runSearch(text); }, 300);
        });

        query.addEventListener('keydown', function (event) {
            var open = !results.hidden && items.length;
            if (event.key === 'ArrowDown' && open) {
                event.preventDefault();
                highlight(activeIndex + 1);
            } else if (event.key === 'ArrowUp' && open) {
                event.preventDefault();
                highlight(activeIndex - 1);
            } else if (event.key === 'Enter') {
                // Entrée choisit un résultat, elle n'envoie jamais le formulaire
                event.preventDefault();
                if (open) pick(items[Math.max(activeIndex, 0)]);
            } else if (event.key === 'Escape') {
                closeResults();
            }
        });

        query.addEventListener('blur', function () { setTimeout(closeResults, 150); });
        picked.querySelector('.js-instrument-clear').addEventListener('click', clearPick);

        // Raccourcis : titres déjà en portefeuille
        if (heldBox) {
            (data.held || []).forEach(function (item) {
                var chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'savings-held-chip';
                chip.textContent = item.name;
                chip.title = item.symbol;
                chip.addEventListener('click', function () { pick(item); });
                heldBox.appendChild(chip);
            });
            heldBox.hidden = !(data.held || []).length;
        }

        // ---------- Cours à la date choisie ----------
        function suggestPrice() {
            var current = kind();
            var symbol = fields.symbol.value;
            var day = dateInput.value;
            if (!urls.price || !symbol || !day || (current !== 'BUY' && current !== 'SELL')) {
                hint.hidden = true;
                priceKey = '';
                return;
            }
            var key = symbol + '|' + day;
            if (key === priceKey) {
                hint.hidden = false;
                return;
            }
            priceKey = key;
            clearTimeout(priceTimer);
            priceTimer = setTimeout(function () {
                hint.hidden = false;
                hint.textContent = 'Recherche du cours…';
                getJson(urls.price + '?symbol=' + encodeURIComponent(symbol) + '&date=' + day)
                    .then(function (result) {
                        if (key !== priceKey) return;
                        if (!result.ok) {
                            hint.textContent = result.error || 'Cours indisponible à cette date.';
                            return;
                        }
                        hint.textContent = '';
                        var text = document.createElement('span');
                        text.textContent = 'Clôture du ' + dayFormat.format(parseDay(result.date)) + ' : ' +
                            money2.format(Number(result.eur)) + ' €' +
                            (result.currency !== 'EUR' ? ' (' + money2.format(Number(result.close)) + ' ' + result.currency + ')' : '');
                        var use = document.createElement('button');
                        use.type = 'button';
                        use.className = 'btn btn-link btn-sm p-0';
                        use.textContent = 'Utiliser ce prix';
                        use.addEventListener('click', function () {
                            priceInput.value = result.eur;
                            updateTotal();
                            (feesInput || priceInput).focus();
                        });
                        hint.appendChild(text);
                        hint.appendChild(document.createTextNode(' · '));
                        hint.appendChild(use);
                    })
                    .catch(function () {
                        if (key === priceKey) hint.textContent = 'Cours indisponible.';
                    });
            }, 250);
        }

        // ---------- Total de l'opération ----------
        function updateTotal() {
            var current = kind();
            var quantity = parseFloat(quantityInput.value);
            var price = parseFloat(priceInput.value);
            var fees = parseFloat(feesInput.value) || 0;
            if ((current === 'BUY' || current === 'SELL') && quantity > 0 && price > 0) {
                var gross = quantity * price;
                var amount = current === 'BUY' ? gross + fees : gross - fees;
                total.hidden = false;
                total.textContent = (current === 'BUY' ? 'Total débité : ' : 'Total crédité : ') +
                    money2.format(amount) + ' €' +
                    (fees > 0 ? ' · frais ' + (fees / gross * 100).toFixed(2).replace('.', ',') + ' %' : '');
            } else {
                total.hidden = true;
            }
        }

        // ---------- Vérifications avant envoi ----------
        function requirePositive(input) {
            var ok = parseFloat(input.value) > 0;
            input.classList.toggle('is-invalid', !ok);
            return ok;
        }

        form.addEventListener('submit', function (event) {
            var current = kind();
            if (current !== 'FEE' && !fields.symbol.value) {
                event.preventDefault();
                setStatus('Choisis d\'abord un titre dans la liste des résultats.', true);
                query.focus();
                return;
            }
            var ok = true;
            if (current === 'BUY' || current === 'SELL') {
                ok = requirePositive(quantityInput) & requirePositive(priceInput);
            } else {
                ok = requirePositive(amountInput);
            }
            if (!ok) event.preventDefault();
        });

        kindInputs.forEach(function (input) { input.addEventListener('change', renderKind); });
        dateInput.addEventListener('change', suggestPrice);
        [quantityInput, priceInput, feesInput].forEach(function (input) {
            input.addEventListener('input', function () {
                input.classList.remove('is-invalid');
                updateTotal();
            });
        });
        amountInput.addEventListener('input', function () { amountInput.classList.remove('is-invalid'); });
        renderKind();
    }

    var operationForm = document.querySelector('.js-operation-form');
    if (operationForm && urls.search) setupOperationForm(operationForm);
})();
