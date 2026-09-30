-- Sample data. Shows are generated for the NEXT 7 DAYS relative to today,
-- so the P2 query returns rows whenever it is run.
USE bookmyshow;

INSERT INTO city (name, state) VALUES ('Bengaluru','Karnataka'), ('Mumbai','Maharashtra');

INSERT INTO theatre (city_id, name, address) VALUES
 (1, 'PVR Orion Mall',   'Dr Rajkumar Rd, Rajajinagar, Bengaluru'),
 (1, 'INOX Garuda Mall', 'Magrath Rd, Ashok Nagar, Bengaluru');

INSERT INTO screen (theatre_id, name) VALUES
 (1,'Audi 1'), (1,'Audi 2'), (2,'Audi 1');

INSERT INTO seat_category (name) VALUES ('Silver'), ('Gold'), ('Recliner');

-- 3 rows x 8 seats per screen: row A = Recliner, B = Gold, C = Silver
INSERT INTO seat (screen_id, row_label, seat_number, category_id)
SELECT sc.screen_id, r.row_label, n.n, r.category_id
FROM screen sc
JOIN (SELECT 'A' AS row_label, 3 AS category_id
      UNION ALL SELECT 'B', 2
      UNION ALL SELECT 'C', 1) r
JOIN (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8) n;

INSERT INTO language (name) VALUES ('English'), ('Hindi'), ('Kannada'), ('Tamil');
INSERT INTO show_format (name) VALUES ('2D'), ('3D'), ('IMAX 2D');

INSERT INTO movie (title, duration_min, certificate) VALUES
 ('Kantara: Chapter 1', 168, 'UA'),
 ('Dune: Part Three',   155, 'UA'),
 ('Inside Out 3',       100, 'U');

INSERT INTO app_user (name, email, phone) VALUES
 ('Asha Rao',   'asha@example.com',  '9800000001'),
 ('Ravi Kumar', 'ravi@example.com',  '9800000002'),
 ('Meera Nair', 'meera@example.com', '9800000003');

-- 7 days x fixed daily timetable
INSERT INTO movie_show (screen_id, movie_id, language_id, format_id, show_date, start_time)
SELECT t.screen_id, t.movie_id, t.language_id, t.format_id,
       CURDATE() + INTERVAL d.d DAY, t.start_time
FROM (SELECT 0 AS d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3
      UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6) d
JOIN (
  SELECT 1 AS screen_id, 1 AS movie_id, 3 AS language_id, 1 AS format_id, TIME('10:00') AS start_time
  UNION ALL SELECT 1, 1, 3, 1, '14:00'
  UNION ALL SELECT 1, 1, 3, 1, '18:30'
  UNION ALL SELECT 2, 2, 1, 3, '11:00'
  UNION ALL SELECT 2, 2, 1, 3, '15:00'
  UNION ALL SELECT 2, 2, 1, 3, '19:30'
  UNION ALL SELECT 3, 3, 1, 2, '10:30'
  UNION ALL SELECT 3, 3, 2, 2, '13:00'
  UNION ALL SELECT 3, 2, 1, 1, '20:00'
) t;

-- show_price: Silver 150 / Gold 250 / Recliner 450 (+50 for 3D / IMAX)
INSERT INTO show_price (show_id, category_id, price)
SELECT ms.show_id, c.category_id,
       CASE c.name WHEN 'Silver' THEN 150 WHEN 'Gold' THEN 250 ELSE 450 END
       + CASE WHEN f.name = '2D' THEN 0 ELSE 50 END
FROM movie_show ms
JOIN show_format f ON f.format_id = ms.format_id
CROSS JOIN seat_category c;

-- one show_seat row per (show, seat of that show's screen)
INSERT INTO show_seat (show_id, seat_id)
SELECT ms.show_id, s.seat_id
FROM movie_show ms
JOIN seat s ON s.screen_id = ms.screen_id;

-- ---------------------------------------------------------------------
-- Sample transactional rows: 1 confirmed booking, 1 live hold, 1 webhook
-- (all on the first show, seats A1..A3 and B1)
-- ---------------------------------------------------------------------
SET @show := (SELECT MIN(show_id) FROM movie_show);

-- Asha: hold A1,A2 -> booking -> payment SUCCESS -> seats BOOKED
INSERT INTO seat_hold (user_id, show_id, seat_count, lock_token, status, expires_at)
VALUES (1, @show, 2, REPLACE(UUID(),'-',''), 'CONFIRMED', NOW(3) + INTERVAL 10 MINUTE);
SET @h1 := LAST_INSERT_ID();

INSERT INTO booking (user_id, show_id, hold_id, status, total_amount)
VALUES (1, @show, @h1, 'CONFIRMED', 900.00);           -- 2 x Recliner @ 450
SET @b1 := LAST_INSERT_ID();

INSERT INTO payment (booking_id, gateway_order_id, amount, status)
VALUES (@b1, 'order_demo_0001', 900.00, 'SUCCESS');

INSERT INTO payment_webhook_event (event_id, gateway_order_id, event_type, payload, processed_at)
VALUES ('evt_demo_0001', 'order_demo_0001', 'payment.success',
        JSON_OBJECT('event_id','evt_demo_0001','type','payment.success','gateway_order_id','order_demo_0001','amount',900.00),
        NOW(3));

UPDATE show_seat ss JOIN seat s ON s.seat_id = ss.seat_id
SET ss.status = 'BOOKED', ss.hold_id = @h1, ss.booking_id = @b1
WHERE ss.show_id = @show AND s.row_label = 'A' AND s.seat_number IN (1,2);

-- Ravi: live 10-minute hold on A3 + B1 (not yet paid)
INSERT INTO seat_hold (user_id, show_id, seat_count, lock_token, status, expires_at)
VALUES (2, @show, 2, REPLACE(UUID(),'-',''), 'ACTIVE', NOW(3) + INTERVAL 10 MINUTE);
SET @h2 := LAST_INSERT_ID();

UPDATE show_seat ss JOIN seat s ON s.seat_id = ss.seat_id
SET ss.status = 'HELD', ss.hold_id = @h2
WHERE ss.show_id = @show
  AND ((s.row_label = 'A' AND s.seat_number = 3) OR (s.row_label = 'B' AND s.seat_number = 1));
