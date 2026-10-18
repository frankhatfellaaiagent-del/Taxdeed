/* Read-only landing preview: no authentication, storage, or workspace writes. */
(function () {
  'use strict';
  var search = document.getElementById('auction-search');
  var filters = document.getElementById('county-filters');
  var grid = document.getElementById('auction-results');
  var count = document.getElementById('result-count');
  var feedDate = document.getElementById('feed-date');
  var more = document.getElementById('show-more');
  var retry = document.getElementById('retry-feed');
  var clear = document.getElementById('clear-filters');
  var pageSize = window.matchMedia('(max-width: 700px)').matches ? 3 : 6;
  var rows = [], county = '', limit = pageSize, countyNames = Object.create(null);
  var fallbackNames = {indianriver:'Indian River',miamidade:'Miami-Dade',palmbeach:'Palm Beach',santarosa:'Santa Rosa',stjohns:'St. Johns',stlucie:'St. Lucie',desoto:'DeSoto'};
  var dateFormat = new Intl.DateTimeFormat('en-US', {month:'short',day:'numeric',year:'numeric',timeZone:'UTC'});

  function escapeHTML(value) {
    return String(value).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; });
  }
  function name(slug) {
    return countyNames[slug] || (Object.prototype.hasOwnProperty.call(fallbackNames, slug) ? fallbackNames[slug] : '') || (slug ? slug.charAt(0).toUpperCase() + slug.slice(1) : 'County not published');
  }
  function money(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? '$' + Math.round(value).toLocaleString('en-US') : '—';
  }
  function saleDate(value) {
    var match = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(value || '');
    if (!match) return null;
    var year = Number(match[3]), month = Number(match[1]), day = Number(match[2]);
    var date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
    return date;
  }
  function todayET() {
    var parts = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
    var values = {};
    parts.forEach(function (part) { values[part.type] = part.value; });
    return Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day));
  }
  function render() {
    var query = search.value.trim().toLowerCase();
    var matches = rows.filter(function (record) { return (!county || record.county === county) && (!query || record.search.indexOf(query) !== -1); });
    var shown = matches.slice(0, limit);
    count.textContent = matches.length.toLocaleString('en-US') + (matches.length === 1 ? ' upcoming record' : ' upcoming records') + (shown.length < matches.length ? ' · showing ' + shown.length : '');
    clear.hidden = !query && !county;
    more.hidden = shown.length >= matches.length;
    grid.innerHTML = shown.length ? shown.map(function (record) {
      var address = record.address || 'Parcel ' + (record.parcel || 'number not published');
      return '<article class="auction-card" tabindex="-1"><div class="auction-card-header"><span class="auction-county">' + escapeHTML(name(record.county)) + '</span><time class="auction-date" datetime="' + record.date.toISOString().slice(0,10) + '">' + escapeHTML(dateFormat.format(record.date)) + '</time></div><h3>' + escapeHTML(address) + '</h3><p class="parcel-number">' + (record.address && record.parcel ? 'Parcel ' + escapeHTML(record.parcel) : 'County-source auction record') + '</p><dl class="auction-values"><div><dt>Opening bid</dt><dd>' + money(record.bid) + '</dd></div><div><dt>Assessed value</dt><dd>' + money(record.assessed) + '</dd></div></dl></article>';
    }).join('') : '<p class="preview-message">' + (rows.length ? 'No records match these filters. Try another county or clear your search.' : 'No upcoming scheduled records are available in this feed. Check the county directly or open the board.') + '</p>';
  }
  function buildFilters() {
    var totals = Object.create(null);
    rows.forEach(function (record) { if (record.county) totals[record.county] = (totals[record.county] || 0) + 1; });
    var top = Object.keys(totals).sort(function (a,b) { return totals[b] - totals[a] || name(a).localeCompare(name(b)); }).slice(0,5);
    filters.replaceChildren();
    [''].concat(top).forEach(function (slug) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'county-chip';
      button.textContent = slug ? name(slug) : 'All counties';
      button.setAttribute('aria-pressed', String(slug === county));
      button.addEventListener('click', function () {
        county = slug; limit = pageSize;
        Array.prototype.forEach.call(filters.children, function (child) { child.setAttribute('aria-pressed', String(child === button)); });
        render();
      });
      filters.appendChild(button);
    });
  }
  async function load() {
    grid.setAttribute('aria-busy', 'true');
    retry.hidden = true; more.hidden = true; clear.hidden = true;
    search.disabled = true; search.value = ''; county = ''; limit = pageSize;
    filters.replaceChildren();
    count.textContent = 'Loading auction records…';
    feedDate.textContent = 'Feed date will appear here';
    grid.innerHTML = '<div class="preview-message"><span class="loading-mark" aria-hidden="true"></span>Finding upcoming records…</div>';
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 12000);
    try {
      var response = await fetch('app/data/exports/master_list.json', {cache:'no-cache',signal:controller.signal});
      if (!response.ok) throw new Error('Feed unavailable');
      var data = await response.json();
      if (!data || !Array.isArray(data.records)) throw new Error('Invalid feed');
      countyNames = Object.create(null);
      (Array.isArray(data.counties_registry) ? data.counties_registry : []).forEach(function (entry) {
        if (entry && typeof entry.slug === 'string' && typeof entry.name === 'string') countyNames[entry.slug] = entry.name;
      });
      var today = todayET();
      rows = data.records.filter(function (record) { return record && record.status === 'Scheduled'; }).map(function (record) {
        var slug = typeof record.county === 'string' ? record.county : '';
        var address = typeof record.property_address === 'string' ? record.property_address : '';
        var parcel = typeof record.parcel_id === 'string' ? record.parcel_id : '';
        return {county:slug,address:address,parcel:parcel,date:saleDate(record.sale_date),bid:record.opening_bid,assessed:record.assessed_value,search:(name(slug) + ' ' + address + ' ' + parcel).toLowerCase()};
      }).filter(function (record) { return record.date && record.date.getTime() >= today; }).sort(function (a,b) {
        return a.date - b.date || Number(!!b.address) - Number(!!a.address) || name(a.county).localeCompare(name(b.county));
      });
      var generated = new Date(data.generated_at);
      feedDate.textContent = !data.generated_at || Number.isNaN(generated.getTime()) ? 'Feed update date unavailable' : 'Feed updated ' + new Intl.DateTimeFormat('en-US', {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',timeZone:'America/New_York',timeZoneName:'short'}).format(generated);
      search.disabled = false;
      buildFilters(); render();
    } catch (error) {
      rows = [];
      count.textContent = 'Auction preview unavailable';
      feedDate.textContent = 'Feed update could not be checked';
      grid.innerHTML = '<p class="preview-message">We couldn’t load the auction feed. Try again, or <a href="app/">open the auction board</a> to continue.</p>';
      retry.hidden = false;
    } finally {
      clearTimeout(timeout);
      grid.setAttribute('aria-busy', 'false');
    }
  }
  search.addEventListener('input', function () { limit = pageSize; render(); });
  clear.addEventListener('click', function () {
    search.value = ''; county = ''; limit = pageSize;
    Array.prototype.forEach.call(filters.children, function (button, i) { button.setAttribute('aria-pressed', String(i === 0)); });
    render(); search.focus();
  });
  more.addEventListener('click', function () {
    var previous = grid.children.length;
    limit += pageSize; render();
    if (grid.children[previous]) grid.children[previous].focus({preventScroll:true});
  });
  retry.addEventListener('click', load);
  // The full feed is several MB. Keep it off the initial rendering path.
  if ('IntersectionObserver' in window) {
    var observer = new IntersectionObserver(function (entries) {
      if (entries.some(function (entry) { return entry.isIntersecting; })) {
        observer.disconnect();
        load();
      }
    }, {rootMargin:'400px'});
    observer.observe(document.getElementById('preview'));
  } else {
    load();
  }
})();
