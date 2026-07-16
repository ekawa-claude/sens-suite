/* RawAccel Studio frontend.
 * Curve math is a faithful port of rawaccel v1.6.1 common/accel-*.hpp —
 * the chart must match what the driver actually does. */
"use strict";

/* ================= math: ports of accel-*.hpp ================= */

const clampLerp = (a, b, t) => {
  const x = a + t * (b - a);
  return a < b ? Math.min(Math.max(x, Math.min(a, b)), b) : Math.max(Math.min(x, a), b);
};

function classicCurve(p, gain) {
  const power = p.exponentClassic, offset = p.inputOffset, accel = p.acceleration;
  const baseFn = (x, accelRaised) => accelRaised * Math.pow(x - offset, power) / x;

  if (!gain) {
    let cap = Infinity, sign = 1, accelRaised;
    if (p.capMode === "in_out") {
      cap = p.capY - 1;
      if (cap < 0) { cap = -cap; sign = -1; }
      const a = Math.pow(p.capX * cap * Math.pow(p.capX - offset, -power), 1 / (power - 1));
      accelRaised = Math.pow(a, power - 1);
    } else if (p.capMode === "input") {
      accelRaised = Math.pow(accel, power - 1);
      if (p.capX > 0) cap = baseFn(p.capX, accelRaised);
    } else {
      accelRaised = Math.pow(accel, power - 1);
      if (p.capY > 0) {
        cap = p.capY - 1;
        if (cap < 0) { cap = -cap; sign = -1; }
      }
    }
    return x => x <= offset ? 1 : sign * Math.min(baseFn(x, accelRaised), cap) + 1;
  }

  const gainFn = (x, a) => power * Math.pow(a * (x - offset), power - 1);
  const gainInverse = y => (accel * offset + Math.pow(y / power, 1 / (power - 1))) / accel;
  let accelRaised, capX = Infinity, capY = Infinity, constant = 0, sign = 1;
  if (p.capMode === "in_out") {
    capX = p.capX; capY = p.capY - 1;
    if (capY < 0) { capY = -capY; sign = -1; }
    const a = -Math.pow(capY / power, 1 / (power - 1)) / (offset - capX);
    accelRaised = Math.pow(a, power - 1);
    constant = (baseFn(capX, accelRaised) - capY) * capX;
  } else if (p.capMode === "input") {
    accelRaised = Math.pow(accel, power - 1);
    if (p.capX > 0) {
      capX = p.capX;
      capY = gainFn(capX, accel);
      constant = (baseFn(capX, accelRaised) - capY) * capX;
    }
  } else {
    accelRaised = Math.pow(accel, power - 1);
    if (p.capY > 0) {
      capY = p.capY - 1;
      if (capY === 0) { capX = 0; }
      else {
        if (capY < 0) { capY = -capY; sign = -1; }
        capX = gainInverse(capY);
        constant = (baseFn(capX, accelRaised) - capY) * capX;
      }
    }
  }
  return x => {
    if (x <= offset) return 1;
    const out = x < capX ? baseFn(x, accelRaised) : constant / x + capY;
    return sign * out + 1;
  };
}

function powerCurve(p, gain) {
  const n = p.exponentPower;
  const gainInverse = (g, scale) => Math.pow(g / (n + 1), 1 / n) / scale;
  let scale, offX, offY, constant;

  if (p.capMode !== "in_out") scale = p.scale;
  else if (gain) scale = Math.pow(p.capY / (n + 1), 1 / n) / p.capX;
  else {
    offX = 0; offY = 0; constant = 0;
    scale = Math.pow(p.capY, 1 / n) / p.capX; // scale_from_sens_point with C=0
  }
  if (offX === undefined) {
    offX = gainInverse(p.outputOffset, scale);
    offY = p.outputOffset;
    constant = offX * offY * n / (n + 1);
  }
  const baseFn = x => x <= offX ? offY : Math.pow(scale * x, n) + constant / x;

  if (!gain) {
    let cap = Infinity;
    if (p.capMode === "in_out") cap = p.capY;
    else if (p.capMode === "input") { if (p.capX > 0) cap = baseFn(p.capX); }
    else if (p.capY > 0) cap = p.capY;
    return x => Math.min(baseFn(x), cap);
  }

  let capX = Infinity, capY = Infinity, constantB = 0;
  if (p.capMode === "in_out") { capX = p.capX; capY = p.capY; }
  else if (p.capMode === "input") {
    if (p.capX > 0) {
      if (p.capX <= offX) return () => offY;
      capX = p.capX;
      capY = (n + 1) * Math.pow(capX * scale, n);
    }
  } else if (p.capY > 0) {
    capX = gainInverse(p.capY, scale);
    capY = p.capY;
  }
  if (isFinite(capX)) constantB = (baseFn(capX) - capY) * capX;
  return x => x < capX ? baseFn(x) : capY + constantB / x;
}

function naturalCurve(p, gain) {
  const offset = p.inputOffset, limit = p.limit - 1;
  const accel = p.decayRate / Math.abs(limit);
  if (!gain) {
    return x => {
      if (x <= offset) return 1;
      const offsetX = offset - x;
      const decay = Math.exp(accel * offsetX);
      return limit * (1 - (offset - decay * offsetX) / x) + 1;
    };
  }
  const constant = -limit / accel;
  return x => {
    if (x <= offset) return 1;
    const offsetX = offset - x;
    const decay = Math.exp(accel * offsetX);
    return (limit * (decay / accel - offsetX) + constant) / x + 1;
  };
}

function jumpCurve(p, gain) {
  const stepX = p.capX, stepY = p.capY - 1;
  const rateInverse = p.smooth * stepX;
  const smoothRate = rateInverse < 1 ? 0 : (2 * Math.PI) / rateInverse;
  const decay = x => Math.exp(smoothRate * (stepX - x));
  const antideriv = x => stepY * (x + Math.log(1 + decay(x)) / smoothRate);
  if (!gain) {
    if (smoothRate === 0) return x => x < stepX ? 1 : 1 + stepY;
    return x => stepY / (1 + decay(x)) + 1;
  }
  if (smoothRate === 0) return x => x <= 0 ? 1 : (x < stepX ? 1 : 1 + stepY * (x - stepX) / x);
  const C = -antideriv(0);
  return x => x <= 0 ? 1 : 1 + (antideriv(x) + C) / x;
}

function motivityCurve(p, gain) {
  const accel = Math.exp(p.growthRate);
  const motivity = 2 * Math.log(p.motivity);
  const midpoint = Math.log(p.midpoint);
  const constant = -motivity / 2;
  const legacy = x => Math.exp(motivity / (Math.exp(accel * (midpoint - Math.log(x))) + 1) + constant);
  if (!gain) return legacy;

  // gain variant: driver integrates the legacy sigmoid over a log-spaced LUT
  const start = -3, stop = 9, num = 8;
  const size = (stop - start) * num + 1;
  const xs = [];
  for (let e = 0; e < stop - start; e++) {
    const expScale = Math.pow(2, e + start) / num;
    for (let i = 0; i < num; i++) xs.push((i + num) * expScale);
  }
  xs.push(Math.pow(2, stop));
  const data = new Array(size);
  let sum = 0, a = 0;
  xs.forEach((b, i) => {
    const interval = (b - a) / 2;
    for (let k = 1; k <= 2; k++) sum += legacy(a + k * interval) * interval;
    a = b;
    data[i] = sum;
  });
  const xStart = Math.pow(2, start);
  return x => {
    if (x <= 0) return data[0] / xStart;
    const e = Math.min(Math.floor(Math.log2(x)), stop - 1);
    if (e >= start) {
      const idxF = num * ((e - start) + (x * Math.pow(2, -e) - 1));
      const idx = Math.min(Math.floor(idxF), size - 2);
      return clampLerp(data[idx], data[idx + 1], idxF - idx) / x;
    }
    return data[0] / xStart;
  };
}

function lutCurve(points, velocity) {
  const pts = points.slice().sort((a, b) => a.x - b.x);
  return x => {
    if (!pts.length) return 1;
    if (x <= 0) return 0;
    if (x <= pts[0].x) return velocity ? pts[0].y / pts[0].x : pts[0].y;
    const last = pts[pts.length - 1];
    if (x >= last.x) return velocity ? last.y / x : last.y;
    let lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (pts[mid].x <= x) lo = mid; else hi = mid;
    }
    const a = pts[lo], b = pts[hi];
    const y = clampLerp(a.y, b.y, (x - a.x) / (b.x - a.x));
    return velocity ? y / x : y;
  };
}

function makeCurve(P) {
  switch (P.mode) {
    case "classic": return classicCurve(P, P.gain);
    case "jump": return jumpCurve(P, P.gain);
    case "natural": return naturalCurve(P, P.gain);
    case "motivity": return motivityCurve(P, P.gain);
    case "power": return powerCurve(P, P.gain);
    case "lut": return lutCurve(P.data, P.gain);
    default: return () => 1;
  }
}
const sensAt = (P, x) => P.sens * makeCurve(P)(x);

/* ================= param metadata ================= */

const MODES = [
  { id: "noaccel", label: "Без ускорения", hint: "Драйвер только применяет множитель чувствительности, кривой нет." },
  { id: "classic", label: "Classic", hint: "Классика (как в Quake/повышение по степени): чувствительность растёт от скорости по кривой rate·v^(n−1)." },
  { id: "jump", label: "Jump", hint: "Две чувствительности: ниже порога одна, выше — другая. «Плавность» размывает ступеньку." },
  { id: "natural", label: "Natural", hint: "Плавный рост, который сам затухает и упирается в предел. Простой и предсказуемый." },
  { id: "motivity", label: "Motivity", hint: "S-образная кривая (твоя текущая): медленно = ниже сенса, быстро = выше, симметрично вокруг середины." },
  { id: "power", label: "Power", hint: "Степенная кривая от нуля (стиль CS:GO m_customaccel)." },
  { id: "lut", label: "Своя кривая", hint: "Рисуешь кривую точками прямо на графике — драйвер исполнит её как есть." },
];

const CAP_OPTS = [["output", "по выходу"], ["input", "по входу"], ["in_out", "вход→выход"]];

// slider ranges: [key, label, min, max, step, tooltip]
const MODE_PARAMS = {
  classic: [
    ["acceleration", "Ускорение", 0.0005, 0.2, 0.0005, "Насколько быстро растёт чувствительность со скоростью."],
    ["exponentClassic", "Степень", 2, 5, 0.05, "Форма кривой: 2 = линейный рост, больше = резче на высоких скоростях."],
    ["inputOffset", "Порог входа", 0, 40, 0.5, "До этой скорости (counts/ms) ускорение не работает."],
    ["capMode", "Кап", null, null, null, "Как ограничивать кривую сверху."],
    ["capX", "Кап: вход", 0, 200, 1, "Скорость, где кривая упирается в потолок (0 = выкл)."],
    ["capY", "Кап: множитель", 0, 5, 0.05, "Максимальный множитель чувствительности (0 = выкл)."],
  ],
  jump: [
    ["capX", "Порог (скорость)", 0, 100, 0.5, "Скорость, на которой происходит скачок."],
    ["capY", "Множитель после", 0, 5, 0.05, "Чувствительность после порога (до порога = 1)."],
    ["smooth", "Плавность", 0, 1, 0.01, "0 = резкая ступенька, 1 = плавный сигмоид."],
  ],
  natural: [
    ["decayRate", "Затухание", 0.005, 1, 0.005, "Насколько быстро кривая выходит на предел."],
    ["limit", "Предел", 0.1, 5, 0.05, "Максимальный множитель, к которому стремится кривая."],
    ["inputOffset", "Порог входа", 0, 40, 0.5, "До этой скорости ускорение не работает."],
  ],
  motivity: [
    ["growthRate", "Крутизна", 0.1, 5, 0.01, "Насколько резко кривая переходит с нижней полки на верхнюю."],
    ["motivity", "Диапазон", 1.01, 4, 0.01, "Верх кривой = это число, низ = 1/число. 1.25 → от 0.8 до 1.25."],
    ["midpoint", "Середина", 0.1, 50, 0.1, "Скорость (counts/ms), где кривая проходит ровно через 1."],
  ],
  power: [
    ["scale", "Масштаб", 0.001, 1, 0.001, "Множитель скорости до возведения в степень."],
    ["exponentPower", "Степень", 0.01, 1, 0.01, "Показатель степени кривой."],
    ["outputOffset", "Старт", 0, 2, 0.01, "Чувствительность на нулевой скорости."],
    ["capMode", "Кап", null, null, null, "Как ограничивать кривую сверху."],
    ["capX", "Кап: вход", 0, 200, 1, "Скорость потолка (0 = выкл)."],
    ["capY", "Кап: множитель", 0, 5, 0.05, "Максимальный множитель (0 = выкл)."],
  ],
  lut: [],
  noaccel: [],
};

const COMMON_PARAMS = [
  ["sens", "Множитель сенса", 0.01, 5, 0.01, "Общий множитель чувствительности, применяется поверх кривой."],
  ["yx", "Y/X", 0.1, 3, 0.01, "Вертикальная чувствительность относительно горизонтальной."],
  ["rot", "Поворот, °", -45, 45, 0.5, "Поворачивает ось движений мыши (компенсация хвата)."],
  ["snap", "Прилипание, °", 0, 45, 0.5, "Движения почти по горизонтали/вертикали прилипают к оси."],
  ["speedCap", "Кап скорости", 0, 1000, 5, "Обрезает входную скорость сверху (0 = выкл)."],
];

/* ================= state ================= */

let baseSettings = null;   // full settings.json from server (template for apply)
let applied = null;        // params snapshot currently in the driver
let cur = null;            // params being edited
let currentProfile = null;
let xMax = 60;

const $ = id => document.getElementById(id);

function extractParams(settings) {
  const prof = settings.profiles[0];
  const wp = prof["Whole or horizontal accel parameters"];
  return {
    mode: wp.mode,
    gain: !!wp["Gain / Velocity"],
    acceleration: wp.acceleration,
    exponentClassic: wp.exponentClassic,
    inputOffset: wp.inputOffset,
    outputOffset: wp.outputOffset,
    decayRate: wp.decayRate,
    growthRate: wp.growthRate,
    motivity: wp.motivity,
    limit: wp.limit,
    midpoint: wp.midpoint,
    scale: wp.scale,
    exponentPower: wp.exponentPower,
    smooth: wp.smooth,
    capMode: wp["Cap mode"],
    capX: wp["Cap / Jump"].x,
    capY: wp["Cap / Jump"].y,
    data: lutPointsFromFlat(wp.data || []),
    sens: prof["Sensitivity multiplier"],
    yx: prof["Y/X sensitivity ratio (vertical sens multiplier)"],
    rot: prof["Degrees of rotation"],
    snap: prof["Degrees of angle snapping"],
    speedCap: prof["Input Speed Cap"],
  };
}

const lutPointsFromFlat = flat => {
  const pts = [];
  for (let i = 0; i + 1 < flat.length; i += 2) pts.push({ x: flat[i], y: flat[i + 1] });
  return pts;
};

function buildSettings(P) {
  const s = JSON.parse(JSON.stringify(baseSettings));
  const prof = s.profiles[0];
  const wp = prof["Whole or horizontal accel parameters"];
  Object.assign(wp, {
    mode: P.mode,
    "Gain / Velocity": P.gain,
    inputOffset: P.inputOffset, outputOffset: P.outputOffset,
    acceleration: P.acceleration, decayRate: P.decayRate, growthRate: P.growthRate,
    motivity: P.motivity, limit: P.limit, exponentClassic: P.exponentClassic, scale: P.scale,
    exponentPower: P.exponentPower, smooth: P.smooth,
    "Cap mode": P.capMode,
  });
  wp["Cap / Jump"] = { x: P.capX, y: P.capY };
  wp.data = P.mode === "lut" ? P.data.slice().sort((a, b) => a.x - b.x).flatMap(p => [p.x, p.y]) : [];
  prof["Sensitivity multiplier"] = P.sens;
  prof["Y/X sensitivity ratio (vertical sens multiplier)"] = P.yx;
  prof["Degrees of rotation"] = P.rot;
  prof["Degrees of angle snapping"] = P.snap;
  prof["Input Speed Cap"] = P.speedCap;
  return s;
}

const isDirty = () => JSON.stringify(cur) !== JSON.stringify(applied);

/* ================= UI: params panel ================= */

function renderModeGrid() {
  const grid = $("modeGrid");
  grid.innerHTML = "";
  MODES.forEach(m => {
    const b = document.createElement("button");
    b.className = "mode-btn" + (cur.mode === m.id ? " on" : "");
    b.textContent = m.label;
    b.onclick = () => {
      if (cur.mode !== "lut" && m.id === "lut" && !cur.data.length) seedLutFromCurve(cur);
      cur.mode = m.id;
      renderAll();
    };
    grid.appendChild(b);
  });
  $("modeHint").textContent = MODES.find(m => m.id === cur.mode)?.hint || "";
  $("gainRow").style.display = cur.mode === "noaccel" ? "none" : "";
  $("gainToggle").checked = cur.gain;
}

function paramRow([key, label, min, max, step, tip]) {
  const row = document.createElement("div");
  row.className = "param-row";
  if (key === "capMode") {
    row.innerHTML = `<div class="param-top"><label title="${tip}">${label}</label></div>`;
    const sel = document.createElement("select");
    CAP_OPTS.forEach(([v, l]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = l; o.selected = cur.capMode === v;
      sel.appendChild(o);
    });
    sel.onchange = () => { cur.capMode = sel.value; onParamsChanged(); };
    row.appendChild(sel);
    return row;
  }
  const top = document.createElement("div");
  top.className = "param-top";
  top.innerHTML = `<label title="${tip}">${label}</label>`;
  const num = document.createElement("input");
  num.type = "number"; num.step = step; num.value = cur[key];
  const range = document.createElement("input");
  range.type = "range"; range.min = min; range.max = max; range.step = step; range.value = cur[key];
  num.oninput = () => { const v = parseFloat(num.value); if (isFinite(v)) { cur[key] = v; range.value = v; onParamsChanged(); } };
  range.oninput = () => { cur[key] = parseFloat(range.value); num.value = range.value; onParamsChanged(); };
  top.appendChild(num);
  row.append(top, range);
  return row;
}

function renderParams() {
  const holder = $("paramRows");
  holder.innerHTML = "";
  MODE_PARAMS[cur.mode].forEach(def => holder.appendChild(paramRow(def)));
  $("lutTools").hidden = cur.mode !== "lut";
  $("paramsCard").style.display = (cur.mode === "noaccel") ? "none" : "";
  const common = $("commonRows");
  common.innerHTML = "";
  COMMON_PARAMS.forEach(def => common.appendChild(paramRow(def)));
}

function onParamsChanged() {
  $("dirtyDot").hidden = !isDirty();
  drawChart();
}

function renderAll() {
  renderModeGrid();
  renderParams();
  onParamsChanged();
}

/* ================= chart ================= */

const canvas = $("chart");
const ctx = canvas.getContext("2d");
const M = { l: 58, r: 18, t: 18, b: 42 };
let hoverX = null;
let dragIdx = -1;

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function chartRect() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  return { w, h, pw: w - M.l - M.r, ph: h - M.t - M.b };
}

function computeYRange() {
  const fns = [P => sensAt(P, Math.max(0.05, 0)), null];
  let min = Infinity, max = -Infinity;
  const consider = P => {
    for (let i = 0; i <= 200; i++) {
      const x = 0.05 + (xMax - 0.05) * i / 200;
      const y = sensAt(P, x);
      if (isFinite(y)) { min = Math.min(min, y); max = Math.max(max, y); }
    }
    if (P.mode === "lut") P.data.forEach(pt => { if (pt.x <= xMax) { min = Math.min(min, ptDisplayY(P, pt)); max = Math.max(max, ptDisplayY(P, pt)); } });
  };
  consider(cur);
  if (isDirty()) consider(applied);
  if (!isFinite(min)) { min = 0.5; max = 1.5; }
  min = Math.min(min, 1); max = Math.max(max, 1);
  const pad = Math.max((max - min) * 0.12, 0.03);
  return { yMin: min - pad, yMax: max + pad };
}

const ptDisplayY = (P, pt) => P.sens * (P.gain ? pt.y / Math.max(pt.x, 1e-9) : pt.y);

let yScale = { yMin: 0.5, yMax: 1.5 };
const X = x => M.l + (x / xMax) * chartRect().pw;
const Y = y => M.t + (1 - (y - yScale.yMin) / (yScale.yMax - yScale.yMin)) * chartRect().ph;
const invX = px => (px - M.l) / chartRect().pw * xMax;
const invY = py => yScale.yMin + (1 - (py - M.t) / chartRect().ph) * (yScale.yMax - yScale.yMin);

function niceTicks(min, max, target) {
  const span = max - min;
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= target) || mag * 10;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(v);
  return ticks;
}

function drawCurveLine(P, color, width, dashed) {
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dashed ? [6, 5] : []);
  const { pw } = chartRect();
  let started = false;
  for (let px = 0; px <= pw; px += 2) {
    const x = invX(M.l + px);
    if (x <= 0) continue;
    const y = sensAt(P, x);
    if (!isFinite(y)) continue;
    const cx = M.l + px, cy = Y(y);
    if (!started) { ctx.moveTo(cx, cy); started = true; } else ctx.lineTo(cx, cy);
  }
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const { w, h, pw, ph } = chartRect();
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  yScale = computeYRange();

  const inkGrid = "rgba(120,140,175,.10)";
  const inkAxis = cssVar("--ink-3"), ink2 = cssVar("--ink-2");
  ctx.font = "12px Segoe UI, system-ui, sans-serif";

  // grid + labels
  niceTicks(0, xMax, 7).forEach(t => {
    const px = X(t);
    ctx.strokeStyle = inkGrid; ctx.beginPath(); ctx.moveTo(px, M.t); ctx.lineTo(px, M.t + ph); ctx.stroke();
    ctx.fillStyle = inkAxis; ctx.textAlign = "center";
    ctx.fillText(String(+t.toFixed(2)), px, M.t + ph + 18);
  });
  niceTicks(yScale.yMin, yScale.yMax, 6).forEach(t => {
    const py = Y(t);
    ctx.strokeStyle = inkGrid; ctx.beginPath(); ctx.moveTo(M.l, py); ctx.lineTo(M.l + pw, py); ctx.stroke();
    ctx.fillStyle = inkAxis; ctx.textAlign = "right";
    ctx.fillText(String(+t.toFixed(3)), M.l - 8, py + 4);
  });

  // reference line y = 1
  ctx.strokeStyle = "rgba(150,165,190,.28)";
  ctx.setLineDash([3, 4]);
  ctx.beginPath(); ctx.moveTo(M.l, Y(1)); ctx.lineTo(M.l + pw, Y(1)); ctx.stroke();
  ctx.setLineDash([]);

  const dirty = isDirty();
  $("lgApplied").hidden = !dirty;
  if (dirty) drawCurveLine(applied, cssVar("--applied"), 1.6, true);
  drawCurveLine(cur, cssVar("--accent"), 2.2, false);

  // LUT points
  if (cur.mode === "lut") {
    cur.data.forEach((pt, i) => {
      if (pt.x > xMax * 1.02) return;
      const cx = X(pt.x), cy = Y(ptDisplayY(cur, pt));
      ctx.beginPath();
      ctx.arc(cx, cy, i === dragIdx ? 7 : 5.5, 0, Math.PI * 2);
      ctx.fillStyle = cssVar("--accent");
      ctx.strokeStyle = cssVar("--bg");
      ctx.lineWidth = 2;
      ctx.fill(); ctx.stroke();
    });
  }

  // hover crosshair
  if (hoverX !== null && hoverX > 0 && hoverX <= xMax) {
    const px = X(hoverX);
    ctx.strokeStyle = "rgba(150,165,190,.35)";
    ctx.beginPath(); ctx.moveTo(px, M.t); ctx.lineTo(px, M.t + ph); ctx.stroke();
    const y = sensAt(cur, hoverX);
    ctx.beginPath();
    ctx.arc(px, Y(y), 4, 0, Math.PI * 2);
    ctx.fillStyle = cssVar("--accent");
    ctx.fill();
  }

  drawLiveMarker();

  // axis titles
  ctx.fillStyle = ink2;
  ctx.textAlign = "center";
  ctx.fillText("скорость, counts/ms", M.l + pw / 2, h - 6);
  ctx.save();
  ctx.translate(14, M.t + ph / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("множитель сенса", 0, 0);
  ctx.restore();
}

/* --- live mouse-speed marker --- */

// Browser deltas are post-driver (curve already applied), so invert
// out(v) = v * sens(v) back to input speed to place the marker where
// RawAccel's own UI would.
function invertOutputSpeed(P, out) {
  if (out <= 0) return 0;
  let lo = 0.001, hi = 600;
  for (let i = 0; i < 40; i++) {
    const mid = (lo + hi) / 2;
    if (mid * sensAt(P, mid) < out) lo = mid; else hi = mid;
  }
  return (lo + hi) / 2;
}

let frameDist = 0, liveSpeed = 0, liveShown = false;
window.addEventListener("mousemove", e => {
  frameDist += Math.hypot(e.movementX || 0, e.movementY || 0);
});
let liveLastT = performance.now();
requestAnimationFrame(function liveTick(t) {
  requestAnimationFrame(liveTick);
  const dt = Math.max(t - liveLastT, 1);
  liveLastT = t;
  const inst = frameDist / dt;
  frameDist = 0;
  liveSpeed = inst > liveSpeed ? inst : liveSpeed * Math.pow(0.99, dt);
  if (!applied) return;
  if (liveSpeed > 0.02 || liveShown) drawChart();
});

function drawLiveMarker() {
  liveShown = false;
  if (liveSpeed <= 0.02) return;
  const v = invertOutputSpeed(applied, liveSpeed);
  if (v <= 0) return;
  const vx = Math.min(v, xMax);
  const cx = X(vx), cy = Y(sensAt(applied, vx));
  ctx.beginPath();
  ctx.arc(cx, cy, 9, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(70,201,141,.22)";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
  ctx.fillStyle = cssVar("--good");
  ctx.strokeStyle = cssVar("--bg");
  ctx.lineWidth = 2;
  ctx.fill(); ctx.stroke();
  liveShown = true;
}

/* --- chart interactions --- */

const tooltip = $("tooltip");

function lutHitTest(mx, my) {
  for (let i = 0; i < cur.data.length; i++) {
    const pt = cur.data[i];
    const dx = X(pt.x) - mx, dy = Y(ptDisplayY(cur, pt)) - my;
    if (dx * dx + dy * dy < 144) return i;
  }
  return -1;
}

canvas.addEventListener("mousemove", e => {
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  if (dragIdx >= 0) {
    const pt = cur.data[dragIdx];
    pt.x = Math.max(0.01, Math.min(invX(mx), xMax));
    let y = invY(my) / cur.sens;
    y = Math.max(0.01, y);
    pt.y = cur.gain ? y * pt.x : y;
    onParamsChanged();
    return;
  }
  hoverX = invX(mx);
  if (hoverX < 0 || hoverX > xMax || my < M.t || my > M.t + chartRect().ph) {
    hoverX = null; tooltip.hidden = true; drawChart(); return;
  }
  const s = sensAt(cur, hoverX);
  let html = `<b>${hoverX.toFixed(1)}</b> counts/ms → <b>${s.toFixed(3)}</b>`;
  if (isDirty()) html += `<br><span class="t2">применённая: ${sensAt(applied, hoverX).toFixed(3)}</span>`;
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const tw = tooltip.offsetWidth;
  tooltip.style.left = Math.min(mx + 14, r.width - tw - 8) + "px";
  tooltip.style.top = (my + 14) + "px";
  drawChart();
});

canvas.addEventListener("mouseleave", () => { hoverX = null; tooltip.hidden = true; dragIdx = -1; drawChart(); });

canvas.addEventListener("mousedown", e => {
  if (cur.mode !== "lut" || e.button !== 0) return;
  const r = canvas.getBoundingClientRect();
  const i = lutHitTest(e.clientX - r.left, e.clientY - r.top);
  if (i >= 0) { dragIdx = i; tooltip.hidden = true; }
});
window.addEventListener("mouseup", () => {
  if (dragIdx >= 0) { dragIdx = -1; cur.data.sort((a, b) => a.x - b.x); onParamsChanged(); }
});

canvas.addEventListener("dblclick", e => {
  if (cur.mode !== "lut") return;
  const r = canvas.getBoundingClientRect();
  const x = invX(e.clientX - r.left);
  let y = Math.max(0.01, invY(e.clientY - r.top) / cur.sens);
  if (x <= 0) return;
  cur.data.push({ x, y: cur.gain ? y * x : y });
  cur.data.sort((a, b) => a.x - b.x);
  onParamsChanged();
});

canvas.addEventListener("contextmenu", e => {
  if (cur.mode !== "lut") return;
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  const i = lutHitTest(e.clientX - r.left, e.clientY - r.top);
  if (i >= 0 && cur.data.length > 2) { cur.data.splice(i, 1); onParamsChanged(); }
});

function seedLutFromCurve(P) {
  const src = { ...P, mode: P.mode === "lut" ? "noaccel" : P.mode };
  const fn = makeCurve(src);
  const pts = [];
  for (let i = 0; i < 16; i++) {
    const x = 0.4 * Math.pow(150 / 0.4, i / 15); // log-spaced 0.4..150
    pts.push({ x: +x.toFixed(3), y: +fn(x).toFixed(4) });
  }
  P.data = pts;
  P.gain = false; // points are sens values
}

$("lutFromCurve").onclick = () => {
  const src = { ...applied, data: [] };
  seedLutFromCurve(src);
  cur.data = src.data;
  cur.gain = false;
  onParamsChanged();
};

/* ================= profiles & api ================= */

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.err || res.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.hidden = true; }, 3200);
}

function renderProfiles(profiles) {
  const strip = $("profilesStrip");
  strip.innerHTML = "";
  profiles.forEach(p => {
    const chip = document.createElement("div");
    chip.className = "profile-chip" + (p.name === currentProfile ? " active" : "");
    chip.innerHTML = `<span>${p.name}</span><span style="color:var(--ink-3);font-size:11px">${p.mode}</span>`;
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "✕"; del.title = "Удалить профиль";
    del.onclick = async e => {
      e.stopPropagation();
      if (!confirm(`Удалить профиль «${p.name}»?`)) return;
      const r = await api("/api/profiles/delete", { name: p.name });
      if (currentProfile === p.name) currentProfile = null;
      lastProfiles = r.profiles;
      renderProfiles(r.profiles);
    };
    chip.appendChild(del);
    chip.onclick = async () => {
      try {
        const settings = await api(`/api/profile?name=${encodeURIComponent(p.name)}`);
        baseSettings = settings;
        cur = extractParams(settings);
        currentProfile = p.name;
        renderAll();
        refreshProfilesUI();
        toast(`Профиль «${p.name}» загружен — жми «Применить»`);
      } catch (err) { toast(String(err.message || err), true); }
    };
    strip.appendChild(chip);
  });
}

let lastProfiles = [];
function refreshProfilesUI() { renderProfiles(lastProfiles); }

$("applyBtn").onclick = async () => {
  try {
    const settings = buildSettings(cur);
    const r = await api("/api/apply", { settings, profileName: currentProfile });
    applied = JSON.parse(JSON.stringify(cur));
    baseSettings = settings;
    onParamsChanged();
    toast("Применено ✓ " + (r.out || ""));
  } catch (err) {
    toast("Ошибка: " + (err.message || err), true);
  }
};

$("saveProfileBtn").onclick = async () => {
  const name = prompt("Имя профиля:", currentProfile || "");
  if (!name) return;
  try {
    const r = await api("/api/profiles/save", { name, settings: buildSettings(cur) });
    currentProfile = name.trim();
    lastProfiles = r.profiles;
    refreshProfilesUI();
    toast(`Профиль «${currentProfile}» сохранён`);
  } catch (err) { toast(String(err.message || err), true); }
};

$("gainToggle").onchange = () => { cur.gain = $("gainToggle").checked; onParamsChanged(); };

$("rangeBtns").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  xMax = +b.dataset.max;
  document.querySelectorAll(".range-btns button").forEach(x => x.classList.toggle("on", x === b));
  drawChart();
});

window.addEventListener("resize", drawChart);

/* ================= Sens Finder integration ================= */

let sfData = null;

const sfDate = ts => ts ? new Date(ts).toLocaleDateString("ru-RU") : "";

/* Effective-sens ratio vs current in-game sens for one test result:
   how much the total sensitivity should change at that mode's speeds,
   with the in-game sens left untouched. */
function sfRatio(entry) {
  const cfg = sfData.config || {};
  if (!entry || !entry.winner_cm360 || !cfg.sens1_cm360 || !cfg.current_sens) return null;
  return (cfg.sens1_cm360 / entry.winner_cm360) / cfg.current_sens;
}

function sfSuggestion() {
  if (!sfData || !sfData.available) return null;
  const kf = sfRatio(sfData.find);
  const kt = sfRatio(sfData.find_track);
  if (kf == null && kt == null) return null;
  if (kf != null && kt != null) {
    if (Math.abs(kf - kt) / Math.max(kf, kt) < 0.05) {
      return { type: "flat", k: (kf + kt) / 2, kf, kt };
    }
    // transition midpoint from measured input speeds (counts/ms) if logged
    const fs = sfData.find.speed, ts = sfData.find_track.speed;
    const vMid = fs && ts ? Math.sqrt(ts.p90 * fs.med) : 8;
    return { type: "curve", kf, kt, vMid };
  }
  return { type: "flat", k: kf != null ? kf : kt, kf, kt };
}

function sfApplySuggestion() {
  const s = sfSuggestion();
  if (!s || !applied) return;
  if (s.type === "flat") {
    cur.sens = +(applied.sens * s.k).toFixed(4);
    renderAll();
    toast(`Множитель сенса → ${cur.sens} (форма кривой не тронута) — смотри график и жми «Применить»`);
    return;
  }
  // LUT = current applied curve × smooth ramp from kt (tracking speeds)
  // to kf (flick speeds); logistic in log-speed space around vMid
  const w = 0.55; // ln-units ≈ 1.6 octaves of transition width
  const scale = v => s.kt + (s.kf - s.kt) / (1 + Math.exp(-(Math.log(v) - Math.log(s.vMid)) / w));
  const pts = [];
  const n = 16, x0 = 0.15, x1 = 90;
  for (let i = 0; i < n; i++) {
    const x = x0 * Math.pow(x1 / x0, i / (n - 1));
    pts.push({ x: +x.toFixed(3), y: +(sensAt(applied, x) * scale(x)).toFixed(4) });
  }
  cur.mode = "lut";
  cur.gain = false;
  cur.data = pts;
  cur.sens = 1;
  renderAll();
  toast("Кривая построена: трекинг-оптимум на малых скоростях → флик-оптимум на больших. Подправь точки и жми «Применить»");
}

function renderSensFinder() {
  const body = $("sfBody");
  if (!sfData || !sfData.available) {
    body.innerHTML = `<p class="mode-hint">Sens Finder не найден или ещё нет результатов. Прогони «Find my sens» и «Find my tracking sens».</p>`;
    $("sfSuggest").hidden = true;
    return;
  }
  const f = sfData.find, t = sfData.find_track;
  const row = (label, e) => e
    ? `<div class="sf-row"><span>${label}</span><b>${e.winner_cm360.toFixed(0)} cm/360</b><span class="sf-date">sens ${e.recommended_sens} · ${sfDate(e.ts)}</span></div>`
    : `<div class="sf-row dim"><span>${label}</span><b>нет данных</b></div>`;
  let html = row("Флики", f) + row("Трекинг", t);
  const s = sfSuggestion();
  if (s) {
    if (s.type === "curve") {
      const dir = s.kt < s.kf ? "медленнее на трекинге, быстрее на фликах" : "быстрее на трекинге, медленнее на фликах";
      html += `<p class="mode-hint">Оптимумы расходятся (${dir}) — акселерация оправдана. Кнопка построит LUT-кривую поверх текущей: ×${s.kt.toFixed(2)} на малых скоростях → ×${s.kf.toFixed(2)} на больших (перегиб ~${s.vMid.toFixed(1)} counts/ms${sfData.find.speed && sfData.find_track.speed ? ", по твоим замерам" : ", оценка — прогони тесты заново для точных скоростей"}).</p>`;
    } else if (s.kf != null && s.kt != null) {
      html += `<p class="mode-hint">Флик- и трекинг-оптимум совпадают — акселерацию менять незачем, достаточно множителя ×${s.k.toFixed(2)}.</p>`;
    } else {
      html += `<p class="mode-hint">Есть только один тест — могу подставить плоскую компенсацию ×${s.k.toFixed(2)}. Для кривой прогони второй режим (${s.kf == null ? "Find my sens" : "Find my tracking sens"}).</p>`;
    }
    $("sfSuggest").textContent = s.type === "curve" ? "✨ Построить кривую" : "Подставить множитель";
    $("sfSuggest").hidden = false;
  } else {
    $("sfSuggest").hidden = true;
  }
  body.innerHTML = html;
}

async function loadSensFinder() {
  try {
    sfData = await api("/api/sensfinder");
  } catch { sfData = null; }
  renderSensFinder();
}

$("sfLaunch").onclick = async () => {
  try {
    await api("/api/sensfinder/launch", {});
    toast("Sens Finder запущен — вернись сюда после теста");
  } catch (err) { toast("Не смог запустить: " + (err.message || err), true); }
};

$("sfSuggest").onclick = sfApplySuggestion;

// refresh results when the window regains focus (after a test run)
window.addEventListener("focus", () => { if (sfData !== undefined) loadSensFinder(); });

/* ================= boot ================= */

function showRaSetup() {
  const div = document.createElement("div");
  div.style.cssText = "position:fixed;inset:0;z-index:99;display:flex;align-items:center;justify-content:center;background:rgba(10,12,16,.9)";
  div.innerHTML = `
    <div class="card" style="max-width:560px;padding:28px 32px">
      <h2>Где установлен RawAccel?</h2>
      <p style="margin:12px 0;line-height:1.5;color:var(--txt-dim,#aab)">
        Не нашёл <code>writer.exe</code>. Укажи папку, куда распакован
        <a href="https://github.com/a1xd/rawaccel/releases" target="_blank">RawAccel</a>
        (драйвер должен быть установлен через его installer.exe + перезагрузка).</p>
      <div style="display:flex;gap:8px;margin:14px 0">
        <input id="raPathInput" type="text" placeholder="например C:\\RawAccel"
               style="flex:1;padding:8px 10px;border-radius:8px;border:1px solid #333;background:#191c24;color:inherit">
        <button id="raPickBtn" class="btn ghost">Выбрать…</button>
        <button id="raSaveBtn" class="btn primary">OK</button>
      </div>
      <p id="raErr" style="color:#e66;min-height:1.2em"></p>
      <p style="color:var(--txt-dim,#aab)">Sens Finder работает и без RawAccel:
        <button id="raSfBtn" class="btn ghost">🎯 Запустить Sens Finder</button></p>
    </div>`;
  document.body.appendChild(div);
  const err = div.querySelector("#raErr");
  const done = (r) => { if (r && r.ok) location.reload(); };
  const fail = (e) => { err.textContent = e.message || e; };
  div.querySelector("#raSaveBtn").onclick = () =>
    api("/api/radir", { path: div.querySelector("#raPathInput").value }).then(done, fail);
  div.querySelector("#raPickBtn").onclick = () =>
    api("/api/radir/pick", {}).then(done, fail);
  div.querySelector("#raSfBtn").onclick = () =>
    api("/api/sensfinder/launch", {}).catch(fail);
}

(async function init() {
  try {
    const st = await api("/api/state");
    if (!st.raDir) { showRaSetup(); return; }
    baseSettings = st.settings;
    cur = extractParams(st.settings);
    applied = JSON.parse(JSON.stringify(cur));
    currentProfile = st.activeProfile;
    lastProfiles = st.profiles;
    renderProfiles(st.profiles);
    renderAll();
    loadSensFinder();
  } catch (err) {
    document.body.innerHTML = `<div style="padding:40px;font-size:16px">Не могу связаться с сервером: ${err.message || err}</div>`;
  }
})();
