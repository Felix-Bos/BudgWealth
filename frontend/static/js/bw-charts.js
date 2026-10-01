/*
 * Graphiques BudgWealth (Chart.js).
 *
 * - Couleurs lues dans les variables CSS --chart-* : elles suivent le thème.
 * - Chaque graphique est recréé quand on passe du mode jour au mode nuit
 *   (bouton du thème ou réglage du système).
 */
(function () {
    var renderers = [];

    function css(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function palette() {
        return {
            text: css('--chart-text'),
            muted: css('--chart-muted'),
            grid: css('--chart-grid'),
            surface: css('--chart-surface'),
            income: css('--chart-income'),
            expense: css('--chart-expense'),
            savings: css('--chart-savings'),
            balance: css('--chart-balance'),
            budget: css('--chart-budget'),
            cats: [1, 2, 3, 4, 5, 6, 7, 8].map(function (i) { return css('--chart-cat-' + i); })
        };
    }

    var money = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 });
    var money2 = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    // Couleur avec transparence, à partir d'un #rrggbb
    function alpha(hex, value) {
        var match = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
        if (!match) return hex;
        return 'rgba(' + parseInt(match[1], 16) + ',' + parseInt(match[2], 16) + ',' + parseInt(match[3], 16) + ',' + value + ')';
    }

    function applyDefaults(p) {
        Chart.defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
        Chart.defaults.color = p.muted;
        Chart.defaults.borderColor = p.grid;
        Chart.defaults.maintainAspectRatio = false;
        Chart.defaults.animation.duration = 450;

        var tooltip = Chart.defaults.plugins.tooltip;
        tooltip.backgroundColor = p.surface;
        tooltip.titleColor = p.text;
        tooltip.bodyColor = p.text;
        tooltip.borderColor = p.grid;
        tooltip.borderWidth = 1;
        tooltip.padding = 12;
        tooltip.cornerRadius = 10;
        tooltip.boxPadding = 6;
        tooltip.usePointStyle = true;
        tooltip.titleFont = { weight: '600' };

        var legend = Chart.defaults.plugins.legend.labels;
        legend.usePointStyle = true;
        legend.boxWidth = 8;
        legend.boxHeight = 8;
        legend.padding = 16;
        legend.color = p.muted;
    }

    // Axes standards : grille discrète, montants en euros
    function scales(p, options) {
        options = options || {};
        return {
            x: {
                stacked: !!options.stacked,
                grid: { display: false },
                border: { display: false },
                ticks: { color: p.muted, maxRotation: 0, autoSkipPadding: 12 }
            },
            y: {
                stacked: !!options.stacked,
                beginAtZero: true,
                grid: { color: p.grid },
                border: { display: false },
                ticks: {
                    color: p.muted,
                    callback: function (value) {
                        return options.percent ? value + ' %' : money.format(value) + ' €';
                    }
                }
            }
        };
    }

    function euroLabel(ctx) {
        var value = ctx.parsed.y !== undefined ? ctx.parsed.y : ctx.parsed;
        if (value === null || value === undefined) return null;
        return ' ' + ctx.dataset.label + ' : ' + money2.format(value) + ' €';
    }

    function create(canvas, build) {
        var handle = { chart: null };
        handle.render = function () {
            if (!canvas || !window.Chart) return;
            var p = palette();
            applyDefaults(p);
            if (handle.chart) handle.chart.destroy();
            handle.chart = new Chart(canvas, build(p));
        };
        renderers.push(handle.render);
        handle.render();
        return handle;
    }

    function rerenderAll() {
        // Laisse le navigateur appliquer le nouveau thème avant de relire les couleurs
        requestAnimationFrame(function () {
            renderers.forEach(function (render) { render(); });
        });
    }

    new MutationObserver(rerenderAll).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-theme']
    });
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', rerenderAll);
    }

    window.BW = window.BW || {};
    window.BW.charts = {
        create: create,
        palette: palette,
        scales: scales,
        euroLabel: euroLabel,
        alpha: alpha,
        money: money,
        money2: money2
    };
})();
