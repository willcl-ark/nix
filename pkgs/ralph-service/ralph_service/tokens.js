(() => {
const root = document.querySelector('#tokens');
if (!root) return;

const DAY = 86400000;
const SMALL_SAMPLE = 5;
const PAGE_SIZE = 50;
const STAGES_SHOWN = 12;
const COLORS = ['--accent', '--violet', '--amber', '--cyan'];
const SVG = 'http://www.w3.org/2000/svg';

const months = root.dataset.months.split(' ').filter(Boolean);
const repositories = JSON.parse(root.dataset.repositories);
const form = root.querySelector('form');
const loaded = new Map();
const charts = new Map();
let reviews = [];
let stageColors = new Map();
let allStages = false;
const tables = {
  reviews: {sort: 'value', descending: true, page: 0},
  stages: {sort: 'total', descending: true, page: 0},
};

const METRICS = {
  input: ['Input tokens', run => run.input],
  uncached: ['Uncached input tokens', run =>
    run.input === null || run.cached === null ? null : run.input - run.cached],
  output: ['Output tokens', run => run.output],
  total: ['Input + output tokens', run =>
    run.input === null || run.output === null ? null : run.input + run.output],
};

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

function add(runs, read) {
  let total = null;
  for (const run of runs) {
    const value = read(run);
    if (value !== null) total = (total ?? 0) + value;
  }
  return total;
}

const toDay = time => new Date(time).toISOString().slice(0, 10);
const fromDay = day => Date.parse(day + 'T00:00:00Z');
const monday = time => time - ((new Date(time).getUTCDay() + 6) % 7) * DAY;
const shortDay = day => new Date(fromDay(day)).toLocaleDateString('en-GB',
  {day: 'numeric', month: 'short', timeZone: 'UTC'});

async function load(month) {
  if (!loaded.has(month)) {
    loaded.set(month, fetch(root.dataset.root + month + '.json')
      .then(response => response.ok ? response.json() : Promise.reject(new Error(response.status)))
      .then(data => {
        const records = (fields, rows) => rows.map(row =>
          Object.fromEntries(fields.map((field, index) => [field, row[index]])));
        const list = records(data.review_fields, data.reviews);
        for (const review of list) review.runs = [];
        for (const run of records(data.run_fields, data.runs)) {
          run.stage = data.names[run.stage];
          run.model = data.names[run.model];
          list[run.review].runs.push(run);
        }
        return list;
      })
      .catch(error => { loaded.delete(month); throw error; }));
  }
  return loaded.get(month);
}

function state() {
  return Object.fromEntries([...form.elements].filter(control => control.name)
    .map(control => [control.name, control.value]));
}

function period(view) {
  const end = fromDay(toDay(Date.now()));
  let start = view.range ? end - (Number(view.range) - 1) * DAY : fromDay(months[0] + '-01');
  if (view.range && view.group === 'week') start = monday(start);
  return {start, end};
}

function buckets(view, list) {
  if (!list.length) return [];
  const step = view.group === 'week' ? 7 * DAY : DAY;
  const align = view.group === 'week' ? monday : time => time;
  const days = list.map(review => fromDay(review.day));
  const first = Math.min(...days), last = Math.max(...days);
  const {start, end} = period(view);
  const result = [];
  for (let time = align(Math.max(start, first)); time <= last; time += step) {
    result.push({
      key: toDay(time),
      partial: view.group === 'week' && (time + 6 * DAY > end || time < first),
    });
  }
  return result;
}
const bucketOf = (view, review) =>
  view.group === 'week' ? toDay(monday(fromDay(review.day))) : review.day;

function grouped(view, list) {
  const result = new Map();
  for (const item of list) {
    const key = bucketOf(view, item.review);
    if (!result.has(key)) result.set(key, []);
    result.get(key).push(item);
  }
  return result;
}

// Compare the earlier and later halves of the days that have data, so a
// comparison is available for every range, including all of the history.
function halves(items, middle) {
  const earlier = items.filter(item => fromDay(item.review.day) < middle).map(item => item.value);
  const later = items.filter(item => fromDay(item.review.day) >= middle).map(item => item.value);
  const enough = Math.min(earlier.length, later.length) >= SMALL_SAMPLE;
  const before = median(earlier);
  return {
    earlier: before,
    later: median(later),
    ratio: enough && before ? median(later) / before : null,
  };
}

function ticks(max) {
  if (!(max > 0)) return [0, 1];
  const rough = max / 4;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].find(size => size * power >= rough) * power;
  const result = [];
  for (let value = 0; value < max + step * .999; value += step) result.push(value);
  return result;
}

function draw(host, options) {
  const {spans, series, format, stacked = false, mini = false, label} = options;
  const width = Math.max(host.clientWidth, 200);
  const height = mini ? 120 : 240;
  const all = spans.map((_, index) => {
    const values = series.map(item => item.values[index]);
    return stacked ? values.reduce((sum, value) => sum + (value ?? 0), 0)
      : Math.max(0, ...values.filter(value => value !== null));
  });
  const marks = options.domain ?? ticks(Math.max(...all, 0));
  const shown = mini ? [marks[0], marks.at(-1)] : marks;
  const top = 10, bottom = 24, right = 14;
  const left = 14 + 7 * Math.max(...shown.map(mark => format(mark).length));
  const max = marks.at(-1);
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
  for (const mark of shown) {
    svg.append(shape('line', {class: 'token-grid', x1: left, x2: width - right, y1: y(mark), y2: y(mark)}));
    text(format(mark), {x: left - 7, y: y(mark) + 4, 'text-anchor': 'end'});
  }
  const every = Math.max(1, Math.ceil(spans.length * 62 / (width - left - right)));
  spans.forEach((span, index) => {
    if ((spans.length - 1 - index) % every === 0) {
      text(shortDay(span.key) + (span.partial ? '*' : ''), {x: x(index), y: height - 6, 'text-anchor': 'middle'});
    }
  });
  const rule = shape('line', {class: 'token-rule', y1: top, y2: height - bottom, visibility: 'hidden'});
  svg.append(rule);
  if (stacked) {
    const bar = Math.min(24, band * .7);
    spans.forEach((_, index) => {
      let base = 0;
      for (const item of series) {
        const value = item.values[index];
        if (!value) continue;
        const from = y(base), to = y(base + value);
        svg.append(shape('rect', {
          x: x(index) - bar / 2, width: bar, y: to,
          height: Math.max(from - to - (base ? 2 : 0), .5), style: `fill: ${item.color}`,
        }));
        base += value;
      }
    });
  } else {
    for (const item of series) {
      let segment = [];
      const flush = () => {
        if (segment.length > 1) {
          svg.append(shape('polyline', {
            class: 'token-line', points: segment.join(' '),
            style: `stroke: ${item.color}${item.dashed ? '; stroke-dasharray: 5 4' : ''}`,
          }));
        }
        segment = [];
      };
      item.values.forEach((value, index) => {
        if (value === null) flush();
        else segment.push(`${x(index)},${y(value)}`);
      });
      flush();
      item.values.forEach((value, index) => {
        if (value === null || (spans.length > 45 && item.values[index - 1] != null)) return;
        const small = item.samples && item.samples[index] < SMALL_SAMPLE;
        svg.append(shape('circle', {
          class: 'token-dot', cx: x(index), cy: y(value), r: mini ? 3 : 4,
          style: small ? `fill: var(--bg); stroke: ${item.color}` : `fill: ${item.color}`,
        }));
      });
    }
  }
  const tooltip = element('div', {class: 'token-tooltip', hidden: true});
  let current = null;
  function show(index) {
    current = Math.min(Math.max(index, 0), spans.length - 1);
    rule.setAttribute('x1', x(current));
    rule.setAttribute('x2', x(current));
    rule.setAttribute('visibility', 'visible');
    tooltip.replaceChildren(
      element('div', {class: 'token-tooltip-title'},
        shortDay(spans[current].key) + (spans[current].partial ? ' · partial week' : '')),
      ...series.map(item => element('div', {class: 'token-tooltip-row'},
        element('i', {style: `background: ${item.color}`}),
        element('b', {}, format(item.values[current])), item.name)),
      ...(options.detail ? [element('div', {class: 'token-tooltip-title'}, options.detail(current))] : []),
    );
    tooltip.hidden = false;
    const side = x(current) > width / 2;
    tooltip.style.left = side ? 'auto' : `${x(current) + 12}px`;
    tooltip.style.right = side ? `${width - x(current) + 12}px` : 'auto';
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

function dataTable(options) {
  const {spans, series, format} = options;
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

function chart(name, options) {
  const figure = root.querySelector(`[data-chart="${name}"]`);
  if (!options.spans.length || !options.series.some(item => item.values.some(value => value !== null))) {
    charts.delete(name);
    figure.replaceChildren(element('p', {class: 'note'}, 'No data for these filters.'));
    return;
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
  charts.set(name, () => draw(plot, options));
  draw(plot, options);
}

function kpi(name, value, detail) {
  const card = root.querySelector(`[data-kpi="${name}"]`);
  card.querySelector('.kpi-value').textContent = value;
  card.querySelector('.kpi-detail').textContent = detail;
}

const color = name => `var(${name})`;

function render() {
  const view = state();
  const [metricName, read] = METRICS[view.metric];
  const {start, end} = period(view);
  const inRange = reviews.filter(review => {
    const time = fromDay(review.day);
    return time >= start && time <= end && (!view.repository || review.repository === view.repository);
  });
  const matching = run => (!view.stage || run.stage === view.stage) && (!view.model || run.model === view.model);
  const items = list => list.map(review => {
    const runs = review.runs.filter(matching);
    return {review, runs, value: runs.length ? add(runs, read) : null};
  }).filter(item => item.runs.length);

  // Reviews that ended early have no saved tier or profile to filter on.
  const spending = items(inRange);
  const selected = spending.filter(({review}) =>
    (!view.tier || review.tier === view.tier) && (!view.profile || review.profile === view.profile));
  const complete = selected.filter(item => item.review.outcome === 'complete' && item.value !== null);
  const spans = buckets(view, inRange);
  const days = complete.map(item => fromDay(item.review.day));
  const middle = days.length ? (Math.min(...days) + Math.max(...days) + DAY) / 2 : 0;
  const subject = view.stage ? `${view.stage} in each review` : 'per review';

  const spread = halves(complete, middle);
  const input = add(selected.flatMap(item => item.runs), run => run.cache_input);
  const cached = add(selected.flatMap(item => item.runs), run => run.cached);
  const spent = add(spending, item => item.value) ?? 0;
  const early = spending.filter(item => item.review.outcome !== 'complete');
  const lost = add(early, item => item.value) ?? 0;
  kpi('reviews', complete.length.toLocaleString('en'), `${early.length} more superseded or failed`);
  kpi('median', compact(median(complete.map(item => item.value))),
    `${metricName}, ${subject}. Later half ${compact(spread.later)}, earlier ${compact(spread.earlier)} (${change(spread.ratio)})`);
  kpi('cache', input ? percent(cached / input) : '–', 'Cached share of input tokens');
  kpi('lost', spent ? percent(lost / spent) : '–', `${compact(lost)} of ${compact(spent)} ${metricName.toLowerCase()}`);

  for (const node of root.querySelectorAll('[data-metric-name]')) node.textContent = metricName;
  for (const node of root.querySelectorAll('[data-subject]')) node.textContent = subject;

  const completeBy = grouped(view, complete);
  const selectedBy = grouped(view, selected);
  const spendingBy = grouped(view, spending);
  const at = (groups, span) => groups.get(span.key) ?? [];
  const samples = spans.map(span => at(completeBy, span).length);
  const values = fraction => spans.map(span => quantile(at(completeBy, span).map(item => item.value), fraction));
  chart('reviews', {
    label: `${metricName} ${subject}`, spans, format: compact,
    detail: index => `${samples[index]} reviews`,
    series: [
      {name: 'Median', color: color('--accent'), values: values(.5), samples},
      {name: 'P90', color: color('--violet'), values: values(.9), samples, dashed: true},
    ],
  });
  const mean = choose => spans.map(span => {
    const group = at(completeBy, span);
    return group.length ? (add(group.flatMap(item => item.runs), choose) ?? 0) / group.length : null;
  });
  chart('cache-mix', {
    label: `Mean input tokens ${subject}`, spans, format: compact, stacked: true,
    detail: index => `${samples[index]} reviews`,
    series: [
      {name: 'Uncached input', color: color('--amber'), values: mean(METRICS.uncached[1])},
      {name: 'Cached input', color: color('--accent'), values: mean(run => run.cached)},
    ],
  });
  const share = (span, matches) => {
    const runs = at(selectedBy, span).flatMap(item => item.runs);
    const total = add(runs, read);
    return total ? (add(runs.filter(matches), read) ?? 0) / total : null;
  };
  chart('share', {
    label: `Share of ${metricName.toLowerCase()} by stage`, spans, format: percent, stacked: true,
    domain: [0, .25, .5, .75, 1],
    series: [
      ...[...stageColors].map(([stage, name]) => ({
        name: stage, color: color(name), values: spans.map(span => share(span, run => run.stage === stage)),
      })),
      {name: 'Other stages', color: color('--muted'), values: spans.map(span => share(span, run => !stageColors.has(run.stage)))},
    ].filter(item => item.values.some(Boolean)),
  });
  const rate = matches => spans.map(span => {
    const runs = at(selectedBy, span).flatMap(item => item.runs).filter(matches);
    const known = add(runs, run => run.cache_input);
    return known ? add(runs, run => run.cached) / known : null;
  });
  chart('cache-rate', {
    label: 'Cached share of input tokens by provider', spans, format: percent, domain: [0, .25, .5, .75, 1],
    series: [
      {name: 'Claude', color: color('--accent'), values: rate(run => run.model.startsWith('claude-'))},
      {name: 'OpenAI', color: color('--violet'), values: rate(run => !run.model.startsWith('claude-'))},
    ].filter(item => item.values.some(value => value !== null)),
  });
  const ended = outcome => spans.map(span => {
    const group = at(spendingBy, span);
    const total = add(group, item => item.value);
    return total ? (add(group.filter(item => item.review.outcome === outcome), item => item.value) ?? 0) / total : null;
  });
  chart('lost', {
    label: `Share of ${metricName.toLowerCase()} in superseded or failed reviews`, spans, format: percent, stacked: true,
    detail: index => `${at(spendingBy, spans[index]).filter(item => item.review.outcome !== 'complete').length} reviews`,
    series: [
      {name: 'Superseded', color: color('--amber'), values: ended('superseded')},
      {name: 'Failed', color: color('--red'), values: ended('failed')},
    ],
  });
  renderStages(view, spans, complete, read, middle, metricName);
  renderStageTable(selected, complete, read, middle);
  renderReviewTable(selected, read);
}

function stageRuns(complete, read, key) {
  const result = new Map();
  for (const {review, runs} of complete) {
    const parts = new Map();
    for (const run of runs) {
      const name = key(run);
      if (!parts.has(name)) parts.set(name, []);
      parts.get(name).push(run);
    }
    for (const [name, list] of parts) {
      const value = add(list, read);
      if (value === null) continue;
      if (!result.has(name)) result.set(name, []);
      result.get(name).push({review, value});
    }
  }
  return result;
}

function renderStages(view, spans, complete, read, middle, metricName) {
  const host = root.querySelector('[data-stage-charts]');
  for (const name of [...charts.keys()]) if (name.startsWith('stage:')) charts.delete(name);
  const ranked = [...stageRuns(complete, read, run => run.stage)].map(([stage, runs]) =>
    ({stage, runs, total: add(runs, run => run.value)})).sort((a, b) => b.total - a.total);
  const shown = allStages ? ranked : ranked.slice(0, STAGES_SHOWN);
  const figures = shown.map(({stage, runs}) => {
    const by = grouped(view, runs);
    const samples = spans.map(span => (by.get(span.key) ?? []).length);
    const plot = element('div', {class: 'token-plot'});
    const spread = halves(runs, middle);
    const options = {
      label: `Median ${metricName.toLowerCase()} per ${stage} run`, spans, format: compact, mini: true,
      detail: index => `${samples[index]} runs`,
      series: [{
        name: 'Median', color: color('--accent'), samples,
        values: spans.map(span => median((by.get(span.key) ?? []).map(item => item.value))),
      }],
    };
    charts.set(`stage:${stage}`, () => draw(plot, options));
    return element('figure', {class: 'chart'},
      element('figcaption', {}, element('button', {
        type: 'button', class: 'token-stage', 'aria-pressed': String(view.stage === stage),
        title: view.stage === stage ? 'Show every stage' : `Filter the page to ${stage}`,
        onclick: () => { form.elements.stage.value = view.stage === stage ? '' : stage; update(); },
      }, stage), element('span', {class: 'note'}, `${compact(median(runs.map(run => run.value)))} · ${change(spread.ratio)}`)),
      plot);
  });
  host.replaceChildren(...(figures.length ? figures : [element('p', {class: 'note'}, 'No data for these filters.')]));
  for (const [name, redraw] of charts) if (name.startsWith('stage:')) redraw();
  const more = root.querySelector('[data-all-stages]');
  more.hidden = ranked.length <= STAGES_SHOWN;
  more.textContent = allStages ? `Show the largest ${STAGES_SHOWN}` : `Show all ${ranked.length} stages`;
}

function sortedTable(name, columns, rows) {
  const host = root.querySelector(`[data-table="${name}"]`);
  const options = tables[name];
  const query = host.querySelector('input').value.trim().toLocaleLowerCase();
  const column = columns.find(item => item.key === options.sort);
  const direction = options.descending ? -1 : 1;
  const matches = rows.filter(row => row.search.toLocaleLowerCase().includes(query)).sort((a, b) => {
    const x = a[column.key], y = b[column.key];
    if (x === null || y === null) return (x === null) - (y === null);
    return direction * (typeof x === 'string' ? x.localeCompare(y) : x - y);
  });
  options.page = Math.min(options.page, Math.max(0, Math.ceil(matches.length / PAGE_SIZE) - 1));
  const offset = options.page * PAGE_SIZE;
  const header = columns.map(item => element('th', {
    'aria-sort': item.key === options.sort ? (options.descending ? 'descending' : 'ascending') : null,
  }, element('button', {
    type: 'button', class: 'sort-button',
    onclick: () => {
      options.descending = options.sort === item.key ? !options.descending : !item.text;
      options.sort = item.key;
      options.page = 0;
      sortedTable(name, columns, rows);
    },
  }, item.name)));
  host.querySelector('thead').replaceChildren(element('tr', {}, ...header));
  host.querySelector('tbody').replaceChildren(...matches.slice(offset, offset + PAGE_SIZE).map(row =>
    element('tr', {}, ...columns.map(item => element('td', {class: item.text ? null : 'numeric'},
      item.render ? item.render(row) : item.format ? item.format(row[item.key]) : row[item.key] ?? '–')))));
  const [previous, next] = host.querySelectorAll('.pagination button');
  host.querySelector('[role="status"]').textContent = matches.length
    ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, matches.length)} of ${matches.length}` : 'No matching rows';
  previous.disabled = options.page === 0;
  next.disabled = offset + PAGE_SIZE >= matches.length;
  previous.onclick = () => { options.page--; sortedTable(name, columns, rows); };
  next.onclick = () => { options.page++; sortedTable(name, columns, rows); };
  host.querySelector('input').oninput = () => { options.page = 0; sortedTable(name, columns, rows); };
}

function renderStageTable(selected, complete, read, middle) {
  const key = run => `${run.stage}\n${run.model}`;
  const runs = stageRuns(complete, read, key);
  const spent = new Map();
  for (const run of selected.flatMap(item => item.runs)) {
    if (!spent.has(key(run))) spent.set(key(run), []);
    spent.get(key(run)).push(run);
  }
  const all = add(selected, item => item.value);
  const rows = [...spent].map(([name, list]) => {
    const [stage, model] = name.split('\n');
    const values = runs.get(name) ?? [];
    const total = add(list, read);
    const known = add(list, run => run.cache_input);
    const spread = halves(values, middle);
    return {
      stage, model, search: name, runs: values.length,
      median: median(values.map(item => item.value)),
      p90: quantile(values.map(item => item.value), .9),
      total, share: all && total !== null ? total / all : null,
      cache: known ? add(list, run => run.cached) / known : null,
      change: spread.ratio,
    };
  });
  sortedTable('stages', [
    {key: 'stage', name: 'Stage', text: true},
    {key: 'model', name: 'Model', text: true},
    {key: 'runs', name: 'Runs'},
    {key: 'median', name: 'Median', format: compact},
    {key: 'p90', name: 'P90', format: compact},
    {key: 'total', name: 'Total', format: compact},
    {key: 'share', name: 'Share', format: percent},
    {key: 'cache', name: 'Cached', format: percent},
    {key: 'change', name: 'Later vs earlier', format: change},
  ], rows);
}

function renderReviewTable(selected, read) {
  const rows = selected.map(({review, runs, value}) => {
    const repository = repositories[review.repository] ?? {name: review.repository};
    return {
      review, repository, value,
      pr: review.number,
      title: review.title ?? '',
      day: review.day,
      kind: [review.tier ?? 'no tier', review.profile ?? 'no profile'].join(' · '),
      outcome: review.outcome,
      input: add(runs, run => run.input),
      uncached: add(runs, METRICS.uncached[1]),
      output: add(runs, run => run.output),
      requests: add(runs, run => run.requests),
      search: `${repository.name}#${review.number} ${review.title ?? ''} ${review.tier ?? ''} ${review.profile ?? ''} ${review.outcome}`,
    };
  });
  const selectedMetric = METRICS[form.elements.metric.value][0];
  sortedTable('reviews', [
    {key: 'pr', name: 'PR', text: true, render: ({review, repository}) => repository.pull
      ? element('a', {href: repository.pull + review.number}, `${repository.label}#${review.number}`)
      : `${repository.label ?? review.repository}#${review.number}`},
    {key: 'title', name: 'Review', text: true, render: ({review, title}) => {
      const label = title || `Job ${review.job}`;
      return element('span', {class: 'review-title', title: label}, review.report && root.dataset.reports
        ? element('a', {href: root.dataset.reports + '/' + review.report.split('/').map(encodeURIComponent).join('/')}, label)
        : label);
    }},
    {key: 'day', name: 'Started', text: true},
    {key: 'kind', name: 'Tier · profile', text: true},
    {key: 'outcome', name: 'Outcome', text: true},
    {key: 'value', name: selectedMetric === 'Input tokens' ? 'Input' : selectedMetric, format: compact},
    ...(selectedMetric === 'Input tokens' ? [] : [{key: 'input', name: 'Input', format: compact}]),
    ...(selectedMetric === METRICS.uncached[0] ? [] : [{key: 'uncached', name: 'Uncached', format: compact}]),
    ...(selectedMetric === METRICS.output[0] ? [] : [{key: 'output', name: 'Output', format: compact}]),
    {key: 'requests', name: 'Requests', format: value => value === null ? '–' : value.toLocaleString('en')},
  ], rows);
}

function setOptions(name, values) {
  const control = form.elements[name];
  if (!control || control.type === 'hidden') return;
  const current = control.value || control.dataset.wanted || '';
  control.replaceChildren(element('option', {value: ''}, 'All'),
    ...values.map(value => element('option', {value}, name === 'repository' ? repositories[value]?.name ?? value : value)));
  control.value = values.includes(current) ? current : '';
}

function save() {
  const view = state();
  const query = new URLSearchParams(Object.entries(view).filter(([name, value]) =>
    value !== form.elements[name].dataset.default));
  history.replaceState(null, '', query.size ? '#' + query : location.pathname + location.search);
}

let request = 0;
async function update() {
  const mine = ++request;
  const status = root.querySelector('[data-load-status]');
  const {start} = period(state());
  const wanted = months.filter(month => month >= toDay(start).slice(0, 7));
  root.classList.add('loading');
  try {
    const lists = await Promise.all(wanted.map(load));
    if (mine !== request) return;
    reviews = lists.flat();
    if (root.dataset.repository) reviews = reviews.filter(review => review.repository === root.dataset.repository);
    status.hidden = true;
  } catch {
    if (mine === request) {
      status.textContent = 'Token data could not be loaded. Reload the page to try again.';
      status.hidden = false;
    }
    return;
  } finally {
    if (mine === request) root.classList.remove('loading');
  }
  const runs = reviews.flatMap(review => review.runs);
  const unique = values => [...new Set(values.filter(Boolean))].sort();
  setOptions('repository', unique(reviews.map(review => review.repository)));
  setOptions('tier', unique(reviews.map(review => review.tier)));
  setOptions('profile', unique(reviews.map(review => review.profile)));
  setOptions('stage', unique(runs.map(run => run.stage)));
  setOptions('model', unique(runs.map(run => run.model)));
  // Colours stay with their stage for as long as the page is open.
  if (!stageColors.size) {
    const totals = new Map();
    for (const run of runs) totals.set(run.stage, (totals.get(run.stage) ?? 0) + (run.input ?? 0));
    stageColors = new Map([...totals].sort((a, b) => b[1] - a[1]).slice(0, COLORS.length)
      .map(([stage], index) => [stage, COLORS[index]]));
  }
  save();
  render();
}

for (const control of form.elements) control.dataset.default = control.value;
for (const [name, value] of new URLSearchParams(location.hash.slice(1))) {
  const control = form.elements[name];
  if (!control) continue;
  control.dataset.wanted = value;
  if ([...control.options].some(option => option.value === value)) control.value = value;
}
form.addEventListener('submit', event => event.preventDefault());
form.addEventListener('change', () => {
  for (const options of Object.values(tables)) options.page = 0;
  update();
});
root.querySelector('[data-all-stages]').addEventListener('click', () => { allStages = !allStages; render(); });
let width = 0;
new ResizeObserver(() => {
  if (root.clientWidth === width) return;
  width = root.clientWidth;
  for (const redraw of charts.values()) redraw();
}).observe(root);
root.querySelector('[data-content]').hidden = false;
update();
})();
