-- 001_init.sql — first version of the database schema.
-- Applied by: uv run --env-file .env python -m app.admin migrate
-- Money is NUMERIC(10,2), never float. Times are TIMESTAMPTZ (stored in UTC, shown in Europe/Amsterdam).

-- Categories, for example 'Groceries' (regular) or 'Rent' (structural).
CREATE TABLE categories (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL DEFAULT 'regular'      -- 'regular' = everyday life, counts in the forecast
              CHECK (kind IN ('regular','structural')),  -- 'structural' = rent, tickets, gifts
  is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

-- One row per purchase, income or transfer.
CREATE TABLE transactions (
  id           BIGSERIAL PRIMARY KEY,
  occurred_at  TIMESTAMPTZ NOT NULL,
  type         TEXT NOT NULL CHECK (type IN ('expense','income','transfer')),
  amount_eur   NUMERIC(10,2) NOT NULL CHECK (amount_eur > 0),   -- always positive; type gives the direction
  category_id  INT REFERENCES categories(id),
  merchant     TEXT,
  note         TEXT,
  source       TEXT NOT NULL CHECK (source IN ('chat','receipt','moneyflow_import')),
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','void')),
  void_reason  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Receipt lines that belong to one transaction.
CREATE TABLE items (
  id              BIGSERIAL PRIMARY KEY,
  transaction_id  BIGINT NOT NULL REFERENCES transactions(id),
  name_raw        TEXT NOT NULL,                  -- as printed on the receipt
  name_clean      TEXT,                           -- readable name
  item_type       TEXT,                           -- for searching by type, e.g. 'ice cream'
  quantity        NUMERIC(8,3) NOT NULL DEFAULT 1 CHECK (quantity > 0),
  price_eur       NUMERIC(10,2) NOT NULL,         -- line total after discount
  discount_eur    NUMERIC(10,2) NOT NULL DEFAULT 0
);

-- Drafts made by preview_expense. Single-use, valid for 30 minutes.
CREATE TABLE drafts (
  id          UUID PRIMARY KEY,
  payload     JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  used        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX transactions_occurred_at_idx ON transactions (occurred_at);
CREATE INDEX items_transaction_id_idx ON items (transaction_id);
CREATE INDEX items_item_type_idx ON items (item_type);

-- Convenient view for questions: only active expenses, with the category name.
CREATE VIEW v_expenses AS
SELECT t.*, c.name AS category, c.kind AS category_kind
FROM transactions t
LEFT JOIN categories c ON c.id = t.category_id
WHERE t.status = 'active' AND t.type = 'expense';
