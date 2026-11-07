"use strict";
// The patch panel. Polls /api/events and draws the buses, the cords and the guest's output. Every value from the
// guest or the back end is put on the page with textContent, never as markup.
(function () {
  const SVG = "http://www.w3.org/2000/svg";
  // Two arrangements of the same panel. Wide: buses above, chips below, side by side. Narrow (the same breakpoint as
  // panel.css): everything in one column so the labels stay a readable size on a phone.
  const LAYOUTS = {
    wide: {
      view: "0 0 1200 430", plate: [10, 10, 1180, 410],
      ears: [[34, 40], [34, 390], [1166, 40], [1166, 390]],
      buses: { i2c: [300, 135], spi: [860, 135] },
      chips: { bno055: [190, 335], sht3x: [470, 335], mcp3008: [860, 335] },
      engBuses: [70, 52], engChips: [70, 262], rule: [70, 215, 1130, 215],
    },
    narrow: {
      view: "0 0 470 620", plate: [10, 10, 450, 600],
      ears: [[34, 36], [34, 584], [436, 36], [436, 584]],
      buses: { i2c: [100, 110], spi: [100, 200] },
      chips: { bno055: [100, 320], sht3x: [100, 420], mcp3008: [100, 520] },
      engBuses: [62, 52], engChips: [62, 275], rule: [62, 245, 420, 245],
      corridor: { bno055: -8, sht3x: 8, mcp3008: 24 },
    },
  };
  const narrowQuery = window.matchMedia("(max-width: 760px)");
  let L = narrowQuery.matches ? LAYOUTS.narrow : LAYOUTS.wide;
  let BUS_POS = L.buses;
  let CHIP_POS = L.chips;

  function applyLayout() {
    L = narrowQuery.matches ? LAYOUTS.narrow : LAYOUTS.wide;
    BUS_POS = L.buses;
    CHIP_POS = L.chips;
    $("rack").setAttribute("viewBox", L.view);
    const [px, py, pw, ph] = L.plate;
    for (const [k, v] of Object.entries({ x: px, y: py, width: pw, height: ph })) $("plate").setAttribute(k, v);
    [...$("ears").children].forEach((c, i) => { c.setAttribute("cx", L.ears[i][0]); c.setAttribute("cy", L.ears[i][1]); });
    $("eng-buses").setAttribute("x", L.engBuses[0]); $("eng-buses").setAttribute("y", L.engBuses[1]);
    $("eng-chips").setAttribute("x", L.engChips[0]); $("eng-chips").setAttribute("y", L.engChips[1]);
    ["x1", "y1", "x2", "y2"].forEach((k, i) => $("rule").setAttribute(k, L.rule[i]));
    if (topo) { drawJacks(); drawCords(); }
  }
  const ROWS = 14;
  const LINES = 9;

  let topo = null;
  let live = false;
  let faultsEnabled = false;
  let since = 0;
  let lap = null;
  let base = null;
  const state = { pulled: {}, stuck: {}, wire: { i2c: [], spi: [] }, tape: {}, lamps: {} };

  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, text) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function svg(tag, attrs, text) {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const chipById = (id) => topo.chips.find((c) => c.id === id);
  function chipFor(bus, target) {
    return topo.chips.find((c) => c.bus === bus && c.target === target);
  }

  function drawJacks() {
    const g = $("jacks");
    g.replaceChildren();
    const jack = (x, y, label, small, lampId) => {
      g.append(svg("circle", { cx: x, cy: y, r: 32, fill: "#0b0d10" }));
      g.append(svg("circle", { cx: x, cy: y, r: 24, fill: "url(#brass)" }));
      g.append(svg("circle", { cx: x, cy: y, r: 10, fill: "#050506" }));
      g.append(svg("circle", { cx: x + 52, cy: y - 18, r: 9, class: "lamp", id: lampId }));
      g.append(svg("text", { x: x + 70, y: y - 12, class: "label" }, label));
      g.append(svg("text", { x: x + 70, y: y + 12, class: "small" }, small));
    };
    for (const b of topo.buses) {
      const [x, y] = BUS_POS[b.id];
      jack(x, y, b.name, b.node, "lamp-" + b.id);
    }
    for (const c of topo.chips) {
      const [x, y] = CHIP_POS[c.id];
      jack(x, y, c.name, `${c.at}  ${c.role}`, "lamp-" + c.id);
    }
  }

  function drawCords() {
    const g = $("cords");
    g.replaceChildren();
    for (const c of topo.chips) {
      const [x1, y1] = BUS_POS[c.bus];
      const [x2, y2] = CHIP_POS[c.id];
      const cls = ["cord", c.bus];
      if (state.stuck[c.bus]) cls.push("stuck");
      let d, end;
      if (L.corridor) {
        // narrow: the cord leaves its jack to the left and runs down a lane of its own beside the jacks
        const cx = L.corridor[c.id];
        if (state.pulled[c.id]) {
          end = [x2 - 40, y2 + 50];
          d = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2 + 50}, ${end[0]} ${end[1]}`;
          cls.push("pulled");
        } else {
          end = [x2, y2];
          d = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
        }
      } else if (state.pulled[c.id]) {
        // the plug hangs loose below and to the left of the empty chip jack, clear of its lamp and label
        end = [x2 - 45, y2 + 72];
        d = `M ${x1} ${y1} C ${x1} ${y1 + 120}, ${x2 - 130} ${y2 + 30}, ${end[0]} ${end[1]}`;
        cls.push("pulled");
      } else {
        end = [x2, y2];
        d = `M ${x1} ${y1} C ${x1} ${y1 + 150}, ${x2} ${y2 - 150}, ${x2} ${y2}`;
      }
      g.append(svg("path", { d, class: cls.join(" ") }));
      g.append(svg("circle", { cx: end[0], cy: end[1], r: 14, class: "plug",
        fill: state.stuck[c.bus] ? "#8a8f96" : c.bus === "i2c" ? "#e8553b" : "#3b8ee8" }));
    }
    const words = [];
    for (const c of topo.chips) words.push(`${c.name} ${state.pulled[c.id] ? "cord pulled" : "patched to " + c.bus.toUpperCase()}`);
    for (const b of topo.buses) if (state.stuck[b.id]) words.push(`${b.name} stuck`);
    $("rack-desc").textContent = "Patch panel: " + words.join("; ") + ".";
  }

  function flash(id, ok) {
    const lamp = $("lamp-" + id);
    if (!lamp) return;
    lamp.classList.remove("ok", "bad");
    lamp.classList.add(ok ? "ok" : "bad");
    clearTimeout(state.lamps[id]);
    state.lamps[id] = setTimeout(() => lamp.classList.remove("ok", "bad"), 220);
  }

  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) $("caption").textContent = "Refused: " + (j.error || r.status);
  }

  function button(label, onClick, pressed) {
    const b = el("button", { type: "button" }, label);
    if (pressed !== undefined) b.setAttribute("aria-pressed", String(pressed));
    b.disabled = !faultsEnabled;
    b.addEventListener("click", onClick);
    return b;
  }

  function drawControls() {
    const ctl = $("ctl");
    ctl.replaceChildren();
    $("ctl-note").textContent = faultsEnabled
      ? "Each button sends the same control request as scripts/run_guest.sh ctl fault, through the back end's control socket."
      : "Recorded session: these buttons only work against a live guest. The faults you see were injected while it was recorded.";
    for (const c of topo.chips) {
      const g = el("div", { class: "ctl-group " + c.bus });
      const h = el("h3", {}, c.name + " ");
      h.append(el("span", { class: "at" }, `${c.at} on ${c.bus.toUpperCase()}`));
      const btns = el("div", { class: "btns", role: "group", "aria-label": c.name + " faults" });
      const pulled = !!state.pulled[c.id];
      btns.append(button(pulled ? "Plug cord back in" : "Pull cord", () =>
        pulled ? post("/api/clear", { bus: c.bus }) : post("/api/fault", { kind: "unplug", bus: c.bus, target: c.target }), pulled));
      if (c.bus === "i2c") btns.append(button("NAK ×3", () => post("/api/fault", { kind: "nak", bus: "i2c", target: c.target, count: 3 })));
      btns.append(button("Corrupt ×5", () => post("/api/fault", { kind: "corrupt", bus: c.bus, target: c.target, count: 5, seed: 1 })));
      g.append(h, btns);
      ctl.append(g);
    }
    for (const b of topo.buses) {
      const g = el("div", { class: "ctl-group " + b.id });
      g.append(el("h3", {}, b.name));
      const btns = el("div", { class: "btns", role: "group", "aria-label": b.name + " faults" });
      const stuck = !!state.stuck[b.id];
      btns.append(button(stuck ? "Release bus" : "Stuck bus", () =>
        stuck ? post("/api/clear", { bus: b.id }) : post("/api/fault", { kind: "stuck", bus: b.id }), stuck));
      btns.append(button("Delay 1.5 s ×2", () => post("/api/fault", { kind: "delay", bus: b.id, delay_ms: 1500, count: 2 })));
      btns.append(button("Clear faults", () => post("/api/clear", { bus: b.id })));
      g.append(btns);
      ctl.append(g);
    }
  }

  function buildColumns() {
    const wires = $("wires");
    wires.replaceChildren();
    for (const b of topo.buses) {
      const s = el("div", { class: "stream" });
      s.append(el("h3", {}, `${b.name}, ${b.driver}`));
      s.append(el("ol", { id: "wire-" + b.id, "aria-label": b.name + " transfers, newest last" }));
      wires.append(s);
    }
    const tapes = $("tapes");
    tapes.replaceChildren();
    for (const c of topo.chips) {
      const t = el("div", { class: "tape" });
      t.append(el("h3", {}, `${c.sample} (${c.name})`));
      t.append(el("ol", { id: "tape-" + c.sample, "aria-label": c.sample + " output, newest last" }));
      tapes.append(t);
      state.tape[c.sample] = [];
    }
  }

  function renderLists() {
    for (const b of topo.buses) {
      const ol = $("wire-" + b.id);
      ol.replaceChildren(...state.wire[b.id].map((r) => {
        const li = el("li", { class: r.ok ? "" : "bad" });
        li.append(el("span", { class: "t" }, r.t));
        const w = el("span", { class: "what" }, r.what);
        w.append(el("span", { class: "raw" }, r.raw));
        li.append(w);
        return li;
      }));
    }
    for (const [name, lines] of Object.entries(state.tape)) {
      const ol = $("tape-" + name);
      if (ol) ol.replaceChildren(...lines.map((l) => el("li", { class: /error|fail|not found/i.test(l) ? "err" : "" }, l)));
    }
  }

  function push(list, item, n) {
    list.push(item);
    if (list.length > n) list.splice(0, list.length - n);
  }

  function reset() {
    state.pulled = {};
    state.stuck = {};
    state.wire = { i2c: [], spi: [] };
    for (const k of Object.keys(state.tape)) state.tape[k] = [];
    base = null;
  }

  function stamp(t) {
    if (base === null) base = t;
    return "+" + (t - base).toFixed(1) + " s";
  }

  function apply(e) {
    if (e.lap !== undefined && e.lap !== lap) {
      lap = e.lap;
      reset();
    }
    const t = stamp(Number(e.t) || 0);
    if (e.type === "i2c") {
      const segs = e.segs || [];
      const c = segs.length ? chipFor("i2c", segs[0].addr) : null;
      flash("i2c", e.ok);
      if (c) flash(c.id, e.ok);
      const raw = segs.map((s) => `0x${Number(s.addr).toString(16).padStart(2, "0")} ${s.dir} ${s.data || "-"} ${s.ok ? "ack" : "nak"}`).join(" | ");
      push(state.wire.i2c, { t, what: String(e.what || ""), raw, ok: !!e.ok }, ROWS);
    } else if (e.type === "spi") {
      const c = chipFor("spi", e.cs);
      flash("spi", e.ok);
      if (c) flash(c.id, e.ok);
      push(state.wire.spi, { t, what: String(e.what || ""), raw: `cs${e.cs} tx ${e.tx || "-"} rx ${e.rx || "-"}`, ok: !!e.ok }, ROWS);
    } else if (e.type === "fault" && e.action === "add" && e.fault) {
      const f = e.fault;
      const c = chipFor(f.bus, f.target);
      if (f.kind === "unplug" && c) state.pulled[c.id] = true;
      if (f.kind === "stuck") state.stuck[f.bus] = true;
      const who = c ? c.name : f.bus.toUpperCase();
      const n = f.count ? ` ×${f.count}` : "";
      $("caption").textContent = `${t}  fault: ${f.kind}${n} on ${who}`;
      drawCords();
      drawControls();
    } else if (e.type === "fault" && e.action === "clear") {
      for (const c of topo.chips) if (!e.bus || c.bus === e.bus) delete state.pulled[c.id];
      for (const b of topo.buses) if (!e.bus || b.id === e.bus) delete state.stuck[b.id];
      $("caption").textContent = `${t}  faults cleared${e.bus ? " on " + e.bus.toUpperCase() : ""}`;
      drawCords();
      drawControls();
    } else if (e.type === "sample") {
      if (!state.tape[e.name]) return;
      push(state.tape[e.name], String(e.line), LINES);
    } else if (e.type === "note") {
      $("caption").textContent = `${t}  ${String(e.line)}`;
    }
  }

  async function tick() {
    try {
      const r = await fetch("/api/events?since=" + since);
      const j = await r.json();
      for (const e of j.events) apply(e);
      since = j.next;
      if (j.events.length) renderLists();
    } catch (err) {
      $("caption").textContent = "Panel server not reachable.";
    }
    setTimeout(tick, 300);
  }

  async function start() {
    const s = await (await fetch("/api/state")).json();
    topo = s.topology;
    live = s.mode === "live";
    faultsEnabled = s.faults_enabled;
    const mode = $("mode");
    if (live) {
      mode.textContent = "LIVE: a QNX guest is running now";
      mode.classList.add("live");
    } else {
      const m = s.meta || {};
      mode.textContent = `RECORDED SESSION, ${m.recorded || ""}, ${m.guest || ""}. Replayed, not live.`;
    }
    applyLayout();
    narrowQuery.addEventListener('change', applyLayout);
    drawControls();
    buildColumns();
    tick();
  }
  start();
})();
