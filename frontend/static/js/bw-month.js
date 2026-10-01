/*
 * Page d'un mois : graphiques, filtres, et modifications sans recharger
 * la page (suppression, intitulé, catégorie, sous-catégorie).
 */
(function () {
    function readJson(id) {
        var el = document.getElementById(id);
        return el ? JSON.parse(el.textContent) : null;
    }

    var charts = window.BW && window.BW.charts;
    var money2 = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    var plain = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false });

    // ============================================================
    // ANNEAU : CATÉGORIES -> SOUS-CATÉGORIES
    // ============================================================

    var donut = (function () {
        var canvas = document.getElementById('categoryDonut');
        if (!canvas || !charts) return { update: function () {} };

        var tree = readJson('category-tree-data') || [];
        var state = { index: null };
        var centerLabel = 'Charges';
        var legend = document.querySelector('.js-donut-legend');
        var back = document.querySelector('.js-donut-back');
        var title = document.querySelector('.js-donut-title');
        var subtitle = document.querySelector('.js-donut-subtitle');

        function level(p) {
            if (state.index === null || !tree[state.index]) {
                state.index = null;
                return {
                    label: 'Charges',
                    items: tree.map(function (node, i) {
                        return { name: node.name, value: Number(node.total), color: p.cats[i % p.cats.length] };
                    })
                };
            }
            var node = tree[state.index];
            var base = p.cats[state.index % p.cats.length];
            return {
                label: node.name,
                items: node.children.map(function (child, i) {
                    return { name: child.name, value: Number(child.total), color: charts.alpha(base, Math.max(0.35, 1 - i * 0.14)) };
                })
            };
        }

        // Total au centre de l'anneau
        var centerText = {
            id: 'centerText',
            afterDraw: function (chart) {
                var meta = chart.getDatasetMeta(0);
                if (!meta.data.length) return;
                var p = charts.palette();
                var x = meta.data[0].x, y = meta.data[0].y;
                var total = chart.data.datasets[0].data.reduce(function (a, b) { return a + b; }, 0);
                var ctx = chart.ctx;
                ctx.save();
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = p.muted;
                ctx.font = '600 12px Inter, sans-serif';
                ctx.fillText(centerLabel, x, y - 14);
                ctx.fillStyle = p.text;
                ctx.font = '600 22px Fraunces, Georgia, serif';
                ctx.fillText(charts.money.format(total) + ' €', x, y + 10);
                ctx.restore();
            }
        };

        function renderLegend(data) {
            var total = data.items.reduce(function (a, b) { return a + b.value; }, 0) || 1;
            legend.innerHTML = '';
            data.items.forEach(function (item, i) {
                var li = document.createElement('li');
                var button = document.createElement('button');
                button.type = 'button';
                button.className = 'month-legend-item';
                button.disabled = state.index !== null;
                button.innerHTML =
                    '<span class="month-legend-dot"></span>' +
                    '<span class="month-legend-name"></span>' +
                    '<span class="month-legend-value"></span>';
                button.querySelector('.month-legend-dot').style.background = item.color;
                button.querySelector('.month-legend-name').textContent = item.name;
                button.querySelector('.month-legend-value').textContent =
                    money2.format(item.value) + ' € · ' + Math.round(item.value / total * 100) + ' %';
                button.addEventListener('click', function () { drill(i); });
                button.addEventListener('mouseenter', function () { highlight(i); });
                button.addEventListener('mouseleave', function () { highlight(null); });
                li.appendChild(button);
                legend.appendChild(li);
            });

            back.hidden = state.index === null;
            title.textContent = state.index === null ? 'Charges par catégorie' : data.label;
            subtitle.textContent = state.index === null
                ? 'Clique sur une catégorie pour voir ses sous-catégories'
                : 'Détail par sous-catégorie';
        }

        var handle = charts.create(canvas, function (p) {
            var data = level(p);
            centerLabel = data.label;
            setTimeout(function () { renderLegend(data); });
            return {
                type: 'doughnut',
                data: {
                    labels: data.items.map(function (i) { return i.name; }),
                    datasets: [{
                        data: data.items.map(function (i) { return i.value; }),
                        backgroundColor: data.items.map(function (i) { return i.color; }),
                        borderColor: p.surface,
                        borderWidth: 3,
                        hoverOffset: 10,
                        borderRadius: 4
                    }]
                },
                options: {
                    cutout: '66%',
                    layout: { padding: 8 },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function (ctx) {
                                    var sum = ctx.dataset.data.reduce(function (a, b) { return a + b; }, 0) || 1;
                                    return ' ' + money2.format(ctx.parsed) + ' € · ' + Math.round(ctx.parsed / sum * 100) + ' %';
                                }
                            }
                        }
                    },
                    onClick: function (event, elements) {
                        if (elements.length) drill(elements[0].index);
                    },
                    onHover: function (event, elements) {
                        event.native.target.style.cursor = elements.length && state.index === null ? 'pointer' : 'default';
                    }
                },
                plugins: [centerText]
            };
        });

        function drill(index) {
            if (state.index !== null) return;
            state.index = index;
            handle.render();
        }

        function highlight(index) {
            var chart = handle.chart;
            if (!chart) return;
            chart.setActiveElements(index === null ? [] : [{ datasetIndex: 0, index: index }]);
            chart.tooltip.setActiveElements(index === null ? [] : [{ datasetIndex: 0, index: index }], { x: 0, y: 0 });
            chart.update();
        }

        back.addEventListener('click', function () {
            state.index = null;
            handle.render();
        });

        return {
            update: function (newTree) {
                var currentName = state.index !== null && tree[state.index] ? tree[state.index].name : null;
                tree = newTree || [];
                state.index = null;
                tree.forEach(function (node, i) { if (node.name === currentName) state.index = i; });
                handle.render();
            }
        };
    })();

    // ============================================================
    // CUMUL AU FIL DU MOIS
    // ============================================================

    var daily = (function () {
        var canvas = document.getElementById('dailyChart');
        if (!canvas || !charts) return { update: function () {} };
        var data = readJson('daily-data');

        function series(values) {
            return values.map(function (v) { return v === null ? null : Number(v); });
        }

        var handle = charts.create(canvas, function (p) {
            var datasets = [
                {
                    label: 'Charges',
                    data: series(data.expense),
                    borderColor: p.expense,
                    backgroundColor: charts.alpha(p.expense, 0.12),
                    fill: true,
                    tension: 0.25,
                    borderWidth: 2.5,
                    pointRadius: 0,
                    pointHoverRadius: 5
                },
                {
                    label: 'Revenus',
                    data: series(data.income),
                    borderColor: p.income,
                    tension: 0.25,
                    borderWidth: 2.5,
                    pointRadius: 0,
                    pointHoverRadius: 5,
                    stepped: false
                },
                {
                    label: 'Épargne',
                    data: series(data.savings),
                    borderColor: p.savings,
                    tension: 0.25,
                    borderWidth: 2.5,
                    pointRadius: 0,
                    pointHoverRadius: 5
                }
            ];
            if (data.pace && data.pace.length) {
                datasets.push({
                    label: 'Rythme du budget',
                    data: series(data.pace),
                    borderColor: p.budget,
                    borderDash: [6, 5],
                    borderWidth: 1.5,
                    pointRadius: 0,
                    pointHoverRadius: 0
                });
            }
            return {
                type: 'line',
                data: { labels: data.labels, datasets: datasets },
                options: {
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                title: function (items) { return 'Au ' + items[0].label; },
                                label: charts.euroLabel
                            }
                        }
                    },
                    scales: charts.scales(p)
                }
            };
        });

        return {
            update: function (newData) {
                data = newData;
                handle.render();
            }
        };
    })();

    // ============================================================
    // FILTRES REVENUS / CHARGES / ÉPARGNE
    // ============================================================

    var panel = document.querySelector('.js-tx-panel');
    var currentFilter = 'all';

    function applyFilter() {
        if (!panel) return;
        var visibleCount = 0;
        panel.querySelectorAll('.js-tx-day').forEach(function (day) {
            var visibleInDay = 0;
            day.querySelectorAll('.js-tx-row').forEach(function (row) {
                var show = currentFilter === 'all' || row.getAttribute('data-kind') === currentFilter;
                row.hidden = !show;
                var editor = row.nextElementSibling;
                if (editor && editor.classList.contains('tx-editor') && !show) editor.remove();
                if (show) visibleInDay += 1;
            });
            day.hidden = visibleInDay === 0;
            visibleCount += visibleInDay;
        });
        var empty = panel.querySelector('.js-tx-empty');
        if (empty) empty.hidden = visibleCount > 0;
    }

    if (panel) {
        panel.querySelectorAll('.tx-filter').forEach(function (button) {
            button.addEventListener('click', function () {
                currentFilter = button.getAttribute('data-filter');
                panel.querySelectorAll('.tx-filter').forEach(function (b) {
                    b.classList.toggle('is-active', b === button);
                });
                applyFilter();
            });
        });
    }

    // ============================================================
    // MISE À JOUR EN DIRECT
    // ============================================================

    function refreshMonth(month) {
        if (!month) return;
        document.querySelectorAll('.js-stat').forEach(function (el) {
            var key = el.getAttribute('data-key');
            var value = key === 'balance' ? month.balance : month.totals[key];
            if (value !== undefined) el.textContent = plain.format(Number(value));
        });
        document.querySelectorAll('.js-filter-total').forEach(function (el) {
            var kind = el.getAttribute('data-kind');
            var prefix = kind === 'income' ? '+' : (kind === 'expense' ? '−' : '');
            el.textContent = prefix + money2.format(Number(month.totals[kind])) + ' €';
        });
        var count = month.counts.income + month.counts.expense + month.counts.savings;
        var countEl = document.querySelector('.js-tx-count');
        if (countEl) countEl.textContent = count + ' opération' + (count > 1 ? 's' : '');
        donut.update(month.categoryTree);
        daily.update(month.daily);
    }

    function csrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    function post(url, data) {
        return fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
            body: data || new FormData(),
            credentials: 'same-origin'
        }).then(function (response) {
            return response.json().then(function (json) {
                if (!response.ok || !json.ok) throw new Error(json.error || 'Erreur');
                return json;
            });
        });
    }

    function toast(message, isError) {
        var el = document.createElement('div');
        el.className = 'bw-toast' + (isError ? ' is-error' : '');
        el.textContent = message;
        document.body.appendChild(el);
        requestAnimationFrame(function () { el.classList.add('is-visible'); });
        setTimeout(function () {
            el.classList.remove('is-visible');
            setTimeout(function () { el.remove(); }, 300);
        }, 2200);
    }

    // ---------- Suppression ----------

    document.addEventListener('submit', function (event) {
        var form = event.target.closest('.js-tx-delete-form');
        if (!form) return;
        event.preventDefault();
        var row = form.closest('.js-tx-row');
        row.classList.add('is-removing');

        post(row.getAttribute('data-delete-url')).then(function (json) {
            var editor = row.nextElementSibling;
            if (editor && editor.classList.contains('tx-editor')) editor.remove();
            var day = row.closest('.js-tx-day');
            row.addEventListener('transitionend', function () {
                row.remove();
                if (day && !day.querySelector('.js-tx-row')) day.remove();
                applyFilter();
            }, { once: true });
            row.classList.add('is-removed');
            refreshMonth(json.month);
            toast('Opération supprimée');
        }).catch(function () {
            row.classList.remove('is-removing');
            toast('Impossible de supprimer cette opération', true);
        });
    });

    // ---------- Édition : intitulé, catégorie, sous-catégorie ----------

    var categories = readJson('editor-categories-data') || [];

    function fillSubcategories(select, categoryId, selected) {
        var category = categories.filter(function (c) { return String(c.id) === String(categoryId); })[0];
        select.innerHTML = '<option value="">—</option>';
        (category ? category.subs : []).forEach(function (sub) {
            var option = document.createElement('option');
            option.value = sub.id;
            option.textContent = sub.name;
            if (String(sub.id) === String(selected)) option.selected = true;
            select.appendChild(option);
        });
        select.disabled = !category || !category.subs.length;
    }

    function closeEditors() {
        document.querySelectorAll('.tx-editor').forEach(function (editor) {
            var row = editor.previousElementSibling;
            if (row) row.classList.remove('is-editing');
            editor.remove();
        });
    }

    function openEditor(row) {
        var already = row.nextElementSibling && row.nextElementSibling.classList.contains('tx-editor');
        closeEditors();
        if (already) return;

        var type = row.getAttribute('data-type');
        var editor = document.createElement('li');
        editor.className = 'tx-editor';
        editor.innerHTML =
            '<form class="tx-editor-form">' +
            '  <label class="tx-editor-field tx-editor-wide"><span>Intitulé</span><input type="text" name="description" class="form-control" maxlength="255"></label>' +
            '  <label class="tx-editor-field"><span>Catégorie</span><select name="category" class="form-control"></select></label>' +
            '  <label class="tx-editor-field"><span>Sous-catégorie</span><select name="subcategory" class="form-control"></select></label>' +
            '  <div class="tx-editor-sub js-editor-sub" hidden>' +
            '    <span class="js-editor-sub-text"></span>' +
            '    <span class="tx-editor-sub-controls js-editor-sub-controls">' +
            '      <select class="form-control form-control-sm js-editor-sub-unit" aria-label="Fréquence">' +
            '        <option value="MONTH">Tous les mois</option><option value="YEAR">Tous les ans</option>' +
            '      </select>' +
            '      <button type="button" class="btn btn-outline-secondary btn-sm js-editor-track">↻ Suivre comme abonnement</button>' +
            '    </span>' +
            '  </div>' +
            '  <div class="tx-editor-actions">' +
            '    <button type="button" class="btn btn-outline-secondary btn-sm js-editor-cancel">Annuler</button>' +
            '    <button type="submit" class="btn btn-primary btn-sm">Enregistrer</button>' +
            '  </div>' +
            '</form>';

        var form = editor.querySelector('form');
        var description = form.querySelector('[name="description"]');
        var categorySelect = form.querySelector('[name="category"]');
        var subSelect = form.querySelector('[name="subcategory"]');

        description.value = row.getAttribute('data-description') || '';
        description.placeholder = row.querySelector('.js-tx-title').textContent.trim();

        categorySelect.innerHTML = '<option value="">À classer</option>';
        categories.filter(function (c) { return c.type === type; }).forEach(function (c) {
            var option = document.createElement('option');
            option.value = c.id;
            option.textContent = c.name;
            if (String(c.id) === row.getAttribute('data-category')) option.selected = true;
            categorySelect.appendChild(option);
        });
        fillSubcategories(subSelect, categorySelect.value, row.getAttribute('data-subcategory'));
        categorySelect.addEventListener('change', function () {
            fillSubcategories(subSelect, categorySelect.value, null);
        });

        // Abonnement : uniquement pour les charges
        var subBox = form.querySelector('.js-editor-sub');
        var subText = form.querySelector('.js-editor-sub-text');
        var subControls = form.querySelector('.js-editor-sub-controls');
        function renderSubscription() {
            var name = row.getAttribute('data-subscription');
            subBox.hidden = type !== 'EXPENSE';
            subControls.hidden = !!name;
            subText.textContent = name
                ? '↻ Suivi comme abonnement : ' + name
                : 'Paiement récurrent ? Suis-le pour être alerté s\u2019il s\u2019arrête.';
        }
        renderSubscription();

        form.querySelector('.js-editor-track').addEventListener('click', function () {
            var button = this;
            button.disabled = true;
            var body = new FormData();
            body.append('unit', form.querySelector('.js-editor-sub-unit').value);
            post(row.getAttribute('data-track-url'), body).then(function (json) {
                row.setAttribute('data-subscription', json.name);
                if (!row.querySelector('.js-tx-sub-flag')) {
                    var flag = document.createElement('span');
                    flag.className = 'tx-flag js-tx-sub-flag';
                    flag.textContent = '↻ Abonnement';
                    row.querySelector('.js-tx-category').after(flag);
                }
                renderSubscription();
                toast(json.message);
            }).catch(function () {
                button.disabled = false;
                toast('Impossible de suivre cet abonnement', true);
            });
        });

        form.querySelector('.js-editor-cancel').addEventListener('click', closeEditors);
        form.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') closeEditors();
        });

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var submit = form.querySelector('[type="submit"]');
            submit.disabled = true;
            post(row.getAttribute('data-edit-url'), new FormData(form)).then(function (json) {
                updateRow(row, json.row);
                closeEditors();
                refreshMonth(json.month);
                applyFilter();
                row.classList.add('is-flash');
                setTimeout(function () { row.classList.remove('is-flash'); }, 900);
            }).catch(function (error) {
                submit.disabled = false;
                toast(error.message || 'Impossible d’enregistrer', true);
            });
        });

        row.classList.add('is-editing');
        row.after(editor);
        description.focus();
    }

    function updateRow(row, data) {
        row.setAttribute('data-kind', data.kind);
        row.setAttribute('data-description', data.rawDescription || '');
        row.setAttribute('data-category', data.categoryId || '');
        row.setAttribute('data-subcategory', data.subcategoryId || '');
        row.className = row.className.replace(/\bis-(income|expense|savings)\b/g, '').trim() + ' is-' + data.kind;

        row.querySelector('.js-tx-title').textContent = data.description;

        var avatar = row.querySelector('.js-tx-avatar');
        avatar.textContent = data.initial;
        avatar.className = avatar.className.replace(/\btx-tone-\d+\b/, 'tx-tone-' + data.tone);

        var kind = row.querySelector('.js-tx-kind');
        kind.className = 'tx-kind tx-kind-' + data.kind + ' js-tx-kind';
        kind.textContent = { income: 'Revenu', savings: 'Épargne', expense: 'Charge' }[data.kind];

        var category = row.querySelector('.js-tx-category');
        if (data.category) {
            category.textContent = data.category + (data.subcategory ? ' › ' + data.subcategory : '');
        } else {
            category.innerHTML = '<span class="tx-unclassified">À classer</span>';
        }

        var amount = row.querySelector('.js-tx-amount');
        amount.className = amount.className.replace(/\bis-(income|expense|savings)\b/g, '').trim() + ' is-' + data.kind;
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('.js-tx-edit');
        if (!button || button.disabled) return;
        openEditor(button.closest('.js-tx-row'));
    });
})();
