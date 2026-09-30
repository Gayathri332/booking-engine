-- =====================================================================
-- P2: list all shows on a given date at a given theatre, with timings
-- Set the two inputs, then run any of the queries below.
-- =====================================================================
USE bookmyshow;

SET @theatre_id = 1;
SET @show_date  = CURDATE();          -- or e.g. '2026-10-01'

-- ---- P2 (main): one row per show, ordered like the BookMyShow UI -----
SELECT
    t.name                                         AS theatre,
    m.title                                        AS movie,
    l.name                                         AS language,
    f.name                                         AS format,
    sc.name                                        AS screen,
    ms.show_id,
    ms.show_date,
    TIME_FORMAT(ms.start_time, '%h:%i %p')         AS show_time,
    TIME_FORMAT(ADDTIME(ms.start_time, SEC_TO_TIME(m.duration_min * 60)), '%h:%i %p') AS ends_at
FROM theatre t
JOIN screen      sc ON sc.theatre_id = t.theatre_id
JOIN movie_show  ms ON ms.screen_id  = sc.screen_id
JOIN movie       m  ON m.movie_id    = ms.movie_id
JOIN language    l  ON l.language_id = ms.language_id
JOIN show_format f  ON f.format_id   = ms.format_id
WHERE t.theatre_id = @theatre_id
  AND ms.show_date = @show_date
  AND ms.status    = 'SCHEDULED'
ORDER BY m.title, ms.start_time;

-- ---- P2 (grouped): one row per movie with all its timings ------------
SELECT
    m.title  AS movie,
    l.name   AS language,
    f.name   AS format,
    GROUP_CONCAT(TIME_FORMAT(ms.start_time, '%h:%i %p') ORDER BY ms.start_time SEPARATOR ' | ') AS show_timings
FROM theatre t
JOIN screen      sc ON sc.theatre_id = t.theatre_id
JOIN movie_show  ms ON ms.screen_id  = sc.screen_id
JOIN movie       m  ON m.movie_id    = ms.movie_id
JOIN language    l  ON l.language_id = ms.language_id
JOIN show_format f  ON f.format_id   = ms.format_id
WHERE t.theatre_id = @theatre_id
  AND ms.show_date = @show_date
  AND ms.status    = 'SCHEDULED'
GROUP BY m.movie_id, m.title, l.language_id, l.name, f.format_id, f.name
ORDER BY m.title;

-- ---- Bonus: the "next 7 dates" date-picker strip ---------------------
SELECT DISTINCT ms.show_date, DATE_FORMAT(ms.show_date, '%a %d %b') AS label
FROM screen sc
JOIN movie_show ms ON ms.screen_id = sc.screen_id
WHERE sc.theatre_id = @theatre_id
  AND ms.show_date BETWEEN CURDATE() AND CURDATE() + INTERVAL 6 DAY
  AND ms.status = 'SCHEDULED'
ORDER BY ms.show_date;

-- ---- Bonus: main query + live seat availability ----------------------
SELECT ms.show_id, m.title,
       TIME_FORMAT(ms.start_time, '%h:%i %p') AS show_time,
       SUM(ss.status = 'AVAILABLE') AS seats_available,
       COUNT(*)                     AS seats_total
FROM screen sc
JOIN movie_show ms ON ms.screen_id = sc.screen_id
JOIN movie      m  ON m.movie_id   = ms.movie_id
JOIN show_seat  ss ON ss.show_id   = ms.show_id
WHERE sc.theatre_id = @theatre_id AND ms.show_date = @show_date AND ms.status = 'SCHEDULED'
GROUP BY ms.show_id, m.title, ms.start_time
ORDER BY m.title, ms.start_time;

-- ---- Index proof (expect: uq_show_slot / ref on screen, no filesort of big data)
EXPLAIN SELECT ms.show_id
FROM screen sc JOIN movie_show ms ON ms.screen_id = sc.screen_id
WHERE sc.theatre_id = @theatre_id AND ms.show_date = @show_date;
