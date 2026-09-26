(function () {
  // ---- sidebar (mobile) ------------------------------------------------------ //
  // On narrow screens the button slides the sidebar over the page; on wide screens it collapses the column.
  var body = document.body, html = document.documentElement, menuBtn = document.getElementById('menuBtn'), scrim = document.getElementById('scrim');
  var narrow = window.matchMedia('(max-width: 860px)');
  function syncExpanded() {
    var open = narrow.matches ? body.classList.contains('nav-open') : html.getAttribute('data-sidebar') !== 'collapsed';
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  function closeNav() { body.classList.remove('nav-open'); syncExpanded(); }
  menuBtn.addEventListener('click', function () {
    if (narrow.matches) { body.classList.toggle('nav-open'); }
    else {
      var collapsed = html.getAttribute('data-sidebar') !== 'collapsed';
      if (collapsed) html.setAttribute('data-sidebar', 'collapsed'); else html.removeAttribute('data-sidebar');
      try { localStorage.setItem('sidebar', collapsed ? 'collapsed' : 'open'); } catch (e) {}
    }
    syncExpanded();
  });
  scrim.addEventListener('click', closeNav);
  if (narrow.addEventListener) narrow.addEventListener('change', syncExpanded); else if (narrow.addListener) narrow.addListener(syncExpanded);
  syncExpanded();

  // ---- theme toggle ------------------------------------------------------------ //
  var root = document.documentElement, themeBtn = document.getElementById('themeBtn');
  function applyTheme(t) {
    root.setAttribute('data-theme', t);
    themeBtn.setAttribute('aria-label', t === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  }
  applyTheme(root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');
  themeBtn.addEventListener('click', function () {
    var t = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(t);
    try { localStorage.setItem('theme', t); } catch (e) {}
  });
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onChange = function (e) {
      var saved = null; try { saved = localStorage.getItem('theme'); } catch (err) {}
      if (!saved) applyTheme(e.matches ? 'dark' : 'light');
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange); else if (mq.addListener) mq.addListener(onChange);
  }

  // ---- expandable nav ---------------------------------------------------------- //
  document.querySelectorAll('.nav-item').forEach(function (item) {
    var btn = item.querySelector('.nav-toggle');
    if (item.classList.contains('active')) item.classList.add('open');
    btn.addEventListener('click', function (e) { e.preventDefault(); item.classList.toggle('open'); });
  });
  var active = document.querySelector('.nav-item.active');
  if (active) active.scrollIntoView({ block: 'nearest' });

  // ---- search ----------------------------------------------------------------------- //
  var input = document.getElementById('searchInput'), results = document.getElementById('searchResults');
  var index = null, sel = -1;
  function loadIndex(cb) {
    if (index) return cb(index);
    index = window.SEARCH_INDEX || [];
    cb(index);
  }
  function esc(s) { return s.replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function highlight(text, q) {
    var i = text.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return esc(text);
    return esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.length)) + '</mark>' + esc(text.slice(i + q.length));
  }
  function snippet(body, q) {
    var i = body.toLowerCase().indexOf(q.toLowerCase()); if (i < 0) return esc(body.slice(0, 140));
    var a = Math.max(0, i - 60), b = Math.min(body.length, i + q.length + 90);
    return (a > 0 ? '… ' : '') + highlight(body.slice(a, b), q) + (b < body.length ? ' …' : '');
  }
  function render(q) {
    q = q.trim();
    if (q.length < 2) { results.hidden = true; return; }
    loadIndex(function (idx) {
      var terms = q.toLowerCase().split(/\s+/), hits = [];
      for (var i = 0; i < idx.length && hits.length < 200; i++) {
        var e = idx[i], hay = (e.t + ' ' + (e.b || '') + ' ' + e.p).toLowerCase(), ok = true, score = 0;
        for (var t = 0; t < terms.length; t++) {
          var pos = hay.indexOf(terms[t]); if (pos < 0) { ok = false; break; }
          score += (e.t.toLowerCase().indexOf(terms[t]) > -1 ? 6 : 1) + (pos < 200 ? 2 : 0);
        }
        if (ok) hits.push({ e: e, s: score });
      }
      hits.sort(function (a, b) { return b.s - a.s; });
      hits = hits.slice(0, 12);
      if (!hits.length) { results.innerHTML = '<div class="none">No matches for “' + esc(q) + '”.</div>'; }
      else results.innerHTML = hits.map(function (h) {
        return '<a href="' + h.e.u + '" role="option"><div class="rp">' + esc(h.e.p) + '</div><div class="rt">' + highlight(h.e.t, terms[0]) + '</div></a>';
      }).join('');
      sel = -1; results.hidden = false;
    });
  }
  input.addEventListener('input', function () { render(input.value); });
  input.addEventListener('focus', function () { if (input.value.trim().length >= 2) render(input.value); });
  input.addEventListener('keydown', function (e) {
    var items = results.querySelectorAll('a');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); if (!items.length) return;
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items.forEach(function (a, i) { a.classList.toggle('sel', i === sel); });
      items[sel].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && sel >= 0 && items[sel]) { window.location.href = items[sel].getAttribute('href'); }
    else if (e.key === 'Escape') { results.hidden = true; input.blur(); }
  });
  document.addEventListener('click', function (e) { if (!e.target.closest('#search')) results.hidden = true; });
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== input && !/input|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); input.focus(); }
  });

  // ---- image viewer (click a figure to enlarge, then zoom and pan) ---------------- //
  var figImgs = document.querySelectorAll('.figure img');
  if (figImgs.length) {
    var ICON = {
      minus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/></svg>',
      plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
      fit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
      close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'
    };
    var lb = document.createElement('div');
    lb.className = 'lb'; lb.hidden = true;
    lb.setAttribute('role', 'dialog'); lb.setAttribute('aria-modal', 'true'); lb.setAttribute('aria-label', 'Image viewer');
    lb.innerHTML = '<div class="lb-bar"><div class="lb-title"></div>' +
      '<button class="lb-btn" data-act="out" type="button" aria-label="Zoom out" title="Zoom out (−)">' + ICON.minus + '</button>' +
      '<button class="lb-btn lb-zoom" data-act="fit" type="button" aria-label="Reset zoom" title="Reset (0)">100%</button>' +
      '<button class="lb-btn" data-act="in" type="button" aria-label="Zoom in" title="Zoom in (+)">' + ICON.plus + '</button>' +
      '<button class="lb-btn" data-act="fit" type="button" aria-label="Fit to screen" title="Fit to screen (0)">' + ICON.fit + '</button>' +
      '<button class="lb-btn" data-act="close" type="button" aria-label="Close" title="Close (Esc)">' + ICON.close + '</button></div>' +
      '<div class="lb-stage"><img alt=""></div>' +
      '<div class="lb-hint">Scroll or pinch to zoom · drag to move · double-click to zoom in · Esc to close</div>';
    document.body.appendChild(lb);
    var stage = lb.querySelector('.lb-stage'), view = stage.querySelector('img');
    var titleEl = lb.querySelector('.lb-title'), zoomEl = lb.querySelector('.lb-zoom');
    // s = zoom relative to "fit to screen"; x, y = image offset inside the stage (px).
    var fitW = 0, fitH = 0, s = 1, x = 0, y = 0, MIN = 0.5, MAX = 12, opener = null;

    function draw() {
      view.style.width = (fitW * s) + 'px'; view.style.height = (fitH * s) + 'px';
      view.style.transform = 'translate(' + x + 'px,' + y + 'px)';
      zoomEl.textContent = Math.round(s * 100) + '%';
    }
    function fit() {
      var r = stage.getBoundingClientRect(), pad = Math.min(48, r.width * 0.04);
      // The on-page size gives the true aspect ratio; SVGs sized with width="100%" can report a bogus natural size.
      var ratio = opener.offsetWidth && opener.offsetHeight ? opener.offsetWidth / opener.offsetHeight : opener.naturalWidth / opener.naturalHeight;
      fitW = Math.min(r.width - pad * 2, (r.height - pad * 2) * ratio); fitH = fitW / ratio;
      s = 1; x = (r.width - fitW) / 2; y = (r.height - fitH) / 2; lastW = r.width; lastH = r.height; draw();
    }
    function zoomAt(ns, px, py) {
      ns = Math.max(MIN, Math.min(MAX, ns));
      x = px - (px - x) * (ns / s); y = py - (py - y) * (ns / s); s = ns; draw();
    }
    function zoomCenter(f) { var r = stage.getBoundingClientRect(); zoomAt(s * f, r.width / 2, r.height / 2); }
    function local(e) { var r = stage.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }

    function open(img) {
      opener = img;
      view.src = img.currentSrc || img.src; view.alt = img.alt;
      var cap = img.closest('figure') && img.closest('figure').querySelector('figcaption');
      titleEl.textContent = cap ? cap.textContent.trim() : img.alt;
      lb.hidden = false; html.style.overflow = 'hidden';
      fit(); lb.querySelector('[data-act="close"]').focus();
    }
    function close() {
      lb.hidden = true; html.style.overflow = ''; view.removeAttribute('src');
      if (opener) opener.focus();
    }

    figImgs.forEach(function (img) {
      img.tabIndex = 0; img.setAttribute('role', 'button'); img.title = 'Click to enlarge';
      img.addEventListener('click', function () { open(img); });
      img.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(img); } });
    });
    lb.querySelector('.lb-bar').addEventListener('click', function (e) {
      var b = e.target.closest('[data-act]'); if (!b) return;
      var a = b.getAttribute('data-act');
      if (a === 'in') zoomCenter(1.4); else if (a === 'out') zoomCenter(1 / 1.4); else if (a === 'fit') fit(); else close();
    });
    stage.addEventListener('wheel', function (e) {
      e.preventDefault(); var p = local(e);
      zoomAt(s * Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), p.x, p.y);
    }, { passive: false });
    stage.addEventListener('dblclick', function (e) { var p = local(e); if (s < 1.9) zoomAt(2.5, p.x, p.y); else fit(); });

    // Drag with one pointer, pinch with two. A click on the backdrop without dragging closes.
    var pts = {}, last = null, pinch = null, moved = false;
    function ptList() { return Object.keys(pts).map(function (k) { return pts[k]; }); }
    stage.addEventListener('pointerdown', function (e) {
      stage.setPointerCapture(e.pointerId); pts[e.pointerId] = local(e);
      var l = ptList(); moved = false; stage.classList.add('dragging');
      if (l.length === 1) last = l[0];
      if (l.length === 2) pinch = { d: Math.hypot(l[0].x - l[1].x, l[0].y - l[1].y), s: s };
    });
    stage.addEventListener('pointermove', function (e) {
      if (!pts[e.pointerId]) return;
      pts[e.pointerId] = local(e); var l = ptList();
      if (l.length === 2 && pinch) {
        var d = Math.hypot(l[0].x - l[1].x, l[0].y - l[1].y);
        zoomAt(pinch.s * d / pinch.d, (l[0].x + l[1].x) / 2, (l[0].y + l[1].y) / 2); moved = true;
      } else if (l.length === 1 && last) {
        var dx = l[0].x - last.x, dy = l[0].y - last.y;
        if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
        x += dx; y += dy; last = l[0]; draw();
      }
    });
    function up(e) {
      var wasTap = !moved && ptList().length === 1 && e.type === 'pointerup' && e.target === stage;
      delete pts[e.pointerId]; var l = ptList();
      pinch = null; last = l.length === 1 ? l[0] : null;
      if (!l.length) stage.classList.remove('dragging');
      if (wasTap) close();
    }
    stage.addEventListener('pointerup', up); stage.addEventListener('pointercancel', up);

    document.addEventListener('keydown', function (e) {
      if (lb.hidden) return;
      var k = e.key, step = 60;
      if (k === 'Escape') close();
      else if (k === '+' || k === '=') zoomCenter(1.4);
      else if (k === '-' || k === '_') zoomCenter(1 / 1.4);
      else if (k === '0') fit();
      else if (k === 'ArrowLeft') { x += step; draw(); } else if (k === 'ArrowRight') { x -= step; draw(); }
      else if (k === 'ArrowUp') { y += step; draw(); } else if (k === 'ArrowDown') { y -= step; draw(); }
      else if (k === 'Tab') { // keep focus inside the viewer
        var f = lb.querySelectorAll('button'), first = f[0], lastB = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); lastB.focus(); }
        else if (!e.shiftKey && document.activeElement === lastB) { e.preventDefault(); first.focus(); }
        return;
      } else return;
      e.preventDefault(); e.stopPropagation();
    }, true);
    // Refit on resize only when not zoomed, so a phone's toolbar showing or hiding doesn't reset the view.
    var lastW = 0, lastH = 0;
    window.addEventListener('resize', function () {
      if (lb.hidden) return;
      var r = stage.getBoundingClientRect();
      if (s === 1) fit(); else { x += (r.width - lastW) / 2; y += (r.height - lastH) / 2; draw(); }
      lastW = r.width; lastH = r.height;
    });
  }

  // ---- scroll spy for right rail ------------------------------------------------- //
  var tocLinks = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  if (tocLinks.length && 'IntersectionObserver' in window) {
    var map = {}, headings = [];
    tocLinks.forEach(function (a) { var el = document.getElementById(a.getAttribute('href').slice(1)); if (el) { map[el.id] = a; headings.push(el); } });
    var current = null;
    function setActive(id) {
      if (current === id) return; current = id;
      tocLinks.forEach(function (a) { a.classList.toggle('active', a.getAttribute('href') === '#' + id); });
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) setActive(en.target.id); });
    }, { rootMargin: '-64px 0px -70% 0px', threshold: 0 });
    headings.forEach(function (h) { io.observe(h); });
  }
})();
