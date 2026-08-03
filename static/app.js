(function () {
  'use strict';

  var isMock = new URLSearchParams(window.location.search).get('mock') === '1';

  var chatLog = document.getElementById('chat-log');
  var typingIndicator = document.getElementById('typing-indicator');
  var form = document.getElementById('chat-form');
  var input = document.getElementById('chat-input');
  var sendBtn = document.getElementById('send-btn');
  var budgetChip = document.getElementById('budget-chip');
  var budgetAmount = document.getElementById('budget-amount');

  function makeUuid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      try {
        return window.crypto.randomUUID();
      } catch (e) {
        /* fall through to manual generation */
      }
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  var conversationId = makeUuid();

  // ---------- rendering helpers ----------

  function scrollToBottom() {
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function appendSafeInlineMarkdown(parent, text) {
    // This accepts only **bold**. Every other character is a text node, so
    // model output can never become HTML or a clickable URL in the chat.
    String(text || '').split(/(\*\*[^*\n]+\*\*)/g).forEach(function (part) {
      if (part.length > 4 && part.slice(0, 2) === '**' && part.slice(-2) === '**') {
        var strong = document.createElement('strong');
        strong.textContent = part.slice(2, -2);
        parent.appendChild(strong);
      } else if (part) {
        parent.appendChild(document.createTextNode(part));
      }
    });
  }

  function renderSafeAssistantMarkdown(bubble, text) {
    // Deliberately small Markdown subset: bold, blank lines, ordered lists,
    // and '-' bullets. It uses DOM nodes only — never innerHTML — and does
    // not linkify URLs because product cards and modals own navigation.
    var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    var i = 0;
    while (i < lines.length) {
      var ordered = lines[i].match(/^\s*\d+\.\s+(.+)$/);
      var bullet = lines[i].match(/^\s*-\s+(.+)$/);
      if (ordered || bullet) {
        var list = document.createElement(ordered ? 'ol' : 'ul');
        list.className = 'chat-markdown-list';
        while (i < lines.length) {
          var match = ordered
            ? lines[i].match(/^\s*\d+\.\s+(.+)$/)
            : lines[i].match(/^\s*-\s+(.+)$/);
          if (!match) break;
          var item = document.createElement('li');
          appendSafeInlineMarkdown(item, match[1]);
          list.appendChild(item);
          i += 1;
        }
        bubble.appendChild(list);
        continue;
      }

      if (lines[i] === '') {
        bubble.appendChild(document.createElement('br'));
      } else {
        appendSafeInlineMarkdown(bubble, lines[i]);
        if (i < lines.length - 1) bubble.appendChild(document.createElement('br'));
      }
      i += 1;
    }
  }

  function addBubble(role, text) {
    var row = document.createElement('div');
    row.className = 'msg-row ' + role;

    var group = document.createElement('div');
    group.className = 'msg-group';

    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    if (role === 'agent') {
      renderSafeAssistantMarkdown(bubble, text);
    } else {
      bubble.textContent = text || '';
    }
    group.appendChild(bubble);

    row.appendChild(group);
    chatLog.appendChild(row);
    scrollToBottom();
    return group;
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

  var currentBudget = null; // numeric budget, tracked for the modal's headroom fetch

  function storeLabel(domain) {
    return (window.GiftModal && window.GiftModal.storeLabel)
      ? window.GiftModal.storeLabel(domain) : domain;
  }

  function approveProduct(product) {
    var price = formatPrice(product.price, product.currency);
    sendMessage("I'd like the " + (product.title || 'gift') + ' for ' + price + '.', {
      id: product.id,
      title: product.title,
      price: product.price,
      store: product.merchant,
      product_url: product.product_url,
    });
  }

  function buildCard(card, index) {
    var cardEl = document.createElement('div');
    cardEl.className = 'product-card';
    cardEl.style.animationDelay = index * 70 + 'ms';

    var frame = document.createElement('div');
    frame.className = 'product-card-image-frame';
    if (card.image_url) {
      var img = document.createElement('img');
      img.className = 'product-card-image';
      img.src = card.image_url;
      img.loading = 'lazy';
      img.alt = card.title || 'Product image';
      img.addEventListener('error', function () {
        img.replaceWith(placeholderImage());
      });
      frame.appendChild(img);
    } else {
      frame.appendChild(placeholderImage());
    }
    cardEl.appendChild(frame);

    var body = document.createElement('div');
    body.className = 'product-card-body';

    var title = document.createElement('div');
    title.className = 'product-card-title';
    title.textContent = card.title || 'Untitled gift';
    body.appendChild(title);

    if (card.merchant) {
      var merchant = document.createElement('span');
      merchant.className = 'product-card-merchant';
      merchant.textContent = storeLabel(card.merchant);
      body.appendChild(merchant);
    }

    var price = document.createElement('div');
    price.className = 'product-card-price';
    price.textContent = formatPrice(card.price, card.currency);
    body.appendChild(price);

    var btn = document.createElement('button');
    btn.className = 'product-card-btn';
    btn.type = 'button';
    btn.textContent = 'Gift this';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      approveProduct(card);
    });
    body.appendChild(btn);

    cardEl.appendChild(body);

    // Click anywhere on the card (not the CTA) opens the detail modal.
    cardEl.addEventListener('click', function (e) {
      if (e.target.closest('.product-card-btn')) return;
      if (window.GiftModal) {
        window.GiftModal.open({
          product: card,
          store: card.merchant,
          budget: currentBudget,
          ctaLabel: 'Gift this',
          onApprove: approveProduct,
        });
      }
    });

    return cardEl;
  }

  function renderCards(group, cards, hasMore) {
    if (!cards || !cards.length) return;

    var row = document.createElement('div');
    row.className = 'card-row';
    cards.forEach(function (card, index) {
      row.appendChild(buildCard(card, index));
    });
    group.appendChild(row);

    if (hasMore) {
      appendShowMoreChip(group, row, cards.length);
    }

    scrollToBottom();
  }

  function appendShowMoreChip(group, row, startIndex) {
    var existing = group.querySelector('.show-more-chip');
    if (existing) existing.remove();
    var existingStatus = group.querySelector('.pagination-status');
    if (existingStatus) existingStatus.remove();

    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'suggestion-chip show-more-chip';
    chip.textContent = 'Show more like this';
    chip.addEventListener('click', function () {
      chip.disabled = true;
      chip.textContent = 'Loading…';
      fetch('/chat/' + conversationId + '/more', { method: 'POST' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          chip.remove();
          var products = (data && data.products) || [];
          products.forEach(function (card, i) {
            row.appendChild(buildCard(card, startIndex + i));
          });
          if (data && data.has_more) {
            appendShowMoreChip(group, row, startIndex + products.length);
          } else if (data && data.message) {
            appendPaginationStatus(group, data.message);
          } else if (!products.length) {
            appendPaginationStatus(group, 'That’s everything we found for this search.');
          }
          scrollToBottom();
        })
        .catch(function () {
          chip.disabled = false;
          chip.textContent = 'Couldn’t load more — try again';
          appendPaginationStatus(group, 'We couldn’t load more options right now. Try again.');
        });
    });
    group.appendChild(chip);
  }

  function appendPaginationStatus(group, message) {
    var existing = group.querySelector('.pagination-status');
    if (existing) existing.remove();
    var status = document.createElement('div');
    status.className = 'pagination-status';
    status.textContent = message;
    group.appendChild(status);
  }

  function labeledRow(label, value) {
    var row = document.createElement('div');
    row.className = 'receipt-row';

    var labelSpan = document.createElement('span');
    labelSpan.textContent = label;
    row.appendChild(labelSpan);

    var valueSpan = document.createElement('span');
    valueSpan.textContent = value || '—';
    row.appendChild(valueSpan);

    return row;
  }

  var SHARE_ICONS = {
    whatsapp:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M20.5 11.9a8.4 8.4 0 0 1-12.3 7.4L4 20.5l1.3-4a8.4 8.4 0 1 1 15.2-4.6Z"></path>' +
      '<path d="M9 9.3c0 3.6 3 6.6 6.6 6.6"></path></svg>',
    messages:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4 5h16v11H8l-4 4V5Z"></path></svg>',
    copy:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="9" y="9" width="11" height="11" rx="1.6"></rect>' +
      '<path d="M5 15V5a1.6 1.6 0 0 1 1.6-1.6H15"></path></svg>',
  };

  function renderAction(group, action, budget) {
    if (!action) return;

    if (action.type === 'gift_link') {
      var linkCard = document.createElement('div');
      linkCard.className = 'gift-link-card';

      var heading = document.createElement('div');
      heading.className = 'gift-link-heading';
      heading.textContent = 'Your gift is ready to send';
      linkCard.appendChild(heading);

      var desc = document.createElement('div');
      desc.className = 'gift-link-desc';
      desc.textContent = 'Send this to the recipient so they can pick their own gift, within budget.';
      linkCard.appendChild(desc);

      var row = document.createElement('div');
      row.className = 'gift-link-row';

      var fullUrl = window.location.origin + action.url;

      var urlSpan = document.createElement('span');
      urlSpan.className = 'gift-link-url';
      urlSpan.textContent = fullUrl;
      row.appendChild(urlSpan);

      linkCard.appendChild(row);

      // ---- share sheet: WhatsApp / Messages / Copy — one control group ----
      var shareRow = document.createElement('div');
      shareRow.className = 'gift-share-row';

      var messageText =
        'I sent you a gift 🎁 Pick anything you like' +
        (budget ? ' up to ₹' + budget : '') +
        ' — ' + fullUrl;
      var encodedMessage = encodeURIComponent(messageText);

      var waLink = document.createElement('a');
      waLink.className = 'btn gift-share-btn';
      waLink.href = 'https://wa.me/?text=' + encodedMessage;
      waLink.target = '_blank';
      waLink.rel = 'noopener';
      waLink.innerHTML = SHARE_ICONS.whatsapp + '<span>WhatsApp</span>';
      shareRow.appendChild(waLink);

      var smsLink = document.createElement('a');
      smsLink.className = 'btn gift-share-btn';
      smsLink.href = 'sms:&body=' + encodedMessage;
      smsLink.target = '_blank';
      smsLink.rel = 'noopener';
      smsLink.innerHTML = SHARE_ICONS.messages + '<span>Messages</span>';
      shareRow.appendChild(smsLink);

      var copyBtn = document.createElement('button');
      copyBtn.className = 'btn gift-share-btn gift-link-copy';
      copyBtn.type = 'button';
      copyBtn.innerHTML = SHARE_ICONS.copy + '<span>Copy link</span>';
      copyBtn.addEventListener('click', function () {
        var label = copyBtn.querySelector('span');
        function showCopied() {
          copyBtn.classList.add('copied');
          label.textContent = 'Copied!';
          setTimeout(function () {
            copyBtn.classList.remove('copied');
            label.textContent = 'Copy link';
          }, 1500);
        }
        if (window.navigator.clipboard && window.navigator.clipboard.writeText) {
          window.navigator.clipboard.writeText(fullUrl).then(showCopied, function () {
            window.prompt('Copy this link:', fullUrl);
          });
        } else {
          window.prompt('Copy this link:', fullUrl);
        }
      });
      shareRow.appendChild(copyBtn);

      linkCard.appendChild(shareRow);
      group.appendChild(linkCard);
      scrollToBottom();
      return;
    }

    if (action.type === 'approve_payment') {
      var panel = document.createElement('div');
      panel.className = 'action-panel';

      var heading = document.createElement('div');
      heading.className = 'action-panel-heading';

      var icon = document.createElement('div');
      icon.className = 'action-panel-icon';
      icon.innerHTML =
        '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3Z"></path>' +
        '<path d="M9 12l2 2 4-4"></path></svg>';
      heading.appendChild(icon);

      var title = document.createElement('div');
      title.className = 'action-panel-title';
      title.textContent = 'Approve with your passkey';
      heading.appendChild(title);

      panel.appendChild(heading);

      var desc = document.createElement('div');
      desc.className = 'action-panel-desc';
      desc.textContent =
        "This opens Prava's secure checkout in a new tab. Approve there with your passkey, then confirm below.";
      panel.appendChild(desc);

      var buttons = document.createElement('div');
      buttons.className = 'action-panel-buttons';

      var openBtn = document.createElement('button');
      openBtn.className = 'btn btn-primary';
      openBtn.type = 'button';
      openBtn.innerHTML =
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
        'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<circle cx="8" cy="15" r="4"></circle><path d="M10.5 12.5 20 3"></path>' +
        '<path d="M17 6l2 2"></path><path d="M14 9l2 2"></path></svg>' +
        '<span>Approve with passkey</span>';
      openBtn.addEventListener('click', function () {
        window.open(action.iframe_url, '_blank', 'noopener,noreferrer');
      });
      buttons.appendChild(openBtn);

      var confirmBtn = document.createElement('button');
      confirmBtn.className = 'btn btn-secondary';
      confirmBtn.type = 'button';
      confirmBtn.innerHTML =
        '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
        'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M20 6 9 17l-5-5"></path></svg>' +
        "<span>I've approved it</span>";
      confirmBtn.addEventListener('click', function () {
        sendMessage('I completed the Prava approval');
      });
      buttons.appendChild(confirmBtn);

      panel.appendChild(buttons);
      group.appendChild(panel);
      scrollToBottom();
      return;
    }

    if (action.type === 'receipt') {
      var card = document.createElement('div');
      card.className = 'receipt-card';

      var check = document.createElement('div');
      check.className = 'receipt-checkmark';
      check.innerHTML =
        '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" ' +
        'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M20 6 9 17l-5-5"></path></svg>';
      card.appendChild(check);

      var receiptTitle = document.createElement('div');
      receiptTitle.className = 'receipt-title';
      receiptTitle.textContent = 'Order confirmed';
      card.appendChild(receiptTitle);

      var details = document.createElement('div');
      details.className = 'receipt-details';
      details.appendChild(labeledRow('Order ID', action.order_id));
      details.appendChild(labeledRow('Amount', action.amount));
      details.appendChild(labeledRow('Merchant', action.merchant));
      card.appendChild(details);

      var footer = document.createElement('div');
      footer.className = 'receipt-footer';
      footer.textContent = 'Gift is on its way';
      card.appendChild(footer);

      group.appendChild(card);
      scrollToBottom();
    }
  }

  function updateBudget(budget) {
    if (budget === undefined || budget === null || budget === '') return;
    var num = Number(budget);
    currentBudget = isNaN(num) ? currentBudget : num;
    var formatted = isNaN(num) ? budget : num.toLocaleString('en-IN');
    budgetAmount.textContent = '₹' + formatted;
    budgetChip.hidden = false;
  }

  // ---------- send / receive ----------

  function setTyping(isTyping) {
    typingIndicator.hidden = !isTyping;
    if (isTyping) scrollToBottom();
  }

  function setInputDisabled(disabled) {
    input.disabled = disabled;
    sendBtn.disabled = disabled;
  }

  function sendMessage(message, selection) {
    var trimmed = (message || '').trim();
    if (!trimmed) return;

    addBubble('buyer', trimmed);
    input.value = '';
    setInputDisabled(true);
    setTyping(true);

    fetchChat(trimmed, selection)
      .then(function (data) {
        setTyping(false);
        var group = addBubble('agent', data && data.reply ? data.reply : '');
        updateBudget(data && data.budget);
        renderCards(group, data && data.cards, data && data.has_more);
        renderAction(group, data && data.action, currentBudget);
      })
      .catch(function (err) {
        setTyping(false);
        addBubble('agent', 'Something went wrong reaching the agent. Please try again.');
        console.error('chat request failed', err);
      })
      .finally(function () {
        setInputDisabled(false);
        input.focus();
      });
  }

  function fetchChat(message, selection) {
    if (isMock) {
      return mockChat(message, selection);
    }
    return fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId, message: message, selection: selection || null }),
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  // ---------- mock mode (?mock=1) ----------
  // Fakes /chat locally so the UI can be built/verified before the backend
  // exists. Covers all four render states by reacting to the message text:
  //   1. plain chat reply           -> anything not matched below
  //   2. product cards (+ budget)   -> message mentions a gift/occasion
  //   3. approve_payment action     -> a structured card selection
  //      (sent automatically by the card's "Gift this" button)
  //   4. receipt action             -> message is "I completed the Prava approval"
  //      (sent automatically by the approval panel's confirm button)

  var mockShownCards = false;

  function mockChat(message, selection) {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(buildMockResponse(message, selection));
      }, 600 + Math.random() * 500);
    });
  }

  function buildMockResponse(message, selection) {
    var lower = message.toLowerCase();

    if (selection || lower.indexOf('i approve:') === 0) {
      return {
        reply:
          "Great choice! I've started checkout with Prava — it's budget-locked and merchant-scoped, so nothing beyond what you approve can ever be charged.",
        cards: null,
        action: {
          type: 'approve_payment',
          iframe_url: 'https://checkout.prava.space/s/demo-session',
          session_id: 'sess_mock_123',
        },
      };
    }

    if (lower.indexOf('completed the prava approval') !== -1) {
      return {
        reply: "All set — payment approved and the order is confirmed.",
        cards: null,
        action: {
          type: 'receipt',
          order_id: '1001',
          amount: '2499.00',
          merchant: 'GIVA',
        },
      };
    }

    if (/let them pick|gift link|they can pick|pick their own/.test(lower)) {
      return {
        reply: "Here's a link you can send them — they'll get to pick anything within budget.",
        cards: null,
        budget: '3000',
        action: {
          type: 'gift_link',
          url: '/gift/mock-token-demo123',
          token: 'mock-token-demo123',
        },
      };
    }

    if (!mockShownCards || /gift|mom|dad|birthday|anniversary|friend/.test(lower)) {
      mockShownCards = true;
      return {
        reply: 'I found a few options within ₹3,000.',
        budget: '3000',
        cards: [
          {
            id: 'p1',
            title: 'Rose Gold Pendant Necklace',
            price: '2499.00',
            currency: 'INR',
            image_url: null,
            product_url: 'https://giva.co/products/rose-gold-pendant',
            merchant: 'GIVA',
            variant_id: 'v1',
          },
          {
            id: 'p2',
            title: 'Silver Charm Bracelet',
            price: '1899.00',
            currency: 'INR',
            image_url: null,
            product_url: 'https://giva.co/products/silver-charm-bracelet',
            merchant: 'GIVA',
            variant_id: 'v2',
          },
          {
            id: 'p3',
            title: 'Minimalist Stud Earrings',
            price: '1299.00',
            currency: 'INR',
            image_url: null,
            product_url: 'https://giva.co/products/stud-earrings',
            merchant: 'GIVA',
            variant_id: 'v3',
          },
        ],
        action: null,
      };
    }

    return {
      reply:
        "Tell me who you're gifting and the occasion, and I'll find something great within budget.",
      cards: null,
      action: null,
    };
  }

  // ---------- wire up ----------

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    sendMessage(input.value);
  });

  // Explicit Enter-to-send, in addition to native form submission, so the
  // key always works even in contexts where implicit submit doesn't fire.
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input.value);
    }
  });

  var WELCOME_SUGGESTIONS = [
    "A birthday gift for my mom, ₹3000 budget",
    "Anniversary gift for my partner",
    "Something thoughtful for a close friend",
  ];

  function renderWelcome() {
    var row = document.createElement('div');
    row.className = 'msg-row agent';

    var card = document.createElement('div');
    card.className = 'welcome-card';

    var icon = document.createElement('div');
    icon.className = 'welcome-icon';
    icon.innerHTML =
      '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="3" y="9" width="18" height="12" rx="1.4"></rect><path d="M3 13h18"></path>' +
      '<path d="M12 9v12"></path><path d="M12 9C10.3 4.8 6 4.6 6 7.4 6 9 8 9 12 9Z"></path>' +
      '<path d="M12 9c1.7-4.2 6-4.4 6-1.6C18 9 16 9 12 9Z"></path></svg>';
    card.appendChild(icon);

    var heading = document.createElement('h2');
    heading.textContent = "Tell me who you're gifting, and the budget";
    card.appendChild(heading);

    var body = document.createElement('p');
    body.textContent =
      "I'll find real products, you approve the one you like, and Prava mints a one-time " +
      'card locked to that exact price and merchant — nothing more can ever be charged.';
    card.appendChild(body);

    var features = document.createElement('ul');
    features.className = 'welcome-features';
    [
      {
        icon: '<circle cx="10.5" cy="10.5" r="6.5"></circle><path d="m20 20-4.4-4.4"></path>',
        label: 'Real products, live from 5 stores',
      },
      {
        icon: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3Z"></path><path d="m9 12 2 2 4-4"></path>',
        label: 'You approve every purchase',
      },
      {
        icon: '<rect x="5" y="11" width="14" height="9" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path>',
        label: 'Budget-locked, one-time card',
      },
    ].forEach(function (feature) {
      var li = document.createElement('li');
      li.innerHTML =
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        feature.icon + '</svg><span></span>';
      li.querySelector('span').textContent = feature.label;
      features.appendChild(li);
    });
    card.appendChild(features);

    if (isMock) {
      var note = document.createElement('p');
      note.className = 'welcome-note';
      note.textContent = 'Mock mode is active — no backend calls are made.';
      card.appendChild(note);
    }

    var suggestions = document.createElement('div');
    suggestions.className = 'welcome-suggestions';
    WELCOME_SUGGESTIONS.forEach(function (text) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'suggestion-chip';
      chip.textContent = text;
      chip.addEventListener('click', function () {
        sendMessage(text);
      });
      suggestions.appendChild(chip);
    });
    card.appendChild(suggestions);

    row.appendChild(card);
    chatLog.appendChild(row);
    scrollToBottom();
  }

  function init() {
    renderWelcome();
    input.focus();
  }

  init();
})();
