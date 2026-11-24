import {DAY, WINDOW, fromDay, onResize, plotChart, toDay, whole} from '../tokens/charts.js';

const redraws = new Map();
const integer = value => value === null ? '–' : whole(Math.round(value));
const color = 'var(--accent)';

function plot(figure, options) {
  redraws.set(figure, plotChart(figure, options));
}

function spansOf(records) {
  return records.map(([key]) => ({key, time: fromDay(key)}));
}

function requests(figure) {
  const records = JSON.parse(figure.dataset.requests);
  const counts = records.map(([, value]) => value);
  // Today is unfinished, so it has a dot but does not pull the line down.
  const values = counts.map((_, index) => {
    if (index === counts.length - 1) return null;
    const days = counts.slice(Math.max(0, index - WINDOW + 1), index + 1);
    return days.reduce((sum, value) => sum + value, 0) / days.length;
  });
  plot(figure, {
    label: 'Request records per day', spans: spansOf(records), format: integer, rolling: true,
    series: [{name: 'Mean per day', color, values, dots: counts}],
  });
}

function production(section) {
  const figures = [...section.querySelectorAll('[data-production]')];
  const controls = section.querySelector('.chart-controls');
  const show = mode => {
    for (const button of controls.querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.trendMode === mode));
    }
    for (const figure of figures) {
      const records = JSON.parse(figure.dataset.production)[mode];
      const spans = spansOf(records);
      const weekly = mode === 'weekly';
      if (weekly) for (const span of spans) span.partial = span.time + 6 * DAY > fromDay(toDay(Date.now()));
      plot(figure, {
        label: figure.dataset.label, spans, format: integer, stacked: weekly, rolling: !weekly,
        period: weekly ? 'Week of' : 'Through',
        series: [{name: figure.dataset.label, color, values: records.map(([, value]) => value)}],
      });
    }
  };
  controls.hidden = false;
  controls.addEventListener('click', event => {
    const mode = event.target.dataset.trendMode;
    if (mode) show(mode);
  });
  show('weekly');
}

for (const figure of document.querySelectorAll('[data-requests]')) requests(figure);
for (const section of document.querySelectorAll('#trends:has([data-production])')) production(section);
onResize(document.querySelector('main'), () => { for (const redraw of redraws.values()) redraw?.(); });
