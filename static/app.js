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

  function addBubble(role, text) {
    var row = document.createElement('div');
    row.className = 'msg-row ' + role;

    var group = document.createElement('div');
    group.className = 'msg-group';

    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = text || '';
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
    div.textContent = '🎁'; // gift emoji
    return div;
  }

  function renderCards(group, cards) {
    if (!cards || !cards.length) return;

    var row = document.createElement('div');
    row.className = 'card-row';

    cards.forEach(function (card) {
      var cardEl = document.createElement('div');
      cardEl.className = 'product-card';

      if (card.image_url) {
        var img = document.createElement('img');
        img.className = 'product-card-image';
        img.src = card.image_url;
        img.alt = card.title || 'Product image';
        img.addEventListener('error', function () {
          img.replaceWith(placeholderImage());
        });
        cardEl.appendChild(img);
      } else {
        cardEl.appendChild(placeholderImage());
      }

      var body = document.createElement('div');
      body.className = 'product-card-body';

      var title = document.createElement('div');
      title.className = 'product-card-title';
      title.textContent = card.title || 'Untitled gift';
      body.appendChild(title);

      if (card.merchant) {
        var merchant = document.createElement('span');
        merchant.className = 'product-card-merchant';
        merchant.textContent = card.merchant;
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
      btn.addEventListener('click', function () {
        sendMessage('I approve: ' + card.title + ' (id ' + card.id + ') at ' + card.price);
      });
      body.appendChild(btn);

      cardEl.appendChild(body);
      row.appendChild(cardEl);
    });

    group.appendChild(row);
    scrollToBottom();
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

  function renderAction(group, action) {
    if (!action) return;

    if (action.type === 'approve_payment') {
      var panel = document.createElement('div');
      panel.className = 'action-panel';

      var title = document.createElement('div');
      title.className = 'action-panel-title';
      title.textContent = 'Approve with your passkey';
      panel.appendChild(title);

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
      openBtn.textContent = 'Approve with passkey ↗';
      openBtn.addEventListener('click', function () {
        window.open(action.iframe_url, '_blank', 'noopener,noreferrer');
      });
      buttons.appendChild(openBtn);

      var confirmBtn = document.createElement('button');
      confirmBtn.className = 'btn btn-secondary';
      confirmBtn.type = 'button';
      confirmBtn.textContent = "I've approved it ✓";
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
      check.textContent = '✓';
      card.appendChild(check);

      var receiptTitle = document.createElement('div');
      receiptTitle.className = 'receipt-title';
      receiptTitle.textContent = 'Order confirmed';
      card.appendChild(receiptTitle);

      card.appendChild(labeledRow('Order ID', action.order_id));
      card.appendChild(labeledRow('Amount', action.amount));
      card.appendChild(labeledRow('Merchant', action.merchant));

      var footer = document.createElement('div');
      footer.className = 'receipt-footer';
      footer.textContent = 'Gift is on its way 🎁';
      card.appendChild(footer);

      group.appendChild(card);
      scrollToBottom();
    }
  }

  function updateBudget(budget) {
    if (budget === undefined || budget === null || budget === '') return;
    budgetAmount.textContent = '₹' + budget;
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

  function sendMessage(message) {
    var trimmed = (message || '').trim();
    if (!trimmed) return;

    addBubble('buyer', trimmed);
    input.value = '';
    setInputDisabled(true);
    setTyping(true);

    fetchChat(trimmed)
      .then(function (data) {
        setTyping(false);
        var group = addBubble('agent', data && data.reply ? data.reply : '');
        renderCards(group, data && data.cards);
        renderAction(group, data && data.action);
        updateBudget(data && data.budget);
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

  function fetchChat(message) {
    if (isMock) {
      return mockChat(message);
    }
    return fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId, message: message }),
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
  //   3. approve_payment action     -> message starts with "I approve:"
  //      (sent automatically by the card's "Gift this" button)
  //   4. receipt action             -> message is "I completed the Prava approval"
  //      (sent automatically by the approval panel's confirm button)

  var mockShownCards = false;

  function mockChat(message) {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(buildMockResponse(message));
      }, 600 + Math.random() * 500);
    });
  }

  function buildMockResponse(message) {
    var lower = message.toLowerCase();

    if (lower.indexOf('i approve:') === 0) {
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

    if (!mockShownCards || /gift|mom|dad|birthday|anniversary|friend/.test(lower)) {
      mockShownCards = true;
      return {
        reply: 'Here are a few gift ideas that fit a ₹3,000 budget:',
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

  function init() {
    if (isMock) {
      addBubble(
        'agent',
        "Hi! I'm your gifting agent. Try “gift for mom's birthday” — " +
          "I'll show products, walk you through Prava approval, and confirm the receipt.\n\n" +
          '(mock mode is active — no backend calls are made)'
      );
    } else {
      addBubble('agent', "Hi! Tell me who you're shopping for and I'll find the perfect gift.");
    }
    input.focus();
  }

  init();
})();
