-- 002_accounts_and_ledger.sql — design v6 (21 Nov 2026, after the architecture review)
-- Applied first on the Neon "dev" branch and tested; production later with the same file.
--
-- In plain words:
--   1. categories: description, parent (max two levels, checked in code), aliases,
--      a third kind 'excluded' (not spending, not income — e.g. loans); names unique ignoring case
--   2. new table accounts (description, unit = EUR or a coin code, price_ref for coins)
--   3. transactions: account, to-account, tags, units + unit, stated value for revaluations,
--      the draft it came from; new types opening + revaluation; bigger euro precision
--   4. drafts: one draft can hold several transactions; saving is single-use and repeatable safely
--   5. change_log: history of every change (the app role may only ADD rows; set in app.admin)
--   6. the database uses Europe/Amsterdam time, so "today" and "this month" are local
--   7. views: v_movements, v_account_balances, v_spending, v_income, v_items (v_expenses removed)
-- Database users and what they may read/write are set by `app.admin create-roles`, not here.
-- Deletes no data: removes only old rules (replaced by better ones), one old view and one unused column.

-- 0. The old view depends on transactions.* — remove it first (replaced by v_spending) ------------
DROP VIEW v_expenses;

-- 1. Categories -------------------------------------------------------------
ALTER TABLE categories
  ADD COLUMN description TEXT,
  ADD COLUMN parent_id   INT REFERENCES categories(id),
  ADD COLUMN aliases     TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD CONSTRAINT categories_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id);

ALTER TABLE categories DROP CONSTRAINT categories_kind_check;
ALTER TABLE categories ADD CONSTRAINT categories_kind_check
  CHECK (kind IN ('regular','structural','excluded'));
  -- regular    = everyday spending, counts in the forecast
  -- structural = counts as spending, not in the forecast (rent, travel, unrecorded money)
  -- excluded   = neither spending nor income (loans)

ALTER TABLE categories DROP CONSTRAINT categories_name_key;                  -- case-sensitive rule
CREATE UNIQUE INDEX categories_name_lower_idx ON categories (lower(name));  -- case-insensitive rule

-- 2. Accounts ---------------------------------------------------------------
CREATE TABLE accounts (
  id                  SERIAL PRIMARY KEY,
  name                TEXT NOT NULL,
  description         TEXT,                          -- Claude reads it: what the account is for
  unit                TEXT NOT NULL DEFAULT 'EUR'    -- 'EUR', or a coin code in capitals: 'BTC', 'ETH'…
                      CHECK (unit ~ '^[A-Z0-9]{2,10}$'),
  price_ref           TEXT,                          -- coins only: the coin's id at the price service
  is_default_expense  BOOLEAN NOT NULL DEFAULT FALSE,
  is_default_income   BOOLEAN NOT NULL DEFAULT FALSE,
  is_active           BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (unit = 'EUR' OR NOT is_default_expense),
  CHECK (unit = 'EUR' OR NOT is_default_income)
);
CREATE UNIQUE INDEX accounts_name_lower_idx      ON accounts (lower(name));
CREATE UNIQUE INDEX accounts_one_default_expense ON accounts ((TRUE)) WHERE is_default_expense;
CREATE UNIQUE INDEX accounts_one_default_income  ON accounts ((TRUE)) WHERE is_default_income;

-- 3. Drafts: one draft = one preview of 1..n transactions ------------------------------------
ALTER TABLE drafts
  DROP COLUMN used,                                 -- replaced by saved_at (table is empty)
  ADD COLUMN saved_at  TIMESTAMPTZ,                 -- set once, in the same DB transaction as the inserts
  ADD COLUMN saved_ids BIGINT[],                    -- returned again if save is called twice
  ADD COLUMN cancelled_at TIMESTAMPTZ;              -- a newer preview replaced this draft

-- 4. Transactions -------------------------------------------------------------
-- No transactions exist yet, so account_id can be required immediately.
ALTER TABLE transactions ALTER COLUMN amount_eur TYPE NUMERIC(12,2);
ALTER TABLE transactions
  ADD COLUMN account_id          INT REFERENCES accounts(id),
  ADD COLUMN to_account_id       INT REFERENCES accounts(id),
  ADD COLUMN tags                TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN units               NUMERIC,          -- coins (2 BTC) or foreign money paid (12 USD); no rounding
  ADD COLUMN unit                TEXT CHECK (unit ~ '^[A-Z0-9]{2,10}$'),
  ADD COLUMN stated_balance_eur  NUMERIC(12,2),    -- revaluation only: the value I said the account is worth
  ADD COLUMN draft_id            UUID REFERENCES drafts(id),
  ADD COLUMN updated_at          TIMESTAMPTZ;
ALTER TABLE transactions ALTER COLUMN account_id SET NOT NULL;

ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
  CHECK (type IN ('expense','income','refund','transfer','opening','revaluation'));

ALTER TABLE transactions DROP CONSTRAINT transactions_source_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_source_check
  CHECK (source IN ('chat','receipt','agent','admin'));

-- amounts: positive; an opening may be any amount (an overdrawn card, 0 € for coins received);
-- a revaluation is + or − but never 0
ALTER TABLE transactions DROP CONSTRAINT transactions_amount_eur_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_amount_check CHECK (
     type = 'opening'
  OR (type = 'revaluation' AND amount_eur <> 0)
  OR (type NOT IN ('opening','revaluation') AND amount_eur > 0)
);
-- only transfers have a to-account, and it must be a different account
ALTER TABLE transactions ADD CONSTRAINT transactions_transfer_check CHECK (
  (type = 'transfer' AND to_account_id IS NOT NULL AND to_account_id <> account_id)
  OR (type <> 'transfer' AND to_account_id IS NULL)
);
-- expenses and refunds always have a category (income and transfers may have one, e.g. loans)
ALTER TABLE transactions ADD CONSTRAINT transactions_category_check
  CHECK (type NOT IN ('expense','refund') OR category_id IS NOT NULL);
-- units and unit come together, and units are positive (the type gives the direction)
ALTER TABLE transactions ADD CONSTRAINT transactions_units_check
  CHECK ((units IS NULL) = (unit IS NULL) AND (units IS NULL OR units > 0));
-- a revaluation always has the stated value and never carries units; nothing else has a stated value
ALTER TABLE transactions ADD CONSTRAINT transactions_revaluation_check CHECK (
  (type = 'revaluation' AND stated_balance_eur IS NOT NULL AND units IS NULL)
  OR (type <> 'revaluation' AND stated_balance_eur IS NULL)
);
-- at most one active opening row per account
CREATE UNIQUE INDEX transactions_one_opening ON transactions (account_id)
  WHERE type = 'opening' AND status = 'active';

CREATE INDEX transactions_account_time_idx ON transactions (account_id, occurred_at);
CREATE INDEX transactions_to_account_idx   ON transactions (to_account_id) WHERE to_account_id IS NOT NULL;
CREATE INDEX transactions_category_idx     ON transactions (category_id);
CREATE INDEX transactions_draft_idx        ON transactions (draft_id);

-- 4b. Receipt lines: the unit of the quantity, so prices can be compared per kg / litre / piece ----
ALTER TABLE items
  ADD COLUMN quantity_unit TEXT CHECK (quantity_unit IN ('pcs','kg','g','l','ml'));

-- 5. Change log (history) -------------------------------------------------------
CREATE TABLE change_log (
  id          BIGSERIAL PRIMARY KEY,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  entity      TEXT NOT NULL CHECK (entity IN ('transaction','category','account')),
  entity_id   BIGINT NOT NULL,
  action      TEXT NOT NULL,             -- 'update', 'void', 'undo_void', 'rename', 'move', 'merge', 'archive' …
  old_values  JSONB NOT NULL DEFAULT '{}',
  new_values  JSONB NOT NULL DEFAULT '{}'  -- a merge stores the list of moved transaction ids here
);
CREATE INDEX change_log_entity_idx ON change_log (entity, entity_id);

-- 6. Local time for every session (one place to change it for a friend in another country) --------
DO $$ BEGIN
  EXECUTE format('ALTER DATABASE %I SET timezone = %L', current_database(), 'Europe/Amsterdam');
END $$;

-- 7. Views (saved queries; they store no data) ----------------------------------
-- `day` = the local calendar date (sessions use Europe/Amsterdam, see 6).

-- One line per effect on an account. A transfer becomes two lines: minus on "from", plus on "to".
-- units count only when they are in the account's own coin unit.
CREATE VIEW v_movements AS
SELECT t.id AS transaction_id, t.occurred_at, t.occurred_at::date AS day,
       t.account_id, t.type, t.category_id, t.to_account_id AS other_account_id,
       CASE WHEN t.type IN ('income','refund','opening','revaluation') THEN t.amount_eur
            ELSE -t.amount_eur END AS amount_eur,
       CASE WHEN t.unit = a.unit AND a.unit <> 'EUR' THEN
            CASE WHEN t.type IN ('income','refund','opening') THEN t.units ELSE -t.units END
       END AS units
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.status = 'active'
UNION ALL
SELECT t.id, t.occurred_at, t.occurred_at::date,
       t.to_account_id, t.type, t.category_id, t.account_id,
       t.amount_eur,
       CASE WHEN t.unit = a.unit AND a.unit <> 'EUR' THEN t.units END
FROM transactions t
JOIN accounts a ON a.id = t.to_account_id
WHERE t.status = 'active' AND t.type = 'transfer';

-- Balance per account (archived accounts included, so no money ever "disappears").
-- The opening row is where an account's ledger starts: the stated opening balance already
-- includes everything before it, so rows dated earlier count in spending statistics but not in balances.
-- EUR accounts: balance_eur. Coin accounts: balance_units (coins) and net_invested_eur (euros put in).
CREATE VIEW v_account_balances AS
WITH starts AS (
  SELECT account_id, occurred_at AS ledger_start
  FROM transactions
  WHERE type = 'opening' AND status = 'active'
)
SELECT a.id AS account_id, a.name, a.unit, a.description, a.is_active, s.ledger_start,
       CASE WHEN a.unit = 'EUR'  THEN COALESCE(sum(m.amount_eur), 0) END AS balance_eur,
       CASE WHEN a.unit <> 'EUR' THEN COALESCE(sum(m.units), 0)      END AS balance_units,
       CASE WHEN a.unit <> 'EUR' THEN COALESCE(sum(m.amount_eur), 0) END AS net_invested_eur
FROM accounts a
LEFT JOIN starts s ON s.account_id = a.id
LEFT JOIN v_movements m ON m.account_id = a.id
                       AND (s.ledger_start IS NULL OR m.occurred_at >= s.ledger_start)
GROUP BY a.id, s.ledger_start;

-- Spending: expenses +, refunds −; 'excluded' categories (loans) never count.
CREATE VIEW v_spending AS
SELECT t.id, t.occurred_at, t.occurred_at::date AS day, t.account_id, t.category_id,
       c.name AS category, p.name AS parent_category, COALESCE(p.name, c.name) AS top_category,
       c.kind AS category_kind,
       CASE WHEN t.type = 'refund' THEN -t.amount_eur ELSE t.amount_eur END AS amount_eur,
       t.merchant, t.note, t.tags
FROM transactions t
JOIN categories c ON c.id = t.category_id
LEFT JOIN categories p ON p.id = c.parent_id
WHERE t.status = 'active' AND t.type IN ('expense','refund') AND c.kind <> 'excluded';

-- Income: 'excluded' categories (borrowed money) never count.
CREATE VIEW v_income AS
SELECT t.id, t.occurred_at, t.occurred_at::date AS day, t.account_id, t.category_id,
       c.name AS category, t.amount_eur, t.merchant, t.note, t.tags
FROM transactions t
LEFT JOIN categories c ON c.id = t.category_id
WHERE t.status = 'active' AND t.type = 'income' AND COALESCE(c.kind, 'regular') <> 'excluded';

-- Receipt lines of active transactions only (so voided purchases never sneak into item questions).
-- Plain lists of accounts and categories for free-form questions (run_query sees only views).
CREATE VIEW v_accounts AS
SELECT id, name, description, unit, is_default_expense, is_default_income, is_active
FROM accounts;

CREATE VIEW v_categories AS
SELECT c.id, c.name, p.name AS parent, c.kind, c.description, c.aliases, c.is_active
FROM categories c
LEFT JOIN categories p ON p.id = c.parent_id;

-- price_per_unit = price per kg / litre / piece, for "how did the price of apples change?"
CREATE VIEW v_items AS
SELECT i.id, i.transaction_id, t.occurred_at::date AS day, t.account_id, t.category_id, t.merchant,
       i.name_raw, i.name_clean, i.item_type, i.quantity, i.quantity_unit, i.price_eur, i.discount_eur,
       CASE WHEN i.quantity > 0 THEN round(i.price_eur / i.quantity, 4) END AS price_per_unit
FROM items i
JOIN transactions t ON t.id = i.transaction_id
WHERE t.status = 'active';
