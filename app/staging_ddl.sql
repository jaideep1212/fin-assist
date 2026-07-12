-- ===========================================================================
-- fin-assist -- STAGING schema DDL (Job B).
--
-- These are the source tables relocated into a `staging` schema in the
-- ephemeral Postgres. Same columns/types/PKs as the source; no added indexes,
-- no identity columns (ids carry real source values). bytea columns are loaded
-- opaquely (decryption happens downstream with the Fernet key).
--
-- All 10 tables fetched by the watchdog are now defined here explicitly, so
-- their types (especially bytea encrypted columns) are correct rather than
-- pandas-inferred. Keep this file in sync with PI_TABLES in app/pi_data.py:
-- every table fetched should have a definition here.
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS staging;

-- ---- dimensions ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS staging.dim_users (
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

CREATE TABLE IF NOT EXISTS staging.dim_users_s (
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

CREATE TABLE IF NOT EXISTS staging.dim_entities (
    id                      bigint      NOT NULL,
    entity_name_hash        bytea       NOT NULL,
    entity_name             bytea,
    entity_branch           bytea,
    address_line1           bytea,
    address_line2           bytea,
    city                    bytea,
    post_code               bytea,
    country                 bytea,
    customer_care_email_id  bytea,
    customer_care_phone_no  bytea,
    customer_care_website   bytea,
    ifsc                    bytea,
    micr                    bytea,
    swift                   bytea,
    iban                    bytea,
    entity_type             varchar(5),
    is_online               boolean,
    registrar_id            integer,
    created_date            timestamp   NOT NULL,
    modified_date           timestamp   NOT NULL,
    CONSTRAINT pk_dim_entities PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS staging.dim_accounts (
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

CREATE TABLE IF NOT EXISTS staging.dim_mutual_funds (
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

CREATE TABLE IF NOT EXISTS staging.fact_mutual_fund_transactions (
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

CREATE TABLE IF NOT EXISTS staging.fact_stock_transactions (
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

CREATE TABLE IF NOT EXISTS staging.fact_aliases (
    id             bigint NOT NULL,
    record_type    varchar(50),
    record_id      integer,
    alias_name     bytea,
    created_date   timestamp,
    modified_date  timestamp,
    CONSTRAINT pk_fact_aliases PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS staging.fact_account_broker_mappings (
    id             bigint NOT NULL,
    account_id     integer   NOT NULL,
    broker_id      integer   NOT NULL,
    created_date   timestamp,
    modified_date  timestamp,
    CONSTRAINT pk_fact_account_broker_mappings PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS staging.fact_deposits (
    id                          bigint        NOT NULL,
    deposit_no                  bytea         NOT NULL,
    deposit_no_hash             bytea         NOT NULL,
    entity_id                   integer       NOT NULL,
    linked_account_id           integer,
    first_holder_id             integer       NOT NULL,
    joint_holder1_id            integer,
    joint_holder2_id            integer,
    operation_type              varchar(10)   NOT NULL,
    nominee1_id                 integer,
    nominee2_id                 integer,
    invested_amount             numeric(11,2) NOT NULL,
    interest_rate               numeric(4,2)  NOT NULL,
    start_date                  timestamp,
    expected_maturity_date      timestamp,
    period_years                integer       NOT NULL,
    period_months               integer       NOT NULL,
    period_days                 integer       NOT NULL,
    expected_maturity_amount    numeric(11,2) NOT NULL,
    expected_interest_amount    numeric(11,2) NOT NULL,
    actual_interest_amount      numeric(11,2) NOT NULL,
    actual_maturity_date        timestamp,
    actual_maturity_amount      numeric(11,2) NOT NULL,
    deposit_currency            varchar(10)   NOT NULL,
    deposit_type                varchar(10)   NOT NULL,
    interest_payment_frequency  varchar(10)   NOT NULL,
    deposit_payment_frequency   varchar(10)   NOT NULL,
    closure_type                varchar(10),
    is_booked_online            boolean       NOT NULL,
    is_auto_renewable           boolean       NOT NULL,
    is_renewed                  boolean       NOT NULL,
    is_active                   boolean       NOT NULL,
    is_premature_withdrawal     boolean       NOT NULL,
    broker_id                   integer,
    comments                    bytea,
    created_date                timestamp     NOT NULL,
    modified_date               timestamp     NOT NULL,
    CONSTRAINT pk_fact_deposits PRIMARY KEY (id)
);
