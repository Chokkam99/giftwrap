(function () {
  'use strict';

  var main = document.getElementById('gift-main');
  var token = window.location.pathname.split('/').filter(Boolean).pop();

  var SUGGESTIONS = ['Something sparkly', 'A cozy pick', 'Skincare', 'Surprise me'];

  var state = {
    budget: null,
    currency: 'INR',
    note: '',
    status: 'awaiting_pick',
    pickedProduct: null,
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

  function renderReveal() {
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

    var budgetLine = document.createElement('p');
    budgetLine.className = 'gift-budget-line';
    budgetLine.textContent = 'Pick anything up to ' + formatPrice(state.budget, state.currency);
    hero.appendChild(budgetLine);

    main.appendChild(hero);

    var searchWrap = document.createElement('div');
    searchWrap.className = 'gift-search-wrap';

    var form = document.createElement('form');
    form.className = 'gift-search-form';

    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'gift-search-input';
    input.placeholder = 'What are you in the mood for?';
    input.setAttribute('aria-label', 'Search for a gift');
    form.appendChild(input);

    var searchBtn = document.createElement('button');
    searchBtn.type = 'submit';
    searchBtn.className = 'gift-search-btn';
    searchBtn.textContent = 'Search';
    form.appendChild(searchBtn);

    searchWrap.appendChild(form);

    var chips = document.createElement('div');
    chips.className = 'welcome-suggestions gift-chips';
    SUGGESTIONS.forEach(function (text) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'suggestion-chip';
      chip.textContent = text;
      chip.addEventListener('click', function () {
        input.value = text;
        doSearch(text);
      });
      chips.appendChild(chip);
    });
    searchWrap.appendChild(chips);

    main.appendChild(searchWrap);

    var grid = document.createElement('div');
    grid.className = 'gift-grid';
    grid.id = 'gift-grid';
    main.appendChild(grid);

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      doSearch(input.value);
    });
  }

  function renderGrid(products) {
    var grid = document.getElementById('gift-grid');
    if (!grid) return;
    clear(grid);

    if (!products || !products.length) {
      var empty = document.createElement('p');
      empty.className = 'gift-empty';
      empty.textContent = 'Nothing found in budget — try another search.';
      grid.appendChild(empty);
      return;
    }

    products.forEach(function (product, index) {
      var card = document.createElement('div');
      card.className = 'product-card';
      card.style.animationDelay = index * 60 + 'ms';

      if (product.image_url) {
        var img = document.createElement('img');
        img.className = 'product-card-image';
        img.src = product.image_url;
        img.alt = product.title || 'Product image';
        img.addEventListener('error', function () {
          img.replaceWith(placeholderImage());
        });
        card.appendChild(img);
      } else {
        card.appendChild(placeholderImage());
      }

      var body = document.createElement('div');
      body.className = 'product-card-body';

      var title = document.createElement('div');
      title.className = 'product-card-title';
      title.textContent = product.title || 'Untitled gift';
      body.appendChild(title);

      if (product.merchant) {
        var merchant = document.createElement('span');
        merchant.className = 'product-card-merchant';
        merchant.textContent = product.merchant;
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
      pickBtn.addEventListener('click', function () {
        pickBtn.disabled = true;
        pickBtn.textContent = 'Picking…';
        doPick(product, pickBtn);
      });
      body.appendChild(pickBtn);

      card.appendChild(body);
      grid.appendChild(card);
    });
  }

  // ---------- network ----------

  function doSearch(query) {
    var trimmed = (query || '').trim();
    if (!trimmed) return;
    var grid = document.getElementById('gift-grid');
    if (grid) {
      clear(grid);
      var loading = document.createElement('p');
      loading.className = 'gift-loading';
      loading.textContent = 'Searching…';
      grid.appendChild(loading);
    }
    fetch('/gift/' + token + '/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: trimmed }),
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        renderGrid(data && data.products);
      })
      .catch(function () {
        renderGrid([]);
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
        if (state.status === 'picked') {
          renderPicked(state.pickedProduct);
        } else {
          renderReveal();
        }
      })
      .catch(function () {
        renderError();
      });
  }

  init();
})();
