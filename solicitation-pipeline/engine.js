/* =============================================================================
   EM Pricing Studio — pricing engine (no DOM, no Zoho). Loaded by widget.html and
   by the Node tests. Every figure the Studio shows comes from price().

   Conventions
   - A month is 365/12 = 30.4167 days. Daily rates x units x DAYS = one month.
   - "Active" units are on leg. Spares are units the customer holds on the shelf.
     Fleet = active + spares. (Inactive units were removed from the Studio on Sep 24.)
   - Vendor spare allowance: the vendor lets you hold spares up to a % of ACTIVE units
     at no charge; every spare above that is charged at the spare overage daily rate.
   - The customer model is separate from how JusticePoint sources the devices (lease
     from the vendor, or buy the fleet). A customer purchase always means JP buys the
     devices and resells them.
   - Transparent pricing: the customer commits to a number of units and pays a flat daily
     rate for every committed unit on every day of the month, used or not. No
     spare charges. The commitment can step up at set months. JusticePoint's vendor
     cost treats every committed unit as on leg, plus the spares JP holds for them.
   - The term is priced month by month, so step increases, escalators and fleet
     purchases all land in the month they happen.
   ============================================================================= */
(function (root) {
  'use strict';

  var DAYS = 365 / 12;
  var MODELS = {
    lease:       { key: 'lease',       label: 'Daily lease',         short: 'Lease' },
    transparent: { key: 'transparent', label: 'Transparent pricing', short: 'Transparent' },
    purchase:    { key: 'purchase',    label: 'Purchase + service',  short: 'Purchase' }
  };
  var TIERS = [
    { max: 0,        key: 'neg',    label: 'Negative' },
    { max: 20,       key: 'thin',   label: 'Thin' },
    { max: 50,       key: 'normal', label: 'Normal' },
    { max: Infinity, key: 'strong', label: 'Strong' }
  ];

  var n = function (v) { v = Number(v); return isFinite(v) ? v : 0; };
  var pct = function (v) { return n(v) / 100; };
  function tierOf(marginPct, revenue) {
    if (!(revenue > 0)) return { key: 'none', label: 'No revenue' };
    for (var i = 0; i < TIERS.length; i++) if (marginPct < TIERS[i].max) return TIERS[i];
    return TIERS[TIERS.length - 1];
  }

  function blankScenario() {
    return {
      id: null, name: 'New scenario', account: null, customerName: '', program: '',
      model: 'lease', vendorSource: 'lease', termMonths: 36, targetMarginPct: 35,
      priceEscalatorPct: 0, costEscalatorPct: 0,
      lines: [], staffing: [], fees: [],
      spareBilling: { enabled: false, overPct: 20, dailyRate: 0 },
      lostRatePct: 0, lostRecoveryPct: 0,
      proposal: { validDays: 30, preparedBy: '', intro: '', options: ['current'], extraTerms: '', showMonthlyEstimate: true }
    };
  }
  function blankLine(itemId) {
    return { itemId: itemId || null, units: 10, sparePct: 20, commitUnits: null, commitSteps: [],
      price: { leaseDaily: 0, transparentDaily: 0, purchasePrice: 0, serviceDaily: 0, lostFee: 0 } };
  }
  /* older saves: SaaS became Transparent pricing; the monthly minimum and inactive units were removed */
  function migrate(sc) {
    if (!sc) return sc;
    if (sc.model === 'saas') sc.model = 'transparent';
    delete sc.minimumMonthly;
    if (sc.proposal && sc.proposal.options) sc.proposal.options = sc.proposal.options.map(function (o) { return o === 'saas' ? 'transparent' : o; });
    (sc.lines || []).forEach(function (l) {
      l.price = l.price || {};
      if (l.price.transparentDaily == null) l.price.transparentDaily = 0;
      delete l.price.saasMonthly; delete l.price.inactiveDaily; delete l.inactivePct;
      if (!Array.isArray(l.commitSteps)) l.commitSteps = [];
      if (l.commitUnits === undefined) l.commitUnits = null;
    });
    return sc;
  }

  /* committed units in month m (1-based): the starting commitment, then the latest step at or before m */
  function committedAt(line, m) {
    var u = line.commitUnits == null || line.commitUnits === '' ? n(line.units) : n(line.commitUnits);
    (line.commitSteps || []).slice().sort(function (a, b) { return n(a.month) - n(b.month); }).forEach(function (s) {
      if (n(s.month) >= 1 && n(s.month) <= m && s.units !== '' && s.units != null) u = n(s.units);
    });
    return Math.max(0, u);
  }

  /* ---------- one device line, one month's volume, one model ---------- */
  function priceLine(line, item, sc, model, k, units) {
    item = item || {};
    k = k == null ? 1 : k;
    var src = model === 'purchase' ? 'buy' : (sc.vendorSource === 'buy' ? 'buy' : 'lease');
    var transparent = model === 'transparent';
    var A = Math.max(0, units == null ? n(line.units) : units);
    var S = A * pct(line.sparePct);
    var F = A + S;
    var p = line.price || {};
    var out = { itemId: line.itemId, name: item.name || 'Device', active: A, spares: S, fleet: F, source: src,
      rev: { device: 0, spares: 0, lost: 0 }, cost: { device: 0, spares: 0, lost: 0 },
      onceRev: 0, onceCost: 0, buyUnit: 0, lifeMonths: 0, missing: [] };

    /* vendor cost */
    if (src === 'lease') {
      if (!(n(item.costLeaseDaily) > 0)) out.missing.push('vendor lease $/day');
      out.cost.device = A * n(item.costLeaseDaily) * DAYS;
      out.sparesOverAllowance = Math.max(0, S - A * pct(item.spareAllowancePct));
      out.cost.spares = out.sparesOverAllowance * n(item.spareOverageDaily) * DAYS;
    } else {
      if (!(n(item.costPurchase) > 0)) out.missing.push('vendor buy $/unit');
      out.sparesOverAllowance = 0;
      out.cost.device = A * n(item.costOwnedDaily) * DAYS;
      if (model === 'purchase') out.onceCost = F * n(item.costPurchase);
      else { out.buyUnit = n(item.costPurchase); out.lifeMonths = Math.max(1, n(item.usefulLifeMonths) || 36); }
    }

    /* losses: the customer owns purchased devices, so losses there are theirs */
    if (model !== 'purchase') {
      var lostPerMonth = F * pct(sc.lostRatePct) / 12;
      var repl = n(item.replacementCost) || (src === 'buy' ? n(item.costPurchase) : 0);
      out.lostUnits = lostPerMonth;
      out.cost.lost = lostPerMonth * repl;
      out.rev.lost = lostPerMonth * pct(sc.lostRecoveryPct) * n(p.lostFee);
    }

    /* customer revenue */
    if (model === 'lease') {
      out.rev.device = k * A * n(p.leaseDaily) * DAYS;
      if (sc.spareBilling && sc.spareBilling.enabled) {
        out.sparesBillable = Math.max(0, S - A * pct(sc.spareBilling.overPct));
        out.rev.spares = out.sparesBillable * n(sc.spareBilling.dailyRate) * DAYS;
      }
    } else if (transparent) {
      out.rev.device = k * A * n(p.transparentDaily) * DAYS;      /* every committed unit, every day */
    } else {
      out.rev.device = k * A * n(p.serviceDaily) * DAYS;
      out.onceRev = k * F * n(p.purchasePrice);
    }
    out.revMonthly = out.rev.device + out.rev.spares + out.rev.lost;
    out.cashCostMonthly = out.cost.device + out.cost.spares + out.cost.lost;
    return out;
  }

  /* ---------- staffing ---------- */
  function priceStaff(s, activeTotal) {
    var P = s.participants == null || s.participants === '' ? activeTotal : Math.max(0, n(s.participants));
    var cost = s.costMethod === 'fte'
      ? n(s.fte) * n(s.fteMonthlyCost)
      : P * n(s.hoursPerParticipant) * n(s.costPerHour) * (1 + pct(s.overheadPct));
    var rev = s.priceMethod === 'monthly' ? n(s.priceMonthly) : P * n(s.priceDaily) * DAYS;
    return { name: s.name || 'Staffing', participants: P, revMonthly: rev, costMonthly: cost, marginMonthly: rev - cost };
  }

  var sum = function (arr, f) { return arr.reduce(function (s, x) { return s + f(x); }, 0); };

  /* ---------- whole scenario ---------- */
  function price(sc, catalog, opts) {
    opts = opts || {};
    migrate(sc);
    var model = opts.model || sc.model || 'lease';
    if (!MODELS[model]) model = 'lease';
    var k = opts.k == null ? 1 : opts.k;
    var byId = {};
    (catalog || []).forEach(function (c) { byId[String(c.id)] = c; });
    var T = Math.max(1, Math.round(n(sc.termMonths) || 12));
    var pe = pct(sc.priceEscalatorPct), ce = pct(sc.costEscalatorPct);
    var fees = sc.fees || [];

    var linesAt = function (m) {
      return (sc.lines || []).map(function (l) {
        var u = model === 'transparent' ? committedAt(l, m) : n(l.units);
        return priceLine(l, byId[String(l.itemId)], sc, model, k, u);
      });
    };
    var monthParts = function (lines) {
      var active = sum(lines, function (l) { return l.active; });
      var staff = (sc.staffing || []).map(function (s) { return priceStaff(s, active); });
      var feesM = fees.filter(function (f) { return f.when === 'monthly'; });
      var fleet = sum(lines, function (l) { return l.fleet; });
      var qty = function (f) { return f.qtyBasis === 'perUnit' ? fleet : n(f.qty); };
      return {
        lines: lines, staff: staff, active: active, fleet: fleet,
        rev: { device: sum(lines, function (l) { return l.rev.device; }), spares: sum(lines, function (l) { return l.rev.spares; }), lost: sum(lines, function (l) { return l.rev.lost; }),
          staffing: sum(staff, function (s) { return s.revMonthly; }), fees: sum(feesM, function (f) { return qty(f) * n(f.price); }) },
        cost: { device: sum(lines, function (l) { return l.cost.device; }), spares: sum(lines, function (l) { return l.cost.spares; }), lost: sum(lines, function (l) { return l.cost.lost; }),
          staffing: sum(staff, function (s) { return s.costMonthly; }), fees: sum(feesM, function (f) { return qty(f) * n(f.cost); }) }
      };
    };
    var tot = function (o) { return Object.keys(o).reduce(function (s, key) { return s + o[key]; }, 0); };

    /* month 0: one-time charges, hardware sales and the opening fleet purchase */
    var first = monthParts(linesAt(1));
    var onceFees = fees.filter(function (f) { return f.when !== 'monthly'; });
    var perUnitOnce = onceFees.filter(function (f) { return f.qtyBasis === 'perUnit'; });
    var once = {
      rev: sum(first.lines, function (l) { return l.onceRev; }) + sum(onceFees, function (f) { return (f.qtyBasis === 'perUnit' ? first.fleet : n(f.qty)) * n(f.price); }),
      cost: sum(first.lines, function (l) { return l.onceCost; }) + sum(onceFees, function (f) { return (f.qtyBasis === 'perUnit' ? first.fleet : n(f.qty)) * n(f.cost); }),
      capex: 0
    };
    /* owned fleet: each purchase is a tranche depreciated from the month it is bought */
    var tranches = [];
    first.lines.forEach(function (l) { if (l.buyUnit > 0 && l.fleet > 0) tranches.push({ start: 1, amount: l.fleet * l.buyUnit, life: l.lifeMonths }); });
    once.capex = sum(tranches, function (t) { return t.amount; });
    var amortAt = function (m) { return sum(tranches, function (t) { return m >= t.start && m < t.start + t.life ? t.amount / t.life : 0; }); };

    var termRev = once.rev, termCost = once.cost, cash = once.rev - once.cost - once.capex;
    var cum = [cash], payback = cash >= 0 ? 0 : null, months = [];
    var prev = first;
    for (var m = 1; m <= T; m++) {
      var cur = m === 1 ? first : monthParts(linesAt(m));
      var addRev = 0, addCost = 0, addCapex = 0;
      if (m > 1) {
        /* commitment increases: per-unit one-time fees and fleet purchases for the added units */
        cur.lines.forEach(function (l, i) {
          var added = l.fleet - prev.lines[i].fleet;
          if (added > 0) {
            if (l.buyUnit > 0) { var amt = added * l.buyUnit; tranches.push({ start: m, amount: amt, life: l.lifeMonths }); addCapex += amt; }
            perUnitOnce.forEach(function (f) { addRev += added * n(f.price); addCost += added * n(f.cost); });
          }
        });
      }
      var yr = Math.floor((m - 1) / 12);
      var rm = tot(cur.rev) * Math.pow(1 + pe, yr);
      var cm = tot(cur.cost) * Math.pow(1 + ce, yr);
      var am = amortAt(m);
      termRev += rm + addRev; termCost += cm + am + addCost;
      cash += rm + addRev - cm - addCost - addCapex; cum.push(cash);
      if (payback == null && cash >= 0) payback = m;
      months.push({ month: m, active: cur.active, revenue: rm, cost: cm + am, margin: rm - cm - am });
      prev = cur;
    }
    var residual = sum(tranches, function (t) { var held = Math.min(t.life, T - t.start + 1); return t.amount * Math.max(0, 1 - held / t.life); });

    /* the headline month is month 1 */
    var M1 = first, amort1 = amortAt(1);
    var R = { device: M1.rev.device, spares: M1.rev.spares, lost: M1.rev.lost, staffing: M1.rev.staffing, fees: M1.rev.fees };
    var C = { device: M1.cost.device, spares: M1.cost.spares, lost: M1.cost.lost, amort: amort1, staffing: M1.cost.staffing, fees: M1.cost.fees };
    var revM = tot(R), costM = tot(C), marginM = revM - costM;
    var last = months[months.length - 1];
    var termMargin = termRev - termCost, unitDays = M1.active * DAYS;

    /* multiplier on this model's device prices that lands the term margin on target;
       revenue is linear in k and cost does not depend on it, so this is exact */
    var target = pct(sc.targetMarginPct), solve = null;
    if (!opts.noSolve && target < 1) {
      var base = price(sc, catalog, { model: model, k: 0, noSolve: true });
      var one = price(sc, catalog, { model: model, k: 1, noSolve: true });
      var slope = one.term.revenue - base.term.revenue;
      if (slope > 0) {
        var needRev = one.term.cost / (1 - target);
        var kk = (needRev - base.term.revenue) / slope;
        solve = { k: kk, feasible: kk > 0, neededTermRevenue: needRev };
      } else solve = { k: null, feasible: false, reason: 'Enter prices for this model first.' };
    }

    var lines = M1.lines.map(function (l) {
      var c = l.cashCostMonthly + (l.buyUnit > 0 ? l.fleet * l.buyUnit / l.lifeMonths : 0);
      l.costMonthly = c; l.marginMonthly = l.revMonthly - c; l.marginPct = l.revMonthly > 0 ? l.marginMonthly / l.revMonthly * 100 : 0;
      return l;
    });
    var missing = [];
    lines.forEach(function (l) { l.missing.forEach(function (x) { missing.push(l.name + ': ' + x); }); });
    var steps = model === 'transparent' && months.some(function (x) { return x.active !== M1.active; });

    /* breakeven calculation for purchase model: how many months of service revenue to recover equipment cost */
    var breakEvenMonths = null, breakEvenServiceRevenue = null;
    if (model === 'purchase' && once.capex > 0 && M1.rev.device > 0) {
      breakEvenServiceRevenue = M1.rev.device;
      breakEvenMonths = once.capex / breakEvenServiceRevenue;
    }

    return {
      model: model, modelLabel: MODELS[model].label, vendorSource: model === 'purchase' ? 'buy' : sc.vendorSource,
      lines: lines, staffing: M1.staff, fees: fees.map(function (f) { var q = f.qtyBasis === 'perUnit' ? M1.fleet : n(f.qty); return { name: f.name || 'Fee', when: f.when === 'monthly' ? 'monthly' : 'once', qty: q, rev: q * n(f.price), cost: q * n(f.cost) }; }),
      units: { active: M1.active, fleet: M1.fleet, unitDays: unitDays, endActive: last ? last.active : M1.active, steps: steps },
      monthly: { revenue: revM, cost: costM, margin: marginM, marginPct: revM > 0 ? marginM / revM * 100 : 0, rev: R, costs: C },
      months: months, lastMonth: last,
      once: once,
      term: { months: T, revenue: termRev, cost: termCost, margin: termMargin, marginPct: termRev > 0 ? termMargin / termRev * 100 : 0, residual: residual, capexTotal: sum(tranches, function (t) { return t.amount; }), cumulativeCash: cum, paybackMonth: payback },
      perUnitDay: unitDays > 0 ? { revenue: revM / unitDays, cost: costM / unitDays, breakEven: costM / unitDays, target: target < 1 ? costM / (1 - target) / unitDays : null } : null,
      tier: tierOf(revM > 0 ? marginM / revM * 100 : 0, revM),
      breakEvenMonths: breakEvenMonths, breakEvenServiceRevenue: breakEvenServiceRevenue,
      solve: solve, missing: missing
    };
  }

  function compare(sc, catalog) { return Object.keys(MODELS).map(function (m) { return price(sc, catalog, { model: m }); }); }

  /* volume sensitivity: margin at a range of volume multipliers (commitments and steps scale too) */
  function sensitivity(sc, catalog, steps) {
    steps = steps || [0.5, 0.75, 1, 1.25, 1.5];
    return steps.map(function (f) {
      var s = JSON.parse(JSON.stringify(migrate(sc)));
      s.lines.forEach(function (l) {
        l.units = n(l.units) * f;
        if (l.commitUnits != null && l.commitUnits !== '') l.commitUnits = n(l.commitUnits) * f;
        (l.commitSteps || []).forEach(function (st) { st.units = n(st.units) * f; });
      });
      s.staffing.forEach(function (st) { if (st.participants != null && st.participants !== '') st.participants = n(st.participants) * f; });
      var r = price(s, catalog, { noSolve: true });
      return { factor: f, active: r.units.active, marginPct: r.monthly.marginPct, margin: r.monthly.margin, revenue: r.monthly.revenue };
    });
  }

  /* multiply this model's device prices by k (used by "Apply target price") */
  function applyMultiplier(sc, model, k) {
    var keys = model === 'lease' ? ['leaseDaily'] : model === 'transparent' ? ['transparentDaily'] : ['purchasePrice', 'serviceDaily'];
    sc.lines.forEach(function (l) { keys.forEach(function (key) { l.price[key] = Math.round(n(l.price[key]) * k * 100) / 100; }); });
    return sc;
  }

  var api = { DAYS: DAYS, MODELS: MODELS, TIERS: TIERS, tierOf: tierOf, price: price, compare: compare, sensitivity: sensitivity,
    applyMultiplier: applyMultiplier, blankScenario: blankScenario, blankLine: blankLine, migrate: migrate, committedAt: committedAt };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.PricingEngine = api;
})(this);
