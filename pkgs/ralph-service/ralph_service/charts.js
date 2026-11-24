const DAY = 86400000;
const SMALL_SAMPLE = 5;
const WINDOW = 7;
const SVG = 'http://www.w3.org/2000/svg';

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (name.startsWith('on')) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? '' : value);
  }
  node.append(...children.filter(child => child !== null));
  return node;
}

function shape(tag, attributes) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

const trim = value => String(Number(value.toPrecision(3)));
function compact(value) {
  if (value === null || value === undefined) return '–';
  for (const [size, suffix] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (Math.abs(value) >= size) return trim(value / size) + suffix;
  }
  return trim(value);
}
const percent = value =>
  value === null || value === undefined ? '–' : `${(100 * value).toFixed(1)}%`;
const change = ratio => ratio === null ? '–'
  : `${ratio >= 1 ? '+' : '−'}${Math.abs(100 * (ratio - 1)).toFixed(0)}%`;

function quantile(values, fraction) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const position = (sorted.length - 1) * fraction;
  const low = Math.floor(position);
  return sorted[low] + (sorted[Math.ceil(position)] - sorted[low]) * (position - low);
}
const median = values => quantile(values, .5);

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '–';
  if (seconds < 90) return trim(seconds) + 's';
  if (seconds < 5400) return trim(seconds / 60) + 'm';
  return trim(seconds / 3600) + 'h';
}
function size(bytes) {
  return bytes === null || bytes === undefined ? '–' : compact(bytes) + 'B';
}
const decimal = value => value === null || value === undefined ? '–' : trim(value);

const toDay = time => new Date(time).toISOString().slice(0, 10);
const fromDay = day => Date.parse(day + 'T00:00:00Z');
const monday = time => time - ((new Date(time).getUTCDay() + 6) % 7) * DAY;
const shortDay = day => new Date(fromDay(day)).toLocaleDateString('en-GB',
  {day: 'numeric', month: 'short', timeZone: 'UTC'});

function ticks(max) {
  if (!(max > 0)) return [0, 1];
  const rough = max / 4;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].find(size => size * power >= rough) * power;
  const result = [];
  for (let value = 0; value < max + step * .999; value += step) result.push(value);
  return result;
}

function beside(tooltip, x, width) {
  const side = x > width / 2;
  tooltip.style.left = side ? 'auto' : `${x + 12}px`;
  tooltip.style.right = side ? `${width - x + 12}px` : 'auto';
  tooltip.style.maxWidth = `${(side ? x : width - x) - 16}px`;
}

function draw(host, options) {
  const {spans, series, format, stacked = false, rolling = false, label, height = 200, markers = []} = options;
  const width = Math.max(host.clientWidth, 200);
  const numbers = list => list.filter(value => value !== null);
  const lines = spans.map((_, index) => {
    const values = series.map(item => item.values[index]);
    return stacked ? values.reduce((sum, value) => sum + (value ?? 0), 0) : Math.max(0, ...numbers(values));
  });
  // A single extreme day must not flatten the lines; its dot sits at the top.
  const dots = numbers(stacked ? [] : series.flatMap(item => item.dots ?? []));
  const highest = Math.max(...lines, quantile(dots, .95) ?? 0);
  const unit = format === duration && highest >= 90 ? (highest >= 5400 ? 3600 : 60) : 1;
  const scale = options.domain ?? ticks(highest / unit).map(mark => mark * unit);
  const top = 10, bottom = 24, right = 14;
  const left = 14 + 7 * Math.max(...scale.map(mark => format(mark).length));
  const max = scale.at(-1);
  const band = (width - left - right) / Math.max(spans.length, 1);
  const x = index => left + band * (index + .5);
  const y = value => top + (height - top - bottom) * (1 - value / max);
  const svg = shape('svg', {
    viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', tabindex: 0, 'aria-label': label,
  });
  const text = (content, attributes) => {
    const node = shape('text', attributes);
    node.textContent = content;
    svg.append(node);
  };
  for (const mark of scale) {
    svg.append(shape('line', {class: 'token-grid', x1: left, x2: width - right, y1: y(mark), y2: y(mark)}));
    text(format(mark), {x: left - 7, y: y(mark) + 4, 'text-anchor': 'end'});
  }
  const every = Math.max(1, Math.ceil(spans.length * 62 / (width - left - right)));
  spans.forEach((span, index) => {
    if ((spans.length - 1 - index) % every === 0) {
      const day = shortDay(span.key) + (span.partial ? '*' : '');
      text(day, {x: Math.min(x(index), width - day.length * 3.5), y: height - 6, 'text-anchor': 'middle'});
    }
  });
  const rule = shape('line', {class: 'token-rule', y1: top, y2: height - bottom, visibility: 'hidden'});
  const bases = spans.map(() => 0);
  for (const item of series) {
    if (stacked && rolling) {
      for (const run of segments(item.values.map((value, index) => lines[index] ? value ?? 0 : null))) {
        const upper = run.map(([index, value]) => `${x(index)},${y(bases[index] + value)}`);
        const lower = run.map(([index]) => `${x(index)},${y(bases[index])}`).reverse();
        svg.append(shape('polygon', {class: 'token-area', points: [...upper, ...lower].join(' '), style: `fill: ${item.color}`}));
      }
    } else if (stacked) {
      const bar = Math.min(24, band * .7);
      item.values.forEach((value, index) => {
        if (!value) return;
        const from = y(bases[index]), to = y(bases[index] + value);
        svg.append(shape('rect', {
          x: x(index) - bar / 2, width: bar, y: to,
          height: Math.max(from - to - (bases[index] ? 2 : 0), .5), style: `fill: ${item.color}`,
        }));
      });
    }
    if (stacked) {
      item.values.forEach((value, index) => { bases[index] += value ?? 0; });
      continue;
    }
    (item.dots ?? []).forEach((value, index) => {
      if (value === null) return;
      svg.append(shape('circle', {
        class: 'token-day', cx: x(index), cy: y(Math.min(value, max)), r: 2.5,
        style: value > max ? `stroke: ${item.color}; fill: none` : `fill: ${item.color}`,
      }));
    });
    for (const run of segments(item.values)) {
      svg.append(shape('polyline', {
        class: 'token-line', points: run.map(([index, value]) => `${x(index)},${y(value)}`).join(' '),
        style: `stroke: ${item.color}${item.dashed ? '; stroke-dasharray: 5 4' : ''}`,
      }));
    }
    if (rolling) continue;
    item.values.forEach((value, index) => {
      if (value === null) return;
      const small = item.samples && item.samples[index] < SMALL_SAMPLE;
      svg.append(shape('circle', {
        class: 'token-dot', cx: x(index), cy: y(value), r: 4,
        style: small ? `fill: var(--bg); stroke: ${item.color}` : `fill: ${item.color}`,
      }));
    });
  }
  const marked = new Set();
  for (const {place, day, line, tick} of markers) {
    // Changes on one day share a line.
    if (line && !marked.has(day)) svg.append(shape('line', {class: 'token-change', x1: left + band * place, x2: left + band * place, y1: top, y2: height - bottom}));
    if (line) marked.add(day);
    if (tick) svg.append(shape('line', {class: 'token-update', x1: left + band * day, x2: left + band * day, y1: height - bottom, y2: height - bottom + 5}));
  }
  svg.append(rule);
  const tooltip = element('div', {class: 'token-tooltip', hidden: true});
  let current = null;
  function show(index) {
    current = Math.min(Math.max(index, 0), spans.length - 1);
    rule.setAttribute('x1', x(current));
    rule.setAttribute('x2', x(current));
    rule.setAttribute('visibility', 'visible');
    tooltip.replaceChildren(
      element('div', {class: 'token-tooltip-title'},
        (options.period ?? (rolling ? '7 days to' : 'Week of')) + ' ' + shortDay(spans[current].key) + (spans[current].partial ? ' · partial' : '')),
      ...series.map(item => element('div', {class: 'token-tooltip-row'},
        element('i', {style: `background: ${item.color}`}),
        element('b', {}, format(item.values[current])), item.name,
        ...(item.dots ? [element('span', {class: 'token-tooltip-title'}, ` · day ${format(item.dots[current])}`)] : []))),
      ...(options.detail ? [options.detail(current)] : []).concat(options.notes?.(current) ?? [])
        .map(line => element('div', {class: 'token-tooltip-title'}, line)),
    );
    tooltip.hidden = false;
    beside(tooltip, x(current), width);
  }
  function hide() {
    rule.setAttribute('visibility', 'hidden');
    tooltip.hidden = true;
  }
  svg.addEventListener('pointermove', event => show(Math.floor(
    (event.clientX - svg.getBoundingClientRect().left - left) / band)));
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focus', () => show(current ?? spans.length - 1));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', event => {
    const offset = {ArrowLeft: -1, ArrowRight: 1}[event.key];
    if (offset) { event.preventDefault(); show((current ?? 0) + offset); }
  });
  host.replaceChildren(svg, tooltip);
}

// Consecutive known values as [index, value] pairs.
function segments(values) {
  const result = [];
  values.forEach((value, index) => {
    if (value === null) return;
    if (index === 0 || values[index - 1] === null) result.push([]);
    result.at(-1).push([index, value]);
  });
  return result;
}

function sparkline(values, width = 90, height = 26) {
  const known = values.filter(value => value !== null);
  const svg = shape('svg', {class: 'token-spark', viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': true});
  const max = Math.max(...known, 0) || 1;
  const x = index => 2 + (width - 4) * index / Math.max(values.length - 1, 1);
  const y = value => 2 + (height - 4) * (1 - value / max);
  for (const run of segments(values)) {
    svg.append(run.length > 1
      ? shape('polyline', {points: run.map(([index, value]) => `${x(index)},${y(value)}`).join(' ')})
      : shape('circle', {cx: x(run[0][0]), cy: y(run[0][1]), r: 1.5}));
  }
  return svg;
}

function dataTable(options) {
  const {spans, format} = options;
  const series = options.series.flatMap(item => item.dots
    ? [{...item, name: `${item.name}, 7 days`}, {name: `${item.name}, day`, values: item.dots}] : [item]);
  return element('div', {class: 'table-wrap', tabindex: 0, role: 'region', 'aria-label': `${options.label}, as a table`},
    element('table', {},
      element('thead', {}, element('tr', {}, element('th', {}, 'Period'),
        ...series.map(item => element('th', {}, item.name)),
        ...(options.detail ? [element('th', {}, 'Data')] : []))),
      element('tbody', {}, ...spans.map((span, index) => element('tr', {},
        element('td', {}, shortDay(span.key) + (span.partial ? ' (partial)' : '')),
        ...series.map(item => element('td', {class: 'numeric'}, format(item.values[index]))),
        ...(options.detail ? [element('td', {class: 'numeric'}, options.detail(index))] : []))))));
}

const color = name => `var(${name})`;

const whole = value => value.toLocaleString('en');

// Draw a chart with its legend and table, and return a function to redraw it.
function plotChart(figure, options) {
  if (!options.spans.length || !options.series.some(item => [...item.values, ...(item.dots ?? [])].some(value => value !== null))) {
    figure.replaceChildren(element('p', {class: 'note'}, 'No data to show.'));
    return null;
  }
  const plot = element('div', {class: 'token-plot'});
  const table = element('details', {}, element('summary', {}, 'Table'));
  table.addEventListener('toggle', () => {
    if (table.open && table.children.length === 1) table.append(dataTable(options));
  });
  figure.replaceChildren(
    ...(options.series.length > 1 ? [element('div', {class: 'token-legend'},
      ...options.series.map(item => element('span', {},
        element('i', {class: options.stacked ? '' : item.dashed ? 'line dashed' : 'line', style: `--key: ${item.color}`}),
        item.name)))] : []),
    plot, table);
  const redraw = () => draw(plot, {height: figure.dataset.height ? Number(figure.dataset.height) : undefined, ...options});
  redraw();
  return redraw;
}

// Call again whenever the width of an element changes.
function onResize(node, redraw) {
  let width = node.clientWidth;
  new ResizeObserver(() => {
    if (node.clientWidth === width) return;
    width = node.clientWidth;
    redraw();
  }).observe(node);
}

export {
  DAY, SMALL_SAMPLE, WINDOW, element, shape, compact, percent, change, quantile, median, duration,
  size, decimal, toDay, fromDay, monday, shortDay, beside, draw, sparkline, color, whole, plotChart,
  onResize,
};
