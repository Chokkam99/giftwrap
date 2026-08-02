(function () {
  'use strict';

  var main = document.getElementById('gift-main');
  var token = window.location.pathname.split('/').filter(Boolean).pop();

  // Quick-filter chips just set the search query — no new backend concept.
  var QUICK_FILTERS = [
    { label: 'All', query: 'gift' },
    { label: 'Jewelry', query: 'jewelry' },
    { label: 'Skincare', query: 'skincare' },
    { label: 'Self-care', query: 'self care' },
    { label: 'Accessories', query: 'accessories' },
  ];

  var STORE_LABELS = {
    'giva-jewelry.myshopify.com': 'GIVA',
    'mamaearth.in': 'Mamaearth',
    'salty.co.in': 'Salty',
    'plumgoodness.com': 'Plum',
    'xyxxcrew.com': 'XYXX',
  };

  function storeLabel(domain) {
    return STORE_LABELS[domain] || domain;
  }

  var state = {
    budget: null,
    currency: 'INR',
    note: '',
    status: 'awaiting_pick',
    pickedProduct: null,
    stores: [],
    activeFilter: QUICK_FILTERS[0].label,
    selectedStore: '', // '' = all stores
    sort: 'relevance',
    query: QUICK_FILTERS[0].query,
    products: [], // all products loaded for the current query+store, unsorted order
    hasMore: false,
    loading: false,
    searchFailed: false, // true only when the /search request itself errored
  };

  // ---------- helpers ----------

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function formatPrice(price, currency) {
    if (price === undefined || price === null || price === '') return '';
    if (currency === 'INR') return '₹' + price;
    if (currency) return currency + ' ' + price;
    return String(price);
  }

  function placeholderImage() {
    var div = document.createElement('div');
    div.className = 'product-card-image-placeholder';
    div.innerHTML =
      '<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="3" y="9" width="18" height="12" rx="1.4"></rect><path d="M3 13h18"></path>' +
      '<path d="M12 9v12"></path><path d="M12 9C10.3 4.8 6 4.6 6 7.4 6 9 8 9 12 9Z"></path>' +
      '<path d="M12 9c1.7-4.2 6-4.4 6-1.6C18 9 16 9 12 9Z"></path></svg>';
    return div;
  }

  // ---------- render states ----------

  function renderError(message) {
    clear(main);
    var card = document.createElement('div');
    card.className = 'gift-card gift-error';
    var icon = document.createElement('div');
    icon.className = 'gift-error-icon';
    icon.textContent = '!';
    card.appendChild(icon);
    var h = document.createElement('h2');
    h.textContent = "This link isn't working";
    card.appendChild(h);
    var p = document.createElement('p');
    p.textContent = message || 'It may have expired, already been used, or the link is incorrect.';
    card.appendChild(p);
    main.appendChild(card);
  }

  function renderPicked(product) {
    clear(main);
    var card = document.createElement('div');
    card.className = 'receipt-card gift-picked-card';

    var check = document.createElement('div');
    check.className = 'receipt-checkmark';
    check.innerHTML =
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" ' +
      'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M20 6 9 17l-5-5"></path></svg>';
    card.appendChild(check);

    var title = document.createElement('div');
    title.className = 'receipt-title';
    title.textContent = 'Great choice!';
    card.appendChild(title);

    if (product) {
      var chosen = document.createElement('div');
      chosen.className = 'gift-picked-product';
      chosen.textContent = product.title +
        (product.price ? ' — ' + formatPrice(product.price, state.currency) : '');
      card.appendChild(chosen);
    }

    var footer = document.createElement('div');
    footer.className = 'receipt-footer';
    footer.textContent = "It's on its way once your gifter approves.";
    card.appendChild(footer);

    main.appendChild(card);
  }

  function sortedProducts() {
    var list = state.products.slice();
    if (state.sort === 'price-asc') {
      list.sort(function (a, b) { return Number(a.price) - Number(b.price); });
    } else if (state.sort === 'price-desc') {
      list.sort(function (a, b) { return Number(b.price) - Number(a.price); });
    }
    return list;
  }

  function renderBrowse() {
    clear(main);

    var hero = document.createElement('div');
    hero.className = 'welcome-card gift-hero';
    var icon = document.createElement('div');
    icon.className = 'gift-hero-icon';
    icon.textContent = '🎁';
    hero.appendChild(icon);
    var heading = document.createElement('h2');
    heading.textContent = 'Someone sent you a gift';
    hero.appendChild(heading);
    if (state.note) {
      var note = document.createElement('p');
      note.className = 'gift-note';
      note.textContent = '“' + state.note + '”';
      hero.appendChild(note);
    }
    main.appendChild(hero);

    // Pinned controls: budget line + quick filters + store toggle + sort.
    var controls = document.createElement('div');
    controls.className = 'gift-controls';

    var budgetLine = document.createElement('p');
    budgetLine.className = 'gift-budget-line';
    budgetLine.textContent = 'Pick anything up to ' + formatPrice(state.budget, state.currency);
    controls.appendChild(budgetLine);

    var filterRow = document.createElement('div');
    filterRow.className = 'gift-chips';
    QUICK_FILTERS.forEach(function (filter) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.dataset.filter = filter.label;
      chip.className = 'suggestion-chip' + (filter.label === state.activeFilter ? ' active' : '');
      chip.textContent = filter.label;
      chip.addEventListener('click', function () {
        state.activeFilter = filter.label;
        syncControlsActive();
        runSearch(filter.query);
      });
      filterRow.appendChild(chip);
    });
    controls.appendChild(filterRow);

    if (state.stores.length > 1) {
      var storeRow = document.createElement('div');
      storeRow.className = 'gift-store-toggle';
      var allBtn = document.createElement('button');
      allBtn.type = 'button';
      allBtn.dataset.store = '';
      allBtn.className = 'store-toggle-btn' + (state.selectedStore === '' ? ' active' : '');
      allBtn.textContent = 'All stores';
      allBtn.addEventListener('click', function () {
        state.selectedStore = '';
        syncControlsActive();
        runSearch(state.query);
      });
      storeRow.appendChild(allBtn);
      state.stores.forEach(function (store) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.dataset.store = store;
        btn.className = 'store-toggle-btn' + (state.selectedStore === store ? ' active' : '');
        btn.textContent = storeLabel(store);
        btn.addEventListener('click', function () {
          state.selectedStore = store;
          syncControlsActive();
          runSearch(state.query);
        });
        storeRow.appendChild(btn);
      });
      controls.appendChild(storeRow);
    }

    var sortRow = document.createElement('div');
    sortRow.className = 'gift-sort-row';
    var sortLabel = document.createElement('label');
    sortLabel.className = 'gift-sort-label';
    sortLabel.textContent = 'Sort';
    var sortSelect = document.createElement('select');
    sortSelect.className = 'gift-sort-select';
    [
      ['relevance', 'Relevance'],
      ['price-asc', 'Price: low to high'],
      ['price-desc', 'Price: high to low'],
    ].forEach(function (opt) {
      var o = document.createElement('option');
      o.value = opt[0];
      o.textContent = opt[1];
      if (opt[0] === state.sort) o.selected = true;
      sortSelect.appendChild(o);
    });
    sortSelect.addEventListener('change', function () {
      state.sort = sortSelect.value;
      renderGrid();
    });
    sortLabel.appendChild(sortSelect);
    sortRow.appendChild(sortLabel);
    controls.appendChild(sortRow);

    main.appendChild(controls);

    var grid = document.createElement('div');
    grid.className = 'gift-grid';
    grid.id = 'gift-grid';
    main.appendChild(grid);

    var loadMoreWrap = document.createElement('div');
    loadMoreWrap.className = 'gift-load-more-wrap';
    loadMoreWrap.id = 'gift-load-more-wrap';
    main.appendChild(loadMoreWrap);

    renderGrid();
  }

  function openDetail(product) {
    if (!window.GiftModal) return;
    window.GiftModal.open({
      product: product,
      store: product.merchant,
      budget: state.budget,
      currency: state.currency,
      ctaLabel: 'This one!',
      onApprove: function (approved) {
        doPick(approved, null);
      },
    });
  }

  function skeletonCard() {
    var card = document.createElement('div');
    card.className = 'skeleton-card';
    var img = document.createElement('div');
    img.className = 'skeleton-block skeleton-image';
    card.appendChild(img);
    var body = document.createElement('div');
    body.className = 'skeleton-body';
    var l1 = document.createElement('div');
    l1.className = 'skeleton-line w-100';
    var l2 = document.createElement('div');
    l2.className = 'skeleton-line w-70';
    var l3 = document.createElement('div');
    l3.className = 'skeleton-line w-40';
    body.appendChild(l1);
    body.appendChild(l2);
    body.appendChild(l3);
    card.appendChild(body);
    return card;
  }

  function buildCard(product, index) {
    var card = document.createElement('div');
    card.className = 'product-card';
    card.style.animationDelay = (index % 12) * 60 + 'ms';

    var frame = document.createElement('div');
    frame.className = 'product-card-image-frame';
    if (product.image_url) {
      var img = document.createElement('img');
      img.className = 'product-card-image';
      img.src = product.image_url;
      img.loading = 'lazy';
      img.alt = product.title || 'Product image';
      img.addEventListener('error', function () {
        img.replaceWith(placeholderImage());
      });
      frame.appendChild(img);
    } else {
      frame.appendChild(placeholderImage());
    }
    card.appendChild(frame);

    var body = document.createElement('div');
    body.className = 'product-card-body';

    var title = document.createElement('div');
    title.className = 'product-card-title';
    title.textContent = product.title || 'Untitled gift';
    body.appendChild(title);

    if (product.merchant) {
      var merchant = document.createElement('span');
      merchant.className = 'product-card-merchant';
      merchant.textContent = storeLabel(product.merchant);
      body.appendChild(merchant);
    }

    var price = document.createElement('div');
    price.className = 'product-card-price';
    price.textContent = formatPrice(product.price, state.currency);
    body.appendChild(price);

    var pickBtn = document.createElement('button');
    pickBtn.className = 'product-card-btn';
    pickBtn.type = 'button';
    pickBtn.textContent = 'This one!';
    pickBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      pickBtn.disabled = true;
      pickBtn.textContent = 'Picking…';
      doPick(product, pickBtn);
    });
    body.appendChild(pickBtn);

    card.appendChild(body);

    card.addEventListener('click', function (e) {
      if (e.target.closest('.product-card-btn')) return;
      openDetail(product);
    });

    return card;
  }

  function renderGrid() {
    var grid = document.getElementById('gift-grid');
    if (!grid) return;
    clear(grid);

    if (state.loading) {
      for (var i = 0; i < 8; i++) {
        grid.appendChild(skeletonCard());
      }
      renderLoadMore();
      return;
    }

    var products = sortedProducts();
    if (state.searchFailed) {
      var errWrap = document.createElement('div');
      errWrap.className = 'gift-search-error';
      var errIcon = document.createElement('div');
      errIcon.className = 'gift-search-error-icon';
      errIcon.textContent = '⚠';
      errWrap.appendChild(errIcon);
      var errMsg = document.createElement('p');
      errMsg.textContent = "Couldn't reach the shops just now.";
      errWrap.appendChild(errMsg);
      var errSub = document.createElement('p');
      errSub.className = 'gift-empty-sub';
      errSub.textContent = 'Check your connection and try that filter again.';
      errWrap.appendChild(errSub);
      var retryBtn = document.createElement('button');
      retryBtn.type = 'button';
      retryBtn.className = 'btn btn-secondary gift-load-more-btn';
      retryBtn.textContent = 'Try again';
      retryBtn.addEventListener('click', function () {
        runSearch(state.query);
      });
      errWrap.appendChild(retryBtn);
      grid.appendChild(errWrap);
    } else if (!products.length) {
      var empty = document.createElement('div');
      empty.className = 'gift-empty';
      var emptyIcon = document.createElement('div');
      emptyIcon.className = 'gift-empty-icon';
      emptyIcon.textContent = '🔍';
      empty.appendChild(emptyIcon);
      var emptyMsg = document.createElement('p');
      emptyMsg.textContent = 'Nothing found in budget.';
      empty.appendChild(emptyMsg);
      var emptySub = document.createElement('p');
      emptySub.className = 'gift-empty-sub';
      emptySub.textContent = 'Try another filter, store, or a wider search.';
      empty.appendChild(emptySub);
      grid.appendChild(empty);
    } else {
      products.forEach(function (product, index) {
        grid.appendChild(buildCard(product, index));
      });
    }

    renderLoadMore();
  }

  function renderLoadMore() {
    var wrap = document.getElementById('gift-load-more-wrap');
    if (!wrap) return;
    clear(wrap);
    if (!state.hasMore) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-secondary gift-load-more-btn';
    btn.textContent = 'Load more';
    btn.addEventListener('click', function () {
      btn.disabled = true;
      btn.textContent = 'Loading…';
      fetch('/gift/' + token + '/more', { method: 'POST' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          state.products = state.products.concat((data && data.products) || []);
          state.hasMore = !!(data && data.has_more);
          renderGrid();
        })
        .catch(function () {
          btn.disabled = false;
          btn.textContent = 'Load more';
        });
    });
    wrap.appendChild(btn);
  }

  // ---------- network ----------

  function runSearch(query) {
    var trimmed = (query || '').trim();
    if (!trimmed) return;
    state.query = trimmed;
    state.products = [];
    state.hasMore = false;
    state.loading = true;
    state.searchFailed = false;
    renderGrid();

    var body = { query: trimmed };
    if (state.selectedStore) body.store = state.selectedStore;

    fetch('/gift/' + token + '/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        state.loading = false;
        state.products = (data && data.products) || [];
        state.hasMore = !!(data && data.has_more);
        renderGrid();
      })
      .catch(function () {
        state.loading = false;
        state.products = [];
        state.hasMore = false;
        state.searchFailed = true;
        renderGrid();
      });
  }

  function doPick(product, pickBtn) {
    fetch('/gift/' + token + '/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        product_id: product.id,
        title: product.title,
        price: product.price,
      }),
    })
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok || result.data.ok === false) {
          window.alert((result.data && (result.data.error || result.data.detail)) ||
            'Could not record your pick.');
          if (pickBtn) {
            pickBtn.disabled = false;
            pickBtn.textContent = 'This one!';
          }
          return;
        }
        state.status = 'picked';
        state.pickedProduct = result.data.picked_product;
        renderPicked(state.pickedProduct);
      })
      .catch(function () {
        window.alert('Something went wrong recording your pick. Please try again.');
        if (pickBtn) {
          pickBtn.disabled = false;
          pickBtn.textContent = 'This one!';
        }
      });
  }

  function init() {
    fetch('/gift/' + token + '/info')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        state.budget = data.budget;
        state.currency = data.currency || 'INR';
        state.note = data.note || '';
        state.status = data.status;
        state.pickedProduct = data.picked_product;
        state.stores = data.stores || [];
        if (state.status === 'picked') {
          renderPicked(state.pickedProduct);
        } else {
          renderBrowse();
          runSearch(state.query); // browsing-first: load the grid immediately
        }
      })
      .catch(function () {
        renderError();
      });
  }

  init();
})();
