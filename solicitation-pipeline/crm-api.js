/* =============================================================================
   EM Pricing Studio - Zoho CRM data layer (and a browser-only preview stand-in).
   Every Zoho SDK call lives here. The UI uses the same promise interface in both modes.

   Field safety: before reading or writing, the layer asks Zoho which fields actually exist
   on Products and Pricing_Scenarios. Fields that do not exist are left out of queries and
   saves (one missing field would otherwise make Zoho reject the whole record) and are
   reported by setupReport() so the Studio can list what to create.
   ============================================================================= */
(function (root) {
  'use strict';

  function create(o) {
    o = o || {};
    var PREVIEW = !!o.preview;
    var E = o.engine;
    var CAT = o.catalogFields || {};          /* cost key -> Products API name */
    var PRICE = o.priceFields || {};          /* customer price key -> Products API name */
    var SCEN = o.scenarioFields || [];        /* Pricing_Scenarios API names the Studio writes */
    var MODEL_PICK = o.modelPick || {};
    var timeout = o.timeout || function (p) { return Promise.resolve(p); };
    var today = o.today, addDays = o.addDays;
    var num = o.number || function (v) { v = Number(v); return isFinite(v) ? v : 0; };

    var clone = function (x) { return JSON.parse(JSON.stringify(x)); };
    var rowsOf = function (r) { if (!r) return []; if (Array.isArray(r)) return r; if (Array.isArray(r.data)) return r.data; if (r.data && Array.isArray(r.data.data)) return r.data.data; return []; };
    var qs = function (v) { return String(v || '').replace(/'/g, "''"); };
    var round2 = function (v) { return Math.round(num(v) * 100) / 100; };
    var store = {
      get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
      set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
    };
    var errText = function (e) {
      if (!e) return 'unknown error';
      if (typeof e === 'string') return e;
      if (e.message) return e.message;
      try { var d = e.data && e.data[0] ? e.data[0] : e; return (d.code ? d.code + ': ' : '') + (d.message || JSON.stringify(d)); } catch (x) { return String(e); }
    };
    function writeOk(resp) {
      var row = resp && resp.data && resp.data[0];
      if (row && (row.code === 'SUCCESS' || row.status === 'success')) return row.details || {};
      throw new Error((row && (row.message + (row.details && row.details.api_name ? ' (' + row.details.api_name + ')' : ''))) || 'Zoho refused the change');
    }

    /* ---------- local scenario storage (preview, and a safety copy when a CRM save fails) ---------- */
    var LOCAL_KEY = 'em-pricing-scenarios-v1';
    var local = {
      list: function () { try { return JSON.parse(store.get(LOCAL_KEY) || '[]') || []; } catch (e) { return []; } },
      put: function (sc) { var all = local.list().filter(function (x) { return x.id !== sc.id; }); all.unshift(sc); store.set(LOCAL_KEY, JSON.stringify(all.slice(0, 40))); }
    };

    var api = {
      keepLocalCopy: function (sc) { var c = clone(sc); c.id = c.id || ('local-' + Date.now()); local.put(c); return c.id; }
    };

    /* =====================================================================
       PREVIEW: a pretend "Zoho" kept in this browser, so matching can be tried
       ===================================================================== */
    if (PREVIEW) {
      var ZKEY = 'em-pricing-preview-zoho-v3';
      var SAMPLE = [
        { id: 'p-gps', name: 'GPS device', vendor: 'Sample vendor A', family: 'GPS', costs: { costLeaseDaily: 2.50, spareAllowancePct: 25, spareOverageDaily: 1.00, costPurchase: 450, costOwnedDaily: 1.00, usefulLifeMonths: 36, replacementCost: 450 }, prices: { leaseDaily: 4.00, inactiveDaily: 0, saasMonthly: 130, purchasePrice: 650, serviceDaily: 2.00, lostFee: 450 } },
        { id: 'p-cam', name: 'Continuous alcohol monitor (CAM)', vendor: 'Sample vendor A', family: 'CAM', costs: { costLeaseDaily: 7.00, usefulLifeMonths: 36, replacementCost: 1200 }, prices: { leaseDaily: 11.00, saasMonthly: 340, lostFee: 1200 } },
        { id: 'p-rb', name: 'Remote breath device', vendor: 'Sample vendor B', family: 'Remote Breath', costs: {}, prices: {} },
        { id: 'p-rf', name: 'RF device', vendor: 'Sample vendor B', family: 'RF', costs: {}, prices: {} }
      ];
      var zload = function () { try { var s = store.get(ZKEY); return s ? JSON.parse(s) : clone(SAMPLE); } catch (e) { return clone(SAMPLE); } };
      var zsave = function (list) { store.set(ZKEY, JSON.stringify(list)); };
      var toItem = function (z) {
        var c = { id: z.id, name: z.name, vendor: z.vendor, vendorId: z.vendorId || '', family: z.family || '', notes: z.notes || '' };
        Object.keys(CAT).forEach(function (k) { if (k !== 'updated' && k !== 'family' && k !== 'notes') c[k] = z.costs[k] == null ? null : z.costs[k]; });
        c.zoho = zohoSnapshot(c); c.zohoPrice = clone(z.prices || {});
        return c;
      };
      api.setupReport = function () { return Promise.resolve({ products: [], prices: [], scenarios: [], metadata: true }); };
      api.currentUser = function () { return Promise.resolve('Beau Carrigan'); };
      api.catalog = function () { return Promise.resolve(zload().map(toItem)); };
      api.vendors = function () { return Promise.resolve([{ id: 'v1', name: 'Sample vendor A' }, { id: 'v2', name: 'Sample vendor B' }]); };
      api.saveCatalog = function (rows) {
        var list = zload();
        rows.forEach(function (c) {
          var z = list.filter(function (x) { return x.id === c.id; })[0];
          if (!z) { z = { id: c.isNew ? 'p-' + Date.now() + Math.floor(Math.random() * 1000) : c.id, costs: {}, prices: {} }; list.push(z); c.id = z.id; c.isNew = false; }
          z.name = c.name; z.vendor = c.vendor; z.vendorId = c.vendorId; z.family = c.family; z.notes = c.notes;
          Object.keys(CAT).forEach(function (k) { if (k !== 'updated' && k !== 'family' && k !== 'notes') z.costs[k] = c[k] === '' ? null : c[k]; });
        });
        zsave(list);
        return Promise.resolve({ count: rows.length, skipped: [] });
      };
      api.savePrice = function (itemId, key, value) {
        var list = zload(), z = list.filter(function (x) { return x.id === itemId; })[0];
        if (!z) return Promise.reject(new Error('Save the device in the Cost catalog first.'));
        z.prices = z.prices || {}; z.prices[key] = value; zsave(list); return Promise.resolve(value);
      };
      api.accounts = function (term) {
        var all = [{ id: 'a1', name: 'Sheboygan County Sheriffs Department' }, { id: 'a2', name: 'Columbia County Treatment Court' }, { id: 'a3', name: 'Iowa County Sheriff' }, { id: 'a4', name: 'Rock County' }];
        term = term.toLowerCase(); return Promise.resolve(all.filter(function (a) { return a.name.toLowerCase().indexOf(term) >= 0; }));
      };
      api.listScenarios = function () {
        var l = local.list();
        if (!l.length && E) { var s = sampleScenario(); s.id = 'local-sample'; local.put(s); l = local.list(); }
        return Promise.resolve(l.map(function (s) { return { id: s.id, name: s.name }; }));
      };
      api.loadScenario = function (id) { var s = local.list().filter(function (x) { return x.id === id; })[0]; return s ? Promise.resolve(clone(s)) : Promise.reject(new Error('Not found')); };
      api.saveScenario = function (sc) { if (!sc.id) sc.id = 'local-' + Date.now(); local.put(clone(sc)); return Promise.resolve({ id: sc.id, skipped: [] }); };
      api.markSent = function () { return Promise.resolve(); };
      api.attach = function () { return Promise.reject(new Error('Preview: nothing is attached. In Zoho the PDF is attached to the scenario record.')); };
      return finish(api);
    }

    /* =====================================================================
       ZOHO CRM
       ===================================================================== */
    var meta = {};   /* module -> Promise<{ ok, fields: {api_name: field} }> */
    function fieldsOf(module, fresh) {
      if (!fresh && meta[module]) return meta[module];
      meta[module] = timeout(ZOHO.CRM.META.getFields({ Entity: module }), 10000).then(function (r) {
        var list = (r && (r.fields || r.data)) || [];
        if (!list.length) throw new Error('empty');
        var map = {}; list.forEach(function (f) { map[f.api_name] = f; });
        return { ok: true, fields: map };
      }).catch(function () { return { ok: false, fields: {} }; });   /* no metadata: assume fields exist */
      return meta[module];
    }
    var has = function (m, apiName) { return !m.ok || !!m.fields[apiName]; };
    var writable = function (m, apiName) { return !m.ok || (!!m.fields[apiName] && !m.fields[apiName].field_read_only && !m.fields[apiName].read_only); };

    api.coql = function (q) { return timeout(ZOHO.CRM.API.coql({ select_query: q })).then(rowsOf); };
    api.currentUser = function () {
      return timeout(ZOHO.CRM.CONFIG.getCurrentUser(), 6000).then(function (r) { return (r && r.users && r.users[0] && r.users[0].full_name) || ''; }).catch(function () { return ''; });
    };

    /* what is missing in Zoho, for the setup panel */
    api.setupReport = function (fresh) {
      return Promise.all([fieldsOf('Products', fresh), fieldsOf('Pricing_Scenarios', fresh)]).then(function (res) {
        var P = res[0], S = res[1];
        var miss = function (m, map) { return Object.keys(map).filter(function (k) { return !has(m, map[k]); }).map(function (k) { return { key: k, api: map[k] }; }); };
        var ro = function (m, map) { return Object.keys(map).filter(function (k) { return has(m, map[k]) && !writable(m, map[k]); }).map(function (k) { return { key: k, api: map[k] }; }); };
        return {
          metadata: P.ok,
          products: miss(P, CAT), productsReadOnly: ro(P, CAT),
          prices: miss(P, PRICE), pricesReadOnly: ro(P, PRICE),
          scenarios: S.ok ? SCEN.filter(function (a) { return !S.fields[a]; }).map(function (a) { return { api: a }; }) : []
        };
      });
    };

    function readProducts(P) {
      var base = ['id', 'Product_Name', 'Vendor_Name', 'Product_Active'];
      var costCols = Object.keys(CAT).map(function (k) { return CAT[k]; }).filter(function (a) { return has(P, a); });
      var priceCols = Object.keys(PRICE).map(function (k) { return PRICE[k]; }).filter(function (a) { return has(P, a); });
      var cols = base.concat(costCols, priceCols).filter(function (a, i, arr) { return arr.indexOf(a) === i; }).slice(0, 50);
      var q = function (c) { return api.coql('select ' + c.join(', ') + " from Products where Product_Active = true order by Product_Name asc limit 200"); };
      return q(cols).catch(function (e) {
        /* COQL refused (unknown column without metadata, etc.): fall back to the Records API */
        var all = [];
        var page = function (p) {
          return timeout(ZOHO.CRM.API.getAllRecords({ Entity: 'Products', sort_order: 'asc', per_page: 200, page: p })).then(function (r) {
            all = all.concat(rowsOf(r));
            return r && r.info && r.info.more_records && p < 10 ? page(p + 1) : all;
          });
        };
        return page(1).then(function (rows) { return rows.filter(function (r) { return r.Product_Active !== false; }); }, function () { throw new Error('Could not read Products: ' + errText(e)); });
      });
    }

    api.catalog = function () {
      return fieldsOf('Products').then(function (P) {
        return readProducts(P).then(function (rows) {
          return rows.map(function (r) {
            var c = { id: String(r.id), name: r.Product_Name || '', vendorId: r.Vendor_Name ? String(r.Vendor_Name.id || r.Vendor_Name) : '', vendor: (r.Vendor_Name && r.Vendor_Name.name) || '' };
            Object.keys(CAT).forEach(function (k) { if (k !== 'updated') c[k] = r[CAT[k]] == null ? null : r[CAT[k]]; });
            c.zoho = zohoSnapshot(c);
            c.zohoPrice = {};
            Object.keys(PRICE).forEach(function (k) { if (has(P, PRICE[k]) && r[PRICE[k]] != null && r[PRICE[k]] !== '') c.zohoPrice[k] = num(r[PRICE[k]]); });
            return c;
          });
        });
      });
    };

    api.vendors = function () {
      return api.coql('select id, Vendor_Name from Vendors where id is not null order by Vendor_Name asc limit 200')
        .then(function (r) { return r.map(function (v) { return { id: String(v.id), name: v.Vendor_Name }; }); }).catch(function () { return []; });
    };

    /* save catalog rows; only fields that exist and are writable are sent */
    api.saveCatalog = function (rows) {
      return fieldsOf('Products', true).then(function (P) {
        var skipped = {};
        var chain = Promise.resolve(0);
        rows.forEach(function (c) {
          chain = chain.then(function (done) {
            var data = { Product_Name: String(c.name || '').trim() };
            if (c.vendorId) data.Vendor_Name = { id: c.vendorId };
            Object.keys(CAT).forEach(function (k) {
              var a = CAT[k]; if (k === 'updated') return;
              if (!writable(P, a)) { if (c[k] != null && c[k] !== '') skipped[a] = true; return; }
              data[a] = c[k] === '' ? null : c[k];
            });
            if (CAT.updated && writable(P, CAT.updated)) data[CAT.updated] = today();
            if (c.isNew) {
              data.Product_Active = true;
              return timeout(ZOHO.CRM.API.insertRecord({ Entity: 'Products', APIData: data })).then(writeOk).then(function (d) { c.id = String(d.id); c.isNew = false; return done + 1; });
            }
            data.id = c.id;
            return timeout(ZOHO.CRM.API.updateRecord({ Entity: 'Products', APIData: data })).then(writeOk).then(function () { return done + 1; });
          });
        });
        return chain.then(function (n) { return { count: n, skipped: Object.keys(skipped) }; });
      });
    };

    /* write one customer price to its Products field ("Save to Zoho" in the builder) */
    api.savePrice = function (itemId, key, value) {
      var a = PRICE[key];
      if (!a) return Promise.reject(new Error('No Zoho field is mapped for this price.'));
      if (!itemId || /^new-/.test(itemId)) return Promise.reject(new Error('Save the device in the Cost catalog first.'));
      return fieldsOf('Products', true).then(function (P) {
        if (!has(P, a)) throw new Error('Products has no field ' + a + '. Create it (Currency) to store this price in Zoho.');
        if (!writable(P, a)) throw new Error('Field ' + a + ' is read-only for your profile.');
        var data = { id: itemId }; data[a] = value;
        return timeout(ZOHO.CRM.API.updateRecord({ Entity: 'Products', APIData: data })).then(writeOk).then(function () { return value; });
      });
    };

    api.accounts = function (term) {
      return api.coql("select id, Account_Name from Accounts where Account_Name like '%" + qs(term) + "%' limit 10")
        .then(function (r) { return r.map(function (a) { return { id: String(a.id), name: a.Account_Name }; }); });
    };
    api.listScenarios = function () {
      return api.coql("select id, Name, Pricing_Model, Est_Margin_Pct, Scenario_Status, Modified_Time from Pricing_Scenarios where Scenario_Status != 'Superseded' order by Modified_Time desc limit 100")
        .catch(function () { return api.coql('select id, Name, Modified_Time from Pricing_Scenarios where id is not null order by Modified_Time desc limit 100'); })
        .then(function (r) { return r.map(function (q) { return { id: String(q.id), name: q.Name, model: q.Pricing_Model, margin: q.Est_Margin_Pct }; }); })
        .catch(function () { return []; });
    };
    api.loadScenario = function (id) {
      return timeout(ZOHO.CRM.API.getRecord({ Entity: 'Pricing_Scenarios', RecordID: id })).then(function (r) {
        var q = rowsOf(r)[0] || {}, w = null;
        try { w = JSON.parse(q.Scenario_JSON || 'null'); } catch (e) {}
        if (!w || !w.scenario) throw new Error('This record has no Pricing Studio scenario on it.');
        var sc = w.scenario; sc.id = String(q.id); sc.name = q.Name || sc.name;
        if (q.Account && q.Account.id) sc.account = { id: String(q.Account.id), name: q.Account.name };
        if (q.Opportunity && q.Opportunity.id) sc.deal = { id: String(q.Opportunity.id), name: q.Opportunity.name };
        return sc;
      });
    };
    api.saveScenario = function (sc, res) {
      return fieldsOf('Pricing_Scenarios').then(function (S) {
        if (S.ok && !S.fields.Scenario_JSON) throw new Error('Pricing_Scenarios has no Scenario_JSON field (Multi Line, large). Create it so scenarios can be reopened.');
        var opts = (sc.proposal.options || ['current']).map(function (m) { return m === 'current' ? res.model : m; });
        var all = {
          Name: String(sc.name || 'Pricing scenario').slice(0, 120),
          Valid_Until: addDays(today(), num(sc.proposal.validDays) || 30).toISOString().slice(0, 10),
          Pricing_Model: MODEL_PICK[res.model], Vendor_Source: res.vendorSource === 'buy' ? 'JP-owned fleet' : 'Lease',
          Proposal_Options: opts.map(function (m) { return MODEL_PICK[m]; }).join(', ').slice(0, 120),
          Term_Months: res.term.months, Active_Units: Math.round(res.units.active),
          Est_Monthly_Revenue: round2(res.monthly.revenue), Est_Monthly_Cost: round2(res.monthly.cost), Est_Monthly_Margin: round2(res.monthly.margin),
          Est_Margin_Pct: round2(res.monthly.marginPct), Target_Margin_Pct: round2(sc.targetMarginPct),
          Term_Revenue: round2(res.term.revenue), Term_Margin: round2(res.term.margin),
          Upfront_Cash: round2(res.once.capex + res.once.cost - res.once.rev),
          Rev_Per_Unit_Day: res.perUnitDay ? round2(res.perUnitDay.revenue) : null, Cost_Per_Unit_Day: res.perUnitDay ? round2(res.perUnitDay.cost) : null,
          Margin_Tier: res.tier.label, Missing_Costs: res.missing.length,
          Account: sc.account && sc.account.id ? { id: sc.account.id } : null,
          Scenario_JSON: JSON.stringify({ v: 3, saved: new Date().toISOString(), scenario: sc })
        };
        var isNew = !sc.id || /^local-/.test(sc.id);
        if (isNew) all.Scenario_Status = 'Draft';
        var data = {}, skipped = [];
        Object.keys(all).forEach(function (a) { if (a === 'Name' || has(S, a)) data[a] = all[a]; else skipped.push(a); });
        var call = isNew
          ? function () { return ZOHO.CRM.API.insertRecord({ Entity: 'Pricing_Scenarios', APIData: data, Trigger: [] }); }
          : function () { data.id = sc.id; return ZOHO.CRM.API.updateRecord({ Entity: 'Pricing_Scenarios', APIData: data, Trigger: [] }); };
        return timeout(call()).then(writeOk).then(function (d) { return { id: String(d.id || sc.id), skipped: skipped }; });
      });
    };
    api.markSent = function (id) {
      return fieldsOf('Pricing_Scenarios').then(function (S) {
        var data = { id: id };
        if (has(S, 'Proposal_Sent')) data.Proposal_Sent = today();
        if (has(S, 'Scenario_Status')) data.Scenario_Status = 'Proposed';
        return timeout(ZOHO.CRM.API.updateRecord({ Entity: 'Pricing_Scenarios', APIData: data })).then(writeOk);
      });
    };
    api.attach = function (id, name, blob) { return timeout(ZOHO.CRM.API.attachFile({ Entity: 'Pricing_Scenarios', RecordID: id, File: { Name: name, Content: blob } }), 30000); };
    return finish(api);

    /* ---------- shared ---------- */
    function zohoSnapshot(c) { var s = {}; Object.keys(CAT).forEach(function (k) { if (k !== 'updated' && k !== 'family' && k !== 'notes') s[k] = c[k] == null || c[k] === '' ? null : num(c[k]); }); return s; }
    function finish(a) { a.snapshot = zohoSnapshot; return a; }
    function sampleScenario() {
      var s = E.blankScenario();
      s.name = 'Example County - Pretrial GPS and CAM (sample)'; s.customerName = 'Example County'; s.program = 'Pretrial Services';
      s.priceEscalatorPct = 3;
      var g = E.blankLine('p-gps'); g.units = 150; g.inactivePct = 8; g.sparePct = 30;
      g.price = { leaseDaily: 4.25, inactiveDaily: 0, saasMonthly: 130, purchasePrice: 650, serviceDaily: 2.00, lostFee: 450 };
      var c = E.blankLine('p-cam'); c.units = 20; c.sparePct = 15;
      c.price = { leaseDaily: 11.00, inactiveDaily: 0, saasMonthly: 350, purchasePrice: 0, serviceDaily: 0, lostFee: 1200 };
      s.lines = [g, c];
      s.staffing = [{ name: 'Monitoring center and case support', costMethod: 'hours', hoursPerParticipant: 0.5, costPerHour: 38, overheadPct: 25, participants: null, priceMethod: 'perDay', priceDaily: 0.75 }];
      s.fees = [{ name: 'Installation', when: 'once', qtyBasis: 'perUnit', qty: 0, price: 25, cost: 8 }];
      s.spareBilling = { enabled: true, overPct: 25, dailyRate: 1.50 };
      s.lostRatePct = 3; s.lostRecoveryPct = 50;
      s.proposal.preparedBy = 'Beau Carrigan';
      return s;
    }
  }

  root.PricingCRM = { create: create };
})(this);
