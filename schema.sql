-- ===========================================================================
-- fin-assist — PLACEHOLDER schema for the 9 Pi source tables.
--
-- IMPORTANT: this is NOT the real schema. The actual column names and types on
-- the Pi have not been confirmed yet (open item #1). Every column below is a
-- GUESS, inferred from the table names and the Indian MF/stock market context
-- (AMFI scheme codes, NSE/BSE symbols). Run inspect_schema.py against the real
-- database and replace this file with its output before doing feature work.
--
-- Do not wire app/data_access.py to these names on trust — verify first.
-- ===========================================================================

-- ---- dimensions ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS dim_users (
    id              bigint NOT NULL,
    user_name_hash  bytea      NOT NULL,
    gender          char(1),
    age             integer,
    father_id       integer,
    mother_id       integer,
    spouse_id       integer,
    marital_status  char(1),
    is_expired      boolean,
    created_date    timestamp,
    modified_date   timestamp,
    CONSTRAINT pk_dim_users PRIMARY KEY (id),
    CONSTRAINT uq_dim_users_user_name_hash UNIQUE (user_name_hash)
);

-- "_s" likely a slowly-changing / snapshot variant of dim_users.
CREATE TABLE IF NOT EXISTS dim_users_s (
    id                       bigint     NOT NULL,
    user_id                  integer    NOT NULL,
    first_name               bytea,
    last_name                bytea,
    birth_date               bytea,
    birth_city               bytea,
    birth_country            bytea,
    marriage_date            bytea,
    current_address_line1    bytea,
    current_address_line2    bytea,
    current_city             bytea,
    current_post_code        bytea,
    current_country          bytea,
    permanent_address_line1  bytea,
    permanent_address_line2  bytea,
    permanent_city           bytea,
    permanent_post_code      bytea,
    permanent_country        bytea,
    contact_email_id         bytea,
    contact_mobile_no        bytea,
    contact_phone_no         bytea,
    work_email_id            bytea,
    work_mobile_no           bytea,
    work_phone_no            bytea,
    expired_date             bytea,
    pan                      bytea,
    aadhar                   bytea,
    tin                      bytea,
    created_date             timestamp,
    modified_date            timestamp,
    CONSTRAINT pk_dim_users_s PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS dim_accounts (
    id                     bigint       NOT NULL,
    account_no             bytea        NOT NULL,
    account_no_hash        bytea        NOT NULL,
    entity_id              integer,
    account_type           text,
    first_holder_id        integer,
    joint_holder1_id       integer,
    joint_holder2_id       integer,
    operation_type         text,
    first_holder_address   bytea,
    nominee1_id            integer,
    nominee2_id            integer,
    cif                    bytea,
    minimum_balance        numeric(18,2),
    open_year              bytea,
    cheque_book_count      integer,
    email_id               bytea,
    contact_no             bytea,
    is_active              boolean,
    passbook_available     boolean,
    online_banking_allowed boolean,
    online_login_available boolean,
    aadhar_linked          boolean,
    brokers_linked         boolean,
    comments               bytea,
    created_date           timestamp,
    modified_date          timestamp,
    CONSTRAINT pk_dim_accounts PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS dim_mutual_funds (
    id                       bigint       NOT NULL,
    isin_folio_holder_hash   bytea        NOT NULL,
    folio_no                 bytea        NOT NULL,
    scheme_name              bytea        NOT NULL,
    isin                     bytea        NOT NULL,
    scheme_code              bytea        NOT NULL,
    scheme_category          bytea        NOT NULL,
    first_holder_id          integer      NOT NULL,
    joint_holder1_id         integer,
    joint_holder2_id         integer,
    nominee1_id              integer,
    nominee2_id              integer,
    operation_mode           varchar(20),
    total_units_bought       numeric(11,4) NOT NULL,
    total_units_sold         numeric(11,4) NOT NULL,
    total_units_held         numeric(11,4) NOT NULL,
    total_invested_amount    numeric(11,2) NOT NULL,
    total_redeemed_amount    numeric(11,2) NOT NULL,
    total_dividend_received  numeric(11,2) NOT NULL,
    is_active                boolean       NOT NULL,
    linked_entity_id         integer       NOT NULL,
    is_dividend              boolean       NOT NULL,
    is_online                boolean       NOT NULL,
    is_demat                 boolean       NOT NULL,
    comments                 bytea,
    created_date             timestamp,
    modified_date            timestamp,
    CONSTRAINT pk_dim_mutual_funds PRIMARY KEY (id)
);

-- ---- facts ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_mutual_fund_transactions (
    id                      bigint NOT NULL,
    fund_id                 integer           NOT NULL,
    transaction_order_hash  bytea,
    exchange                varchar(10),
    transaction_date        timestamp         NOT NULL,
    transaction_type        varchar(10)       NOT NULL,
    realized_amount         numeric(11,2)     NOT NULL,
    transaction_amount      numeric(11,2)     NOT NULL,
    transaction_nav         numeric(11,4)     NOT NULL,
    transaction_units       numeric(11,4)     NOT NULL,
    transaction_stt         numeric(11,2)     NOT NULL,
    transaction_tds         numeric(11,2)     NOT NULL,
    transaction_stamp_duty  numeric(11,2)     NOT NULL,
    broker_id               integer           NOT NULL,
    order_id                bytea,
    trade_id                bytea,
    created_date            timestamp,
    modified_date           timestamp,
    CONSTRAINT pk_fact_mutual_fund_transactions PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS fact_stock_transactions (
    id                bigint NOT NULL,
    trade_order_hash  bytea,
    holder_id         integer       NOT NULL,
    symbol            bytea         NOT NULL,
    isin              bytea         NOT NULL,
    exchange          varchar(10),
    trade_date        timestamp     NOT NULL,
    trade_type        varchar(10)   NOT NULL,
    trade_amount      numeric(11,2) NOT NULL,
    trade_price       numeric(11,2) NOT NULL,
    trade_quantity    numeric(11,2) NOT NULL,
    nominee_id        integer       NOT NULL,
    linked_entity_id  integer       NOT NULL,
    broker_id         integer       NOT NULL,
    order_id          bytea,
    trade_id          bytea,
    created_date      timestamp,
    modified_date     timestamp,
    CONSTRAINT pk_fact_stock_transactions PRIMARY KEY (id)
);

-- Maps alternate names/tickers to canonical instrument identifiers.
CREATE TABLE IF NOT EXISTS fact_aliases (
    id             bigint NOT NULL,
    record_type    varchar(50),
    record_id      integer,
    alias_name     bytea,
    created_date   timestamp,
    modified_date  timestamp,
    CONSTRAINT pk_fact_aliases PRIMARY KEY (id)
);

-- Maps accounts to brokers / external account references.
CREATE TABLE IF NOT EXISTS fact_account_broker_mappings (
    id             bigint NOT NULL,
    account_id     integer   NOT NULL,
    broker_id      integer   NOT NULL,
    created_date   timestamp,
    modified_date  timestamp,
    CONSTRAINT pk_fact_account_broker_mappings PRIMARY KEY (id)
);

