
// Renders one call's body on demand. Two rules govern everything here:
//   1. NOTHING comes from the log enters innerHTML. Every value goes in
//      through textContent or a created element, so a response containing
//      markup is displayed, never executed.
//   2. The shape was decided in Python (_parse_body, registry in logdashboard)
//      and shipped as record.shape. This file does NOT re-guess it -- a second
//      copy of the dispatch rules would be a second thing to keep in sync.
var BODIES = [];
var OPENED = {};

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) { n.className = cls; }
  if (text !== undefined && text !== null) { n.textContent = String(text); }
  return n;
}

function table(head, rows) {
  // NOT class "body": the toggle locates an open panel with .body, and a
  // nested match would make it remove the wrong element.
  var t = el('table', 'tbl btbl');
  var thead = el('thead');
  var tr = el('tr');
  head.forEach(function (h) {
    var th = el('th', null, h);
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);
  var tb = el('tbody');
  rows.forEach(function (r) {
    var row = el('tr');
    r.forEach(function (c) {
      var td = el('td', null, c === undefined || c === null ? '—' : c);
      row.appendChild(td);
    });
    tb.appendChild(row);
  });
  t.appendChild(tb);
  return t;
}

// The generic renderer. This is the DEFAULT, not a fallback -- it is what
// makes a schema added to the pipeline later degrade to "readable" instead of
// to "raw JSON". Handles object, array-of-objects (union of keys, first-seen
// order, em dash where a row lacks one), array-of-scalars, scalar and null.
function generic(value) {
  var wrap = el('div', 'gen');
  if (value === null || value === undefined) {
    wrap.appendChild(el('p', 'na', 'null'));
    return wrap;
  }
  if (Array.isArray(value)) {
    if (!value.length) {
      wrap.appendChild(el('p', 'na', 'empty array'));
      return wrap;
    }
    var objs = value.every(function (v) { return v && typeof v === 'object' && !Array.isArray(v); });
    if (!objs) {
      var ul = el('ul', 'bullets');
      value.forEach(function (v) { ul.appendChild(el('li', null, JSON.stringify(v))); });
      wrap.appendChild(ul);
      return wrap;
    }
    var keys = [];
    value.forEach(function (o) {
      Object.keys(o).forEach(function (k) { if (keys.indexOf(k) === -1) { keys.push(k); } });
    });
    wrap.appendChild(table(keys, value.map(function (o) {
      return keys.map(function (k) {
        var v = o[k];
        if (v === null || v === undefined) { return '—'; }
        if (typeof v === 'object') { return JSON.stringify(v); }
        return v;
      });
    })));
    return wrap;
  }
  if (typeof value === 'object') {
    var dl = el('dl', 'kv');
    Object.keys(value).forEach(function (k) {
      var v = value[k];
      dl.appendChild(el('dt', null, k));
      if (v !== null && typeof v === 'object') {
        var d = el('dd');
        d.appendChild(generic(v));
        dl.appendChild(d);
      } else {
        dl.appendChild(el('dd', null, v === null ? 'null' : String(v)));
      }
    });
    wrap.appendChild(dl);
    return wrap;
  }
  wrap.appendChild(el('p', null, String(value)));
  return wrap;
}

// Side-by-side: source line next to EVERY fan-out column of this chapter+job.
// N candidates plus, when present, one consensus column -- so N+1, never
// assumed to be two. Each header takes its model name from that call's own
// record, because the same candidate position is a different model for a
// different job and the order is not even stable across jobs.
function sideBySide(rec) {
  var siblings = BODIES.filter(function (o) {
    return o.__chapter === rec.__chapter && o.task === rec.task && o.shape === 'lines';
  });
  var wrap = el('div', 'sbs');
  if (!siblings.length) { siblings = [rec]; }
  var src = null;
  for (var s = 0; s < siblings.length && !src; s++) {
    if (siblings[s].source_lines) { src = siblings[s].source_lines; }
  }
  if (!src) { src = rec.source_lines || null; }

  var head = ['i', 'source'];
  var cols = siblings.slice();
  cols.sort(function (a, b) {
    var ca = typeof a.candidate === 'number' ? a.candidate : 1e9;
    var cb = typeof b.candidate === 'number' ? b.candidate : 1e9;
    if (ca !== cb) { return ca - cb; }
    return (a.consensus_for ? 1 : 0) - (b.consensus_for ? 1 : 0);
  });
  cols.forEach(function (o) {
    var who = o.model || '?';
    if (typeof o.candidate === 'number') { who = 'c' + o.candidate + ' · ' + who; }
    else if (o.consensus_for) { who = 'consensus · ' + who; }
    head.push(who);
  });
  var thead = el('thead');
  var htr = el('tr');
  head.forEach(function (h) { htr.appendChild(el('th', null, h)); });
  thead.appendChild(htr);
  var tbl = el('table', 'tbl sbs-tbl');
  tbl.appendChild(thead);
  var tb = el('tbody');

  var n = 0;
  if (src && src.length) { n = src.length; }
  else {
    var mx = 0;
    cols.forEach(function (o) {
      if (o.parsed && o.parsed.lines && o.parsed.lines.length > mx) { mx = o.parsed.lines.length; }
    });
    n = mx;
  }
  for (var i = 0; i < n; i++) {
    var tr = el('tr');
    var sline = src && src[i];
    tr.appendChild(el('td', 'v', sline ? sline.i : (i + 1)));
    var srcCell = el('td', 'src');
    srcCell.textContent = sline ? (sline.t || '') : '— source not recoverable';
    tr.appendChild(srcCell);
    cols.forEach(function (o) {
      var L = (o.parsed && o.parsed.lines) || [];
      var byI = null;
      for (var k = 0; k < L.length; k++) { if (L[k].i === i + 1) { byI = L[k]; break; } }
      tr.appendChild(el('td', 'tgt', byI ? (byI.t || '') : '—'));
    });
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  wrap.appendChild(tbl);
  if (!src) {
    wrap.appendChild(el('p', 'na note',
      'Source lines were not recoverable from this prompt, so only the ' +
      'translation is shown. No line offsets were guessed.'));
  }
  return wrap;
}

function renderBody(rec) {
  var wrap = el('div', 'body');
  if (!rec.paired) {
    wrap.appendChild(el('p', 'flag flag-bad',
      'unpaired: this call has a log line on one side only'));
  }
  var head = el('div', 'body-h');
  head.appendChild(el('code', null, rec.i));
  ['chapter', 'job', 'model'].forEach(function (k) {
    if (rec[k] !== undefined && rec[k] !== null) {
      head.appendChild(el('span', 'pill', k + ': ' + rec[k]));
    }
  });
  if (rec.candidate !== undefined && rec.candidate !== null) {
    head.appendChild(el('span', 'pill',
      'candidate ' + rec.candidate + ' of ' + rec.candidates));
  }
  if (rec.consensus_for) {
    head.appendChild(el('span', 'pill', 'consensus for ' + rec.consensus_for));
  }
  head.appendChild(el('span', 'pill pill-shape', 'shape: ' + rec.shape));
  wrap.appendChild(head);

  var out;
  if (rec.shape === 'lines') {
    out = sideBySide(rec);
  } else if (rec.shape === 'verdict' && rec.parsed) {
    out = el('div', 'verdict');
    var v = el('p', 'verdict-v ' + (rec.parsed.verdict === 'SUCCESS' ? 'ok' : 'bad'),
      String(rec.parsed.verdict));
    out.appendChild(v);
    var rs = rec.parsed.reasons || [];
    out.appendChild(el('p', 'na', rs.length + ' reason(s)'));
    if (rs.length) {
      var ul = el('ul', 'bullets');
      rs.forEach(function (r) { ul.appendChild(el('li', null, String(r))); });
      out.appendChild(ul);
    }
  } else if (rec.shape === 'recap' && rec.parsed) {
    out = el('div');
    out.appendChild(el('h4', 'sub-h', 'Recap'));
    out.appendChild(el('p', 'prose', rec.parsed.recap));
  } else if (rec.shape === 'profile' && rec.parsed) {
    out = el('div');
    [['style_summary', 'Style summary'], ['background', 'Background']].forEach(function (p) {
      if (rec.parsed[p[0]] !== undefined) {
        out.appendChild(el('h4', 'sub-h', p[1]));
        out.appendChild(el('p', 'prose', String(rec.parsed[p[0]])));
      }
    });
  } else if (rec.shape === 'merge' && rec.parsed) {
    out = el('div', 'merge');
    ['source', 'translation', 'category', 'definition'].forEach(function (k) {
      out.appendChild(el('span', 'pill', k + ': ' + rec.parsed[k]));
    });
  } else if (rec.shape === 'terms' && rec.parsed && Array.isArray(rec.parsed.terms)) {
    out = table(['source', 'translation', 'category', 'definition', 'variants'],
      rec.parsed.terms.map(function (t) {
        return [t.source, t.translation, t.category, t.definition,
                (t.variants || []).join(', ') || '—'];
      }));
  } else if (rec.shape === 'notes' && rec.parsed && Array.isArray(rec.parsed.notes)) {
    out = table(['line', 'term', 'category', 'note', 'threshold'],
      rec.parsed.notes.map(function (n) {
        return [n.line, n.term, n.category, n.note, n.threshold];
      }));
  } else if (rec.shape === 'cleanup' && rec.parsed && Array.isArray(rec.parsed.decisions)) {
    out = table(['source', 'keep', 'reason'],
      rec.parsed.decisions.map(function (d) {
        return [d.source, String(d.keep), d.reason];
      }));
  } else if (rec.parsed !== undefined && rec.parsed !== null) {
    out = generic(rec.parsed);
    wrap.appendChild(el('p', 'na note',
      'No dedicated view for this shape — rendered generically.'));
  } else {
    out = el('div');
    var pre = el('pre', 'raw');
    pre.textContent = (rec.body === undefined || rec.body === null)
      ? '(no body stored — log_prompt_bodies was off for this run)'
      : rec.body;
    out.appendChild(pre);
    if (typeof rec.body === 'string' && rec.body.length) {
      out.appendChild(el('p', 'na', rec.body.length + ' characters, not JSON'));
    }
  }
  wrap.appendChild(out);

  var det = el('details', 'rawwrap');
  det.appendChild(el('summary', null, 'raw response body'));
  var pre2 = el('pre', 'raw');
  pre2.textContent = rec.body === undefined || rec.body === null
    ? '(not stored)'
    : (rec.shape === 'text' ? rec.body : JSON.stringify(rec.parsed, null, 2));
  det.appendChild(pre2);
  wrap.appendChild(det);

  if (rec.prompt) {
    var pd = el('details', 'rawwrap');
    pd.appendChild(el('summary', null,
      'prompt as sent (' + rec.prompt.length + ' chars)'));
    var pp = el('pre', 'raw');
    pp.textContent = rec.prompt;
    pd.appendChild(pp);
    wrap.appendChild(pd);
  }
  if (rec.params) {
    var gd = el('details', 'rawwrap');
    gd.appendChild(el('summary', null, 'request params'));
    gd.appendChild(generic(rec.params));
    wrap.appendChild(gd);
  }
  return wrap;
}

function openCall(idx) {
  var rec = BODIES[idx];
  var existing = document.getElementById('rowhost' + idx);
  if (existing) {
    // Remove the WRAPPER, and via its own parentNode. querySelector matches at
    // any depth, so a `.body` panel is a grandchild of the cell -- calling
    // removeChild on the cell throws NotFoundError, which aborted the whole
    // expand/collapse loop and left "hide all" doing nothing at all.
    var openWrap = existing.querySelector('.bodywrap');
    if (openWrap) {
      openWrap.parentNode.removeChild(openWrap);
      existing.hidden = true;
      OPENED[idx] = false;
      var cbtn = document.querySelector('tr.callrow[data-idx="' + idx +
                                       '"] button[data-open]');
      if (cbtn) { cbtn.setAttribute('aria-expanded', 'false'); }
      return;
    }
  }
  if (!rec) { return; }
  var box = el('div', 'bodywrap');
  box.setAttribute('data-idx', String(idx));
  box.appendChild(renderBody(rec));
  var host = document.getElementById('rowhost' + idx);
  if (host) {
    host.hidden = false;
    host.firstChild.appendChild(box);
  } else {
    document.getElementById('callbodies').appendChild(box);
  }
  OPENED[idx] = true;
  var btn = document.querySelector('tr.callrow[data-idx="' + idx +
                                  '"] button[data-open]');
  if (btn) { btn.setAttribute('aria-expanded', 'true'); }
}

document.addEventListener('DOMContentLoaded', function () {
  var store = document.getElementById('dl-bodies');
  if (!store) { return; }
  try { BODIES = JSON.parse(store.textContent); }
  catch (e) {
    var host = document.getElementById('callbodies');
    if (host) {
      host.appendChild(el('p', 'flag flag-bad',
        'embedded call records could not be read: ' + e.message));
    }
    return;
  }
  BODIES.forEach(function (o, n) { o.__idx = n; o.__chapter = o.chapter; });
  document.querySelectorAll('tr.callrow').forEach(function (r) {
    var rec = BODIES[parseInt(r.getAttribute('data-idx'), 10)] || {};
    r.setAttribute('data-task', rec.task || '');
  });
  document.querySelectorAll('[data-open]').forEach(function (b) {
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openCall(parseInt(b.getAttribute('data-open'), 10));
    });
  });
  // Whole-row affordance. Three guards, each earning its place:
  //  - skip a click that came from the call-id button (stopPropagation above
  //    already stops it; this also covers a click on the <td> holding it);
  //  - skip while text is SELECTED, so dragging to copy a model name does not
  //    collapse the row out from under the selection;
  //  - Enter/Space toggle, because a click-only row is mouse-only. The
  //    <button> stays the accessible control and now reports aria-expanded, so
  //    this row handler is a pointer convenience layered on top of it rather
  //    than a second, divergent control.
  document.querySelectorAll('tr.callrow').forEach(function (r) {
    r.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('button, a, input')) { return; }
      var sel = window.getSelection ? window.getSelection().toString() : '';
      if (sel && sel.length) { return; }
      openCall(parseInt(r.getAttribute('data-idx'), 10));
    });
    r.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') {
        return;
      }
      ev.preventDefault();
      if (ev.target !== r) { return; }
      openCall(parseInt(r.getAttribute('data-idx'), 10));
    });
  });
  var q = document.getElementById('callq');
  if (q) {
    q.addEventListener('input', function () {
      var want = q.value.toLowerCase();
      document.querySelectorAll('tr.callrow').forEach(function (r) {
        var hay = (r.getAttribute('data-cid') + ' ' +
                   r.getAttribute('data-chapter') + ' ' +
                   r.getAttribute('data-job') + ' ' +
                   r.getAttribute('data-model') + ' ' +
                   r.getAttribute('data-endpoint') + ' ' +
                   r.getAttribute('data-shape')).toLowerCase();
        r.hidden = want !== '' && hay.indexOf(want) === -1;
      });
    });
  }
  var ea = document.getElementById('expandall');
  if (ea) {
    ea.addEventListener('click', function () {
      var on = ea.getAttribute('aria-pressed') !== 'true';
      ea.setAttribute('aria-pressed', String(on));
      ea.textContent = on ? 'hide all bodies' : 'show all bodies';
      document.querySelectorAll('tr.callrow').forEach(function (r) {
        if (r.hidden) { return; }
        var i = parseInt(r.getAttribute('data-idx'), 10);
        if (on && !OPENED[i]) { openCall(i); }
        if (!on && OPENED[i]) { openCall(i); }
      });
    });
  }
});
