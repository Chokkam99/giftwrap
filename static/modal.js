(function () {
  'use strict';

  // Shared product detail modal (desktop) / bottom-sheet (<=480px), used by both
  // the buyer chat (app.js) and the recipient grid (gift.js). Exposes
  // window.GiftModal.open({ product, budget, currency, ctaLabel, onApprove }).
  //
  // `product` only needs { id, merchant, title, price, currency, image_url } —
  // everything else (all images, description, variant chips, budget headroom)
  // is fetched live from GET /product?store=..&id=..&budget=.. (WP-BROWSE).

  var SWIPE_CLOSE_THRESHOLD = 90; // px of downward drag on the sheet to dismiss

  // Same curated labels as app.js / gift.js — kept small and duplicated rather
  // than coupling load order across the three plain <script> files.
  var STORE_LABELS = {
    'giva-jewelry.myshopify.com': 'GIVA',
    'mamaearth.in': 'Mamaearth',
    'salty.co.in': 'Salty',
    'plumgoodness.com': 'Plum',
    'xyxxcrew.com': 'XYXX',
  };

  function storeLabel(domain) {
    return STORE_LABELS[domain] || domain || '';
  }

  var backdrop = null;
  var panel = null;
  var state = null; // { product, images, index, detail, currency, onApprove }

  function formatPrice(price, currency) {
    if (price === undefined || price === null || price === '') return '';
    if (currency === 'INR') return '₹' + price;
    if (currency) return currency + ' ' + price;
    return String(price);
  }

  function clear(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function placeholderFrame() {
    var div = document.createElement('div');
    div.className = 'modal-image-placeholder';
    div.innerHTML =
      '<svg viewBox="0 0 24 24" width="44" height="44" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="3" y="9" width="18" height="12" rx="1.4"></rect><path d="M3 13h18"></path>' +
      '<path d="M12 9v12"></path><path d="M12 9C10.3 4.8 6 4.6 6 7.4 6 9 8 9 12 9Z"></path>' +
      '<path d="M12 9c1.7-4.2 6-4.4 6-1.6C18 9 16 9 12 9Z"></path></svg>';
    return div;
  }

  function ensureDom() {
    if (backdrop) return;
    backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.hidden = true;

    panel = document.createElement('div');
    panel.className = 'modal-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    backdrop.appendChild(panel);

    document.body.appendChild(backdrop);

    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !backdrop.hidden) close();
    });

    // Swipe-down-to-close on the sheet (mobile only, CSS gates the layout).
    var dragStartY = null;
    panel.addEventListener('touchstart', function (e) {
      if (e.target.closest('.modal-carousel')) return; // let carousel swipe own horizontal drags
      dragStartY = e.touches[0].clientY;
      panel.style.transition = 'none';
    }, { passive: true });
    panel.addEventListener('touchmove', function (e) {
      if (dragStartY === null) return;
      var dy = e.touches[0].clientY - dragStartY;
      if (dy > 0) panel.style.transform = 'translateY(' + dy + 'px)';
    }, { passive: true });
    panel.addEventListener('touchend', function (e) {
      if (dragStartY === null) return;
      var dy = (e.changedTouches[0].clientY - dragStartY);
      panel.style.transition = '';
      panel.style.transform = '';
      dragStartY = null;
      if (dy > SWIPE_CLOSE_THRESHOLD) close();
    });
  }

  function close() {
    if (!backdrop) return;
    backdrop.hidden = true;
    clear(panel);
    document.body.classList.remove('modal-open');
    state = null;
  }

  function renderCarousel() {
    var wrap = document.createElement('div');
    wrap.className = 'modal-carousel';

    var frame = document.createElement('div');
    frame.className = 'modal-carousel-frame';

    var images = state.images;
    if (!images.length) {
      frame.appendChild(placeholderFrame());
    } else {
      var img = document.createElement('img');
      img.className = 'modal-carousel-image';
      img.loading = 'lazy';
      img.src = images[state.index];
      img.alt = state.product.title || 'Product image';
      img.addEventListener('error', function () {
        img.replaceWith(placeholderFrame());
      });
      frame.appendChild(img);

      if (images.length > 1) {
        var prev = document.createElement('button');
        prev.type = 'button';
        prev.className = 'modal-carousel-arrow modal-carousel-arrow-prev';
        prev.setAttribute('aria-label', 'Previous image');
        prev.innerHTML = '&#8592;';
        prev.addEventListener('click', function () { step(-1); });
        frame.appendChild(prev);

        var next = document.createElement('button');
        next.type = 'button';
        next.className = 'modal-carousel-arrow modal-carousel-arrow-next';
        next.setAttribute('aria-label', 'Next image');
        next.innerHTML = '&#8594;';
        next.addEventListener('click', function () { step(1); });
        frame.appendChild(next);

        var touchStartX = null;
        frame.addEventListener('touchstart', function (e) {
          touchStartX = e.touches[0].clientX;
        }, { passive: true });
        frame.addEventListener('touchend', function (e) {
          if (touchStartX === null) return;
          var dx = e.changedTouches[0].clientX - touchStartX;
          touchStartX = null;
          if (Math.abs(dx) > 40) step(dx > 0 ? -1 : 1);
        });
      }
    }
    wrap.appendChild(frame);

    if (images.length > 1) {
      var dots = document.createElement('div');
      dots.className = 'modal-carousel-dots';
      images.forEach(function (_, i) {
        var dot = document.createElement('span');
        dot.className = 'modal-carousel-dot' + (i === state.index ? ' active' : '');
        dots.appendChild(dot);
      });
      wrap.appendChild(dots);
    }

    return wrap;
  }

  function step(delta) {
    var n = state.images.length;
    if (!n) return;
    state.index = (state.index + delta + n) % n;
    render();
  }

  function renderBody() {
    var body = document.createElement('div');
    body.className = 'modal-body';

    var titleRow = document.createElement('div');
    titleRow.className = 'modal-title-row';
    var title = document.createElement('h3');
    title.className = 'modal-title';
    title.textContent = state.product.title || 'Untitled gift';
    titleRow.appendChild(title);
    if (state.product.merchant) {
      var merchant = document.createElement('span');
      merchant.className = 'product-card-merchant modal-merchant';
      merchant.textContent = storeLabel(state.product.merchant);
      titleRow.appendChild(merchant);
    }
    body.appendChild(titleRow);

    var price = document.createElement('div');
    price.className = 'modal-price';
    price.textContent = formatPrice(state.currentPrice(), state.currency);
    body.appendChild(price);

    var headroom = state.detail && state.detail.budget_headroom;
    if (headroom) {
      var hLine = document.createElement('p');
      hLine.className = 'modal-headroom';
      hLine.textContent =
        formatPrice(headroom.spend.toFixed(2), headroom.currency) +
        ' — leaves ' + formatPrice(headroom.remaining.toFixed(2), headroom.currency) +
        ' of your ' + formatPrice(headroom.budget.toFixed(2), headroom.currency);
      body.appendChild(hLine);
    }

    if (state.detail === undefined) {
      var loading = document.createElement('p');
      loading.className = 'modal-loading';
      loading.textContent = 'Loading details…';
      body.appendChild(loading);
    } else if (state.detail === null) {
      var err = document.createElement('p');
      err.className = 'modal-error';
      err.textContent = "Couldn't load full details, but you can still gift this.";
      body.appendChild(err);
    } else {
      if (state.detail.description) {
        var descWrap = document.createElement('div');
        descWrap.className = 'modal-description-wrap';
        var desc = document.createElement('p');
        desc.className = 'modal-description clamped';
        desc.textContent = state.detail.description;
        descWrap.appendChild(desc);
        if (desc.textContent.length > 160) {
          var more = document.createElement('button');
          more.type = 'button';
          more.className = 'modal-more-btn';
          more.textContent = 'more';
          more.addEventListener('click', function () {
            var isClamped = desc.classList.toggle('clamped');
            more.textContent = isClamped ? 'more' : 'less';
          });
          descWrap.appendChild(more);
        }
        body.appendChild(descWrap);
      }

      if (state.detail.variants && state.detail.variants.length) {
        var chips = document.createElement('div');
        chips.className = 'modal-variants';
        state.detail.variants.forEach(function (variant, i) {
          var chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'variant-chip' + (i === state.variantIndex ? ' selected' : '');
          chip.textContent = variant.label;
          chip.disabled = !variant.available;
          if (!chip.disabled) {
            chip.addEventListener('click', function () {
              state.variantIndex = i;
              render();
            });
          }
          chips.appendChild(chip);
        });
        body.appendChild(chips);
      }
    }

    var cta = document.createElement('button');
    cta.type = 'button';
    cta.className = 'btn btn-primary modal-cta';
    cta.textContent = state.ctaLabel;
    cta.addEventListener('click', function () {
      close();
      state && state.onApprove(state.approvalProduct());
    });
    body.appendChild(cta);

    return body;
  }

  function render() {
    clear(panel);

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.innerHTML = '&times;';
    closeBtn.addEventListener('click', close);
    panel.appendChild(closeBtn);

    panel.appendChild(renderCarousel());
    panel.appendChild(renderBody());
  }

  function open(opts) {
    ensureDom();
    var product = opts.product || {};
    var initialImages = product.image_url ? [product.image_url] : [];

    state = {
      product: product,
      images: initialImages,
      index: 0,
      variantIndex: -1,
      detail: undefined, // undefined = loading, null = failed, object = loaded
      currency: opts.currency || product.currency || 'INR',
      ctaLabel: opts.ctaLabel || 'Gift this',
      onApprove: opts.onApprove || function () {},
      currentPrice: function () {
        if (this.detail && this.variantIndex >= 0 && this.detail.variants[this.variantIndex]) {
          return this.detail.variants[this.variantIndex].price;
        }
        return (this.detail && this.detail.price) || product.price;
      },
      approvalProduct: function () {
        var variant = this.detail && this.variantIndex >= 0
          ? this.detail.variants[this.variantIndex] : null;
        return {
          id: product.id,
          title: variant ? product.title + ' (' + variant.label + ')' : product.title,
          price: this.currentPrice(),
        };
      },
    };

    backdrop.hidden = false;
    document.body.classList.add('modal-open');
    render();

    var store = opts.store || product.merchant;
    if (!store || !product.id) return;
    var url = '/product?store=' + encodeURIComponent(store) + '&id=' + encodeURIComponent(product.id);
    if (opts.budget !== undefined && opts.budget !== null) {
      url += '&budget=' + encodeURIComponent(opts.budget);
    }
    fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (detail) {
        if (!state || state.product !== product) return; // modal moved on
        state.detail = detail;
        if (detail.images && detail.images.length) state.images = detail.images;
        // Default selection: cheapest in-stock, matching the server's pick.
        if (detail.variants && detail.variants.length) {
          var idx = detail.variants.findIndex(function (v) {
            return v.id === detail.variant_id;
          });
          state.variantIndex = idx >= 0 ? idx : 0;
        }
        render();
      })
      .catch(function () {
        if (!state || state.product !== product) return;
        state.detail = null;
        render();
      });
  }

  window.GiftModal = { open: open, close: close, storeLabel: storeLabel };
})();
