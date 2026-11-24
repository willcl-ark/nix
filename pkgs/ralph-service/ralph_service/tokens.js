import {
  DAY, SMALL_SAMPLE, WINDOW, element, shape, compact, percent, change, quantile, median, duration,
  size, decimal, toDay, fromDay, monday, shortDay, beside, draw, sparkline, color, whole, plotChart,
  onResize,
} from './charts.js';

const root = document.querySelector('#tokens');

const PAGE_SIZE = 15;
const COLORS = ['--accent', '--violet', '--amber', '--cyan'];

const months = root.dataset.months.split(' ').filter(Boolean);
const repositories = JSON.parse(root.dataset.repositories);
const changes = JSON.parse(root.dataset.changes);
const form = root.querySelector('form');
const loaded = new Map();
const charts = new Map();
let reviews = [];
let stageColors = new Map();
const tables = Object.fromEntries([
  ['reviews', 'value'], ['stages', 'total'], ['findings', 'alone'], ['efficiency', 'input'],
  ['models', 'requests'], ['time', 'median'], ['failures', 'lost'],
].map(([name, sort]) => [name, {sort, descending: true, page: 0, open: new Set()}]));
const SIZE_BANDS = [
  ['Under 10 KB', 0, 1e4, '--cyan'], ['10–40 KB', 1e4, 4e4, '--accent'], ['Over 40 KB', 4e4, Infinity, '--violet'],
];
const isClaude = run => run.model.startsWith('claude-');

const METRICS = {
  input: ['Input tokens', run => run.input],
  uncached: ['Uncached input tokens', run =>
    run.input === null || run.cached === null ? null : run.input - run.cached],
  output: ['Output tokens', run => run.output],
  total: ['Input + output tokens', run =>
    run.input === null || run.output === null ? null : run.input + run.output],
};

function add(runs, read) {
  let total = null;
  for (const run of runs) {
    const value = read(run);
    if (value !== null) total = (total ?? 0) + value;
  }
  return total;
}

// Ratios of totals, so that a review counts in proportion to its size.
function ratio(list, numerator, denominator) {
  const total = add(list, denominator);
  return total ? (add(list, numerator) ?? 0) / total : null;
}

async function load(month) {
  if (!loaded.has(month)) {
    loaded.set(month, fetch(root.dataset.root + month + '.json')
      .then(response => response.ok ? response.json() : Promise.reject(new Error(response.status)))
      .then(data => {
        const records = (fields, rows) => rows.map(row =>
          Object.fromEntries(fields.map((field, index) => [field, row[index]])));
        const list = records(data.review_fields, data.reviews);
        for (const review of list) { review.runs = []; review.stages = []; }
        for (const run of records(data.run_fields, data.runs)) {
          run.stage = data.names[run.stage];
          run.model = data.names[run.model];
          list[run.review].runs.push(run);
        }
        for (const stage of records(data.stage_fields, data.stages)) {
          for (const name of ['stage', 'model', 'status']) stage[name] = data.names[stage[name]];
          list[stage.review].stages.push(stage);
        }
        return list;
      })
      .catch(error => { loaded.delete(month); throw error; }));
  }
  return loaded.get(month);
}

function state() {
  return Object.fromEntries(new FormData(form));
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
      key: toDay(time), time,
      partial: view.group === 'week' && (time + 6 * DAY > end || time < first),
    });
  }
  return result;
}

// Summarize each point of a chart. A rolling line covers the seven days up to
// each day, with that day alone as a dot; weeks have a single value.
function trends(view, spans) {
  return (list, summarize, samples = group => group.length) => {
    const groups = new Map();
    for (const item of list) {
      const key = view.group === 'week' ? toDay(monday(fromDay(item.review.day))) : item.review.day;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    const at = time => groups.get(toDay(time)) ?? [];
    if (view.group === 'week') {
      const weeks = spans.map(span => at(span.time));
      return {values: weeks.map(summarize), samples: weeks.map(samples)};
    }
    const windows = spans.map(span =>
      Array.from({length: WINDOW}, (_, back) => at(span.time - back * DAY)).flat());
    const counts = windows.map(samples);
    return {
      values: windows.map((group, index) => counts[index] < SMALL_SAMPLE ? null : summarize(group)),
      dots: spans.map(span => at(span.time).length ? summarize(at(span.time)) : null),
      samples: counts,
    };
  };
}

// Changes to the stages or models in view are lines, other updates one tick a day.
function marks(view, spans) {
  if (!spans.length) return [];
  const step = (view.group === 'week' ? 7 : 1) * DAY;
  const result = [];
  const days = new Set();
  for (const change of changes) {
    const time = change.time * 1000;
    const place = (time - spans[0].time) / step;
    if (place < 0 || place >= spans.length) continue;
    const models = change.models.filter(([profile, stage, before, after]) =>
      (!view.profile || profile === view.profile) && (!view.stage || stage === view.stage)
        && (!view.model || [before[0], after[0]].includes(view.model)));
    const stages = change.stages.filter(stage => !view.stage || stage === view.stage);
    const line = models.length || stages.length;
    const first = !days.has(toDay(time));
    days.add(toDay(time));
    result.push({
      place, models, stages, line, tick: first,
      day: (fromDay(toDay(time)) + DAY / 2 - spans[0].time) / step,
    });
  }
  return result;
}
const setting = ([model, effort]) => effort ? `${model} ${effort}` : model;

function changeNotes(markers, index) {
  const here = markers.filter(marker => Math.floor(marker.place) === index);
  const list = (label, names) => {
    const unique = [...new Set(names)];
    return unique.length
      ? [`${label}: ${unique.slice(0, 3).join(', ')}${unique.length > 3 ? ` and ${unique.length - 3} more` : ''}`] : [];
  };
  return [
    ...list('New stage', here.flatMap(marker => marker.stages)),
    ...list('Model changed', here.flatMap(marker => marker.models.map(([, stage]) => stage))),
    ...(here.length ? [`${here.length} engine or config update${here.length > 1 ? 's' : ''}`] : []),
  ];
}

// Add the mode, markers and change notes of the current view.
function inView(options) {
  const view = state();
  const markers = marks({...view, ...options.only}, options.spans);
  return {...options, rolling: view.group !== 'week', markers, notes: index => changeNotes(markers, index)};
}

function chart(name, options) {
  const redraw = plotChart(root.querySelector(`[data-chart="${name}"]`), inView(options));
  if (redraw) charts.set(name, redraw);
}

// Show a figure for the period, how its later half compares and its trend.
function kpi(context, name, {list, read, format, detail, samples}) {
  const card = root.querySelector(`[data-kpi="${name}"]`);
  const now = context.now(list);
  const half = later => now.filter(item => (fromDay(item.review.day) >= context.middle) === later);
  const [before, after] = [half(false), half(true)];
  const enough = Math.min(...[before, after].map(samples ?? (group => group.length))) >= SMALL_SAMPLE;
  const [from, to] = [read(before), read(after)];
  let moved = '';
  if (enough && from !== null && to !== null) {
    if (format === percent) moved = `${to >= from ? '+' : '−'}${Math.abs(100 * (to - from)).toFixed(1)} pts later half`;
    else if (from) moved = `${change(to / from)} later half`;
  }
  card.querySelector('.kpi-value').textContent = format(read(now));
  card.querySelector('.kpi-detail').replaceChildren(
    element('div', {}, detail), element('div', {class: 'token-moved'}, moved, sparkline(context.trend(list, read, samples).values)));
}

function render() {
  const view = state();
  const [metricName, read] = METRICS[view.metric];
  const {start, end} = period(view);
  const warm = view.group === 'week' ? start : start - (WINDOW - 1) * DAY;
  const available = reviews.filter(review => {
    const time = fromDay(review.day);
    return time >= warm && time <= end && (!view.repository || review.repository === view.repository);
  });
  const matching = run => (!view.stage || run.stage === view.stage) && (!view.model || run.model === view.model);
  // Rolling lines also need the days before the period; figures and tables do not.
  const now = list => list.filter(item => fromDay(item.review.day) >= start);
  const spending = available.map(review => {
    const runs = review.runs.filter(matching);
    return {review, runs, stages: review.stages.filter(matching), value: runs.length ? add(runs, read) : null};
  }).filter(item => item.runs.length);
  // Reviews that ended early have no saved tier or profile to filter on.
  const selected = spending.filter(({review}) =>
    (!view.tier || review.tier === view.tier) && (!view.profile || review.profile === view.profile));
  const complete = selected.filter(item => item.review.outcome === 'complete' && item.value !== null);
  const spans = buckets(view, available.filter(review => fromDay(review.day) >= start));
  const days = now(complete).map(item => fromDay(item.review.day));
  const middle = days.length ? (Math.min(...days) + Math.max(...days) + DAY) / 2 : 0;
  const subject = view.stage ? `${view.stage} in each review` : 'per review';

  for (const panel of root.querySelectorAll('[data-tab]')) panel.hidden = panel.dataset.tab !== view.tab;
  form.elements.metric.closest('label').hidden = ['time', 'failures'].includes(view.tab);
  for (const node of root.querySelectorAll('[data-metric-name]')) node.textContent = metricName;
  for (const node of root.querySelectorAll('[data-subject]')) node.textContent = subject;
  const more = ['tier', 'profile', 'model'].filter(name => view[name]).length;
  root.querySelector('[data-more]').textContent = `More filters${more ? ` (${more} set)` : ''}`;
  charts.clear();
  const tab = {use: renderUse, findings: renderFindings, efficiency: renderEfficiency, time: renderTime, failures: renderFailures};
  tab[view.tab]({view, metricName, read, spending, selected, complete, spans, middle, now, trend: trends(view, spans)});
}

const count = (list, read) => add(list, read) ?? 0;
const runsOf = list => list.flatMap(item => item.runs);
const stagesOf = list => list.flatMap(item => item.stages);
const valuesOf = list => list.map(item => item.value).filter(value => value !== null);
const line = (name, tone, trend, extra = {}) => ({name, color: color(tone), ...trend, ...extra});
// Dots of a secondary line would only add noise.
const undotted = trend => ({...trend, dots: undefined});
const samplesIn = (series, unit) => index => `${whole(series.samples[index])} ${unit}`;

function byProvider(list, trend, read, samples) {
  return [['Claude', '--accent', isClaude], ['OpenAI', '--violet', run => !isClaude(run)]].map(([name, tone, matches]) => {
    const part = list.map(item => ({...item, runs: item.runs.filter(matches), stages: item.stages.filter(matches)}))
      .filter(item => item.runs.length || item.stages.length);
    return line(name, tone, trend(part, read, samples));
  }).filter(item => item.values.some(value => value !== null));
}

function renderUse(context) {
  const {metricName, read, spending, selected, complete, spans, now, trend} = context;
  const middleOf = list => median(valuesOf(list));
  const cacheRate = list => ratio(runsOf(list), run => run.cached, run => run.cache_input);
  const ended = outcomes => list => ratio(list, item => outcomes.includes(item.review.outcome) ? item.value ?? 0 : 0, item => item.value);
  const early = ['superseded', 'failed'];
  kpi(context, 'reviews', {list: complete, read: list => list.length, format: whole,
    detail: `${now(spending).filter(item => item.review.outcome !== 'complete').length} more superseded or failed`});
  kpi(context, 'median', {list: complete, read: middleOf, format: compact, detail: metricName});
  kpi(context, 'cache', {list: selected, read: cacheRate, format: percent, detail: 'Cached share of input tokens'});
  kpi(context, 'lost', {list: spending, read: ended(early), format: percent,
    detail: `Of ${compact(add(now(spending), item => item.value))} ${metricName.toLowerCase()}`});

  const medians = trend(complete, middleOf);
  chart('reviews', {
    label: metricName, spans, format: compact, detail: samplesIn(medians, 'reviews'),
    series: [
      line('Median', '--accent', medians),
      line('P90', '--violet', undotted(trend(complete, list => quantile(valuesOf(list), .9))), {dashed: true}),
    ],
  });
  const mean = choose => trend(complete, list => list.length ? count(runsOf(list), choose) / list.length : null);
  chart('cache-mix', {
    label: 'Mean input tokens', spans, format: compact, stacked: true, detail: samplesIn(medians, 'reviews'),
    series: [
      line('Uncached input', '--amber', undotted(mean(METRICS.uncached[1]))),
      line('Cached input', '--accent', undotted(mean(run => run.cached))),
    ],
  });
  const share = matches => undotted(trend(selected, list => ratio(runsOf(list), run => matches(run) ? read(run) ?? 0 : 0, read)));
  chart('share', {
    label: `Share of ${metricName.toLowerCase()} by stage`, spans, format: percent, stacked: true,
    domain: [0, .25, .5, .75, 1],
    series: [
      ...[...stageColors].map(([stage, name]) => line(stage, name, share(run => run.stage === stage))),
      line('Other stages', '--muted', share(run => !stageColors.has(run.stage))),
    ].filter(item => item.values.some(Boolean)),
  });
  chart('cache-rate', {
    label: 'Cached share of input tokens by provider', spans, format: percent, domain: [0, .25, .5, .75, 1],
    series: byProvider(selected, trend, cacheRate),
  });
  const reviewsEnded = trend(spending, list => list.filter(item => item.review.outcome !== 'complete').length);
  chart('lost', {
    label: `Share of ${metricName.toLowerCase()} in superseded or failed reviews`, spans, format: percent, stacked: true,
    detail: index => `${reviewsEnded.values[index] ?? 0} reviews`,
    series: [
      line('Superseded', '--amber', undotted(trend(spending, ended(['superseded'])))),
      line('Failed', '--red', undotted(trend(spending, ended(['failed'])))),
    ],
  });

  const all = add(now(selected), item => item.value);
  const perRun = list => list.map(item => add(item.runs, read)).filter(value => value !== null);
  const finished = list => list.filter(item => item.review.outcome === 'complete');
  breakdown(context, 'stages', 'stage', selected, {
    format: compact, unit: 'runs', label: name => `Median ${metricName.toLowerCase()} per ${name} run`,
    read: list => median(perRun(finished(list))), samples: list => finished(list).length,
    columns: [
      {key: 'runs', name: 'Runs'},
      {key: 'median', name: 'Median', format: compact},
      {key: 'p90', name: 'P90', format: compact},
      {key: 'total', name: 'Total', format: compact},
      {key: 'share', name: 'Share', format: percent},
      {key: 'cache', name: 'Cached', format: percent},
    ],
    row: list => {
      const values = perRun(finished(list));
      const total = add(runsOf(list), read);
      return {runs: values.length, median: median(values), p90: quantile(values, .9), total,
        share: all && total !== null ? total / all : null, cache: cacheRate(list)};
    },
  });
  renderReviewTable(now(selected), read);
}

// Saved stage runs and their requests, by stage, by model or by both.
function divide(list, key) {
  const result = new Map();
  const group = (item, record) => {
    const name = key(record);
    if (!result.has(name)) result.set(name, {name, stage: record.stage, model: record.model, items: new Map()});
    const all = result.get(name).items;
    if (!all.has(item.review)) all.set(item.review, {review: item.review, runs: [], stages: []});
    return all.get(item.review);
  };
  for (const item of list) {
    for (const run of item.runs) group(item, run).runs.push(run);
    for (const stage of item.stages) group(item, stage).stages.push(stage);
  }
  return [...result.values()].map(entry => ({...entry, items: [...entry.items.values()]}));
}

// A table by stage or model whose rows show their trend and open to a chart.
function breakdown(context, name, kind, list, options) {
  const {now, trend} = context;
  const other = kind === 'stage' ? 'model' : 'stage';
  const rows = divide(list, record => record[kind]).map(entry => {
    const parts = divide(now(entry.items), record => record[other]).filter(part => options.include?.(part.items) ?? true);
    const series = trend(entry.items, options.read, options.samples);
    const names = parts.map(part => part.name).sort();
    return {
      ...options.row(now(entry.items)), key: entry.name, name: entry.name, parts, series,
      other: names.length > 1 ? `${names.length} ${other}s` : names.join(', '),
      search: [entry.name, ...names].join(' '), include: now(entry.items).length && (options.include?.(now(entry.items)) ?? true),
    };
  }).filter(row => row.include);
  const title = text => text[0].toUpperCase() + text.slice(1);
  const columns = [
    {key: 'name', name: title(kind), text: true, render: row => element('button', {
      type: 'button', class: 'token-stage', 'aria-expanded': String(tables[name].open.has(row.key)),
      onclick: () => { tables[name].open.has(row.key) ? tables[name].open.delete(row.key) : tables[name].open.add(row.key); show(); },
    }, row.name)},
    {key: 'other', name: title(other) + 's', text: true},
    {name: 'Trend', render: row => sparkline(row.series.values)},
    ...options.columns,
  ];
  const expand = row => {
    const plot = element('div', {class: 'token-plot'});
    const chosen = context.view[kind] === row.name;
    const chartOptions = {
      label: options.label(row.name), spans: context.spans, format: options.format, only: {[kind]: row.name},
      detail: samplesIn(row.series, options.unit), series: [line(options.label(row.name), '--accent', row.series)],
    };
    charts.set(`${name}:${row.name}`, () => draw(plot, inView(chartOptions)));
    return element('div', {class: 'token-expanded'},
      element('p', {class: 'note'}, options.label(row.name)), plot,
      ...(row.parts.length > 1 ? [element('table', {},
        element('thead', {}, element('tr', {}, element('th', {}, title(other)), ...options.columns.map(item => element('th', {}, item.name)))),
        element('tbody', {}, ...row.parts.map(part => {
          const values = options.row(part.items);
          return element('tr', {}, element('td', {}, part.name),
            ...options.columns.map(item => element('td', {class: 'numeric'}, item.format ? item.format(values[item.key]) : values[item.key] ?? '–')));
        })))] : []),
      element('p', {}, element('button', {
        type: 'button', onclick: () => { form.elements[kind].value = chosen ? '' : row.name; update(); },
      }, chosen ? `Show every ${kind}` : `Filter the page to ${row.name}`)));
  };
  const show = () => {
    for (const key of [...charts.keys()]) if (key.startsWith(name + ':')) charts.delete(key);
    sortedTable(name, columns, rows, expand);
  };
  show();
}

function renderFindings(context) {
  const {view, metricName, read, complete, spans, now, trend} = context;
  const known = complete.filter(item => item.review.findings !== null);
  const filtered = view.stage || view.model;
  // A shared finding belongs to every stage that raised it, so a filtered page
  // counts what the selected stages raised and the whole page counts findings.
  const published = list => filtered ? count(stagesOf(list), stage => stage.accepted)
    : count(list, item => item.review.findings);
  const alone = list => count(stagesOf(list), stage => stage.sole);
  const perFinding = list => published(list) ? count(list, item => item.value) / published(list) : null;
  const found = list => list.length ? list.filter(item => published([item])).length / list.length : null;
  const aloneShare = list => published(list) ? alone(list) / published(list) : null;
  kpi(context, 'findings', {list: known, read: published, format: whole,
    detail: `${filtered ? 'Raised by this selection' : 'Published'} in ${whole(now(known).length)} reviews`});
  kpi(context, 'per-finding', {list: known, read: perFinding, format: compact, samples: published, detail: metricName});
  kpi(context, 'found', {list: known, read: found, format: percent, detail: 'Reviews with at least one finding'});
  kpi(context, 'alone', {list: known, read: aloneShare, format: percent, samples: published, detail: 'Findings only one stage raised'});
  const costs = trend(known, perFinding, published);
  const detail = samplesIn(costs, 'findings');
  chart('per-finding', {
    label: `${metricName} per published finding`, spans, format: compact, detail,
    series: [line('Per finding', '--accent', costs)],
  });
  const rate = trend(known, list => list.length ? published(list) / list.length : null);
  chart('finding-rate', {
    label: 'Published findings per completed review', spans, format: decimal, detail: samplesIn(rate, 'reviews'),
    series: [line('Findings per review', '--accent', rate)],
  });
  chart('finding-sources', {
    label: 'Findings raised by one stage or several', spans, format: percent, stacked: true, domain: [0, .25, .5, .75, 1], detail,
    series: [
      line('One stage', '--accent', undotted(trend(known, aloneShare, published))),
      line('Several stages', '--violet', undotted(trend(known, list => published(list) ? 1 - aloneShare(list) : null, published))),
    ],
  });
  const cost = (list, field) => ratio([{list}], () => count(runsOf(list), read), () => count(stagesOf(list), stage => stage[field]));
  breakdown(context, 'findings', 'stage', known, {
    format: compact, unit: 'found alone', label: name => `${metricName} per published finding only ${name} raised`,
    read: list => cost(list, 'sole'), samples: alone,
    include: list => count(stagesOf(list), stage => stage.candidates) > 0,
    columns: [
      {key: 'runs', name: 'Runs'},
      {key: 'candidates', name: 'Raised'},
      {key: 'accepted', name: 'Published'},
      {key: 'alone', name: 'Alone'},
      {key: 'rate', name: 'Kept', format: percent},
      {key: 'perAccepted', name: 'Per published', format: compact},
      {key: 'perAlone', name: 'Per alone', format: compact},
    ],
    row: list => {
      const records = stagesOf(list);
      const candidates = count(records, item => item.candidates);
      const accepted = count(records, item => item.accepted);
      return {runs: records.length, candidates, accepted, alone: alone(list), rate: candidates ? accepted / candidates : null,
        perAccepted: cost(list, 'accepted'), perAlone: cost(list, 'sole')};
    },
  });
}

function renderEfficiency(context) {
  const {metricName, complete, spans, now, trend} = context;
  const toolsPerTurn = list => ratio(stagesOf(list), stage => stage.tools, stage => stage.turns);
  const inputPerTurn = list => ratio(runsOf(list), run => run.input, run => run.requests - run.unknown);
  const turns = list => list.length ? count(stagesOf(list), stage => stage.turns) / list.length : null;
  const sized = complete.filter(item => item.review.size !== null);
  const medianSize = list => median(list.map(item => item.review.size));
  kpi(context, 'turns', {list: complete, read: turns, format: decimal, detail: 'Mean model turns'});
  kpi(context, 'tools', {list: complete, read: toolsPerTurn, format: decimal, detail: 'Higher means more batching'});
  kpi(context, 'turn-input', {list: complete, read: inputPerTurn, format: compact, detail: 'Mean input tokens'});
  kpi(context, 'size', {list: sized, read: medianSize, format: size,
    detail: `Median, ${now(complete).length - now(sized).length} reviews without a size`});
  const all = trend(complete, turns);
  const detail = samplesIn(all, 'reviews');
  const bands = SIZE_BANDS.map(([name, low, high, tone]) => line(name, tone,
    trend(sized.filter(item => item.review.size >= low && item.review.size < high), list => median(valuesOf(list)))))
    .filter(item => item.values.some(value => value !== null));
  chart('size-bands', {label: `Median ${metricName.toLowerCase()} by pull request size`, spans, format: compact, series: bands,
    detail: index => bands.map(item => item.samples[index]).join(' / ') + ' reviews'});
  chart('tools-turn', {label: 'Tool calls per model turn', spans, format: decimal, detail, series: byProvider(complete, trend, toolsPerTurn)});
  chart('input-turn', {label: 'Mean input tokens per request', spans, format: compact, detail, series: byProvider(complete, trend, inputPerTurn)});
  chart('turns', {label: 'Mean model turns per review', spans, format: decimal, detail, series: [line('Turns per review', '--accent', all)]});
  const sizes = trend(sized, medianSize);
  chart('size', {label: 'Median pull request size', spans, format: size, detail: samplesIn(sizes, 'reviews'), series: [line('Median', '--accent', sizes)]});
  scatter('size-tokens', metricName, now(sized).filter(item => item.review.size > 0 && item.value > 0));
  breakdown(context, 'efficiency', 'stage', complete, {
    format: decimal, unit: 'runs', label: name => `Tool calls per model turn in ${name}`,
    read: toolsPerTurn, samples: list => stagesOf(list).length, include: list => stagesOf(list).length > 0,
    columns: [
      {key: 'runs', name: 'Runs'},
      {key: 'turns', name: 'Turns/run', format: decimal},
      {key: 'tools', name: 'Tools/run', format: decimal},
      {key: 'perTurn', name: 'Tools/turn', format: decimal},
      {key: 'inputTurn', name: 'Input/request', format: compact},
      {key: 'input', name: 'Input', format: compact},
    ],
    row: list => {
      const records = stagesOf(list);
      const mean = field => records.length ? count(records, item => item[field]) / records.length : null;
      return {runs: records.length, turns: mean('turns'), tools: mean('tools'), perTurn: toolsPerTurn(list),
        inputTurn: inputPerTurn(list), input: add(runsOf(list), run => run.input)};
    },
  });
}

// Elapsed time from the first matching request to the last completed one.
function elapsed(item) {
  const ends = item.runs.map(run => run.end).filter(end => end !== null);
  return ends.length ? Math.max(...ends) - Math.min(...item.runs.map(run => run.start)) : null;
}
const times = list => list.map(elapsed).filter(value => value !== null);

function renderTime(context) {
  const {complete, spans, trend} = context;
  const latency = list => ratio(runsOf(list), run => run.seconds, run => run.timed);
  const requests = list => count(runsOf(list), run => run.timed);
  const medianTime = list => median(times(list));
  const slow = list => quantile(times(list), .9);
  const modelTime = list => list.length ? count(runsOf(list), run => run.seconds) / list.length : null;
  kpi(context, 'wall', {list: complete, read: medianTime, format: duration, detail: 'Median, first request to last'});
  kpi(context, 'wall-p90', {list: complete, read: slow, format: duration, detail: '90th percentile'});
  kpi(context, 'model-time', {list: complete, read: modelTime, format: duration, detail: 'Mean, added across stages'});
  kpi(context, 'latency', {list: complete, read: latency, format: duration, detail: 'Mean completed request'});
  const medians = trend(complete, medianTime);
  const detail = samplesIn(medians, 'reviews');
  chart('wall', {label: 'Elapsed time', spans, format: duration, detail, series: [
    line('Median', '--accent', medians), line('P90', '--violet', undotted(trend(complete, slow)), {dashed: true}),
  ]});
  chart('model-time', {label: 'Mean model time', spans, format: duration, detail, series: [line('Model time', '--accent', trend(complete, modelTime))]});
  chart('request-time', {label: 'Mean request time by provider', spans, format: duration, detail, series: byProvider(complete, trend, latency)});
  breakdown(context, 'models', 'model', complete, {
    format: duration, unit: 'requests', label: name => `Mean request time of ${name}`, read: latency, samples: requests,
    include: list => runsOf(list).length > 0,
    columns: [{key: 'requests', name: 'Requests', format: whole}, {key: 'latency', name: 'Mean request', format: duration}],
    row: list => ({requests: requests(list), latency: latency(list)}),
  });
  breakdown(context, 'time', 'stage', complete, {
    format: duration, unit: 'runs', label: name => `Median elapsed time of ${name}`, read: medianTime,
    include: list => runsOf(list).length > 0,
    columns: [
      {key: 'runs', name: 'Runs'},
      {key: 'median', name: 'Median', format: duration},
      {key: 'p90', name: 'P90', format: duration},
      {key: 'requests', name: 'Requests', format: whole},
      {key: 'latency', name: 'Mean request', format: duration},
    ],
    row: list => ({runs: times(list).length, median: medianTime(list), p90: slow(list), requests: requests(list), latency: latency(list)}),
  });
}

function renderFailures(context) {
  const {selected, spans, middle, now, trend} = context;
  const saved = selected.filter(item => item.stages.length);
  const refused = stage => stage.refused;
  const failed = stage => Number(stage.status === 'failed' && !stage.refused);
  const invalid = stage => Number(stage.status === 'invalid' && !stage.refused);
  const lost = stage => Number(stage.status !== 'completed');
  const share = read => list => ratio(stagesOf(list), read, () => 1);
  const affected = list => list.length ? list.filter(item => item.stages.some(lost)).length / list.length : null;
  const runs = list => stagesOf(list).length;
  const all = stagesOf(now(saved));
  kpi(context, 'stage-runs', {list: saved, read: runs, format: whole, detail: `In ${whole(now(saved).length)} reviews with saved results`});
  kpi(context, 'lost-runs', {list: saved, read: share(lost), format: percent, detail: `${count(all, lost)} failed, invalid or refused`});
  kpi(context, 'refused', {list: saved, read: share(refused), format: percent, detail: `${count(all, refused)} refused by the provider`});
  kpi(context, 'affected', {list: saved, read: affected, format: percent, detail: 'Lost at least one stage'});
  const part = read => trend(saved, share(read), runs);
  chart('losses', {label: 'Share of stage runs lost', spans, format: percent, stacked: true, detail: samplesIn(part(lost), 'stage runs'),
    series: [
      line('Failed', '--red', undotted(part(failed))),
      line('Invalid output', '--amber', undotted(part(invalid))),
      line('Refused', '--violet', undotted(part(refused))),
    ].filter(item => item.values.some(Boolean))});
  const reviewsAffected = trend(saved, affected);
  chart('affected', {label: 'Share of reviews that lost a stage', spans, format: percent, detail: samplesIn(reviewsAffected, 'reviews'),
    series: [line('Reviews', '--accent', reviewsAffected)]});
  chart('loss-provider', {label: 'Share of stage runs lost by provider', spans, format: percent,
    series: byProvider(saved, trend, share(lost), runs)});
  const half = (list, later) => {
    const records = stagesOf(list.filter(item => (fromDay(item.review.day) >= middle) === later));
    return records.length >= SMALL_SAMPLE ? ratio(records, lost, () => 1) : null;
  };
  breakdown(context, 'failures', 'stage', saved, {
    format: percent, unit: 'runs', label: name => `Share of ${name} runs lost`, read: share(lost), samples: runs,
    include: list => stagesOf(list).length > 0,
    columns: [
      {key: 'runs', name: 'Runs'},
      {key: 'failed', name: 'Failed'},
      {key: 'invalid', name: 'Invalid'},
      {key: 'refused', name: 'Refused'},
      {key: 'lost', name: 'Lost'},
      {key: 'rate', name: 'Share', format: percent},
      {key: 'earlier', name: 'Earlier', format: percent},
      {key: 'later', name: 'Later', format: percent},
    ],
    row: list => {
      const records = stagesOf(list);
      return {runs: records.length, failed: count(records, failed), invalid: count(records, invalid),
        refused: count(records, refused), lost: count(records, lost), rate: share(lost)(list),
        earlier: half(list, false), later: half(list, true)};
    },
  });
}

function scatter(name, metricName, items) {
  const figure = root.querySelector(`[data-chart="${name}"]`);
  if (!items.length) {
    figure.replaceChildren(element('p', {class: 'note'}, 'No data for these filters.'));
    return;
  }
  const points = [...items].sort((a, b) => a.review.size - b.review.size);
  const days = points.map(item => fromDay(item.review.day));
  const middle = (Math.min(...days) + Math.max(...days) + DAY) / 2;
  const tones = [['Earlier half', '--violet'], ['Later half', '--accent']];
  const tone = item => tones[Number(fromDay(item.review.day) >= middle)][1];
  const plot = element('div', {class: 'token-plot'});
  figure.replaceChildren(element('div', {class: 'token-legend'},
    ...tones.map(([label, key]) => element('span', {}, element('i', {style: `--key: ${color(key)}`}), label))), plot);
  const redraw = () => drawScatter(plot, points, tone, metricName);
  charts.set(name, redraw);
  redraw();
}

function drawScatter(host, points, tone, metricName) {
  const width = Math.max(host.clientWidth, 200), height = 300;
  const left = 56, right = 14, top = 10, bottom = 24;
  const decades = values => {
    const low = Math.floor(Math.log10(Math.min(...values))), high = Math.ceil(Math.log10(Math.max(...values)));
    return Array.from({length: Math.max(high - low, 1) + 1}, (_, index) => low + index);
  };
  const across = decades(points.map(item => item.review.size)), up = decades(points.map(item => item.value));
  const place = (value, powers) => (Math.log10(value) - powers[0]) / (powers.at(-1) - powers[0]);
  const x = value => left + (width - left - right) * place(value, across);
  const y = value => top + (height - top - bottom) * (1 - place(value, up));
  const svg = shape('svg', {viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', tabindex: 0,
    'aria-label': `${metricName} against pull request size, one point per review`});
  const text = (content, attributes) => {
    const node = shape('text', attributes);
    node.textContent = content;
    svg.append(node);
  };
  for (const power of up) {
    const at = y(10 ** power);
    svg.append(shape('line', {class: 'token-grid', x1: left, x2: width - right, y1: at, y2: at}));
    text(compact(10 ** power), {x: left - 7, y: at + 4, 'text-anchor': 'end'});
  }
  for (const power of across) text(size(10 ** power), {x: x(10 ** power), y: height - 6, 'text-anchor': power === across.at(-1) ? 'end' : 'middle'});
  for (const item of points) {
    svg.append(shape('circle', {class: 'token-point', cx: x(item.review.size), cy: y(item.value), r: 3, style: `fill: ${color(tone(item))}`}));
  }
  const ring = shape('circle', {class: 'token-ring', r: 6, visibility: 'hidden'});
  svg.append(ring);
  const tooltip = element('div', {class: 'token-tooltip', hidden: true});
  let current = null;
  const show = index => {
    current = index;
    const item = points[index], at = [x(item.review.size), y(item.value)];
    const repository = repositories[item.review.repository];
    ring.setAttribute('cx', at[0]);
    ring.setAttribute('cy', at[1]);
    ring.setAttribute('visibility', 'visible');
    tooltip.replaceChildren(
      element('div', {class: 'token-tooltip-title'}, `${repository?.label ?? item.review.repository}#${item.review.number} · ${shortDay(item.review.day)}`),
      element('div', {}, element('b', {}, compact(item.value)), metricName),
      element('div', {}, element('b', {}, size(item.review.size)), 'pull request'));
    tooltip.hidden = false;
    beside(tooltip, at[0], width);
  };
  const hide = () => { ring.setAttribute('visibility', 'hidden'); tooltip.hidden = true; };
  svg.addEventListener('pointermove', event => {
    const box = svg.getBoundingClientRect();
    let best = null, nearest = 24 ** 2;
    points.forEach((item, index) => {
      const distance = (x(item.review.size) - event.clientX + box.left) ** 2 + (y(item.value) - event.clientY + box.top) ** 2;
      if (distance < nearest) { best = index; nearest = distance; }
    });
    if (best === null) hide(); else show(best);
  });
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focus', () => show(current ?? 0));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', event => {
    const offset = {ArrowLeft: -1, ArrowRight: 1}[event.key];
    if (!offset) return;
    event.preventDefault();
    show(Math.min(Math.max((current ?? 0) + offset, 0), points.length - 1));
  });
  host.replaceChildren(svg, tooltip);
}

function sortedTable(name, columns, rows, expand) {
  const again = () => sortedTable(name, columns, rows, expand);
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
  const header = columns.map(item => item.key ? element('th', {
    'aria-sort': item.key === options.sort ? (options.descending ? 'descending' : 'ascending') : null,
  }, element('button', {
    type: 'button', class: 'sort-button',
    onclick: () => {
      options.descending = options.sort === item.key ? !options.descending : !item.text;
      options.sort = item.key;
      options.page = 0;
      again();
    },
  }, item.name)) : element('th', {}, item.name));
  host.querySelector('thead').replaceChildren(element('tr', {}, ...header));
  host.querySelector('tbody').replaceChildren(...matches.slice(offset, offset + PAGE_SIZE).flatMap(row => [
    element('tr', {}, ...columns.map(item => element('td', {class: item.text ? null : 'numeric'},
      item.render ? item.render(row) : item.format ? item.format(row[item.key]) : row[item.key] ?? '–'))),
    ...(expand && options.open.has(row.key) ? [element('tr', {class: 'token-open'},
      element('td', {colspan: columns.length}, expand(row)))] : []),
  ]));
  if (expand) {
    for (const [key, redraw] of charts) if (key.startsWith(name + ':')) redraw();
  }
  const [previous, next] = host.querySelectorAll('.pagination button');
  host.querySelector('[role="status"]').textContent = matches.length
    ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, matches.length)} of ${matches.length}` : 'No matching rows';
  previous.disabled = options.page === 0;
  next.disabled = offset + PAGE_SIZE >= matches.length;
  previous.onclick = () => { options.page--; again(); };
  next.onclick = () => { options.page++; again(); };
  host.querySelector('input').oninput = () => { options.page = 0; again(); };
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
      size: review.size,
      findings: review.outcome === 'complete' ? review.findings : null,
      minutes: elapsed({runs}),
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
    {key: 'findings', name: 'Findings'},
    {key: 'size', name: 'PR size', format: size},
    {key: 'minutes', name: 'Time', format: duration},
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
  const query = new URLSearchParams(Object.entries(state()).filter(([name, value]) => value !== defaults[name]));
  history.replaceState(null, '', query.size ? '#' + query : location.pathname + location.search);
}

function renderChanges() {
  const days = new Map();
  for (const item of changes) {
    const day = toDay(item.time * 1000);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(item);
  }
  root.querySelector('[data-changes]').replaceChildren(...[...days].reverse().map(([day, list]) => {
    return element('tr', {}, element('td', {}, shortDay(day)),
      element('td', {class: 'numeric'}, String(list.length)),
      element('td', {}, ...list.flatMap(item => item.stages).map(stage => element('div', {}, `${stage}: new stage`)),
        ...list.flatMap(item => item.models).map(([profile, stage, before, after]) =>
        element('div', {}, `${stage} (${profile}): ${setting(before)} → ${setting(after)}`))));
  }));
}

let request = 0;
async function update() {
  const mine = ++request;
  const status = root.querySelector('[data-load-status]');
  const view = state();
  const start = period(view).start - (view.group === 'week' ? 0 : (WINDOW - 1) * DAY);
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

const wanted = new URLSearchParams(location.hash.slice(1));
const defaults = state();
for (const [name, value] of wanted) {
  const control = form.elements[name];
  if (!control) continue;
  if (name !== 'tab') control.dataset.wanted = value;
  if ([...(control.options ?? control)].some(option => option.value === value)) control.value = value;
}
form.addEventListener('submit', event => event.preventDefault());
form.addEventListener('change', () => {
  for (const options of Object.values(tables)) options.page = 0;
  update();
});
if (['tier', 'profile', 'model'].some(name => wanted.get(name))) root.querySelector('.token-more').open = true;
renderChanges();
onResize(root, () => { for (const redraw of charts.values()) redraw(); });
root.querySelector('[data-content]').hidden = false;
update();
