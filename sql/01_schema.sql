-- =====================================================================
-- High-Concurrency Booking Engine  |  MySQL 8.0.16+ (InnoDB, utf8mb4)
-- P1: schema (3NF / BCNF) designed for seat-level locking
-- =====================================================================
DROP DATABASE IF EXISTS bookmyshow;
CREATE DATABASE bookmyshow CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE bookmyshow;

-- ---------- Reference / catalogue ------------------------------------
CREATE TABLE city (
  city_id   INT UNSIGNED NOT NULL AUTO_INCREMENT,
  name      VARCHAR(80)  NOT NULL,
  state     VARCHAR(80)  NOT NULL,
  PRIMARY KEY (city_id),
  UNIQUE KEY uq_city (name, state)
) ENGINE=InnoDB;

CREATE TABLE theatre (
  theatre_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  city_id    INT UNSIGNED NOT NULL,
  name       VARCHAR(120) NOT NULL,
  address    VARCHAR(255) NOT NULL,
  PRIMARY KEY (theatre_id),
  UNIQUE KEY uq_theatre_city_name (city_id, name),
  CONSTRAINT fk_theatre_city FOREIGN KEY (city_id) REFERENCES city (city_id)
) ENGINE=InnoDB;

CREATE TABLE screen (
  screen_id  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  theatre_id INT UNSIGNED NOT NULL,
  name       VARCHAR(40)  NOT NULL,                -- 'Audi 1', 'IMAX'
  PRIMARY KEY (screen_id),
  UNIQUE KEY uq_screen_name (theatre_id, name),    -- also serves theatre_id lookups
  CONSTRAINT fk_screen_theatre FOREIGN KEY (theatre_id) REFERENCES theatre (theatre_id)
) ENGINE=InnoDB;

CREATE TABLE seat_category (
  category_id TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(30) NOT NULL,                -- 'Silver', 'Gold', 'Recliner'
  PRIMARY KEY (category_id),
  UNIQUE KEY uq_seat_category (name)
) ENGINE=InnoDB;

CREATE TABLE seat (                                -- physical seat in a screen
  seat_id     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  screen_id   INT UNSIGNED NOT NULL,
  row_label   CHAR(2)      NOT NULL,               -- 'A', 'B', ...
  seat_number SMALLINT UNSIGNED NOT NULL,
  category_id TINYINT UNSIGNED NOT NULL,
  PRIMARY KEY (seat_id),
  UNIQUE KEY uq_seat_position (screen_id, row_label, seat_number),
  CONSTRAINT fk_seat_screen   FOREIGN KEY (screen_id)   REFERENCES screen (screen_id),
  CONSTRAINT fk_seat_category FOREIGN KEY (category_id) REFERENCES seat_category (category_id)
) ENGINE=InnoDB;

CREATE TABLE language (
  language_id TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(30) NOT NULL,
  PRIMARY KEY (language_id),
  UNIQUE KEY uq_language (name)
) ENGINE=InnoDB;

CREATE TABLE show_format (
  format_id TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name      VARCHAR(20) NOT NULL,                  -- '2D', '3D', 'IMAX 2D'
  PRIMARY KEY (format_id),
  UNIQUE KEY uq_show_format (name)
) ENGINE=InnoDB;

CREATE TABLE movie (
  movie_id     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  title        VARCHAR(200) NOT NULL,
  duration_min SMALLINT UNSIGNED NOT NULL,
  certificate  VARCHAR(5)   NOT NULL,              -- U, UA, A
  PRIMARY KEY (movie_id),
  UNIQUE KEY uq_movie_title (title)
) ENGINE=InnoDB;

-- ---------- Shows -----------------------------------------------------
-- theatre is reachable via screen -> not repeated here (3NF).
CREATE TABLE movie_show (
  show_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  screen_id   INT UNSIGNED NOT NULL,
  movie_id    INT UNSIGNED NOT NULL,
  language_id TINYINT UNSIGNED NOT NULL,
  format_id   TINYINT UNSIGNED NOT NULL,
  show_date   DATE NOT NULL,
  start_time  TIME NOT NULL,
  status      ENUM('SCHEDULED','CANCELLED') NOT NULL DEFAULT 'SCHEDULED',
  PRIMARY KEY (show_id),
  -- one screen cannot start two shows at the same instant; also the P2 access path
  UNIQUE KEY uq_show_slot (screen_id, show_date, start_time),
  KEY idx_show_date (show_date),
  CONSTRAINT fk_show_screen   FOREIGN KEY (screen_id)   REFERENCES screen (screen_id),
  CONSTRAINT fk_show_movie    FOREIGN KEY (movie_id)    REFERENCES movie (movie_id),
  CONSTRAINT fk_show_language FOREIGN KEY (language_id) REFERENCES language (language_id),
  CONSTRAINT fk_show_format   FOREIGN KEY (format_id)   REFERENCES show_format (format_id)
) ENGINE=InnoDB;

-- price depends on (show, seat category) -> its own table
CREATE TABLE show_price (
  show_id     BIGINT UNSIGNED  NOT NULL,
  category_id TINYINT UNSIGNED NOT NULL,
  price       DECIMAL(8,2)     NOT NULL CHECK (price >= 0),
  PRIMARY KEY (show_id, category_id),
  CONSTRAINT fk_sp_show     FOREIGN KEY (show_id)     REFERENCES movie_show (show_id),
  CONSTRAINT fk_sp_category FOREIGN KEY (category_id) REFERENCES seat_category (category_id)
) ENGINE=InnoDB;

-- ---------- Users, holds, bookings, payments --------------------------
CREATE TABLE app_user (
  user_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name       VARCHAR(100) NOT NULL,
  email      VARCHAR(160) NOT NULL,
  phone      VARCHAR(20)  NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  UNIQUE KEY uq_user_email (email)
) ENGINE=InnoDB;

-- A hold = one user's temporary claim on N seats of one show.
-- expires_at lives HERE (not per seat) so it is stored once per hold (3NF).
CREATE TABLE seat_hold (
  hold_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id    BIGINT UNSIGNED NOT NULL,
  show_id    BIGINT UNSIGNED NOT NULL,
  seat_count TINYINT UNSIGNED NOT NULL,            -- fact about the hold; survives seat release
  lock_token CHAR(32)    NOT NULL,                 -- owner token stored in the Redis seat locks
  status     ENUM('ACTIVE','CONFIRMED','EXPIRED','RELEASED') NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  expires_at DATETIME(3) NOT NULL,
  PRIMARY KEY (hold_id),
  UNIQUE KEY uq_hold_token (lock_token),
  KEY idx_hold_reaper (status, expires_at),        -- expiry sweeper
  KEY idx_hold_user (user_id),
  CONSTRAINT fk_hold_user FOREIGN KEY (user_id) REFERENCES app_user (user_id),
  CONSTRAINT fk_hold_show FOREIGN KEY (show_id) REFERENCES movie_show (show_id)
) ENGINE=InnoDB;

CREATE TABLE booking (
  booking_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id      BIGINT UNSIGNED NOT NULL,
  show_id      BIGINT UNSIGNED NOT NULL,
  hold_id      BIGINT UNSIGNED NOT NULL,
  status       ENUM('PENDING_PAYMENT','CONFIRMED','FAILED','CANCELLED') NOT NULL DEFAULT 'PENDING_PAYMENT',
  total_amount DECIMAL(10,2) NOT NULL,             -- price snapshot at booking time
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (booking_id),
  UNIQUE KEY uq_booking_hold (hold_id),            -- a hold can become at most one booking
  KEY idx_booking_user (user_id),
  KEY idx_booking_show (show_id),
  CONSTRAINT fk_booking_user FOREIGN KEY (user_id) REFERENCES app_user (user_id),
  CONSTRAINT fk_booking_show FOREIGN KEY (show_id) REFERENCES movie_show (show_id),
  CONSTRAINT fk_booking_hold FOREIGN KEY (hold_id) REFERENCES seat_hold (hold_id)
) ENGINE=InnoDB;

-- THE CONCURRENCY TABLE: one row per (show, physical seat), pre-generated
-- when a show is created. UNIQUE (show_id, seat_id) + row locks = no double booking.
CREATE TABLE show_seat (
  show_seat_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  show_id      BIGINT UNSIGNED NOT NULL,
  seat_id      INT UNSIGNED    NOT NULL,
  status       ENUM('AVAILABLE','HELD','BOOKED') NOT NULL DEFAULT 'AVAILABLE',
  hold_id      BIGINT UNSIGNED NULL,
  booking_id   BIGINT UNSIGNED NULL,
  PRIMARY KEY (show_seat_id),
  UNIQUE KEY uq_show_seat (show_id, seat_id),
  KEY idx_ss_availability (show_id, status),
  KEY idx_ss_hold (hold_id),
  KEY idx_ss_booking (booking_id),
  CONSTRAINT fk_ss_show    FOREIGN KEY (show_id)    REFERENCES movie_show (show_id),
  CONSTRAINT fk_ss_seat    FOREIGN KEY (seat_id)    REFERENCES seat (seat_id),
  CONSTRAINT fk_ss_hold    FOREIGN KEY (hold_id)    REFERENCES seat_hold (hold_id),
  CONSTRAINT fk_ss_booking FOREIGN KEY (booking_id) REFERENCES booking (booking_id),
  -- state machine enforced by the database itself
  CONSTRAINT ck_ss_state CHECK (
       (status = 'AVAILABLE' AND hold_id IS NULL     AND booking_id IS NULL)
    OR (status = 'HELD'      AND hold_id IS NOT NULL AND booking_id IS NULL)
    OR (status = 'BOOKED'    AND booking_id IS NOT NULL)
  )
) ENGINE=InnoDB;

CREATE TABLE payment (
  payment_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  booking_id       BIGINT UNSIGNED NOT NULL,
  gateway_order_id VARCHAR(64) NOT NULL,           -- id issued by payment gateway
  amount           DECIMAL(10,2) NOT NULL,
  status           ENUM('CREATED','SUCCESS','FAILED','REFUND_PENDING') NOT NULL DEFAULT 'CREATED',
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (payment_id),
  UNIQUE KEY uq_payment_order (gateway_order_id),
  KEY idx_payment_booking (booking_id),
  CONSTRAINT fk_payment_booking FOREIGN KEY (booking_id) REFERENCES booking (booking_id)
) ENGINE=InnoDB;

-- Idempotency ledger: the gateway's event id is the PRIMARY KEY, so a
-- duplicate/retried webhook can be inserted only once.
CREATE TABLE payment_webhook_event (
  event_id         VARCHAR(64) NOT NULL,
  gateway_order_id VARCHAR(64) NOT NULL,
  event_type       VARCHAR(40) NOT NULL,           -- payment.success / payment.failed
  payload          JSON NOT NULL,
  received_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  processed_at     DATETIME(3) NULL,
  PRIMARY KEY (event_id),
  KEY idx_pwe_order (gateway_order_id)
) ENGINE=InnoDB;
